"""Weinstein Stage Analysis Engine  (v4 — Weekly 30-SMA 원전 충실)

v4 업데이트:
  • 주봉 30-SMA + 10-SMA 기반 Stage 판정 (원전 기준)
  • Mansfield RS: (ratio / SMA52(ratio) - 1) * 100  (0선이 기준선)
  • Base Pivot: 5~26주 tight(≤15% 폭) 횡보 → pivot 돌파
  • REBOUND: 시간순(과거→현재) 눌림→반등 탐지
  • analyze_stock 리턴에 weekly/Mansfield 필드 + warning_flags 추가
  • BEAR 장세 Stage4 2중 필터 (analyze + scan_engine)

v3 이하 하위 호환: 기존 테스트가 쓰는 stage_of(), calc_rs(), _build_indicators(),
_signal_quality(), _find_*() 는 legacy wrapper 로 그대로 동작.

신호 유형:
  BREAKOUT   — Stage1→Stage2 base pivot 상향 돌파 (거래량 동반)
  RE_BREAKOUT — Stage2 진행 중 continuation base 돌파
  REBOUND    — Stage2 MA50 눌림목 반등 (지지 확인)
  SELL       — Stage3/4 진입 징후 + 손절 + 기울기 반전
"""
import numpy as np
import pandas as pd
from typing import Optional, Dict, Any, List, Tuple

from config import (
    MA_PERIOD, MA_SLOPE_PERIOD, VOLUME_AVG_PERIOD, SCAN_LOOKBACK_DAYS,
    # BREAKOUT
    BREAKOUT_BASE_LOOKBACK_DAYS, BREAKOUT_MIN_BASE_DAYS,
    BREAKOUT_VOLUME_RATIO, BREAKOUT_MAX_EXTENDED_PCT, REQUIRE_PRICE_ABOVE_MA50,
    # RE_BREAKOUT
    REBREAKOUT_BASE_LOOKBACK_DAYS, REBREAKOUT_MAX_PULLBACK_PCT,
    REBREAKOUT_VOLUME_RATIO, REBREAKOUT_REQUIRE_VOLUME_DRYUP,
    # REBOUND
    REBOUND_MA_PERIOD, REBOUND_TOUCH_PCT, REBOUND_CONFIRM_PCT,
    REBOUND_MAX_PULLBACK_PCT, REBOUND_REQUIRE_VOLUME_DRYUP,
)

# v4 REBOUND 게이트 (backward compat)
try:
    from config import REBOUND_REQUIRE_BASE_RETEST
except ImportError:
    REBOUND_REQUIRE_BASE_RETEST = True

# Step 1 — 주봉 거래량을 hard gate 로 쓸지 warning 으로만 쓸지 (backward compat)
try:
    from config import BREAKOUT_WEEKLY_VOL_AS_GATE
except ImportError:
    BREAKOUT_WEEKLY_VOL_AS_GATE = True

# Step 2 — 2단(2-tier) base 구조 A/B 토글 + 시장별 파라미터 조회 (backward compat)
try:
    from config import BASE_MODE, market_param
except ImportError:
    BASE_MODE = "v2"

    def market_param(name, market, default):  # pragma: no cover - 구버전 config fallback
        return default

# v4 신규 파라미터 (backward compat: 없으면 기본값)
try:
    from config import (
        WEEKLY_MA_LONG, WEEKLY_MA_SHORT,
        DAILY_MA_FAST, DAILY_MA_SLOW,
        BREAKOUT_WEEKLY_VOL_RATIO, BREAKOUT_DAILY_VOL_RATIO,
        RS_LOOKBACK_WEEKS, BASE_MIN_WEEKS, PIVOT_LOOKBACK_WEEKS,
    )
except ImportError:
    WEEKLY_MA_LONG            = 30
    WEEKLY_MA_SHORT           = 10
    DAILY_MA_FAST             = 50
    DAILY_MA_SLOW             = 150
    BREAKOUT_WEEKLY_VOL_RATIO = 2.0
    BREAKOUT_DAILY_VOL_RATIO  = 3.0
    RS_LOOKBACK_WEEKS         = 52
    BASE_MIN_WEEKS            = 5
    PIVOT_LOOKBACK_WEEKS      = 26


RS_PERIOD = 65  # 13주(65거래일) 상대강도 — legacy ratio RS용

# Stage 판정 기울기 임계값 (% / bar)
_RISING_SLOPE = 0.05
_FLAT_SLOPE   = 0.02


# ══════════════════════════════════════════════════════════════════
# 진단 계측 (diagnostics) — 탈락 사유 enum
# ══════════════════════════════════════════════════════════════════
# detect_* / analyze_stock 이 return None 할 때 "왜 탈락했는지" 를 호출자에게
# 알려주기 위한 사유 키. 순수 out-parameter (diag dict) 로만 전달되며 탐지
# 로직/반환값에는 일절 영향을 주지 않는다.
#
# scan_engine 의 funnel 집계와 DB/대시보드가 이 문자열에 의존하므로 값 변경
# 시 changelog 의무 (strict_filter.py 의 reason enum 과 동일 규약).
# rs_* 3종은 strict_filter 의 RS_BELOW_ZERO / RS_FALLING / RS_NO_ZERO_CROSS
# 와 값이 일치하도록 유지해야 funnel 에서 같은 키로 집계된다.

# 데이터 부족
REJECT_INSUFFICIENT_HISTORY   = "insufficient_history"
REJECT_NO_DAILY_DATA          = "no_daily_data"
REJECT_NO_WEEKLY_DATA         = "no_weekly_data"
# Stage
REJECT_WEEKLY_STAGE_NOT_1_OR_2 = "weekly_stage_not_1_or_2"
REJECT_WEEKLY_STAGE_NOT_2      = "weekly_stage_not_2"
REJECT_DAILY_STAGE_NOT_2       = "daily_stage_not_2"
REJECT_DAILY_STAGE_NOT_2_OR_3  = "daily_stage_not_2_or_3"
REJECT_BEAR_MARKET_STAGE4      = "bear_market_stage4"
# 주봉 거래량
REJECT_WEEKLY_VOLUME_INSUFFICIENT = "weekly_volume_insufficient"
# 이동평균 위치 / 기울기
REJECT_NO_MA150               = "no_ma150"
REJECT_PRICE_BELOW_MA150      = "price_below_ma150"
REJECT_PRICE_BELOW_MA50       = "price_below_ma50"
REJECT_MA150_NOT_RISING       = "ma150_not_rising"
# Base / Pivot
REJECT_NO_BASE_FOUND          = "no_base_found"
REJECT_BASE_TOO_WIDE          = "base_too_wide"
REJECT_BASE_TOO_SHORT         = "base_too_short"
REJECT_PULLBACK_TOO_SHALLOW   = "pullback_too_shallow"
REJECT_NO_VOLUME_DRYUP        = "no_volume_dryup"
REJECT_NO_PIVOT_BREAKOUT      = "no_pivot_breakout"
# Base / Pivot — v2 전용 (Step 2, detect_base_pivot_v2)
REJECT_TIGHT_TOO_WIDE         = "tight_too_wide"
REJECT_NO_CONTRACTION         = "no_contraction"
# 일봉 거래량 / 과열
REJECT_DAILY_VOLUME_INSUFFICIENT = "daily_volume_insufficient"
REJECT_EXTENSION_TOO_HIGH        = "extension_too_high"
# REBOUND 전용
REJECT_NO_REBOUND_TOUCH       = "no_rebound_touch"
REJECT_NO_REBOUND_CONFIRM     = "no_rebound_confirm"
REJECT_REBOUND_TOO_OLD        = "rebound_too_old"
REJECT_REBOUND_GATE_FAILED    = "rebound_gate_failed"
REJECT_REBOUND_POS_UNMAPPED   = "rebound_signal_pos_unmapped"
# Mansfield RS (실제 거부는 strict_filter 가 수행 — 여기서는 플래그 기록용)
REJECT_RS_BELOW_ZERO          = "rs_below_zero"
REJECT_RS_FALLING             = "rs_falling"
REJECT_RS_NO_ZERO_CROSS       = "rs_no_zero_cross"
# fallback
REJECT_NO_SIGNAL              = "no_signal"


# 사유별 "파이프라인 통과 깊이". 한 함수 안에서 여러 후보 bar 가 각기 다른
# 지점에서 탈락할 수 있으므로, 더 멀리 통과한(랭크가 높은) 사유가 diag 에
# 남는다 — 즉 "가장 아깝게 탈락한" 이유가 보고된다.
#
# 랭크는 **각 탐지기의 실제 체크 순서** 에서 역산했다. 순서가 어긋나면 나중
# 단계에서 탈락한 후보가 앞 단계 사유를 밀어내지 못해 funnel 이 잘못된
# 병목을 지목한다 (Codex 리뷰 P2). 코드 순서:
#
#   detect_stage2_breakout : stage → weekly_vol → base → pivot → ma150
#                            → extension → daily_vol
#   _find_rebreakout_signal: daily_stage → ma150 → price>ma150 → price>ma50
#                            → slope → base → pullback → pivot → daily_vol
#                            → volume_dryup
#   _find_rebound_signal   : daily_stage → slope → touch → confirm
#                            → daily_vol → too_old
#
# 유일한 예외: detect_stage2_breakout 은 pivot 다음에 ma150 을 보지만
# REJECT_NO_MA150 은 RE_BREAKOUT 기준(앞단)으로 매겼다. 해당 분기는 루프가
# 최근 SCAN_LOOKBACK_DAYS 구간만 훑고 analyze_stock 이 MA_PERIOD 이상을
# 요구하므로 BREAKOUT 경로에서는 사실상 도달 불가라 무해하다.
#
# Step 2 — detect_base_pivot_v2 의 체크 순서(자격 구간 폭 → tight 구간 폭
# → 수축 확인)를 base_too_wide 와 no_pivot_breakout 사이에 끼워 넣었다:
#   base_too_wide(9) → tight_too_wide(10) → no_contraction(11) → no_pivot_breakout(12) → ...
_REJECT_RANK = {
    # 0 — 데이터 부족 (탐지 전)
    REJECT_INSUFFICIENT_HISTORY:      0,
    REJECT_NO_DAILY_DATA:             0,
    REJECT_NO_WEEKLY_DATA:            0,
    REJECT_NO_SIGNAL:                 0,
    # 1 — Stage 게이트 (루프 밖)
    REJECT_WEEKLY_STAGE_NOT_1_OR_2:   1,
    REJECT_WEEKLY_STAGE_NOT_2:        1,
    REJECT_DAILY_STAGE_NOT_2:         1,
    REJECT_DAILY_STAGE_NOT_2_OR_3:    1,
    REJECT_BEAR_MARKET_STAGE4:        1,
    # 2 — 주봉 거래량 (BREAKOUT 전용, 루프 밖 단독 체크)
    REJECT_WEEKLY_VOLUME_INSUFFICIENT: 2,
    # 3~6 — 이동평균 가용성 / 위치 / 기울기
    REJECT_NO_MA150:                  3,
    REJECT_PRICE_BELOW_MA150:         4,
    REJECT_PRICE_BELOW_MA50:          5,
    REJECT_MA150_NOT_RISING:          6,
    # 7~11 — Base / Pivot (REBOUND 의 touch/confirm 도 같은 깊이대)
    REJECT_NO_BASE_FOUND:             7,
    REJECT_BASE_TOO_SHORT:            7,
    REJECT_NO_REBOUND_TOUCH:          7,
    REJECT_PULLBACK_TOO_SHALLOW:      8,
    REJECT_NO_REBOUND_CONFIRM:        8,
    REJECT_BASE_TOO_WIDE:             9,
    REJECT_TIGHT_TOO_WIDE:           10,   # v2 전용 — 자격 구간은 통과, tight 구간에서 탈락
    REJECT_NO_CONTRACTION:           11,   # v2 전용 — 폭은 둘 다 통과, 수축 미달로 탈락
    REJECT_NO_PIVOT_BREAKOUT:        12,
    # 13 — 과열 (BREAKOUT 전용, pivot 이후)
    REJECT_EXTENSION_TOO_HIGH:       13,
    # 14 — 일봉 거래량
    REJECT_DAILY_VOLUME_INSUFFICIENT: 14,
    # 15 — 각 탐지기의 최종 관문 (일봉 거래량 *이후*)
    REJECT_NO_VOLUME_DRYUP:          15,
    REJECT_REBOUND_TOO_OLD:          15,
    # 16 — v4 REBOUND 게이트 (legacy 시그널 확보 후)
    REJECT_REBOUND_POS_UNMAPPED:     16,
    REJECT_REBOUND_GATE_FAILED:      16,
    # 17 — RS (실제 거부는 strict_filter — 탐지기와 경쟁하지 않음)
    REJECT_RS_BELOW_ZERO:            17,
    REJECT_RS_FALLING:               17,
    REJECT_RS_NO_ZERO_CROSS:         17,
}


def _diag_set(diag: Optional[Dict[str, Any]],
              reason: str,
              values: Optional[Dict[str, Any]] = None) -> None:
    """diag 에 탈락 사유를 기록한다 (out-parameter 전용, 반환값 없음).

    diag is None 이면 완전한 no-op — 호출부의 제어 흐름은 절대 바뀌지 않는다.
    이미 기록된 사유보다 _REJECT_RANK 가 낮으면(= 더 앞단에서 탈락) 무시하여
    "가장 멀리 간 후보" 의 사유가 남도록 한다.
    """
    if diag is None:
        return
    prev = diag.get("reject")
    if prev is not None and _REJECT_RANK.get(reason, 0) < _REJECT_RANK.get(prev, 0):
        return
    diag["reject"] = reason
    diag["values"] = dict(values) if values else {}


def _diag_ok(diag: Optional[Dict[str, Any]]) -> None:
    """시그널이 생성됐음을 diag 에 기록 (reject = None)."""
    if diag is None:
        return
    diag["reject"] = None
    diag["values"] = {}


# ══════════════════════════════════════════════════════════════════
# v4 — 주봉 / Mansfield RS / Base Pivot (신규 공개 API)
# ══════════════════════════════════════════════════════════════════

def to_weekly_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """일봉 OHLCV → 주봉 OHLCV (금요일 기준).

    Open=첫날, High=max, Low=min, Close=마지막날, Volume=sum
    """
    if df is None or len(df) < 5:
        return pd.DataFrame()
    idx = pd.to_datetime(df.index)
    weekly = df.copy()
    weekly.index = idx
    agg = weekly.resample("W-FRI").agg({
        "Open":   "first",
        "High":   "max",
        "Low":    "min",
        "Close":  "last",
        "Volume": "sum",
    }).dropna(how="any")
    return agg


# ── 주봉 거래량 산출 기준 (Step 1) ─────────────────────────────────
# W-FRI resample + Volume sum 구조상, 주중 스캔 시 현재 주 행은 *부분합* 이라
# 10주 평균 대비 비율이 구조적으로 낮게 나온다 (Step 0 실측: S&P500 피벗 돌파
# 34건의 주봉 비율 최댓값 0.98 — 임계값 2.0 도달이 물리적으로 불가능).
#
# 정책 (거래량에만 적용 — 종가/SMA/Stage 판정은 무관):
#   주 완성      → 현재 주 그대로.            CURRENT_COMPLETE
#   경과 3~4일   → 현재 주 * 5/elapsed.        CURRENT_NORMALIZED
#   경과 1~2일   → 직전 완성 주, 정규화 없음.  PREVIOUS_COMPLETE
#
# 요일별 거래량 점유율이 월 18.9 / 화 19.5 / 수 19.7 / 목 20.0 / 금 21.9% 로
# 거의 균등해 단순 5/경과일 정규화로 충분하다 (오차 2~6%).

WEEK_BASIS_CURRENT_COMPLETE   = "CURRENT_COMPLETE"
WEEK_BASIS_CURRENT_NORMALIZED = "CURRENT_NORMALIZED"
WEEK_BASIS_PREVIOUS_COMPLETE  = "PREVIOUS_COMPLETE"

_TRADING_DAYS_PER_WEEK = 5
_FRIDAY = 4


def _iso_week_key(ts) -> Tuple[int, int]:
    iso = ts.isocalendar()
    # pandas Timestamp.isocalendar() 는 버전에 따라 tuple / named tuple
    return (int(iso[0]), int(iso[1]))


def _current_week_state(daily_df: pd.DataFrame) -> Optional[Tuple[int, bool]]:
    """일봉 인덱스에서 (마지막 주의 경과 거래일 수, 완성 여부) 를 구한다.

    완성 판정은 경과일 카운트가 아니라 **마지막 일봉이 그 주의 마지막 거래일
    인지** 로 한다. 공휴일 단축주(거래일 4일)를 5일 기준으로 정규화하면 25%
    과대평가되기 때문이다.

      1. 마지막 일봉 뒤에 더 늦은 ISO 주의 일봉이 있으면 → 완성 (확정).
         (compute 시점의 마지막 주에는 해당되지 않지만, 슬라이스 입력에서는
          유효하다.)
      2. 마지막 일봉이 금요일이면 → 완성 (금요일은 항상 그 주 마지막 거래일).
         월요일 공휴일 등으로 거래일이 4일뿐이어도 금요일 마감이면 완성으로
         잡히므로 5/4 곱셈이 일어나지 않는다.
      3. 그 외 → 미완성.

    별도 휴장일 캘린더는 도입하지 않는다. 따라서 "금요일이 휴장인 주의
    목요일 장 마감 시점" 은 인덱스만으로 구분할 수 없어 미완성으로 처리된다
    (docs/step1_review_checklist.md 항목 3 참고).
    """
    if daily_df is None or len(daily_df) == 0:
        return None
    idx = pd.to_datetime(daily_df.index)
    last = idx[-1]
    week_key = _iso_week_key(last)
    elapsed = int(sum(1 for ts in idx if _iso_week_key(ts) == week_key))

    later = [ts for ts in idx if _iso_week_key(ts) > week_key]
    if later:
        return elapsed, True
    return elapsed, bool(last.weekday() == _FRIDAY)


def compute_weekly_indicators(weekly_df: pd.DataFrame,
                              daily_df: Optional[pd.DataFrame] = None
                              ) -> Optional[Dict[str, Any]]:
    """주봉 기반 지표 (30-SMA, 10-SMA, slope).

    ``daily_df`` 를 넘기면 현재 ISO 주의 경과 거래일을 세어 **거래량 비율에만**
    위 정책을 적용한다. daily_df=None (기본값) 이면 기존 동작 그대로
    (CURRENT_COMPLETE, 정규화 없음) — 하위 호환.
    """
    if weekly_df is None or len(weekly_df) < WEEKLY_MA_LONG:
        return None

    close = weekly_df["Close"]
    vol   = weekly_df["Volume"]
    sma30 = close.rolling(WEEKLY_MA_LONG,  min_periods=WEEKLY_MA_LONG  // 2).mean()
    sma10 = close.rolling(WEEKLY_MA_SHORT, min_periods=WEEKLY_MA_SHORT // 2).mean()
    vol_avg = vol.rolling(10, min_periods=5).mean()

    if pd.isna(sma30.iloc[-1]):
        return None

    cur_close = float(close.iloc[-1])
    cur_sma30 = float(sma30.iloc[-1])
    cur_sma10 = float(sma10.iloc[-1]) if not pd.isna(sma10.iloc[-1]) else cur_close
    cur_vol   = float(vol.iloc[-1])
    cur_volavg = float(vol_avg.iloc[-1]) if not pd.isna(vol_avg.iloc[-1]) else 1.0

    # ── 거래량 basis 결정 (종가/SMA/Stage 에는 일절 영향 없음) ──
    week_state    = _current_week_state(daily_df) if daily_df is not None else None
    week_elapsed  = None
    week_basis    = WEEK_BASIS_CURRENT_COMPLETE
    vol_numerator = cur_vol
    # 분모 평균은 항상 *완성 주만* 으로 계산한다. 부분 주가 rolling(10) 에
    # 섞이면 평균이 낮아져 비율이 부풀려진다 (CURRENT_NORMALIZED 경로도 동일).
    vol_series_for_avg = vol

    if week_state is not None:
        week_elapsed, week_complete = week_state
        if week_complete or week_elapsed >= _TRADING_DAYS_PER_WEEK:
            # 이미 5거래일 이상이면 정규화할 것이 없다 (정책상 3~4일만 정규화).
            week_basis = WEEK_BASIS_CURRENT_COMPLETE
        elif week_elapsed >= 3:
            week_basis = WEEK_BASIS_CURRENT_NORMALIZED
            vol_numerator = cur_vol * _TRADING_DAYS_PER_WEEK / week_elapsed
            vol_series_for_avg = vol.iloc[:-1]
        else:
            week_basis = WEEK_BASIS_PREVIOUS_COMPLETE
            vol_series_for_avg = vol.iloc[:-1]
            vol_numerator = (float(vol_series_for_avg.iloc[-1])
                             if len(vol_series_for_avg) else 0.0)

    if vol_series_for_avg is vol:
        vol_avg_used = cur_volavg
    else:
        avg_series = vol_series_for_avg.rolling(10, min_periods=5).mean()
        vol_avg_used = (float(avg_series.iloc[-1])
                        if len(avg_series) and not pd.isna(avg_series.iloc[-1])
                        else 1.0)

    weekly_volume_ratio = (round(vol_numerator / vol_avg_used, 2)
                           if vol_avg_used > 0 else 0.0)
    weekly_volume_ratio_raw = (round(cur_vol / cur_volavg, 2)
                               if cur_volavg > 0 else 0.0)

    # 30w SMA 기울기 (% per week)
    s30 = sma30.dropna().iloc[-MA_SLOPE_PERIOD:]
    if len(s30) >= 3:
        x = np.arange(len(s30))
        k = np.polyfit(x, s30.values, 1)[0]
        slope30 = float(k / cur_sma30 * 100) if cur_sma30 else 0.0
    else:
        slope30 = 0.0

    return {
        "weekly_df":   weekly_df,
        "weekly_close": close,
        "weekly_vol":  vol,
        "sma30w":      sma30,
        "sma10w":      sma10,
        "cur_close_w": cur_close,
        "cur_sma30w":  cur_sma30,
        "cur_sma10w":  cur_sma10,
        "slope30w":    slope30,
        "cur_vol_w":   cur_vol,
        "cur_volavg_w": cur_volavg,
        "weekly_volume_ratio":     weekly_volume_ratio,
        "weekly_volume_ratio_raw": weekly_volume_ratio_raw,
        "week_elapsed_days":       week_elapsed,
        "week_volume_basis":       week_basis,
    }


def compute_daily_indicators(df: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """일봉 기반 지표 (MA50, MA150, slope)."""
    return _build_indicators(df)  # legacy 와 동일


def classify_stage(weekly_ind: Optional[Dict], daily_ind: Optional[Dict]) -> str:
    """주봉 30-SMA 기준 Stage 분류 (원전 충실).

      STAGE2: close > sma30w AND slope30w > RISING  (상승 진행)
      STAGE3: close > sma30w AND slope30w ≤ FLAT   (분배/고점)
      STAGE4: close < sma30w AND slope30w < -FLAT  (하락)
      STAGE1: 그 외 (기저 형성)

    weekly_ind 이 None 이면 (데이터 부족) 일봉 기반 legacy 로 fallback.
    """
    if weekly_ind is None:
        if daily_ind is None:
            return "STAGE1"
        return stage_of(daily_ind["cur_p"], daily_ind["cur_m150"], daily_ind["slope150"])

    close  = weekly_ind["cur_close_w"]
    sma30  = weekly_ind["cur_sma30w"]
    sma10  = weekly_ind["cur_sma10w"]
    slope  = weekly_ind["slope30w"]

    above  = close > sma30
    rising = slope >  _RISING_SLOPE
    flat   = -_FLAT_SLOPE <= slope <= _FLAT_SLOPE
    falling = slope < -_FLAT_SLOPE

    # Stage2: 주봉 close > 30w SMA > 10w 도 참고 (강한 상승 추세)
    if above and rising:
        return "STAGE2"
    # Stage3: 30w 위지만 기울기 둔화/평평 (분배)
    if above and (flat or (slope <= _RISING_SLOPE and sma10 < close * 0.98)):
        return "STAGE3"
    # Stage4: 30w 아래 + 하락
    if (not above) and falling:
        return "STAGE4"
    # Stage1: 기저
    return "STAGE1"


def compute_relative_performance(close: pd.Series,
                                 benchmark_close: pd.Series,
                                 lookback_weeks: int = RS_LOOKBACK_WEEKS
                                 ) -> Tuple[Optional[float], Optional[str]]:
    """Mansfield Relative Strength.

    공식: RS_raw = stock / benchmark (가격비)
          Mansfield = (RS_raw[−1] / SMA(RS_raw, 52주) − 1) × 100

    반환:
      (rs_value, rs_trend)
        rs_value: Mansfield RS 값 (>0: 시장 대비 상대 강도, <0: 약함)
        rs_trend: "RISING" / "FALLING" / "FLAT"  (최근 5주 기울기 기준)
    """
    if close is None or benchmark_close is None:
        return None, None
    try:
        # 인덱스 정렬: 공통 날짜만
        aligned = pd.DataFrame({
            "s": close.astype(float),
            "b": benchmark_close.astype(float),
        }).dropna()
        if len(aligned) < lookback_weeks * 5:   # 일봉 기준 최소 52주 ≈ 260일
            return None, None

        # 일봉에서 주 단위 lookback (× 5)
        ratio = aligned["s"] / aligned["b"].replace(0, np.nan)
        ratio = ratio.dropna()
        if len(ratio) < lookback_weeks * 5:
            return None, None

        win = lookback_weeks * 5
        sma = ratio.rolling(win, min_periods=win // 2).mean()
        cur_ratio = float(ratio.iloc[-1])
        cur_sma   = float(sma.iloc[-1]) if not pd.isna(sma.iloc[-1]) else None
        if cur_sma is None or cur_sma == 0:
            return None, None

        rs_value = (cur_ratio / cur_sma - 1.0) * 100.0

        # trend: 최근 25거래일(≈5주) 기울기
        recent = ratio.iloc[-25:]
        if len(recent) >= 5:
            x = np.arange(len(recent))
            k = np.polyfit(x, recent.values, 1)[0]
            rel = k / recent.mean() * 100 if recent.mean() else 0.0
            if rel >   0.1: trend = "RISING"
            elif rel < -0.1: trend = "FALLING"
            else: trend = "FLAT"
        else:
            trend = "FLAT"

        return round(rs_value, 2), trend
    except Exception:
        return None, None


def detect_rs_zero_cross(close: pd.Series,
                         benchmark_close: pd.Series,
                         lookback_weeks: Optional[int] = None) -> bool:
    """Mansfield Relative Strength 가 최근 N주 안에 0선을 음→양 으로 전환했는지.

    엄격 매수 필터(Gate 6, Phase 2) 의 RS zero-cross 판정용 순수 함수.

    인접 두 점 (prev, curr) 가 (prev <= 0 and curr > 0) 면 zero-cross 로 본다.
    benchmark 결측·데이터 부족·예외 발생 시 모두 False 로 안전 폴백.

    Args:
        close:           종목 일봉 Close 시리즈.
        benchmark_close: 벤치마크(예: SPY) 일봉 Close 시리즈.
        lookback_weeks:  검사 윈도우(주). 기본값은 config.RS_ZERO_CROSS_LOOKBACK_WEEKS.

    Returns:
        bool — 윈도우 내 음→양 전환이 한 번이라도 있으면 True.
    """
    from config import RS_ZERO_CROSS_LOOKBACK_WEEKS as _DEFAULT_LB
    if lookback_weeks is None:
        lookback_weeks = _DEFAULT_LB

    if close is None or benchmark_close is None:
        return False
    try:
        aligned = pd.DataFrame({
            "s": close.astype(float),
            "b": benchmark_close.astype(float),
        }).dropna()
        # SMA52 계산이 가능해야 의미 있음
        if len(aligned) < RS_LOOKBACK_WEEKS * 5:
            return False

        ratio = aligned["s"] / aligned["b"].replace(0, np.nan)
        ratio = ratio.dropna()
        if len(ratio) < RS_LOOKBACK_WEEKS * 5:
            return False

        win = RS_LOOKBACK_WEEKS * 5
        sma = ratio.rolling(win, min_periods=win // 2).mean()
        rs_series = (ratio / sma - 1.0) * 100.0
        rs_series = rs_series.dropna()
        if len(rs_series) < 2:
            return False

        window_days = lookback_weeks * 5
        recent = rs_series.iloc[-window_days:]
        if len(recent) < 2:
            return False

        prev = recent.shift(1)
        crossed = ((prev <= 0) & (recent > 0)).any()
        return bool(crossed)
    except Exception:
        return False


def compute_stop_loss(signal: Dict[str, Any],
                      daily_ind: Optional[Dict[str, Any]] = None,
                      weekly_ind: Optional[Dict[str, Any]] = None
                      ) -> Optional[float]:
    """Strict Weinstein Gate 8 — BUY 시그널의 손절가 계산.

    signal_type 별 후보 우선순위 (앞이 우선; 첫 번째로 *price 미만* 인 값 사용):

        BREAKOUT (v1)  base_low * 0.99   →  pivot_price * 0.97  →  cur_sma30w * 0.97
        BREAKOUT (v2)  stop_ref * 0.99   →  pivot_price * 0.97  →  cur_sma30w * 0.97
        RE_BREAKOUT    swing_low(30d) * 0.99  →  cur_m50 * 0.97
        REBOUND        cur_sma30w * 0.97  →  cur_m50 * 0.97

    Step 2 — v2(2단 base) 시그널은 ``stop_ref``(tight 구간 저점, base 전체
    길이와 무관)를 1순위로 쓴다. v1 시그널은 stop_ref 키 자체가 없으므로
    기존 base_low 경로로 그대로 떨어진다 (하위 호환, 동작 불변).

    모든 후보가 None 또는 >= price 면 None 반환 → strict 필터에서
    `stop_loss_missing` / `stop_loss_above_price` 거부 사유 트리거.

    Args:
        signal:     analyze_stock 또는 detect_* 가 반환한 시그널 dict.
                    필요 키: signal_type, price, pivot_price, base_low
                    (v2 는 stop_ref 도 사용 — 있으면 base_low 대신 우선).
        daily_ind:  _build_indicators() 출력 (cur_m50, low 시리즈 사용).
        weekly_ind: compute_weekly_indicators() 출력 (cur_sma30w 사용).
    """
    price = signal.get("price")
    try:
        price = float(price) if price is not None else None
    except (TypeError, ValueError):
        price = None
    if price is None or price <= 0:
        return None

    sig_type = signal.get("signal_type")
    candidates: List[Optional[float]] = []

    if sig_type == "BREAKOUT":
        stop_ref = signal.get("stop_ref")
        if stop_ref is not None:
            # v2 — tight 구간 저점. base_low 는 (v2 에서 stop_ref 와 같은
            # 값으로 채워지지만) 의도적으로 건너뛴다 — stop_ref 가 존재하면
            # 그것이 손절 기준의 유일한 진실.
            candidates.append(float(stop_ref) * 0.99)
        else:
            bl = signal.get("base_low")
            if bl is not None:
                candidates.append(float(bl) * 0.99)
        pp = signal.get("pivot_price")
        if pp is not None:
            candidates.append(float(pp) * 0.97)
        if weekly_ind is not None:
            sma30w = weekly_ind.get("cur_sma30w")
            if sma30w is not None:
                candidates.append(float(sma30w) * 0.97)

    elif sig_type == "RE_BREAKOUT":
        if daily_ind is not None:
            low_series = daily_ind.get("low")
            if low_series is not None and len(low_series) >= 30:
                try:
                    swing = float(low_series.iloc[-30:].min())
                    if swing > 0:
                        candidates.append(swing * 0.99)
                except Exception:
                    pass
            cm50 = daily_ind.get("cur_m50")
            if cm50 is not None:
                candidates.append(float(cm50) * 0.97)

    elif sig_type == "REBOUND":
        if weekly_ind is not None:
            sma30w = weekly_ind.get("cur_sma30w")
            if sma30w is not None:
                candidates.append(float(sma30w) * 0.97)
        if daily_ind is not None:
            cm50 = daily_ind.get("cur_m50")
            if cm50 is not None:
                candidates.append(float(cm50) * 0.97)

    # 첫 sane 후보 (price 미만) 채택
    for cand in candidates:
        if cand is not None and cand < price:
            return round(float(cand), 4)
    return None


def detect_base_pivot(df: pd.DataFrame,
                      lookback_weeks: int = PIVOT_LOOKBACK_WEEKS,
                      min_weeks: int = BASE_MIN_WEEKS,
                      diag: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Base(횡보 압축) 구간과 pivot(고점) 을 탐지.

    조건:
      - 최소 min_weeks 이상 연속 횡보
      - 폭(high_max - low_min)/pivot ≤ 15% (WIDE 는 거부)
      - 가장 최근 base 1개만 반환

    반환:
      { pivot_price, base_low, base_start_idx, base_end_idx, base_weeks,
        base_width_pct, base_quality: "TIGHT" | "LOOSE" | "WIDE" }
    """
    if df is None or len(df) < min_weeks * 5 + 5:
        _diag_set(diag, REJECT_BASE_TOO_SHORT,
                  {"bars": 0 if df is None else len(df),
                   "required_bars": min_weeks * 5 + 5})
        return None

    close = df["Close"]
    high  = df["High"]
    low   = df["Low"]
    n     = len(df)

    lookback_days = lookback_weeks * 5
    start = max(0, n - lookback_days - 2)
    end   = n - 1  # 현재 bar 는 돌파 후보 (base 에서 제외)

    best = None
    # 진단용 — 로직에는 영향 없음
    broke_on_width = False
    widest_pct     = 0.0
    reached_weeks  = 0.0
    # 뒤에서 앞으로 확장하면서 폭 ≤ 15% 를 유지하는 최장 base 찾기
    pivot_price = float(high.iloc[end - 1])
    base_low    = float(low.iloc[end - 1])

    for j in range(end - 1, start - 1, -1):
        h = float(high.iloc[j])
        l = float(low.iloc[j])
        pivot_price = max(pivot_price, h)
        base_low    = min(base_low, l)
        if pivot_price <= 0:
            continue
        width_pct = (pivot_price - base_low) / pivot_price * 100
        base_days = end - j
        base_weeks = base_days / 5.0

        # 폭이 너무 크면 중단
        if width_pct > 15.0:
            broke_on_width = True
            widest_pct     = width_pct
            break

        reached_weeks = max(reached_weeks, base_weeks)
        if base_weeks >= min_weeks:
            if width_pct <= 8.0:   quality = "TIGHT"
            elif width_pct <= 15.0: quality = "LOOSE"
            else:                   quality = "WIDE"
            best = {
                "pivot_price":     round(pivot_price, 4),
                "base_low":        round(base_low, 4),
                "base_high":       round(pivot_price, 4),
                "base_start_idx":  j,
                "base_end_idx":    end,
                "base_start_date": str(pd.Timestamp(df.index[j]).date()),
                # end는 신호일 bar 위치이며 base에는 end-1까지만 포함된다.
                "base_end_date":   str(pd.Timestamp(df.index[end - 1]).date()),
                "base_weeks":      round(base_weeks, 1),
                "base_width_pct":  round(width_pct, 2),
                "base_quality":    quality,
            }
    if best is None:
        if broke_on_width:
            _diag_set(diag, REJECT_BASE_TOO_WIDE,
                      {"width_pct": round(widest_pct, 2), "max_width_pct": 15.0,
                       "base_weeks": round(reached_weeks, 1)})
        elif reached_weeks > 0:
            _diag_set(diag, REJECT_BASE_TOO_SHORT,
                      {"base_weeks": round(reached_weeks, 1), "min_weeks": min_weeks})
        else:
            _diag_set(diag, REJECT_NO_BASE_FOUND, {})
    return best


def detect_base_pivot_v2(df: pd.DataFrame,
                         market: Optional[str] = None,
                         diag: Optional[Dict[str, Any]] = None
                         ) -> Optional[Dict[str, Any]]:
    """2단(2-tier) base/pivot 탐지 — Step 2 신규 (v1 detect_base_pivot 은 그대로 유지).

    v1 은 base 기간과 pivot 을 하나의 확장 루프에서 함께 계산해, 창을 뒤로
    넓힐수록 pivot(=구간 최고가)도 함께 높아지고 손절 기준(base_low)도 함께
    벌어지는 구조였다(S&P500 실측: 돌파 34건, 평균 손절폭 14.5%). v2 는 두
    구간을 분리한다:

      [자격 구간] 최근 BASE_LOOKBACK_DAYS(기본 25거래일) — 폭이 시장 변동성
                  대비 과하지 않은지만 확인한다 (거래 후보 자격 심사).
      [tight 구간] 최근 TIGHT_LOOKBACK_DAYS(기본 10거래일) — 이 구간의
                  고점을 pivot, 저점을 stop_ref(손절 기준)로 쓴다. base 전체
                  길이와 무관하게 손절 기준이 "최근 압축 구간"으로 고정되어
                  손절폭이 좁게 유지된다 (동일 실측: 돌파 82건, 평균 8.3%).
      [수축 확인] tight 구간 폭이 자격 구간 폭의 TIGHT_CONTRACTION_RATIO
                  (기본 0.85) 미만이어야 한다 — "최근이 더 좁아졌는가"(매물
                  고갈 신호). 이 조건이 없으면 단순 25일 신고가 돌파와
                  구분되지 않는다.

    v1 과 동일한 규약: 두 구간 모두 **신호일(현재 bar, df 의 마지막 행)을
    제외**하고 계산한다 (look-ahead 방지 — 신호일의 고가/저가를 base/pivot
    산출에 절대 포함하지 않는다).

    market 이 주어지면 config.market_param() 으로 KR_/US_ 오버라이드
    (BASE_MAX_WIDTH_PCT, TIGHT_MAX_WIDTH_PCT) 를 적용한다. None 이면 공통
    기본값만 본다.

    반환:
      { pivot_price, stop_ref, base_width, tight_width, contraction_ratio,
        base_days }
      실패 시 None (diag 는 base_too_wide / tight_too_wide / no_contraction /
      base_too_short 중 하나를 기록).
    """
    base_lookback   = int(market_param("BASE_LOOKBACK_DAYS", market, 25))
    base_max_width  = float(market_param("BASE_MAX_WIDTH_PCT", market, 25.0))
    tight_lookback  = int(market_param("TIGHT_LOOKBACK_DAYS", market, 10))
    tight_max_width = float(market_param("TIGHT_MAX_WIDTH_PCT", market, 10.0))
    contraction_max = float(market_param("TIGHT_CONTRACTION_RATIO", market, 0.85))

    if df is None:
        _diag_set(diag, REJECT_BASE_TOO_SHORT,
                  {"bars": 0, "required_bars": base_lookback + 1})
        return None

    n   = len(df)
    end = n - 1  # 현재(신호일) bar 제외 — v1 과 동일 규약

    if end - base_lookback < 0:
        _diag_set(diag, REJECT_BASE_TOO_SHORT,
                  {"bars": n, "required_bars": base_lookback + 1})
        return None

    base_slice = df.iloc[end - base_lookback: end]
    base_high  = float(base_slice["High"].max())
    base_low_v = float(base_slice["Low"].min())
    if base_high <= 0:
        _diag_set(diag, REJECT_BASE_TOO_SHORT, {"bars": n})
        return None

    base_width = (base_high - base_low_v) / base_high * 100
    if base_width > base_max_width:
        _diag_set(diag, REJECT_BASE_TOO_WIDE,
                  {"width_pct": round(base_width, 2), "max_width_pct": base_max_width})
        return None

    tight_slice = df.iloc[end - tight_lookback: end]
    tight_high  = float(tight_slice["High"].max())
    tight_low   = float(tight_slice["Low"].min())
    if tight_high <= 0:
        _diag_set(diag, REJECT_TIGHT_TOO_WIDE, {"width_pct": None})
        return None

    tight_width = (tight_high - tight_low) / tight_high * 100
    if tight_width > tight_max_width:
        _diag_set(diag, REJECT_TIGHT_TOO_WIDE,
                  {"width_pct": round(tight_width, 2), "max_width_pct": tight_max_width})
        return None

    # 수축 확인 — 곱셈으로 판정해 base_width==0(완전 평탄) 에서도 0-division 없이 안전.
    if tight_width >= base_width * contraction_max:
        contraction_ratio = round(tight_width / base_width, 3) if base_width > 0 else 0.0
        _diag_set(diag, REJECT_NO_CONTRACTION,
                  {"tight_width_pct": round(tight_width, 2),
                   "base_width_pct":  round(base_width, 2),
                   "contraction_ratio": contraction_ratio,
                   "max_ratio": contraction_max})
        return None

    contraction_ratio = round(tight_width / base_width, 3) if base_width > 0 else 0.0
    return {
        "pivot_price":       round(tight_high, 4),
        "stop_ref":          round(tight_low, 4),
        # 판정에 실제 사용한 두 구간의 불변 스냅샷. 아래 값은 반환 메타데이터만
        # 추가하며 위의 폭/수축/피벗 판정에는 관여하지 않는다.
        "base_start_date":   str(pd.Timestamp(base_slice.index[0]).date()),
        "base_end_date":     str(pd.Timestamp(base_slice.index[-1]).date()),
        "tight_start_date":  str(pd.Timestamp(tight_slice.index[0]).date()),
        "base_high":         round(base_high, 4),
        "base_low":          round(base_low_v, 4),
        "tight_high":        round(tight_high, 4),
        "tight_low":         round(tight_low, 4),
        "base_width":        round(base_width, 2),
        "tight_width":       round(tight_width, 2),
        "contraction_ratio": contraction_ratio,
        "base_days":         base_lookback,
    }


def _daily_vol_ratio(df: pd.DataFrame, idx: int) -> float:
    vol = df["Volume"]
    avg = vol.rolling(VOLUME_AVG_PERIOD, min_periods=10).mean()
    v = float(vol.iloc[idx])
    a = float(avg.iloc[idx]) if not pd.isna(avg.iloc[idx]) else 0.0
    return v / a if a > 0 else 0.0


def _any_recent_bar_meets_daily_volume(df: pd.DataFrame) -> bool:
    """SCAN_LOOKBACK_DAYS 이내에 일봉 거래량 조건(BREAKOUT_DAILY_VOL_RATIO)
    을 충족하는 bar 가 하나라도 있는지 — base/pivot 과 무관하게 순수 거래량
    신호 존재 여부만 본다.

    Step 2 진단 전용 헬퍼. 주봉 게이트(BREAKOUT_WEEKLY_VOL_RATIO)가 "일봉
    거래량 조건을 충족했을 실질 후보" 까지 잘라내는지 검증하는 데 쓰인다
    (funnel 의 ``weekly_gate_cut_but_would_pass_daily``). 판정 로직에는
    쓰이지 않는 순수 계측용 — 반환값이 어느 쪽이든 호출부 동작은 불변.
    """
    close = df["Close"]
    n = len(close)
    for i in range(1, min(SCAN_LOOKBACK_DAYS + 1, n)):
        abs_i = n - i
        if abs_i < 1:
            continue
        if _daily_vol_ratio(df, abs_i) >= BREAKOUT_DAILY_VOL_RATIO:
            return True
    return False


def _stage2_breakout_loop_v1(df: pd.DataFrame,
                             daily_ind: Dict,
                             stage: str,
                             wvr: float,
                             weekly_vol_short: bool,
                             diag: Optional[Dict[str, Any]] = None
                             ) -> Optional[Dict[str, Any]]:
    """v1 base pivot(detect_base_pivot, 단일 확장 루프) 기반 BREAKOUT 탐지.

    Step 2 이전 detect_stage2_breakout 의 루프 본문을 그대로 옮긴 것 —
    로직 변경 없음 (BASE_MODE="v1" 일 때만 호출된다).
    """
    close = df["Close"]
    ma150 = daily_ind["ma150"]
    ma50  = daily_ind["ma50"]
    n     = len(close)

    for i in range(1, min(SCAN_LOOKBACK_DAYS + 1, n)):
        abs_i = n - i
        if abs_i < 1:
            continue

        cp = float(close.iloc[abs_i])
        pp = float(close.iloc[abs_i - 1])

        df_pre = df.iloc[: abs_i + 1]
        base_diag: Optional[Dict[str, Any]] = {} if diag is not None else None
        base = detect_base_pivot(df_pre,
                                 lookback_weeks=PIVOT_LOOKBACK_WEEKS,
                                 min_weeks=BASE_MIN_WEEKS,
                                 diag=base_diag)
        if base is None or base["base_quality"] == "WIDE":
            if base is None:
                _diag_set(diag,
                          (base_diag or {}).get("reject") or REJECT_NO_BASE_FOUND,
                          (base_diag or {}).get("values"))
            else:
                _diag_set(diag, REJECT_BASE_TOO_WIDE,
                          {"width_pct": base["base_width_pct"], "max_width_pct": 15.0})
            continue

        pivot_price = float(base["pivot_price"])
        if not (pp <= pivot_price < cp):
            _diag_set(diag, REJECT_NO_PIVOT_BREAKOUT,
                      {"prev_close": pp, "close": cp, "pivot_price": pivot_price})
            continue

        cm150_raw = ma150.iloc[abs_i]
        if pd.isna(cm150_raw) or float(cm150_raw) <= 0:
            _diag_set(diag, REJECT_NO_MA150, {})
            continue
        cm150 = float(cm150_raw)
        ext_pct = (cp - cm150) / cm150 * 100
        if ext_pct > BREAKOUT_MAX_EXTENDED_PCT:
            _diag_set(diag, REJECT_EXTENSION_TOO_HIGH,
                      {"ext_pct": round(ext_pct, 2),
                       "threshold": BREAKOUT_MAX_EXTENDED_PCT})
            continue

        dvr = _daily_vol_ratio(df, abs_i)
        if dvr < BREAKOUT_DAILY_VOL_RATIO:
            _diag_set(diag, REJECT_DAILY_VOLUME_INSUFFICIENT,
                      {"dvr": round(dvr, 2), "threshold": BREAKOUT_DAILY_VOL_RATIO})
            continue

        cm50_raw = ma50.iloc[abs_i]
        cm50 = float(cm50_raw) if not pd.isna(cm50_raw) else float(daily_ind["cur_m50"])

        # legacy STRONG/WEAK 매핑 (scan_engine._grade 호환)
        legacy_quality = "STRONG" if base["base_quality"] == "TIGHT" else "WEAK"

        warning_flags: List[str] = []
        if stage == "STAGE1":
            warning_flags.append("STAGE1 → 2 전환 (조기 진입)")
        if weekly_vol_short:
            # AS_GATE=False 일 때만 도달 — 차단 대신 경고로만 남긴다
            warning_flags.append(
                f"주봉 거래량 미달 ({wvr:.2f} < {BREAKOUT_WEEKLY_VOL_RATIO})")

        _diag_ok(diag)
        return {
            "signal_type":     "BREAKOUT",
            "signal_date":     str(close.index[abs_i].date()),
            "vol_ratio":       round(dvr, 2),
            "pivot_price":     round(pivot_price, 4),
            "support_level":   round(cm50, 4),
            "base_quality":    legacy_quality,
            "base_quality_v4": base["base_quality"],
            "base_weeks":      base["base_weeks"],
            "base_width_pct":  base["base_width_pct"],
            "base_low":        base["base_low"],   # Phase 2 — compute_stop_loss 1순위
            "base_start_date": base.get("base_start_date"),
            "base_end_date":   base.get("base_end_date"),
            "base_high":       base.get("base_high", base.get("pivot_price")),
            "base_range_low":  base.get("base_low"),
            "warning_flags":   warning_flags,
            "stage_v4":        stage,
            "base_mode":       "v1",
        }

    if diag is not None and diag.get("reject") is None:
        _diag_set(diag, REJECT_NO_PIVOT_BREAKOUT, {})
    return None


def _stage2_breakout_loop_v2(df: pd.DataFrame,
                             daily_ind: Dict,
                             stage: str,
                             market: Optional[str],
                             wvr: float,
                             weekly_vol_short: bool,
                             diag: Optional[Dict[str, Any]] = None
                             ) -> Optional[Dict[str, Any]]:
    """2단 base pivot(detect_base_pivot_v2) 기반 BREAKOUT 탐지 — Step 2 신규.

    _stage2_breakout_loop_v1 과 같은 뼈대(같은 SCAN_LOOKBACK_DAYS 루프,
    같은 ma150/extension/daily-volume 순서)를 쓰지만 base/pivot 판정만
    detect_base_pivot_v2 로 교체되어 있다.
    """
    close = df["Close"]
    ma150 = daily_ind["ma150"]
    ma50  = daily_ind["ma50"]
    n     = len(close)

    for i in range(1, min(SCAN_LOOKBACK_DAYS + 1, n)):
        abs_i = n - i
        if abs_i < 1:
            continue

        cp = float(close.iloc[abs_i])
        pp = float(close.iloc[abs_i - 1])

        df_pre = df.iloc[: abs_i + 1]
        base_diag: Optional[Dict[str, Any]] = {} if diag is not None else None
        base = detect_base_pivot_v2(df_pre, market=market, diag=base_diag)
        if base is None:
            _diag_set(diag,
                      (base_diag or {}).get("reject") or REJECT_NO_BASE_FOUND,
                      (base_diag or {}).get("values"))
            continue

        pivot_price = float(base["pivot_price"])
        if not (pp <= pivot_price < cp):
            _diag_set(diag, REJECT_NO_PIVOT_BREAKOUT,
                      {"prev_close": pp, "close": cp, "pivot_price": pivot_price})
            continue

        cm150_raw = ma150.iloc[abs_i]
        if pd.isna(cm150_raw) or float(cm150_raw) <= 0:
            _diag_set(diag, REJECT_NO_MA150, {})
            continue
        cm150 = float(cm150_raw)
        ext_pct = (cp - cm150) / cm150 * 100
        if ext_pct > BREAKOUT_MAX_EXTENDED_PCT:
            _diag_set(diag, REJECT_EXTENSION_TOO_HIGH,
                      {"ext_pct": round(ext_pct, 2),
                       "threshold": BREAKOUT_MAX_EXTENDED_PCT})
            continue

        dvr = _daily_vol_ratio(df, abs_i)
        if dvr < BREAKOUT_DAILY_VOL_RATIO:
            _diag_set(diag, REJECT_DAILY_VOLUME_INSUFFICIENT,
                      {"dvr": round(dvr, 2), "threshold": BREAKOUT_DAILY_VOL_RATIO})
            continue

        cm50_raw = ma50.iloc[abs_i]
        cm50 = float(cm50_raw) if not pd.isna(cm50_raw) else float(daily_ind["cur_m50"])

        # v2 후보는 이미 tight-width + 수축 필터를 통과했으므로 legacy
        # TIGHT/LOOSE 등급의 최상위(STRONG)에 대응 — v1 의 STRONG/WEAK 와
        # 동일한 필드로 scan_engine._grade 호환 유지.
        warning_flags: List[str] = []
        if stage == "STAGE1":
            warning_flags.append("STAGE1 → 2 전환 (조기 진입)")
        if weekly_vol_short:
            warning_flags.append(
                f"주봉 거래량 미달 ({wvr:.2f} < {BREAKOUT_WEEKLY_VOL_RATIO})")

        _diag_ok(diag)
        return {
            "signal_type":       "BREAKOUT",
            "signal_date":       str(close.index[abs_i].date()),
            "vol_ratio":         round(dvr, 2),
            "pivot_price":       round(pivot_price, 4),
            "support_level":     round(cm50, 4),
            "base_quality":      "STRONG",
            "base_quality_v4":   "TIGHT",
            "base_weeks":        round(base["base_days"] / 5.0, 1),
            "base_width_pct":    base["base_width"],
            "tight_width_pct":   base["tight_width"],
            "contraction_ratio": base["contraction_ratio"],
            "base_low":          base["stop_ref"],  # 공개 필드 호환값 (compute_stop_loss 는 stop_ref 우선)
            "stop_ref":          base["stop_ref"],   # Phase 2 — compute_stop_loss v2 1순위
            "base_start_date":   base["base_start_date"],
            "base_end_date":     base["base_end_date"],
            "tight_start_date":  base["tight_start_date"],
            "base_high":         base["base_high"],
            # 기존 signal["base_low"]는 v2 손절 호환상 tight 저점을 유지한다.
            # 차트/DB용 실제 자격구간 저점은 별도 키로 전달한다.
            "base_range_low":    base["base_low"],
            "tight_high":        base["tight_high"],
            "tight_low":         base["tight_low"],
            "warning_flags":     warning_flags,
            "stage_v4":          stage,
            "base_mode":         "v2",
        }

    if diag is not None and diag.get("reject") is None:
        _diag_set(diag, REJECT_NO_PIVOT_BREAKOUT, {})
    return None


def detect_stage2_breakout(df: pd.DataFrame,
                           weekly_ind: Optional[Dict],
                           daily_ind: Optional[Dict],
                           diag: Optional[Dict[str, Any]] = None,
                           market: Optional[str] = None
                           ) -> Optional[Dict[str, Any]]:
    """Stage1→Stage2 base pivot 상향 돌파 감지 (v4).

    조건:
      - 주봉 데이터 존재 (weekly_ind is not None)
      - Stage == STAGE1 or STAGE2 (30w SMA 위 + 상승)
      - base/pivot 확인 — BASE_MODE 로 두 구현 중 택1 (기본 "v2"):
          v1: detect_base_pivot — 5주+ 단일 확장 루프, 폭 ≤15%
          v2: detect_base_pivot_v2 — 자격구간(폭)+tight구간(pivot/손절)+수축확인,
              market 인자로 KR/US 임계값 오버라이드 (Step 2)
      - 최근 SCAN_LOOKBACK_DAYS 내 pivot 상향 돌파
      - 일봉 거래량 ≥ BREAKOUT_DAILY_VOL_RATIO (hard block)
      - 주봉 거래량 ≥ BREAKOUT_WEEKLY_VOL_RATIO (BREAKOUT_WEEKLY_VOL_AS_GATE=True
        일 때만 hard block — False 면 warning 으로 강등)
      - MA150 대비 과매수 < BREAKOUT_MAX_EXTENDED_PCT

    ``diag`` 는 순수 out-parameter (진단 계측). None 이면 완전한 no-op 이고,
    dict 를 넘기면 탈락 사유가 diag["reject"] / diag["values"] 에 기록된다.
    반환값과 판정 로직은 diag 유무와 무관하게 동일하다.

    ``market`` 은 "KR"/"US" — v2 의 시장별 base 임계값 조회에만 쓰인다
    (v1 경로에서는 무시).
    """
    if daily_ind is None or weekly_ind is None:
        _diag_set(diag,
                  REJECT_NO_DAILY_DATA if daily_ind is None else REJECT_NO_WEEKLY_DATA,
                  {})
        return None
    stage = classify_stage(weekly_ind, daily_ind)
    if stage not in ("STAGE1", "STAGE2"):
        _diag_set(diag, REJECT_WEEKLY_STAGE_NOT_1_OR_2, {"stage": stage})
        return None

    wvr = float(weekly_ind.get("weekly_volume_ratio", 0.0) or 0.0)
    if diag is not None:
        # Step 2 임계값 재설정 근거 — BREAKOUT 탐지기가 실제로 임계값과
        # 비교한 종목의 비율 분포를 scan_engine 이 집계한다.
        diag["wvr"] = wvr
    weekly_vol_short = wvr < BREAKOUT_WEEKLY_VOL_RATIO
    if weekly_vol_short and BREAKOUT_WEEKLY_VOL_AS_GATE:
        if diag is not None:
            # Step 2 — 주봉 게이트가 "일봉 거래량 조건은 충족했을" 실질
            # 후보까지 잘라내는지 진단 (순수 계측, 판정에는 영향 없음).
            diag["would_pass_daily_volume"] = _any_recent_bar_meets_daily_volume(df)
        _diag_set(diag, REJECT_WEEKLY_VOLUME_INSUFFICIENT,
                  {"wvr": wvr, "threshold": BREAKOUT_WEEKLY_VOL_RATIO})
        return None

    if BASE_MODE == "v2":
        return _stage2_breakout_loop_v2(df, daily_ind, stage, market,
                                        wvr, weekly_vol_short, diag=diag)
    return _stage2_breakout_loop_v1(df, daily_ind, stage,
                                    wvr, weekly_vol_short, diag=diag)


def detect_continuation_breakout(df: pd.DataFrame,
                                 weekly_ind: Optional[Dict],
                                 daily_ind: Optional[Dict],
                                 diag: Optional[Dict[str, Any]] = None
                                 ) -> Optional[Dict[str, Any]]:
    """Stage2 진행 중 continuation base 돌파 감지 (v4).

    ``diag`` 는 순수 out-parameter (진단 계측). 반환값/로직 불변.
    """
    if daily_ind is None:
        _diag_set(diag, REJECT_NO_DAILY_DATA, {})
        return None
    stage = classify_stage(weekly_ind, daily_ind)
    if stage != "STAGE2":
        _diag_set(diag, REJECT_WEEKLY_STAGE_NOT_2, {"stage": stage})
        return None
    sig = _find_rebreakout_signal(daily_ind, diag=diag)
    if sig is None:
        if diag is not None and diag.get("reject") is None:
            _diag_set(diag, REJECT_NO_PIVOT_BREAKOUT, {})
        return None

    warning_flags: List[str] = []
    if weekly_ind and weekly_ind.get("weekly_volume_ratio", 0) < 1.5:
        warning_flags.append("주봉 거래량 감소")
    sig["warning_flags"] = warning_flags
    sig["stage_v4"]      = stage
    _diag_ok(diag)
    return sig


def detect_rebound_entry(df: pd.DataFrame,
                         weekly_ind: Optional[Dict],
                         daily_ind: Optional[Dict],
                         diag: Optional[Dict[str, Any]] = None
                         ) -> Optional[Dict[str, Any]]:
    """Stage2 MA50 눌림목 반등 감지 (시간순, v4).

    Strategy invariants:
      - 주봉 STAGE2 필수 (weekly_ind 없거나 STAGE2 아니면 거부).
      - REBOUND_REQUIRE_BASE_RETEST=True 일 때:
          (a) 직전 v4 base pivot 위에서의 MA50 눌림 OR
          (b) 주봉 30-SMA 터치 + 회복 — 둘 중 하나 미충족 시 거부.

    ``diag`` 는 순수 out-parameter (진단 계측). 반환값/로직 불변.
    """
    if daily_ind is None:
        _diag_set(diag, REJECT_NO_DAILY_DATA, {})
        return None
    if weekly_ind is None:
        _diag_set(diag, REJECT_NO_WEEKLY_DATA, {})
        return None  # 주봉 데이터 없는 종목은 REBOUND 판정 금지
    stage = classify_stage(weekly_ind, daily_ind)
    if stage != "STAGE2":
        _diag_set(diag, REJECT_WEEKLY_STAGE_NOT_2, {"stage": stage})
        return None  # 일봉 fallback 제거 — 주봉 STAGE2 필수

    sig = _find_rebound_signal_v4(df, daily_ind, weekly_ind, diag=diag)
    if sig is None:
        if diag is not None and diag.get("reject") is None:
            _diag_set(diag, REJECT_NO_REBOUND_CONFIRM, {})
        return None

    warning_flags: List[str] = []
    if weekly_ind.get("slope30w", 0) <= _FLAT_SLOPE:
        warning_flags.append("주봉 30-SMA 기울기 둔화")
    sig["warning_flags"] = warning_flags
    sig["stage_v4"]      = stage
    _diag_ok(diag)
    return sig


def _find_rebound_signal_v4(df: pd.DataFrame,
                            daily_ind: Dict,
                            weekly_ind: Dict,
                            diag: Optional[Dict[str, Any]] = None
                            ) -> Optional[Dict]:
    """v4 REBOUND: legacy MA50 touch+rebound + base/30w 재테스트 게이트.

    1. legacy `_find_rebound_signal` 으로 후보 시그널 추출.
    2. REBOUND_REQUIRE_BASE_RETEST=False → 그대로 통과.
    3. True → 다음 두 조건 중 하나 이상 만족해야 통과:
       (a) 직전 base pivot 위에서의 MA50 눌림: 터치 직전 일봉 종가 ≥ pivot_price
       (b) 주봉 30-SMA 재테스트: 터치 일봉 종가가 cur_sma30w ±REBOUND_TOUCH_PCT
           이내 + 시그널 시점 종가가 cur_sma30w 위로 회복.
    """
    legacy_sig = _find_rebound_signal(daily_ind, diag=diag)
    if legacy_sig is None:
        return None

    if not REBOUND_REQUIRE_BASE_RETEST:
        return legacy_sig

    close = daily_ind["close"]
    low   = daily_ind["low"]
    ma50  = daily_ind["ma50"]
    n = len(close)

    # 시그널 위치(j_signal) 매핑
    try:
        sig_pos_raw = close.index.get_loc(pd.Timestamp(legacy_sig["signal_date"]))
    except (KeyError, TypeError, ValueError):
        _diag_set(diag, REJECT_REBOUND_POS_UNMAPPED,
                  {"signal_date": legacy_sig.get("signal_date")})
        return None
    if isinstance(sig_pos_raw, slice):
        sig_pos = sig_pos_raw.start
    else:
        sig_pos = int(sig_pos_raw)
    if sig_pos <= 0:
        _diag_set(diag, REJECT_REBOUND_POS_UNMAPPED, {"sig_pos": sig_pos})
        return None

    # 터치 위치 추정: 시그널 직전 14일 윈도우에서 MA50 ±touch_pct 안에 들어간
    # 가장 깊은 (low 가 m50 에 가장 가까운) 시점.
    touch_pos = None
    best_dist = None
    win_lo = max(0, sig_pos - 14)
    for k in range(win_lo, sig_pos):
        if pd.isna(ma50.iloc[k]):
            continue
        m50_k = float(ma50.iloc[k])
        if m50_k <= 0:
            continue
        l_k = float(low.iloc[k])
        touch_lo = m50_k * (1.0 - REBOUND_MAX_PULLBACK_PCT / 100)
        touch_hi = m50_k * (1.0 + REBOUND_TOUCH_PCT / 100)
        if not (touch_lo <= l_k <= touch_hi):
            continue
        dist = abs(l_k - m50_k)
        if best_dist is None or dist < best_dist:
            best_dist = dist
            touch_pos = k
    if touch_pos is None:
        _diag_set(diag, REJECT_NO_REBOUND_TOUCH, {"sig_pos": sig_pos})
        return None

    # condition (a): 직전 base 위에서의 MA50 눌림
    cond_a = False
    base_meta = None
    weekly_df = weekly_ind.get("weekly_df")
    if weekly_df is not None and len(weekly_df) >= BASE_MIN_WEEKS + 1:
        base = detect_base_pivot(
            weekly_df,
            lookback_weeks=PIVOT_LOOKBACK_WEEKS,
            min_weeks=BASE_MIN_WEEKS,
        )
        if base is not None and base["base_quality"] != "WIDE":
            pivot_price = float(base["pivot_price"])
            pre_touch = touch_pos - 1
            if pre_touch >= 0:
                cp_pre = float(close.iloc[pre_touch])
                if cp_pre >= pivot_price:
                    cond_a = True
                    base_meta = {
                        "pivot_price":     pivot_price,
                        "base_quality_v4": base["base_quality"],
                        "base_weeks":      base["base_weeks"],
                    }

    # condition (b): 30w SMA 재테스트
    cond_b = False
    sma30w = float(weekly_ind.get("cur_sma30w") or 0.0)
    if sma30w > 0:
        tol = sma30w * REBOUND_TOUCH_PCT / 100
        cp_touch = float(close.iloc[touch_pos])
        cp_sig   = float(close.iloc[sig_pos])
        if abs(cp_touch - sma30w) <= tol and cp_sig > sma30w:
            cond_b = True

    if not (cond_a or cond_b):
        _diag_set(diag, REJECT_REBOUND_GATE_FAILED,
                  {"base_retest": cond_a, "sma30w_retest": cond_b})
        return None

    legacy_sig["v4_gate"] = "BASE_RETEST" if cond_a else "30W_RETEST"
    if base_meta is not None:
        legacy_sig["pivot_price"]     = round(base_meta["pivot_price"], 4)
        legacy_sig["base_quality_v4"] = base_meta["base_quality_v4"]
        legacy_sig["base_weeks"]      = base_meta["base_weeks"]
    return legacy_sig


def detect_exit_warning(df: pd.DataFrame,
                        weekly_ind: Optional[Dict],
                        daily_ind: Optional[Dict],
                        buy_price: Optional[float] = None,
                        stop_loss:  Optional[float] = None
                        ) -> Optional[Dict[str, Any]]:
    """Stage3/4 진입, 손절, 30w SMA 이탈 등 종합 매도 경고 (v4).

    기존 check_sell_signal 과 동일한 severity 체계 사용.
    """
    # 현재는 legacy check_sell_signal 을 그대로 사용
    return None  # 상위 API 는 check_sell_signal 을 호출


# ══════════════════════════════════════════════════════════════════
# Legacy 유틸리티 (하위 호환 — 기존 34개 테스트가 의존)
# ══════════════════════════════════════════════════════════════════

def _slope(series: pd.Series, n: int = MA_SLOPE_PERIOD) -> float:
    """MA 기울기(% / bar). 양수 = 상승 추세."""
    s = series.dropna().iloc[-n:]
    if len(s) < max(2, n // 2):
        return 0.0
    x = np.arange(len(s))
    k = np.polyfit(x, s.values, 1)[0]
    cur = s.iloc[-1]
    return float(k / cur * 100) if cur else 0.0


def stage_of(price: float, ma: float, slope: float) -> str:
    """(Legacy) 일봉 MA150 + slope 로 Stage 분류.

    v4 는 classify_stage() 를 사용하지만 호환을 위해 유지.
    """
    up      = price > ma
    rising  = slope >  0.02
    falling = slope < -0.02
    if up and rising:                     return "STAGE2"
    if up and not rising and not falling: return "STAGE3"
    if not up and falling:                return "STAGE4"
    return "STAGE1"


def calc_rs(close: pd.Series, benchmark_close: pd.Series,
            period: int = RS_PERIOD) -> Optional[float]:
    """(Legacy) 단순 ratio RS: 주식수익률 / 지수수익률.

    v4 는 compute_relative_performance() 의 Mansfield RS 를 사용.
    """
    try:
        if len(close) < period or len(benchmark_close) < period:
            return None
        stock_ret = float(close.iloc[-1]) / float(close.iloc[-period]) - 1
        bench_ret = float(benchmark_close.iloc[-1]) / float(benchmark_close.iloc[-period]) - 1
        if bench_ret == 0:
            return None
        return round(stock_ret / bench_ret, 2)
    except Exception:
        return None


# ── 지표 빌드 ─────────────────────────────────────────────────────

def _build_indicators(df: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """공통 일봉 기술적 지표를 계산해 dict로 반환."""
    close   = df["Close"]
    vol     = df["Volume"]
    ma150   = close.rolling(MA_PERIOD,        min_periods=MA_PERIOD // 2).mean()
    ma50    = close.rolling(REBOUND_MA_PERIOD, min_periods=REBOUND_MA_PERIOD // 2).mean()
    vol_avg = vol.rolling(VOLUME_AVG_PERIOD,  min_periods=10).mean()

    if pd.isna(ma150.iloc[-1]):
        return None

    cur_p    = float(close.iloc[-1])
    cur_m150 = float(ma150.iloc[-1])
    cur_m50  = float(ma50.iloc[-1]) if not pd.isna(ma50.iloc[-1]) else cur_p
    sl150    = _slope(ma150)
    sl50     = _slope(ma50)
    cur_v    = float(vol.iloc[-1])
    cur_va   = float(vol_avg.iloc[-1]) if not pd.isna(vol_avg.iloc[-1]) else 1.0

    high = df["High"]
    low  = df["Low"]

    return {
        "close": close, "high": high, "low": low, "vol": vol,
        "ma150": ma150, "ma50": ma50, "vol_avg": vol_avg,
        "cur_p": cur_p, "cur_m150": cur_m150, "cur_m50": cur_m50,
        "slope150": sl150, "slope50": sl50,
        "cur_v": cur_v, "cur_va": cur_va,
        "stage": stage_of(cur_p, cur_m150, sl150),
    }


# ── 시그널 탐지 헬퍼 ──────────────────────────────────────────────

def _find_breakout_signal(ind: Dict) -> Optional[Dict]:
    """Pivot/Base 돌파 감지 (legacy, v3 검증본)."""
    close, high, low = ind["close"], ind["high"], ind["low"]
    vol, vol_avg = ind["vol"], ind["vol_avg"]
    ma150, ma50 = ind["ma150"], ind["ma50"]
    n = len(close)

    for i in range(1, min(SCAN_LOOKBACK_DAYS + 1, n - BREAKOUT_BASE_LOOKBACK_DAYS - 2)):
        abs_i = n - i

        cp   = float(close.iloc[abs_i])
        cm   = float(ma150.iloc[abs_i]) if not pd.isna(ma150.iloc[abs_i]) else None
        cm50 = float(ma50.iloc[abs_i])  if not pd.isna(ma50.iloc[abs_i])  else None
        if cm is None or cm50 is None:
            continue

        if cp <= cm:
            continue
        if _slope(ma150.iloc[:abs_i + 1]) <= 0:
            continue
        if REQUIRE_PRICE_ABOVE_MA50 and cp <= cm50:
            continue

        ext_pct = (cp - cm) / cm * 100
        if ext_pct > BREAKOUT_MAX_EXTENDED_PCT:
            continue

        base_start = abs_i - BREAKOUT_BASE_LOOKBACK_DAYS
        base_end   = abs_i
        if base_start < 0:
            continue
        base_slice = close.iloc[base_start:base_end]
        if len(base_slice) < BREAKOUT_MIN_BASE_DAYS:
            continue

        pivot_high = float(base_slice.max())
        pp = float(close.iloc[abs_i - 1]) if abs_i > 0 else cp
        if pp > pivot_high:
            continue
        if cp <= pivot_high:
            continue

        dv  = float(vol.iloc[abs_i])
        dva = float(vol_avg.iloc[abs_i])
        dvr = dv / dva if dva > 0 else 0.0
        if dvr < BREAKOUT_VOLUME_RATIO:
            continue

        day_high = float(high.iloc[abs_i])
        if day_high > 0 and cp < day_high * 0.70:
            continue

        base_quality = "WEAK"
        pre_len = min(10, abs_i)
        pre_close = close.iloc[abs_i - pre_len:abs_i]
        pre_ma150 = ma150.iloc[abs_i - pre_len:abs_i]
        if pre_len >= 10:
            in_range = sum(
                1 for k in range(pre_len)
                if not pd.isna(pre_ma150.iloc[k]) and pre_ma150.iloc[k] > 0
                and abs(float(pre_close.iloc[k]) - float(pre_ma150.iloc[k]))
                    / float(pre_ma150.iloc[k]) <= 0.05
            )
            if in_range >= 7:
                base_quality = "STRONG"

        return {
            "signal_type":  "BREAKOUT",
            "signal_date":  str(close.index[abs_i].date()),
            "vol_ratio":    round(dvr, 2),
            "pivot_price":  round(pivot_high, 4),
            "support_level": round(cm50, 4),
            "base_quality": base_quality,
        }

    return None


def _find_rebreakout_signal(ind: Dict,
                            diag: Optional[Dict[str, Any]] = None) -> Optional[Dict]:
    """Stage2 연속 돌파(재돌파) 감지 (legacy).

    ``diag`` 는 순수 out-parameter (진단 계측). 반환값/로직 불변.
    """
    if ind["stage"] != "STAGE2":
        _diag_set(diag, REJECT_DAILY_STAGE_NOT_2, {"stage": ind["stage"]})
        return None

    close, vol, vol_avg = ind["close"], ind["vol"], ind["vol_avg"]
    ma150, ma50 = ind["ma150"], ind["ma50"]
    n = len(close)

    for i in range(1, min(SCAN_LOOKBACK_DAYS + 1, n - REBREAKOUT_BASE_LOOKBACK_DAYS - 2)):
        abs_i = n - i

        cp   = float(close.iloc[abs_i])
        cm   = float(ma150.iloc[abs_i]) if not pd.isna(ma150.iloc[abs_i]) else None
        cm50 = float(ma50.iloc[abs_i])  if not pd.isna(ma50.iloc[abs_i])  else None
        if cm is None or cm50 is None:
            _diag_set(diag, REJECT_NO_MA150, {})
            continue

        if cp <= cm:
            _diag_set(diag, REJECT_PRICE_BELOW_MA150, {"close": cp, "ma150": cm})
            continue
        if cp <= cm50:
            _diag_set(diag, REJECT_PRICE_BELOW_MA50, {"close": cp, "ma50": cm50})
            continue
        if _slope(ma150.iloc[:abs_i + 1]) <= 0:
            _diag_set(diag, REJECT_MA150_NOT_RISING, {})
            continue

        base_start = abs_i - REBREAKOUT_BASE_LOOKBACK_DAYS
        base_end   = abs_i
        if base_start < 0:
            _diag_set(diag, REJECT_BASE_TOO_SHORT, {"base_start": base_start})
            continue
        base_slice = close.iloc[base_start:base_end]
        if len(base_slice) < 5:
            _diag_set(diag, REJECT_BASE_TOO_SHORT, {"base_bars": len(base_slice)})
            continue

        pivot_high = float(base_slice.max())
        pivot_low  = float(base_slice.min())

        pullback_pct = (pivot_high - pivot_low) / pivot_high * 100
        if pullback_pct < 3.0:
            _diag_set(diag, REJECT_PULLBACK_TOO_SHALLOW,
                      {"pullback_pct": round(pullback_pct, 2), "min_pct": 3.0})
            continue
        if pullback_pct > REBREAKOUT_MAX_PULLBACK_PCT:
            _diag_set(diag, REJECT_BASE_TOO_WIDE,
                      {"pullback_pct": round(pullback_pct, 2),
                       "max_pct": REBREAKOUT_MAX_PULLBACK_PCT})
            continue

        pp = float(close.iloc[abs_i - 1]) if abs_i > 0 else cp
        if pp > pivot_high:
            _diag_set(diag, REJECT_NO_PIVOT_BREAKOUT,
                      {"prev_close": pp, "pivot_price": pivot_high})
            continue
        if cp <= pivot_high:
            _diag_set(diag, REJECT_NO_PIVOT_BREAKOUT,
                      {"close": cp, "pivot_price": pivot_high})
            continue

        dv  = float(vol.iloc[abs_i])
        dva = float(vol_avg.iloc[abs_i])
        dvr = dv / dva if dva > 0 else 0.0
        if dvr < REBREAKOUT_VOLUME_RATIO:
            _diag_set(diag, REJECT_DAILY_VOLUME_INSUFFICIENT,
                      {"dvr": round(dvr, 2), "threshold": REBREAKOUT_VOLUME_RATIO})
            continue

        if REBREAKOUT_REQUIRE_VOLUME_DRYUP:
            base_vol     = vol.iloc[base_start:base_end]
            base_vol_avg = vol_avg.iloc[base_start:base_end]
            valid_mask   = base_vol_avg > 0
            if valid_mask.any():
                avg_ratio = float((base_vol[valid_mask] / base_vol_avg[valid_mask]).mean())
                if avg_ratio > 0.8:
                    _diag_set(diag, REJECT_NO_VOLUME_DRYUP,
                              {"base_vol_ratio": round(avg_ratio, 2), "max_ratio": 0.8})
                    continue

        _diag_ok(diag)
        return {
            "signal_type":  "RE_BREAKOUT",
            "signal_date":  str(close.index[abs_i].date()),
            "vol_ratio":    round(dvr, 2),
            "pivot_price":  round(pivot_high, 4),
            "support_level": round(cm50, 4),
        }

    if diag is not None and diag.get("reject") is None:
        _diag_set(diag, REJECT_NO_PIVOT_BREAKOUT, {})
    return None


def _find_rebound_signal(ind: Dict,
                         diag: Optional[Dict[str, Any]] = None) -> Optional[Dict]:
    """MA50 눌림목 반등 감지 (시간순, legacy).

    ``diag`` 는 순수 out-parameter (진단 계측). 반환값/로직 불변.
    """
    if ind["stage"] not in ("STAGE2", "STAGE3"):
        _diag_set(diag, REJECT_DAILY_STAGE_NOT_2_OR_3, {"stage": ind["stage"]})
        return None
    if ind["slope150"] <= 0.02:
        _diag_set(diag, REJECT_MA150_NOT_RISING,
                  {"slope150": ind["slope150"], "threshold": 0.02})
        return None

    close, low, vol, vol_avg = ind["close"], ind["low"], ind["vol"], ind["vol_avg"]
    ma150, ma50 = ind["ma150"], ind["ma50"]
    n = len(close)

    scan_len   = min(SCAN_LOOKBACK_DAYS + 20, n - 2)
    win_start  = n - scan_len

    touched_low  = None
    touch_ma50   = None
    latest_sig   = None

    for j in range(win_start, n - 1):
        p   = float(close.iloc[j])
        l   = float(low.iloc[j])
        if pd.isna(ma50.iloc[j]) or pd.isna(ma150.iloc[j]):
            continue

        m50  = float(ma50.iloc[j])
        m150 = float(ma150.iloc[j])

        if p < m150 * 0.95:
            touched_low = None
            touch_ma50  = None
            continue

        if touched_low is None:
            touch_limit = m50 * (1.0 + REBOUND_TOUCH_PCT / 100)
            max_pullback = m50 * (1.0 - REBOUND_MAX_PULLBACK_PCT / 100)
            if max_pullback <= l <= touch_limit:
                touched_low = l
                touch_ma50  = m50
                _diag_set(diag, REJECT_NO_REBOUND_CONFIRM,
                          {"touch_date": str(close.index[j].date())})
        else:
            if l < touched_low:
                touched_low = l
            if m50 > 0 and l < m50 * (1.0 - REBOUND_MAX_PULLBACK_PCT / 100):
                touched_low = None
                touch_ma50  = None
                continue

            if (touched_low and
                p >= touched_low * (1.0 + REBOUND_CONFIRM_PCT / 100) and
                p > m50):

                dv  = float(vol.iloc[j])
                dva = float(vol_avg.iloc[j])
                dvr = dv / dva if dva > 0 else 0.0
                if dvr < 1.3:
                    _diag_set(diag, REJECT_DAILY_VOLUME_INSUFFICIENT,
                              {"dvr": round(dvr, 2), "threshold": 1.3})
                    touched_low = None
                    touch_ma50  = None
                    continue

                days_ago = (n - 1) - j
                if days_ago >= SCAN_LOOKBACK_DAYS:
                    _diag_set(diag, REJECT_REBOUND_TOO_OLD,
                              {"days_ago": days_ago,
                               "lookback_days": SCAN_LOOKBACK_DAYS})
                if days_ago < SCAN_LOOKBACK_DAYS:
                    latest_sig = {
                        "signal_type":   "REBOUND",
                        "signal_date":   str(close.index[j].date()),
                        "vol_ratio":     round(dvr, 2),
                        "support_level": round(touch_ma50, 4) if touch_ma50 else round(m50, 4),
                        "pivot_price":   None,
                    }
                touched_low = None
                touch_ma50  = None

    if diag is not None:
        if latest_sig is not None:
            _diag_ok(diag)
        elif diag.get("reject") is None:
            _diag_set(diag, REJECT_NO_REBOUND_TOUCH, {})
    return latest_sig


# ── 신호 품질 계산 ─────────────────────────────────────────────────

def _signal_quality(vol_ratio: float, slope: float,
                    rs_value: Optional[float], rs_trend: Optional[str],
                    signal_type: str) -> str:
    """STRONG / MODERATE / WEAK 품질 점수 (Mansfield RS 기준).

    점수:
      vol_ratio  ≥ 3.0 → +2 / ≥ 2.0 → +1
      slope      > 0.10 → +2 / > 0.04 → +1
      rs_value   ≥ +5 → +2 / ≥ 0 → +1 / < 0 → 0
      rs_trend   RISING → +1 / FALLING → -1
      signal_type BREAKOUT → +1
      → ≥5 STRONG / ≥3 MODERATE / 그 외 WEAK
    """
    score = 0
    if vol_ratio >= 3.0:  score += 2
    elif vol_ratio >= 2.0: score += 1

    if slope > 0.10:  score += 2
    elif slope > 0.04: score += 1

    if rs_value is not None:
        if rs_value >= 5.0:  score += 2
        elif rs_value >= 0.0: score += 1

    if rs_trend == "RISING":   score += 1
    elif rs_trend == "FALLING": score -= 1

    if signal_type == "BREAKOUT": score += 1

    if score >= 5: return "STRONG"
    if score >= 3: return "MODERATE"
    return "WEAK"


# ══════════════════════════════════════════════════════════════════
# 공개 API — analyze_stock / check_sell_signal
# ══════════════════════════════════════════════════════════════════

def analyze_stock(df: pd.DataFrame, ticker: str, name: str, market: str,
                  benchmark_close: pd.Series = None,
                  market_condition: str = None,
                  diag: Optional[Dict[str, Any]] = None) -> Optional[dict]:
    """주식 하나에 대해 Weinstein 매수 시그널을 탐지 (v4 강화).

    반환 dict:
      기존 필드: ticker, name, market, signal_type, stage, price, ma150, ma50,
                price_vs_ma_pct, ma_slope, volume, volume_avg, volume_ratio,
                signal_date, rs, pivot_price, support_level, base_quality,
                market_condition, signal_quality, rs_passed
      v4 신규: sma30w, sma10w, weekly_stage, rs_value (Mansfield),
              rs_trend, weekly_volume_ratio, base_weeks, warning_flags
      Step 2 신규(BREAKOUT, BASE_MODE="v2" 일 때만 채워짐): base_width_pct,
              tight_width_pct, contraction_ratio, stop_ref, base_mode

    ``diag`` 는 순수 out-parameter (진단 계측). dict 를 넘기면:
      diag["reject"]    — 탈락 사유 키 (시그널 생성 시 None)
      diag["values"]    — 사유 관련 수치
      diag["detectors"] — 세 탐지기별 개별 diag
      diag["rs_flags"]  — 시그널이 있을 때 RS 경고 플래그 (실제 거부는
                          strict_filter 가 수행)
    diag=None (기본값) 이면 완전한 no-op — 반환값/동작 불변.
    """
    if df is None or len(df) < MA_PERIOD + BREAKOUT_BASE_LOOKBACK_DAYS + 10:
        _diag_set(diag, REJECT_INSUFFICIENT_HISTORY,
                  {"bars": 0 if df is None else len(df),
                   "required_bars": MA_PERIOD + BREAKOUT_BASE_LOOKBACK_DAYS + 10})
        return None

    df = df.copy().sort_index()

    # ── v4: 주봉 + 일봉 indicator 병렬 계산 ──
    daily_ind  = _build_indicators(df)
    if daily_ind is None:
        _diag_set(diag, REJECT_NO_DAILY_DATA, {})
        return None

    weekly_df  = to_weekly_ohlcv(df)
    weekly_ind = compute_weekly_indicators(weekly_df, df) if len(weekly_df) > 0 else None
    v4_stage   = classify_stage(weekly_ind, daily_ind)
    if diag is not None and weekly_ind is not None:
        diag["week_basis"]        = weekly_ind.get("week_volume_basis")
        diag["week_elapsed_days"] = weekly_ind.get("week_elapsed_days")

    # ── v4: BEAR 장세에서 Stage4 는 1차 차단 (scan_engine 필터와 2중) ──
    if market_condition == "BEAR" and v4_stage == "STAGE4":
        _diag_set(diag, REJECT_BEAR_MARKET_STAGE4, {"stage": v4_stage})
        return None

    # 시그널 탐지 — v4 detector 우선, legacy 로직 그대로 위임
    d_breakout: Optional[Dict[str, Any]] = {} if diag is not None else None
    d_cont:     Optional[Dict[str, Any]] = {} if diag is not None else None
    d_rebound:  Optional[Dict[str, Any]] = {} if diag is not None else None
    sig = (
        detect_stage2_breakout(df, weekly_ind, daily_ind, diag=d_breakout, market=market)
        or detect_continuation_breakout(df, weekly_ind, daily_ind, diag=d_cont)
        or detect_rebound_entry(df, weekly_ind, daily_ind, diag=d_rebound)
    )
    if diag is not None:
        # `or` 단축평가 때문에 앞선 탐지기가 성공하면 뒤 dict 는 비어 있다.
        diag["detectors"] = {"BREAKOUT":    d_breakout,
                             "RE_BREAKOUT": d_cont,
                             "REBOUND":     d_rebound}
    if sig is None:
        if diag is not None:
            for d in (d_breakout, d_cont, d_rebound):
                r = (d or {}).get("reject")
                if r is not None:
                    _diag_set(diag, r, (d or {}).get("values"))
            if diag.get("reject") is None:
                _diag_set(diag, REJECT_NO_SIGNAL, {})
        return None

    _diag_ok(diag)

    cur_p    = daily_ind["cur_p"]
    cur_m150 = daily_ind["cur_m150"]
    cur_v    = daily_ind["cur_v"]
    cur_va   = daily_ind["cur_va"]
    slope    = daily_ind["slope150"]

    pct = (cur_p - cur_m150) / cur_m150 * 100 if cur_m150 else 0.0

    # ── signal_date 시점까지의 데이터 슬라이스 (Phase 2 — no-look-ahead) ──
    # detect_* 는 SCAN_LOOKBACK_DAYS 안의 *과거* bar 에서 신호를 잡을 수 있어
    # df.index[-1] 가 아닌 sig["signal_date"] 가 진짜 신호 시점이다.
    # signal 은 시점 스냅샷이므로, RS / stop_loss / signal_quality / warning
    # 모두 *signal 발생 시점* 의 시리즈로 산출해야 한다 (CLAUDE.md "Stage 2
    # candidates ... no look-ahead pivot"). DB 에 기록되는 rs_value/rs_trend
    # 도 이 신호의 RS 스냅샷이지, 스캔 직전 마지막 bar 의 RS 가 아니다.
    df_at_signal     = df.loc[: sig["signal_date"]]
    daily_at_signal  = daily_ind
    weekly_at_signal = weekly_ind
    if len(df_at_signal) >= MA_PERIOD:
        d_signal = _build_indicators(df_at_signal)
        if d_signal is not None:
            daily_at_signal = d_signal
        w_signal_df = to_weekly_ohlcv(df_at_signal)
        if len(w_signal_df) > 0:
            # no-look-ahead — signal 시점까지 슬라이스한 일봉으로 basis 판정
            w_signal = compute_weekly_indicators(w_signal_df, df_at_signal)
            if w_signal is not None:
                weekly_at_signal = w_signal

    # signal 시점 close — stop_loss sanity 비교 (stop < price) 가 일관되도록.
    sig_close = float(df_at_signal["Close"].iloc[-1]) if len(df_at_signal) else cur_p

    # ── Phase 3 follow-up — strict gate 전용 signal-date 스냅샷 필드 ──
    # 공개 필드(price/ma150/volume/stage/sma30w 등) 는 _save() 가 "최신 가격"
    # 으로 사용하고 알림/UI 가 "현재가" 로 표시하므로 *항상 last-bar* 기준을
    # 유지해야 한다. strict_filter 는 별도 strict_* 필드(아래) 만 읽도록 분리
    # 해 no-look-ahead invariant 와 공개 필드 의미를 동시에 보존.
    # daily_at_signal 이 fallback 으로 daily_ind 와 동일한 경우에도 동일 값
    # 으로 전파되므로 안전.
    strict_price          = round(daily_at_signal["cur_p"],    4)
    strict_ma150          = round(daily_at_signal["cur_m150"], 4)
    strict_ma50           = round(daily_at_signal["cur_m50"],  4)
    strict_weekly_stage   = classify_stage(weekly_at_signal, daily_at_signal)
    strict_sma30w: Optional[float] = None
    strict_slope30w: Optional[float] = None
    strict_weekly_volume_ratio: Optional[float] = None
    if weekly_at_signal is not None:
        strict_sma30w              = round(weekly_at_signal["cur_sma30w"], 4)
        strict_slope30w            = round(weekly_at_signal["slope30w"],   6)
        wvr_sig                    = weekly_at_signal.get("weekly_volume_ratio")
        strict_weekly_volume_ratio = float(wvr_sig) if wvr_sig is not None else None

    # ── Mansfield RS (v4) + legacy ratio RS — signal 시점까지의 시리즈로 산출 ──
    rs_value, rs_trend = (None, None)
    rs_legacy = None
    rs_zero_crossed: Optional[bool] = None
    if benchmark_close is not None:
        bench_at_signal = benchmark_close.loc[: sig["signal_date"]]
        rs_value, rs_trend = compute_relative_performance(
            daily_at_signal["close"], bench_at_signal, lookback_weeks=RS_LOOKBACK_WEEKS
        )
        rs_legacy = calc_rs(daily_at_signal["close"], bench_at_signal)
        # Strict Gate 6 — RS 0선 음→양 zero-cross
        rs_zero_crossed = detect_rs_zero_cross(
            daily_at_signal["close"], bench_at_signal
        )

    if diag is not None:
        # 시그널은 생성됐으므로 reject 는 None 유지. RS 경고는 참고용 플래그로
        # 만 남기고, 실제 거부/집계는 scan_engine 의 strict_filter 결과가 담당.
        rs_flags: List[str] = []
        if rs_value is not None and rs_value < 0:
            rs_flags.append(REJECT_RS_BELOW_ZERO)
        if rs_trend == "FALLING":
            rs_flags.append(REJECT_RS_FALLING)
        if sig["signal_type"] == "BREAKOUT" and rs_zero_crossed is False:
            rs_flags.append(REJECT_RS_NO_ZERO_CROSS)
        diag["rs_flags"] = rs_flags

    # ── Phase 2 — Strict Gate 8 손절가 계산 (signal 시점 indicator 사용) ──
    stop_loss = compute_stop_loss(
        {
            "signal_type": sig["signal_type"],
            "price":       sig_close,
            "pivot_price": sig.get("pivot_price"),
            "base_low":    sig.get("base_low"),
            "stop_ref":    sig.get("stop_ref"),   # Step 2 — v2 BREAKOUT 1순위 (v1 은 키 자체가 없어 무영향)
        },
        daily_ind=daily_at_signal,
        weekly_ind=weekly_at_signal,
    )

    # signal_quality 는 Mansfield RS (rs_value/rs_trend) 기준
    qual = _signal_quality(sig["vol_ratio"], slope, rs_value, rs_trend, sig["signal_type"])

    # warning_flags 축적
    warning_flags: List[str] = list(sig.get("warning_flags") or [])
    if rs_value is not None and rs_value < 0:
        warning_flags.append(f"Mansfield RS < 0 ({rs_value:+.1f})")
    if rs_trend == "FALLING":
        warning_flags.append("RS 하락 추세")

    result = {
        "ticker":          ticker,
        "name":            name,
        "market":          market,
        "signal_type":     sig["signal_type"],
        "stage":           daily_ind["stage"],       # legacy — 일봉 기준 (last-bar, 공개 표시용)
        "weekly_stage":    v4_stage,                 # v4 — 주봉 기준 (last-bar, 공개 표시용)
        "price":           round(cur_p, 4),
        "ma150":           round(cur_m150, 4),
        "ma50":            round(daily_ind["cur_m50"], 4),
        "price_vs_ma_pct": round(pct, 2),
        "ma_slope":        round(slope, 4),
        "volume":          int(cur_v),
        "volume_avg":      int(cur_va),
        "volume_ratio":    sig["vol_ratio"],
        "signal_date":     sig["signal_date"],
        "rs":              rs_legacy,                # legacy ratio RS
        "rs_value":        rs_value,                 # Mansfield RS
        "rs_trend":        rs_trend,
        "pivot_price":     sig.get("pivot_price"),
        "support_level":   sig.get("support_level"),
        "base_quality":    sig.get("base_quality", "N/A"),
        # Phase 3 — Strict Gate 4 (Base) 입력. sig dict 의 signal-time 값을 그대로 노출.
        # base_low / base_weeks / base_quality_v4 는 BREAKOUT 만, v4_gate 는 REBOUND 만 가짐.
        "base_low":        sig.get("base_low"),
        "base_weeks":      sig.get("base_weeks"),
        "base_quality_v4": sig.get("base_quality_v4"),
        "v4_gate":         sig.get("v4_gate"),
        # Step 2 — 2단 base 구조(v2) 전용 필드. v1 시그널에는 base_mode="v1"
        # 만 채워지고 나머지 셋은 None (detect_base_pivot 은 이 개념이 없음).
        "base_width_pct":    sig.get("base_width_pct"),
        "tight_width_pct":   sig.get("tight_width_pct"),
        "contraction_ratio": sig.get("contraction_ratio"),
        "stop_ref":          sig.get("stop_ref"),
        "base_mode":         sig.get("base_mode"),
        # 차트는 이 signal-date 판정 스냅샷만 사용하며 구간을 재계산하지 않는다.
        "base_start_date":   sig.get("base_start_date"),
        "base_end_date":     sig.get("base_end_date"),
        "tight_start_date":  sig.get("tight_start_date"),
        "base_high":         sig.get("base_high"),
        "base_range_low":    sig.get("base_range_low"),
        "tight_high":        sig.get("tight_high"),
        "tight_low":         sig.get("tight_low"),
        "market_condition": market_condition,
        "signal_quality":  qual,
        "rs_passed":       (rs_value is not None and rs_value >= 0.0),
        "warning_flags":   warning_flags,
        # ── Strict Weinstein filter ──
        # stop_loss            : Phase 2 — compute_stop_loss() 로 계산 (price 미만 후보 없으면 None)
        # rs_zero_crossed      : Phase 2 — detect_rs_zero_cross() (벤치마크 없으면 None)
        # strict_filter_passed : Phase 4 — scan_engine 에서 apply_strict_filter() 결과로 채움
        # filter_reasons       : Phase 4 — 거부 사유 enum 문자열 리스트
        "stop_loss":            stop_loss,
        "rs_zero_crossed":      rs_zero_crossed,
        "strict_filter_passed": None,
        "filter_reasons":       [],
        # ── Strict-only signal-date 스냅샷 (Phase 3 follow-up) ──
        # apply_strict_filter / scan_engine 에서 strict gate 평가용으로만 소비.
        # 공개 필드 price/ma150/sma30w 등은 last-bar 의미를 유지하므로 stale
        # 신호도 알림/UI/DB persistence 가 정상 "현재가" 를 보여준다.
        # detect_* 가 며칠 전 signal_date 를 반환할 때 strict_* 와 공개 필드의
        # 값이 의도적으로 어긋나는 것이 invariant.
        "strict_price":               strict_price,
        "strict_ma150":               strict_ma150,
        "strict_ma50":                strict_ma50,
        "strict_weekly_stage":        strict_weekly_stage,
        "strict_sma30w":              strict_sma30w,
        "strict_slope30w":            strict_slope30w,
        "strict_weekly_volume_ratio": strict_weekly_volume_ratio,
    }
    if weekly_ind is not None:
        # 공개 필드 — last-bar 기준 (display/persist 의미 보존)
        result["sma30w"] = round(weekly_ind["cur_sma30w"], 4)
        result["sma10w"] = round(weekly_ind["cur_sma10w"], 4)
        result["weekly_volume_ratio"] = weekly_ind.get("weekly_volume_ratio")
        result["slope30w"] = round(weekly_ind["slope30w"], 6)
    # Step 4는 이미 생성된 신호를 관측만 한다. base/pivot 탐지 입력이나
    # strict_* signal-date 스냅샷에는 손대지 않는다.
    from scanner.entry_control import annotate_signal_entry
    annotate_signal_entry(result, df)
    return result


def _weekly_breakdown(weekly_df: Optional[pd.DataFrame]) -> bool:
    """현재 주봉 종가가 30주 SMA 아래로 이탈했는지 (true weekly path)."""
    if weekly_df is None or len(weekly_df) < WEEKLY_MA_LONG:
        return False
    ind = compute_weekly_indicators(weekly_df)
    if ind is None:
        return False
    return ind["cur_close_w"] < ind["cur_sma30w"]


def _weekly_slope_reversal(weekly_df: Optional[pd.DataFrame]) -> bool:
    """주봉 30-SMA 기울기가 양→음으로 반전했는지 (현재 ≤ 0, 5주 전 > 0)."""
    if weekly_df is None or len(weekly_df) < WEEKLY_MA_LONG + 5:
        return False
    sma30 = (weekly_df["Close"]
             .rolling(WEEKLY_MA_LONG, min_periods=WEEKLY_MA_LONG // 2)
             .mean()
             .dropna())
    if len(sma30) < MA_SLOPE_PERIOD + 5:
        return False
    cur_slope  = _slope(sma30,           n=MA_SLOPE_PERIOD)
    past_slope = _slope(sma30.iloc[:-5], n=MA_SLOPE_PERIOD)
    return past_slope > 0 and cur_slope <= 0


def _rs_deteriorating(close: pd.Series,
                      benchmark_close: Optional[pd.Series]) -> bool:
    """Mansfield RS < 0 AND 추세 == FALLING."""
    if benchmark_close is None:
        return False
    rs_value, rs_trend = compute_relative_performance(close, benchmark_close)
    if rs_value is None:
        return False
    return rs_value < 0 and rs_trend == "FALLING"


def check_sell_signal(df: pd.DataFrame, ticker: str, name: str, market: str,
                      buy_price: float = None, stop_loss: float = None,
                      weekly_df: Optional[pd.DataFrame] = None,
                      benchmark_close: Optional[pd.Series] = None) -> Optional[dict]:
    """감시 종목 매도 시그널 체크 (severity: HIGH / MEDIUM / LOW).

    옵션 인자 weekly_df / benchmark_close 가 제공되면 30주 SMA 붕괴/슬로프
    반전/Mansfield RS 악화 분기를 추가로 평가한다. 인자가 None 이면 기존 결과를
    그대로 유지하므로 Phase 1 단독 머지 시 호출부 회귀가 없다.
    """
    if df is None or len(df) < MA_PERIOD + 20:
        return None

    df    = df.copy().sort_index()
    close = df["Close"]
    ma    = close.rolling(MA_PERIOD, min_periods=MA_PERIOD // 2).mean()

    cur_p  = float(close.iloc[-1])
    cur_ma = float(ma.iloc[-1])
    slope  = _slope(ma)
    stage  = stage_of(cur_p, cur_ma, slope)

    reason   = None
    severity = None

    if stop_loss and cur_p <= stop_loss:
        reason   = f"손절가 도달 (현재 {cur_p:,.0f} ≤ 손절 {stop_loss:,.0f})"
        severity = "HIGH"

    elif _weekly_breakdown(weekly_df):
        reason   = "주봉 30-SMA 하향 이탈"
        severity = "HIGH"

    elif stage == "STAGE4":
        for i in range(1, 4):
            pp = float(close.iloc[-i - 1])
            pm = float(ma.iloc[-i - 1]) if not pd.isna(ma.iloc[-i - 1]) else None
            if pm and pp > pm and cur_p < cur_ma:
                reason   = "MA150 하향 이탈 (Stage4 진입)"
                severity = "HIGH"
                break

    if reason is None and len(ma.dropna()) >= 6:
        slope_past = _slope(ma.iloc[:-5], n=MA_SLOPE_PERIOD)
        if slope_past > 0 and slope <= 0:
            reason   = "MA150 기울기 반전 (상승 추세 약화)"
            severity = "MEDIUM"

    if reason is None and _weekly_slope_reversal(weekly_df):
        reason   = "주봉 30-SMA 기울기 반전"
        severity = "MEDIUM"

    if reason is None and _rs_deteriorating(close, benchmark_close):
        reason   = "상대강도(Mansfield RS) 악화"
        severity = "MEDIUM"

    if reason is None and stage == "STAGE3":
        reason   = "Stage3 진입 징후 (고점 부근, 분배 주의)"
        severity = "LOW"

    if reason is None:
        return None

    return {
        "ticker":      ticker,
        "name":        name,
        "market":      market,
        "signal_type": "SELL",
        "stage":       stage,
        "price":       round(cur_p, 4),
        "ma150":       round(cur_ma, 4),
        "ma_slope":    round(slope, 4),
        "sell_reason": reason,
        "severity":    severity,
        "buy_price":   buy_price,
        "profit_pct":  round((cur_p - buy_price) / buy_price * 100, 2) if buy_price else None,
    }
