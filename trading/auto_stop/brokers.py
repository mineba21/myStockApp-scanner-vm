"""자동 손절 브로커 어댑터.

LiveStopSellBroker: account1 실계좌. 조회는 기존 조회 클라이언트·주문내역 모듈, 전송은 KiwoomOrderClient
  지정가(ust20001)와 취소(ust20003)만 사용한다. 새 주문 유형을 쓰지 않는다. 주문·취소는 재시도하지 않는다.
ShadowBroker: 시세·잔고는 실계좌에서 읽고, 매도·취소는 가상으로 처리한다. 체결 모델:
  IMMEDIATE  — 조회 시점 현재가 ≥ 지정가면 전량 체결 (주문에 쓴 시세로도 체결될 수 있음)
  NEXT_QUOTE — 주문에 쓴 시세보다 '증권사 시각이 새로운' 유효 시세가 지정가 이상일 때만 전량 체결 (기본)
  STRESS     — 테스트 전용. 원주문은 부분 체결 후 멈추고 취소는 지연 확정된다. 재주문은 NEXT_QUOTE 규칙.
  어떤 모델도 실제 체결률·실현손익이 아니다. STRESS 는 ShadowBroker 안에만 있고 LIVE 경로에는 없다.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from trading.auto_stop.quotes import Quote, QuoteUnavailable
from trading.auto_stop.us_calendar import NEW_YORK
from trading.kiwoom_allocation_live_broker import KiwoomAllocationLiveBroker, orderbook_side_prices
from trading.kiwoom_execution_evidence import KST, ExecutionEvidence, quantity
from trading.kiwoom_orders import KiwoomOrderClient
from trading.auto_stop.settings import MAX_ORDER_HTTP_TIMEOUT_SEC
from trading.kiwoom_readonly import REAL_BASE_URL, KiwoomError, KiwoomReadOnlyClient

US_EXCHANGES = ("ND", "NY", "NA")


STRESS_PARTIAL_AFTER_SEC = 60
STRESS_CANCEL_DELAY_SEC = 90


class BalanceUnavailable(Exception):
    """잔고가 불완전하거나 종목 행을 하나로 확정할 수 없음 (0주로 추정하지 않는다)."""


def _dec(value) -> Decimal:
    return abs(Decimal(str(value).replace(",", "")))


def _parse_orderbook(raw, ticker, exchange) -> Quote:
    if (str(raw.get("stk_cd") or "").strip().upper() != ticker
            or str(raw.get("stex_tp") or "").strip().upper() != exchange):
        raise QuoteUnavailable(f"{ticker}: 시세 응답의 종목·거래소가 일치하지 않습니다.")
    try:
        price = _dec(raw.get("cur_prc")).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
    except Exception as exc:
        raise QuoteUnavailable(f"{ticker}: 현재가를 해석할 수 없습니다.") from exc
    try:
        as_of = datetime.strptime(f"{raw['dt']} {raw['bid_tm']}", "%Y%m%d %H:%M").replace(
            tzinfo=NEW_YORK).timestamp()
    except (KeyError, TypeError, ValueError):
        as_of = None
    bid = None
    try:
        _, sell_limit = orderbook_side_prices(raw, price)
        if sell_limit != price:
            bid = Decimal(str(sell_limit))
    except KiwoomError as exc:
        # 1호가 필드를 설정했는데 응답이 비정상이면 주문 근거로 쓰지 않는다 (fail-closed)
        raise QuoteUnavailable(f"{ticker}: {exc}") from exc
    return Quote(ticker, exchange, price, as_of, "KIWOOM_USA20101", bid)


class LiveStopSellBroker:
    """account1 실계좌. orders=None 이면 조회 전용 (SHADOW·OFF 대조용)."""

    def __init__(self, config, *, client=None, token=None, evidence=None, orders=None,
                 allow_orders=False, clock=time.time):
        if config.mode != "real" or config.base_url != REAL_BASE_URL:
            raise KiwoomError("account1 자동 손절은 실계좌 설정에서만 동작합니다.")
        if allow_orders and not 0 < float(config.timeout_seconds) <= MAX_ORDER_HTTP_TIMEOUT_SEC:
            raise KiwoomError(f"주문 HTTP timeout 은 {MAX_ORDER_HTTP_TIMEOUT_SEC:.0f}초 이하여야 합니다 (KIWOOM_TIMEOUT_SECONDS).")
        self.client = client or KiwoomReadOnlyClient(config)
        self.http_429 = 0
        session = getattr(self.client, "session", None)
        if session is not None and not getattr(session, "_auto_stop_counting", False):
            post = session.post

            def counting_post(*a, **k):
                response = post(*a, **k)
                if getattr(response, "status_code", None) == 429:
                    self.http_429 += 1
                return response
            session.post = counting_post
            session._auto_stop_counting = True
        self.token = str(token or self.client.issue_token()["token"])
        self.evidence = evidence or ExecutionEvidence(self.client, self.token, account="account1")
        self.orders = (orders or KiwoomOrderClient(config, self.client.session)) if allow_orders else None
        self.clock = clock

    _retry = staticmethod(KiwoomAllocationLiveBroker._read_retry)

    def pop_429(self) -> int:
        n, self.http_429 = self.http_429, 0
        return n

    def quote(self, ticker, exchange) -> Quote:
        try:
            raw = self._retry(lambda: self.client.get_overseas_orderbook(
                self.token, exchange=exchange, ticker=ticker))["quote"]
        except KiwoomError as exc:
            raise QuoteUnavailable(f"{ticker}: 시세 조회 실패") from exc
        return _parse_orderbook(raw, ticker, exchange)

    def probe_exchange(self, ticker) -> Quote:
        """등록 시 거래소를 확정한다: 세 거래소 중 정확히 하나만 일치해야 한다."""
        found, failed = [], []
        for exchange in US_EXCHANGES:
            try:
                raw = self._retry(lambda: self.client.get_overseas_orderbook(
                    self.token, exchange=exchange, ticker=ticker))["quote"]
            except KiwoomError as exc:
                if "[1903:" not in str(exc):
                    failed.append(exchange)
                continue
            try:
                found.append(_parse_orderbook(raw, ticker, exchange))
            except QuoteUnavailable:
                continue
        if failed or len(found) != 1:
            raise QuoteUnavailable(f"{ticker}: 거래소를 하나로 확정하지 못했습니다.")
        return found[0]

    def holdings(self) -> dict:
        try:
            balance = self._retry(lambda: self.client.get_overseas_account_balance(self.token))
        except KiwoomError as exc:
            raise BalanceUnavailable("잔고 조회 실패") from exc
        if balance.get("complete") is not True:
            raise BalanceUnavailable("잔고 조회가 완전하지 않습니다.")
        out = {}
        for row in balance.get("holdings", []):
            ticker = str(row.get("stk_cd") or "").strip().upper()
            if not ticker:
                continue
            if ticker in out:
                raise BalanceUnavailable(f"{ticker}: 잔고 행이 여러 개라 확정할 수 없습니다.")
            try:
                held = quantity(row.get("poss_qty"))
                sellable = quantity(row.get("sell_alowq"))
            except Exception as exc:
                raise BalanceUnavailable(f"{ticker}: 보유·매도가능 수량을 해석할 수 없습니다.") from exc
            try:
                avg = _dec(row.get("frgn_stk_book_uv"))
            except Exception:
                avg = None
            out[ticker] = {"qty": held, "sellable": sellable, "avg_cost": avg}
        return out

    def day_orders(self, ticker) -> list:
        now = datetime.fromtimestamp(self.clock(), KST)
        rows = []
        for offset in (0, 1):
            day = (now - timedelta(days=offset)).strftime("%Y%m%d")
            rows += [r for r in self.evidence.history(day) if r["ticker"] == ticker]
        return rows

    def sell(self, ticker, exchange, qty, price: Decimal):
        if self.orders is None:
            raise KiwoomError("조회 전용 어댑터로는 주문할 수 없습니다.")
        return self.orders.sell_us_limit(self.token, exchange=exchange, ticker=ticker,
                                         quantity=int(qty), price=float(price))

    def cancel(self, ticker, exchange, order_number):
        if self.orders is None:
            raise KiwoomError("조회 전용 어댑터로는 취소할 수 없습니다.")
        return self.orders.cancel_us_order(self.token, exchange=exchange, ticker=ticker,
                                           original_order_number=str(order_number))

    def status(self, order: dict):
        if not order.get("order_number"):
            return None
        return self.evidence.lookup({
            "id": order["id"], "order_number": order["order_number"], "ticker": order["ticker"],
            "side": "SELL", "quantity": order["requested_qty"], "price": order["limit_price"],
            "sent_at": order["sent_at"], "created_at": order["created_at"],
        })


class ShadowBroker:
    """가상 주문. reader 는 조회 전용 LiveStopSellBroker (또는 테스트 대역)."""

    def __init__(self, reader, model="NEXT_QUOTE", clock=time.time):
        if model not in ("IMMEDIATE", "NEXT_QUOTE", "STRESS"):
            raise ValueError(model)
        self.reader, self.model, self.clock = reader, model, clock

    def pop_429(self):
        pop = getattr(self.reader, "pop_429", None)
        return pop() if pop else 0

    def quote(self, ticker, exchange):
        return self.reader.quote(ticker, exchange)

    def probe_exchange(self, ticker):
        return self.reader.probe_exchange(ticker)

    def holdings(self):
        return self.reader.holdings()

    def day_orders(self, ticker):
        return []          # 가상 주문만 다루므로 실계좌 외부 주문 차단 검사는 LIVE 에서만 한다

    def sell(self, ticker, exchange, qty, price):
        return {"order_number": "V" + uuid.uuid4().hex[:12], "virtual": True}

    def cancel(self, ticker, exchange, order_number):
        return {"order_number": "VC" + uuid.uuid4().hex[:10], "virtual": True}

    def _row(self, order, state, filled, **extra):
        qty = order["requested_qty"]
        return {"order_number": order["order_number"], "ticker": order["ticker"], "side": "SELL",
                "quantity": qty, "price": order["limit_price"], "virtual": True, "model": self.model,
                "state": state, "filled_quantity": filled,
                "remaining_quantity": 0 if state in ("FILLED", "CANCELLED") else qty - filled, **extra}

    def _fill_quote(self, order):
        """체결 판정용 시세. NEXT_QUOTE 는 주문 근거 시세보다 증권사 시각이 새로워야 한다."""
        try:
            q = self.reader.quote(order["ticker"], order["exchange"])
        except QuoteUnavailable:
            return None
        if q.as_of is None:
            return None
        if self.model != "IMMEDIATE" and (order.get("quote_as_of") is None or q.as_of <= order["quote_as_of"]):
            return None
        return q if q.price >= Decimal(order["limit_price"]) else None

    def status(self, order: dict):
        if not order.get("order_number"):
            return None
        qty, filled, now = order["requested_qty"], order["filled_qty"], self.clock()
        open_state = "PARTIAL" if filled else "OPEN"
        if order["state"] in ("CANCEL_PENDING", "CANCEL_UNKNOWN"):
            if self.model == "STRESS" and now - (order.get("cancel_sent_at") or now) < STRESS_CANCEL_DELAY_SEC:
                return self._row(order, open_state, filled, stress="cancel_delay")
            return self._row(order, "CANCELLED", filled)
        if self.model == "STRESS" and order.get("attempt_no", 0) == 0:
            sent = order.get("sent_at") or now
            if now - sent >= STRESS_PARTIAL_AFTER_SEC and qty >= 2 and filled == 0:
                return self._row(order, "PARTIAL", qty // 2, stress="partial",
                                 virtual_fill_price=order["limit_price"])
            return self._row(order, open_state, filled, stress="unfilled")
        q = self._fill_quote(order)
        if q is None:
            return self._row(order, open_state, filled)
        # 지정가 매도는 지정가 이상에서 체결되므로 가상 체결가는 보수적으로 지정가로 기록한다.
        return self._row(order, "FILLED", qty, virtual_fill_price=order["limit_price"],
                         virtual_quote_price=str(q.price), virtual_quote_as_of=q.as_of)
