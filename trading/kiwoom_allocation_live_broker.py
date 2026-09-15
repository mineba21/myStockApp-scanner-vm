"""Real-account Kiwoom adapter for a user-authorized ETF allocation cycle.

Every order receives a fresh broker-side capacity check. Transport failures are
never retried by this adapter; the durable rebalance journal reconciles them.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from trading.asset_allocation_universe import ETF_EXCHANGES
from trading.kiwoom_execution_evidence import ExecutionEvidence, quantity
from trading.kiwoom_orders import KiwoomOrderClient, OrderRejected
from trading.kiwoom_readonly import REAL_BASE_URL, KiwoomError, KiwoomReadOnlyClient

KST = ZoneInfo("Asia/Seoul")
NEW_YORK = ZoneInfo("America/New_York")


class KiwoomAllocationLiveBroker:
    """Account2 real adapter with fail-closed, per-order preflight checks."""

    def __init__(self, config, *, client=None, token=None, evidence=None,
                 orders=None, reserve_bps=100, clock=time.time):
        if config.mode != "real" or config.base_url != REAL_BASE_URL:
            raise KiwoomError("ETF 실계좌 연결은 real account2 설정만 허용합니다.")
        self.config = config
        self.client = client or KiwoomReadOnlyClient(config)
        self.token = str(token or self.client.issue_token()["token"])
        self.evidence = evidence or ExecutionEvidence(self.client, self.token)
        self.orders = orders or KiwoomOrderClient(config, self.client.session)
        self.reserve_bps = int(reserve_bps)
        if not 0 <= self.reserve_bps < 10000:
            raise KiwoomError("유효한 여유금 bps가 필요합니다.")
        self.clock = clock

    @staticmethod
    def _read_retry(operation):
        last = None
        for delay in (0, 2, 4, 8):
            if delay:
                time.sleep(delay)
            try:
                return operation()
            except KiwoomError as exc:
                last = exc
                if "HTTP 오류: 429" not in str(exc):
                    raise
        raise last

    def _days(self):
        now = datetime.fromtimestamp(self.clock(), KST)
        return now.strftime("%Y%m%d"), (now - timedelta(days=1)).strftime("%Y%m%d")

    def snapshot(self):
        today, _ = self._days()
        cash = self.evidence.cash_check(today, include_previous_day=True)
        balance = self._read_retry(
            lambda: self.client.get_overseas_account_balance(self.token)
        )
        holdings = {}
        for row in balance.get("holdings", []):
            ticker = str(row.get("stk_cd") or "").strip().upper()
            if not ticker:
                continue
            held = quantity(row.get("qty", 0))
            holdings[ticker] = holdings.get(ticker, 0) + held
        return {
            "account_profile": "account2", "currency": "USD", "complete": True,
            # ust21110.fc_ord_alowa is used as the broker's already-net
            # orderable amount. It is not manually reduced by open orders.
            "cash_includes_reservations": True, "as_of": self.clock(),
            "orderable_cash": cash["broker_orderable_cash"],
            "holdings": holdings,
            "open_orders": [{} for _ in range(cash["open_order_count"])],
            "source": "kiwoom_real",
        }

    def quotes(self, tickers):
        result = {}
        for ticker in tickers:
            ticker = str(ticker).strip().upper()
            exchange = ETF_EXCHANGES.get(ticker)
            if not exchange:
                raise KiwoomError(f"{ticker}: 거래소 매핑이 없습니다.")
            quote = self._read_retry(lambda: self.client.get_overseas_orderbook(
                self.token, exchange=exchange, ticker=ticker,
            ))["quote"]
            if (str(quote.get("stk_cd") or "").strip().upper() != ticker
                    or str(quote.get("stex_tp") or "").strip().upper() != exchange):
                raise KiwoomError(f"{ticker}: 호가 응답 종목/거래소가 일치하지 않습니다.")
            try:
                observed = datetime.strptime(
                    f"{quote['dt']} {quote['bid_tm']}", "%Y%m%d %H:%M"
                ).replace(tzinfo=NEW_YORK).timestamp()
                price = abs(Decimal(str(quote.get("cur_prc")))).quantize(
                    Decimal(".01"), rounding=ROUND_HALF_UP,
                )
            except Exception as exc:
                raise KiwoomError(f"{ticker}: 호가 가격/시각을 확인할 수 없습니다.") from exc
            age = self.clock() - observed
            if price <= 0 or not 0 <= age <= 60:
                raise KiwoomError(f"{ticker}: 호가가 누락되었거나 60초를 초과했습니다.")
            result[ticker] = {
                "limit_price": str(price), "as_of": observed,
                "time_source": "usa20101.dt+bid_tm_America/New_York",
            }
        return result

    def _sellable_quantity(self, ticker, exchange):
        balance = self._read_retry(lambda: self.client.get_overseas_account_balance(
            self.token, exchange=exchange, ticker=ticker,
        ))
        matching = [row for row in balance.get("holdings", [])
                    if str(row.get("stk_cd") or "").strip().upper() == ticker]
        if len(matching) != 1:
            raise OrderRejected(f"{ticker}: 매도 가능 잔고를 하나로 확정할 수 없습니다.")
        return quantity(matching[0].get("sell_alowq"))

    def submit(self, intent):
        ticker = str(intent["ticker"]).strip().upper()
        exchange = ETF_EXCHANGES.get(ticker)
        if not exchange:
            raise OrderRejected(f"{ticker}: ETF 관리 범위의 거래소 매핑이 없습니다.")
        desired = int(intent["quantity"])
        price = str(intent["price"])
        if intent["side"] == "BUY":
            capacity = self.evidence.buy_capacity(
                ticker, exchange, price, reserve_bps=self.reserve_bps,
            )
            if desired > capacity["max_quantity_with_reserve"]:
                raise OrderRejected(
                    f"{ticker}: 키움 주문가능수량 {capacity['max_quantity_with_reserve']}주보다 큽니다."
                )
            return self.orders.buy_us_limit(
                self.token, exchange=exchange, ticker=ticker,
                quantity=desired, price=float(price),
            )
        if intent["side"] == "SELL":
            sellable = self._sellable_quantity(ticker, exchange)
            if desired > sellable:
                raise OrderRejected(f"{ticker}: 매도가능수량 {sellable}주보다 큽니다.")
            return self.orders.sell_us_limit(
                self.token, exchange=exchange, ticker=ticker,
                quantity=desired, price=float(price),
            )
        raise OrderRejected("지원하지 않는 주문 방향입니다.")

    def lookup(self, intent):
        return self.evidence.lookup(intent)
