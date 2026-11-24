"""시세 검증과 지정가 계산 (Decimal, 센트 단위)."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from trading.auto_stop.us_calendar import ny_date

CENT = Decimal("0.01")
MIN_AUTO_PRICE = Decimal("1.00")   # $1 미만 호가 단위는 주문 클라이언트(센트 전용)로 검증되지 않았다


class QuoteUnavailable(Exception):
    """시세를 주문 근거로 쓸 수 없음."""


@dataclass(frozen=True)
class Quote:
    ticker: str
    exchange: str
    price: Decimal
    as_of: float | None          # 증권사 제공 시각 (usa20101 dt+bid_tm, 분 단위)
    source: str
    bid: Decimal | None = None   # 1호가 필드가 검증·설정된 경우만


def validate(quote: Quote, now: float, clock, settings) -> str | None:
    """주문 근거로 쓸 수 없으면 사유를 돌려준다. bid_tm 만으로 실시간·거래 가능을 확정하지 않는다."""
    if not isinstance(quote.price, Decimal) or not quote.price.is_finite() or quote.price <= 0:
        return "시세 가격이 유효하지 않습니다."
    if quote.as_of is None:
        return "증권사 시세 시각이 없습니다 (usa20100 대체 응답 등)."
    if clock.session is None:
        return "정규장 거래일이 아닙니다."
    if ny_date(quote.as_of) != clock.session.day:
        return "당일 정규장 시세가 아닙니다."
    # 분 단위 시각이므로 개장 직전 1분까지는 허용하지 않는다: 개장 시각 이전 시세는 프리마켓.
    if quote.as_of < clock.session.open_ts:
        return "정규장 개장 이전 시세입니다."
    age = now - quote.as_of
    if age < -60:
        return "시세 시각이 현재보다 앞섭니다."
    if age > settings.quote_max_age_sec:
        return f"시세가 {int(age)}초 지났습니다 (한도 {settings.quote_max_age_sec}초)."
    return None


def is_jump(prev: Decimal | None, price: Decimal, settings) -> bool:
    if prev is None or prev <= 0:
        return False
    return abs(price - prev) / prev * 100 >= settings.quote_jump_pct


def consistent(a: Decimal, b: Decimal, settings) -> bool:
    base = max(a, b)
    return base > 0 and abs(a - b) / base * 100 <= settings.quote_confirm_tolerance_pct


def reference_price(quote: Quote, allow_bid: bool = True) -> tuple[Decimal, str]:
    """기준가: 현재가. 1호가가 검증·설정된 경우에만 min(현재가, 매수1호가)."""
    if allow_bid and quote.bid is not None and quote.bid.is_finite() and quote.bid > 0 and quote.bid < quote.price:
        return quote.bid, "BID"
    return quote.price, "CUR"


def limit_price(ref: Decimal, slip_bps: int) -> Decimal:
    value = (ref * (Decimal(10000) - Decimal(slip_bps)) / Decimal(10000)).quantize(CENT, rounding=ROUND_DOWN)
    return value
