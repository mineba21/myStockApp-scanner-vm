"""account1 키움 조회 전용 어댑터 (SHADOW). 시세·거래소 확정·계좌 스냅샷만, 주문 경로 없음.

S01 LiveStopSellBroker(allow_orders=False)를 조회 대역으로 재사용하되, 자기 전용
KiwoomReadOnlyClient + ThrottledSession 을 만들어 넘긴다 (S01 세션·429 계수를 건드리지 않는다).
체결 모델 CUR_V1 이므로 현재가(cur_prc)만 쓰고 1호가(bid)는 쓰지 않는다.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import requests

from trading.auto_buy_us.limiter import CallBudget, ThrottledSession
from trading.auto_buy_us.settings import ACCOUNT_PROFILE
from trading.auto_stop.brokers import BalanceUnavailable, LiveStopSellBroker
from trading.auto_stop.quotes import QuoteUnavailable, validate
from trading.auto_stop.us_calendar import market_clock
from trading.kiwoom_readonly import KiwoomConfig, KiwoomError, KiwoomReadOnlyClient

QUOTE_SOURCE = "KIWOOM_USA20101"
_TOKEN_MARKERS = ("8005", "Token이 유효하지 않")
_READ_ERRORS = (KiwoomError, requests.RequestException)


@dataclass(frozen=True)
class QuoteEvidence:
    ticker: str
    exchange: str
    requested_at: float
    received_at: float
    as_of: float | None           # 증권사 시각 (usa20101 dt+bid_tm, 분 단위)
    price: Decimal | None         # 현재가 (센트)
    source: str
    valid: bool
    invalid_reason: str | None


@dataclass(frozen=True)
class AccountSnapshot:
    at: float
    net_asset_usd: Decimal | None        # ust21110 USD fc_entra + ust21120 USD evlt_amt
    orderable_cash_usd: Decimal | None   # ust21110 USD fc_ord_alowa
    holdings_count: int | None
    complete: bool
    reason: str | None


def _chain_text(exc: BaseException) -> str:
    """예외와 원인 사슬의 메시지 (S01 은 KiwoomError 를 QuoteUnavailable 로 감싸 원문을 잃는다)."""
    parts, seen = [], set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        text = str(exc)
        if text and text not in parts:
            parts.append(text)
        exc = exc.__cause__ or exc.__context__
    return " ← ".join(parts) or "알 수 없는 조회 오류"


def _usd_item(report: dict) -> dict:
    """통화 목록에서 USD 행 (web.kiwoom_holdings._currency_item 과 같은 crnc_code 비교)."""
    return next((item for item in report.get("items", [])
                 if str(item.get("crnc_code") or "").strip().upper() == "USD"), {})


def _amount(value, *, absolute: bool = False) -> Decimal | None:
    """금액 해석. 없거나 해석할 수 없으면 None (0 으로 대체하지 않는다)."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        result = Decimal(text)
    except InvalidOperation:
        return None
    if not result.is_finite():
        return None
    return abs(result) if absolute else result


class KiwoomMarketData:
    """조회 전용. RateLimited·BudgetExhausted 는 호출자(엔진 tick)까지 그대로 올라간다."""

    def __init__(self, reader, budget: CallBudget, settings, clock=time.time):
        self.reader, self.budget, self.settings, self.clock = reader, budget, settings, clock
        self.token_invalid = False

    def _note(self, exc: BaseException) -> str:
        text = _chain_text(exc)
        if any(marker in text for marker in _TOKEN_MARKERS):
            self.token_invalid = True
        return text

    def quote(self, ticker: str, exchange: str) -> QuoteEvidence:
        ticker, exchange = ticker.strip().upper(), exchange.strip().upper()
        requested = self.clock()
        try:
            q = self.reader.quote(ticker, exchange)
        except (QuoteUnavailable, *_READ_ERRORS) as exc:
            return QuoteEvidence(ticker, exchange, requested, self.clock(), None, None,
                                 QUOTE_SOURCE, False, self._note(exc))
        received = self.clock()
        reason = validate(q, received, market_clock(received, self.settings), self.settings)
        return QuoteEvidence(ticker, exchange, requested, received, q.as_of, q.price,
                             q.source, reason is None, reason)

    def resolve_exchange(self, ticker: str) -> str | None:
        """세 거래소 중 정확히 하나만 응답하면 그 거래소. 모호·미확인·일시 오류는 None."""
        try:
            return self.reader.probe_exchange(ticker.strip().upper()).exchange
        except (QuoteUnavailable, requests.RequestException) as exc:
            self._note(exc)
            return None

    def account_snapshot(self) -> AccountSnapshot:
        client, token = self.reader.client, self.reader.token
        try:
            deposit = client.get_overseas_deposit(token)
            currency = client.get_overseas_currency_valuation(token)
        except _READ_ERRORS as exc:
            return AccountSnapshot(self.clock(), None, None, None, False, self._note(exc))
        cash_row, asset_row = _usd_item(deposit), _usd_item(currency)
        cash = _amount(cash_row.get("fc_entra"))
        evaluation = _amount(asset_row.get("evlt_amt"), absolute=True)
        orderable = _amount(cash_row.get("fc_ord_alowa"))
        reasons = []
        if cash is None:
            reasons.append("ust21110 USD 예수금(fc_entra)이 없습니다.")
        if evaluation is None:
            reasons.append("ust21120 USD 평가금액(evlt_amt)이 없습니다.")
        if orderable is None:
            reasons.append("ust21110 USD 주문가능금액(fc_ord_alowa)이 없습니다.")
        net = cash + evaluation if cash is not None and evaluation is not None else None
        try:
            holdings_count = len(self.reader.holdings())
        except (BalanceUnavailable, requests.RequestException) as exc:
            holdings_count = None
            reasons.append(self._note(exc))
        return AccountSnapshot(self.clock(), net, orderable, holdings_count,
                               not reasons, " / ".join(reasons) or None)


def build_market_data(settings, budget: CallBudget, *, force_token: bool = False) -> KiwoomMarketData:
    """tick 마다 새 조회 어댑터를 만든다. 전용 세션이라 S01 의 세션 객체와 공유하지 않는다."""
    from trading.auto_stop.settings import read_access_block_reason
    reason = read_access_block_reason()
    if reason:
        raise RuntimeError(reason)
    from web.kiwoom_holdings import _get_token      # 무거운 web 모듈은 실제 조립 시에만
    config = KiwoomConfig.from_profile(ACCOUNT_PROFILE)
    client = KiwoomReadOnlyClient(config, session=ThrottledSession(budget))
    token = _get_token(ACCOUNT_PROFILE, config, client, force=force_token)
    reader = LiveStopSellBroker(config, client=client, token=token, allow_orders=False)
    return KiwoomMarketData(reader, budget, settings, clock=budget.clock)
