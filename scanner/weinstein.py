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


def compute_weekly_indicators(weekly_df: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """주봉 기반 지표 (30-SMA, 10-SMA, slope)."""
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
        "weekly_volume_ratio": round(cur_vol / cur_volavg, 2) if cur_volavg > 0 else 0.0,
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

        BREAKOUT     base_low * 0.99  →  pivot_price * 0.97  →  cur_sma30w * 0.97
        RE_BREAKOUT  swing_low(30d) * 0.99  →  cur_m50 * 0.97
        REBOUND      cur_sma30w * 0.97  →  cur_m50 * 0.97

    모든 후보가 None 또는 >= price 면 None 반환 → strict 필터에서
    `stop_loss_missing` / `stop_loss_above_price` 거부 사유 트리거.

    Args:
        signal:     analyze_stock 또는 detect_* 가 반환한 시그널 dict.
                    필요 키: signal_type, price, pivot_price, base_low.
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
                      min_weeks: int = BASE_MIN_WEEKS) -> Optional[Dict[str, Any]]:
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
        return None

    close = df["Close"]
    high  = df["High"]
    low   = df["Low"]
    n     = len(df)

    lookback_days = lookback_weeks * 5
    start = max(0, n - lookback_days - 2)
    end   = n - 1  # 현재 bar 는 돌파 후보 (base 에서 제외)

    best = None
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
            break

        if base_weeks >= min_weeks:
            if width_pct <= 8.0:   quality = "TIGHT"
            elif width_pct <= 15.0: quality = "LOOSE"
            else:                   quality = "WIDE"
            best = {
                "pivot_price":     round(pivot_price, 4),
                "base_low":        round(base_low, 4),
                "base_start_idx":  j,
                "base_end_idx":    end,
                "base_weeks":      round(base_weeks, 1),
                "base_width_pct":  round(width_pct, 2),
                "base_quality":    quality,
            }
    return best


def _daily_vol_ratio(df: pd.DataFrame, idx: int) -> float:
    vol = df["Volume"]
    avg = vol.rolling(VOLUME_AVG_PERIOD, min_periods=10).mean()
    v = float(vol.iloc[idx])
    a = float(avg.iloc[idx]) if not pd.isna(avg.iloc[idx]) else 0.0
    return v / a if a > 0 else 0.0


def detect_stage2_breakout(df: pd.DataFrame,
                           weekly_ind: Optional[Dict],
                           daily_ind: Optional[Dict]) -> Optional[Dict[str, Any]]:
    """Stage1→Stage2 base pivot 상향 돌파 감지 (v4).

    조건:
      - 주봉 데이터 존재 (weekly_ind is not None)
      - Stage == STAGE1 or STAGE2 (30w SMA 위 + 상승)
      - detect_base_pivot 으로 5주+ 이상 tight/loose base(폭 ≤15%) 확인
      - 최근 SCAN_LOOKBACK_DAYS 내 pivot 상향 돌파
      - 일봉 거래량 ≥ BREAKOUT_DAILY_VOL_RATIO (hard block)
      - 주봉 거래량 ≥ BREAKOUT_WEEKLY_VOL_RATIO (hard block)
      - MA150 대비 과매수 < BREAKOUT_MAX_EXTENDED_PCT
    """
    if daily_ind is None or weekly_ind is None:
        return None
    stage = classify_stage(weekly_ind, daily_ind)
    if stage not in ("STAGE1", "STAGE2"):
        return None

    wvr = float(weekly_ind.get("weekly_volume_ratio", 0.0) or 0.0)
    if wvr < BREAKOUT_WEEKLY_VOL_RATIO:
        return None

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
        base = detect_base_pivot(df_pre,
                                 lookback_weeks=PIVOT_LOOKBACK_WEEKS,
                                 min_weeks=BASE_MIN_WEEKS)
        if base is None or base["base_quality"] == "WIDE":
            continue

        pivot_price = float(base["pivot_price"])
        if not (pp <= pivot_price < cp):
            continue

        cm150_raw = ma150.iloc[abs_i]
        if pd.isna(cm150_raw) or float(cm150_raw) <= 0:
            continue
        cm150 = float(cm150_raw)
        ext_pct = (cp - cm150) / cm150 * 100
        if ext_pct > BREAKOUT_MAX_EXTENDED_PCT:
            continue

        dvr = _daily_vol_ratio(df, abs_i)
        if dvr < BREAKOUT_DAILY_VOL_RATIO:
            continue

        cm50_raw = ma50.iloc[abs_i]
        cm50 = float(cm50_raw) if not pd.isna(cm50_raw) else float(daily_ind["cur_m50"])

        # legacy STRONG/WEAK 매핑 (scan_engine._grade 호환)
        legacy_quality = "STRONG" if base["base_quality"] == "TIGHT" else "WEAK"

        warning_flags: List[str] = []
        if stage == "STAGE1":
            warning_flags.append("STAGE1 → 2 전환 (조기 진입)")

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
            "warning_flags":   warning_flags,
            "stage_v4":        stage,
        }

    return None


def detect_continuation_breakout(df: pd.DataFrame,
                                 weekly_ind: Optional[Dict],
                                 daily_ind: Optional[Dict]) -> Optional[Dict[str, Any]]:
    """Stage2 진행 중 continuation base 돌파 감지 (v4)."""
    if daily_ind is None:
        return None
    stage = classify_stage(weekly_ind, daily_ind)
    if stage != "STAGE2":
        return None
    sig = _find_rebreakout_signal(daily_ind)
    if sig is None:
        return None

    warning_flags: List[str] = []
    if weekly_ind and weekly_ind.get("weekly_volume_ratio", 0) < 1.5:
        warning_flags.append("주봉 거래량 감소")
    sig["warning_flags"] = warning_flags
    sig["stage_v4"]      = stage
    return sig


def detect_rebound_entry(df: pd.DataFrame,
                         weekly_ind: Optional[Dict],
                         daily_ind: Optional[Dict]) -> Optional[Dict[str, Any]]:
    """Stage2 MA50 눌림목 반등 감지 (시간순, v4).

    Strategy invariants:
      - 주봉 STAGE2 필수 (weekly_ind 없거나 STAGE2 아니면 거부).
      - REBOUND_REQUIRE_BASE_RETEST=True 일 때:
          (a) 직전 v4 base pivot 위에서의 MA50 눌림 OR
          (b) 주봉 30-SMA 터치 + 회복 — 둘 중 하나 미충족 시 거부.
    """
    if daily_ind is None:
        return None
    if weekly_ind is None:
        return None  # 주봉 데이터 없는 종목은 REBOUND 판정 금지
    stage = classify_stage(weekly_ind, daily_ind)
    if stage != "STAGE2":
        return None  # 일봉 fallback 제거 — 주봉 STAGE2 필수

    sig = _find_rebound_signal_v4(df, daily_ind, weekly_ind)
    if sig is None:
        return None

    warning_flags: List[str] = []
    if weekly_ind.get("slope30w", 0) <= _FLAT_SLOPE:
        warning_flags.append("주봉 30-SMA 기울기 둔화")
    sig["warning_flags"] = warning_flags
    sig["stage_v4"]      = stage
    return sig


def _find_rebound_signal_v4(df: pd.DataFrame,
                            daily_ind: Dict,
                            weekly_ind: Dict) -> Optional[Dict]:
    """v4 REBOUND: legacy MA50 touch+rebound + base/30w 재테스트 게이트.

    1. legacy `_find_rebound_signal` 으로 후보 시그널 추출.
    2. REBOUND_REQUIRE_BASE_RETEST=False → 그대로 통과.
    3. True → 다음 두 조건 중 하나 이상 만족해야 통과:
       (a) 직전 base pivot 위에서의 MA50 눌림: 터치 직전 일봉 종가 ≥ pivot_price
       (b) 주봉 30-SMA 재테스트: 터치 일봉 종가가 cur_sma30w ±REBOUND_TOUCH_PCT
           이내 + 시그널 시점 종가가 cur_sma30w 위로 회복.
    """
    legacy_sig = _find_rebound_signal(daily_ind)
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
        return None
    if isinstance(sig_pos_raw, slice):
        sig_pos = sig_pos_raw.start
    else:
        sig_pos = int(sig_pos_raw)
    if sig_pos <= 0:
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


def _find_rebreakout_signal(ind: Dict) -> Optional[Dict]:
    """Stage2 연속 돌파(재돌파) 감지 (legacy)."""
    if ind["stage"] != "STAGE2":
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
            continue

        if cp <= cm:
            continue
        if cp <= cm50:
            continue
        if _slope(ma150.iloc[:abs_i + 1]) <= 0:
            continue

        base_start = abs_i - REBREAKOUT_BASE_LOOKBACK_DAYS
        base_end   = abs_i
        if base_start < 0:
            continue
        base_slice = close.iloc[base_start:base_end]
        if len(base_slice) < 5:
            continue

        pivot_high = float(base_slice.max())
        pivot_low  = float(base_slice.min())

        pullback_pct = (pivot_high - pivot_low) / pivot_high * 100
        if pullback_pct < 3.0:
            continue
        if pullback_pct > REBREAKOUT_MAX_PULLBACK_PCT:
            continue

        pp = float(close.iloc[abs_i - 1]) if abs_i > 0 else cp
        if pp > pivot_high:
            continue
        if cp <= pivot_high:
            continue

        dv  = float(vol.iloc[abs_i])
        dva = float(vol_avg.iloc[abs_i])
        dvr = dv / dva if dva > 0 else 0.0
        if dvr < REBREAKOUT_VOLUME_RATIO:
            continue

        if REBREAKOUT_REQUIRE_VOLUME_DRYUP:
            base_vol     = vol.iloc[base_start:base_end]
            base_vol_avg = vol_avg.iloc[base_start:base_end]
            valid_mask   = base_vol_avg > 0
            if valid_mask.any():
                avg_ratio = float((base_vol[valid_mask] / base_vol_avg[valid_mask]).mean())
                if avg_ratio > 0.8:
                    continue

        return {
            "signal_type":  "RE_BREAKOUT",
            "signal_date":  str(close.index[abs_i].date()),
            "vol_ratio":    round(dvr, 2),
            "pivot_price":  round(pivot_high, 4),
            "support_level": round(cm50, 4),
        }

    return None


def _find_rebound_signal(ind: Dict) -> Optional[Dict]:
    """MA50 눌림목 반등 감지 (시간순, legacy)."""
    if ind["stage"] not in ("STAGE2", "STAGE3"):
        return None
    if ind["slope150"] <= 0.02:
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
                    touched_low = None
                    touch_ma50  = None
                    continue

                days_ago = (n - 1) - j
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
                  market_condition: str = None) -> Optional[dict]:
    """주식 하나에 대해 Weinstein 매수 시그널을 탐지 (v4 강화).

    반환 dict:
      기존 필드: ticker, name, market, signal_type, stage, price, ma150, ma50,
                price_vs_ma_pct, ma_slope, volume, volume_avg, volume_ratio,
                signal_date, rs, pivot_price, support_level, base_quality,
                market_condition, signal_quality, rs_passed
      v4 신규: sma30w, sma10w, weekly_stage, rs_value (Mansfield),
              rs_trend, weekly_volume_ratio, base_weeks, warning_flags
    """
    if df is None or len(df) < MA_PERIOD + BREAKOUT_BASE_LOOKBACK_DAYS + 10:
        return None

    df = df.copy().sort_index()

    # ── v4: 주봉 + 일봉 indicator 병렬 계산 ──
    daily_ind  = _build_indicators(df)
    if daily_ind is None:
        return None

    weekly_df  = to_weekly_ohlcv(df)
    weekly_ind = compute_weekly_indicators(weekly_df) if len(weekly_df) > 0 else None
    v4_stage   = classify_stage(weekly_ind, daily_ind)

    # ── v4: BEAR 장세에서 Stage4 는 1차 차단 (scan_engine 필터와 2중) ──
    if market_condition == "BEAR" and v4_stage == "STAGE4":
        return None

    # 시그널 탐지 — v4 detector 우선, legacy 로직 그대로 위임
    sig = (
        detect_stage2_breakout(df, weekly_ind, daily_ind)
        or detect_continuation_breakout(df, weekly_ind, daily_ind)
        or detect_rebound_entry(df, weekly_ind, daily_ind)
    )
    if sig is None:
        return None

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
            w_signal = compute_weekly_indicators(w_signal_df)
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

    # ── Phase 2 — Strict Gate 8 손절가 계산 (signal 시점 indicator 사용) ──
    stop_loss = compute_stop_loss(
        {
            "signal_type": sig["signal_type"],
            "price":       sig_close,
            "pivot_price": sig.get("pivot_price"),
            "base_low":    sig.get("base_low"),
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
