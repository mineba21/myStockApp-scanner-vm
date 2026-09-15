"""Mock-only Kiwoom adapter for a user-triggered allocation cycle."""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from trading.asset_allocation_universe import ETF_EXCHANGES
from trading.kiwoom_execution_evidence import ExecutionEvidence, quantity
from trading.kiwoom_orders import KiwoomOrderClient
from trading.kiwoom_readonly import MOCK_BASE_URL, KiwoomError, KiwoomReadOnlyClient

KST = ZoneInfo("Asia/Seoul")
class KiwoomAllocationMockBroker:
    """Actual Kiwoom mock server adapter; never accepts a real config."""

    def __init__(self, config):
        if config.mode != "mock" or config.base_url != MOCK_BASE_URL:
            raise KiwoomError("ETF 최종 테스트는 키움 모의투자 설정만 허용합니다.")
        self.config = config
        self.client = KiwoomReadOnlyClient(config)
        self.token = str(self.client.issue_token()["token"])
        self.evidence = ExecutionEvidence(self.client, self.token)
        self.orders = KiwoomOrderClient(config, self.client.session)

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

    @staticmethod
    def _days():
        now = datetime.now(KST)
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
            # Mock account exposes same-day tradable shares in sell_alowq even
            # while qty remains unsettled. Use the executable quantity.
            held = max(quantity(row.get("qty", 0)), quantity(row.get("sell_alowq", 0)))
            holdings[ticker] = holdings.get(ticker, 0) + held
        return {
            "account_profile": "account2", "currency": "USD", "complete": True,
            "cash_includes_reservations": True, "as_of": time.time(),
            "orderable_cash": cash["broker_orderable_cash"],
            "holdings": holdings,
            "open_orders": [{} for _ in range(cash["open_order_count"])],
            "source": "kiwoom_mock",
        }

    def quotes(self, tickers):
        result = {}
        for ticker in tickers:
            exchange = ETF_EXCHANGES.get(ticker)
            if not exchange:
                raise KiwoomError(f"{ticker}: 거래소 매핑이 없습니다.")
            quote = self._read_retry(lambda: self.client.get_overseas_quote(
                self.token, exchange=exchange, ticker=ticker,
            ))["quote"]
            try:
                price = abs(Decimal(str(quote.get("cur_prc")))).quantize(
                    Decimal(".01"), rounding=ROUND_HALF_UP,
                )
            except Exception as exc:
                raise KiwoomError(f"{ticker}: 모의 현재가를 확인할 수 없습니다.") from exc
            if price <= 0:
                raise KiwoomError(f"{ticker}: 모의 현재가를 확인할 수 없습니다.")
            result[ticker] = {
                "limit_price": str(price), "as_of": time.time(),
                "time_source": "mock_response_observed_at",
            }
        return result

    def submit(self, intent):
        ticker = intent["ticker"]
        exchange = ETF_EXCHANGES.get(ticker)
        if not exchange:
            raise KiwoomError(f"{ticker}: 거래소 매핑이 없습니다.")
        method = self.orders.buy_us_limit if intent["side"] == "BUY" else self.orders.sell_us_limit
        return method(
            self.token, exchange=exchange, ticker=ticker,
            quantity=int(intent["quantity"]), price=float(intent["price"]),
        )

    def lookup(self, intent):
        return self.evidence.lookup(intent)
