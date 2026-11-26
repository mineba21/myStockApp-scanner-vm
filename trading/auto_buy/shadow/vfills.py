"""가상 체결 모델 DAILY_T1_100_v1 과 가상 S01 (순수 함수, 일봉). P0~P7 백테스트 ``fills.py`` 와 같은 규칙에
운영 정책(거래량 확인)을 더한다. 운영 정책 비교판은 ``backtest/entry_strategy/src/p7_ops.py``.

매수: 판단일 종가 +1% 지정가(센트 내림 — 상한 초과 없음, ``risk.limit_price``), 다음 정규장 1일.
- 거래량 0 이 확정된 봉 → 체결하지 않고 미체결 만료 (다음 날로 이월·재주문 없음, 엔진이 처리)
- 시가 ≤ 손절가 또는 (시가−손절)/시가 < 3% → 전송하지 않음 (SKIP_OPEN_STOP)
- 시가 ≤ 지정가 → 시가 체결, 저가 ≤ 지정가 → 지정가 체결, 그 밖 → 미체결 (추격 재주문 없음)
S01: 고정 손절가, 기간 제한 없음.
- 체결 당일 저가 ≤ 손절가 → 손절가 청산 (보수 가정, STOP_DAY0). 대안 가정(다음 거래일부터 감시)은 details 에 기록
- 이후: 시가 ≤ 손절가 → 시가 청산 (STOP_GAP), 저가 ≤ 손절가 → 손절가 청산 (STOP)
- 거래량 0 이 확정된 날은 손절 체결을 만들지 않는다 (미청산 위험 유지). 거래가 재개된 첫 봉에서 같은 규칙으로
  판단하므로 시가가 손절가 아래면 시가 청산(갭 손실)이 된다.

거래량이 양수여도 실제 지정가 체결이 보장되지는 않는다 (일봉 근사, 장중 호가·체결량 미재현).

거래량 상태 (저장 값 / 운영 명칭)
- ``POSITIVE`` = POSITIVE_VOLUME: 양수 거래량.
- ``ZERO`` = CONFIRMED_ZERO_VOLUME: **원본 응답에서 확인된** 거래량 0. 출처(``volume_source``)가 원본 확인 출처
  (``CONFIRMED_ZERO_SOURCES``, Yahoo chart 원본 JSON)일 때만 0 을 확정으로 본다. yfinance 가 DataFrame 으로 바꾸면서
  원본 ``volume: null`` 을 0 으로 채우므로(``fillna(0)``) 변환된 DataFrame 의 0 만으로는 확정하지 않는다.
- ``UNKNOWN`` = UNKNOWN_VOLUME: NULL·NaN·음수·숫자 아님·필드 없음·출처 확인 불가 0.
"""
from __future__ import annotations

import math
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional

from trading.auto_buy.settings import COSTS, MIN_R_PCT


# OHLC 일관성 검사의 상대 허용 오차. 수정주가 계산의 부동소수점 오차(1e-14 수준)는 정상 봉으로 보고,
# 이보다 큰 불일치(실제 비정상 봉)는 무효로 거부한다.
OHLC_REL_TOL = 1e-9


VOLUME_POSITIVE, VOLUME_ZERO, VOLUME_UNKNOWN = "POSITIVE", "ZERO", "UNKNOWN"
POSITIVE_VOLUME, CONFIRMED_ZERO_VOLUME, UNKNOWN_VOLUME = VOLUME_POSITIVE, VOLUME_ZERO, VOLUME_UNKNOWN

# 거래량 출처. 0 을 확정할 수 있는 것은 원본 응답에서 null 과 0 을 구분해 읽은 출처뿐이다.
SOURCE_YAHOO_CHART_RAW = "yahoo_chart_raw"          # Yahoo v8 chart 원본 JSON (market_data.parse_chart)
SOURCE_YF_DATAFRAME = "yfinance_dataframe"          # yfinance 변환 DataFrame (null → 0 변환됨, 0 확정 불가)
CONFIRMED_ZERO_SOURCES = frozenset({SOURCE_YAHOO_CHART_RAW})
_ASSUMED_ZERO_SOURCES: set = set()                  # 백테스트 비교 전용 명시적 가정 (운영 경로는 쓰지 않음)


@contextmanager
def assume_confirmed_zero(source: str):
    """백테스트 비교 전용: 원본을 확인할 수 없는 캐시의 거래량 0 을 '확정 0' 으로 **가정**한다.
    운영 SHADOW 경로(runner·market_data)는 호출하지 않는다. 결과에는 가정임을 기록해야 한다."""
    _ASSUMED_ZERO_SOURCES.add(source)
    try:
        yield
    finally:
        _ASSUMED_ZERO_SOURCES.discard(source)


def zero_confirmable(source: Optional[str]) -> bool:
    return source in CONFIRMED_ZERO_SOURCES or source in _ASSUMED_ZERO_SOURCES


def volume_state(v, source: Optional[str] = None) -> str:
    """거래량 상태. 0 확정과 미확인(NULL·음수·NaN·숫자 아님)을 구분한다.
    0 은 원본 확인 출처(``zero_confirmable``)일 때만 ZERO, 아니면 UNKNOWN (변환 과정에서 null 이 0 이 됐을 수 있다)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
        return VOLUME_UNKNOWN
    if v == 0:
        return VOLUME_ZERO if zero_confirmable(source) else VOLUME_UNKNOWN
    return VOLUME_POSITIVE


@dataclass(frozen=True)
class Bar:
    open: float
    high: float
    low: float
    close: float
    # 거래량 (None = 미확인). OHLC 동등 비교에는 넣지 않는다 (가격 봉 비교와 거래량 상태 판정을 분리).
    volume: Optional[float] = field(default=None, compare=False)
    # 거래량 출처 (0 확정 가능 여부 판단)와 원본 근거(원본 값·응답 해시 등, 저장용)
    volume_source: Optional[str] = field(default=None, compare=False)
    evidence: Optional[dict] = field(default=None, compare=False, repr=False)

    @property
    def volume_state(self) -> str:
        return volume_state(self.volume, self.volume_source)

    @property
    def no_volume(self) -> bool:
        """거래량 0 확정 (미확인은 False)."""
        return self.volume_state == VOLUME_ZERO

    def valid(self) -> bool:
        vals = (self.open, self.high, self.low, self.close)
        if not all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0 for v in vals):
            return False
        tol = OHLC_REL_TOL * max(vals)
        return self.low <= min(self.open, self.close) + tol and self.high >= max(self.open, self.close) - tol


def try_buy(bar: Bar, limit: float, stop: float, min_r_pct: float = MIN_R_PCT) -> tuple[str, Optional[float], Optional[str]]:
    o, lo = bar.open, bar.low
    if o <= stop or (o - stop) / o < min_r_pct / 100.0:
        return "SKIP_OPEN_STOP", None, None
    if o <= limit:
        return "FILLED", o, "open"
    if lo <= limit:
        return "FILLED", limit, "limit"
    return "UNFILLED", None, None


def check_stop(bar: Bar, stop: float, day0: bool) -> tuple[Optional[str], Optional[float]]:
    if day0:
        return ("STOP_DAY0", stop) if bar.low <= stop else (None, None)
    if bar.open <= stop:
        return "STOP_GAP", bar.open
    if bar.low <= stop:
        return "STOP", stop
    return None, None


def unit_cost(px: float) -> float:
    return px * (1 + COSTS["slippage_bp"] / 10000.0) * (1 + COSTS["buy_commission"])


def unit_proceeds(px: float) -> float:
    return px * (1 - COSTS["slippage_bp"] / 10000.0) * (1 - COSTS["sell_commission"] - COSTS["sell_tax"])
