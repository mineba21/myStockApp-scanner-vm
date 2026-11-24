"""매도 판정용 지표 (모두 '해당 봉 종가 확정 시점'에 알 수 있는 값만).

일봉 지표의 t 행 값은 t 종가까지의 정보로 계산된다. 이를 쓰는 종가 기반 신호는
반드시 t+1 시가에 체결한다 (execution_simulator 가 강제).

주봉 지표는 '완료된 주'만 사용한다. 주의 마지막 거래일 종가가 확정된 뒤에만 그 주
값이 일봉 행에 반영되고, 주 중간 일봉에는 직전 완료 주 값이 들어간다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

SLOPE_N = 10          # 운영 MA_SLOPE_PERIOD 기본값
FLAT_BAND = 0.02      # 운영 stage_of 의 rising/falling 임계값


def _rolling_slope_pct(series: pd.Series, n: int = SLOPE_N) -> pd.Series:
    """운영 weinstein._slope 와 같은 정의(최근 n개 선형회귀 기울기 / 현재값 × 100)."""
    vals = series.to_numpy(dtype=float)
    out = np.full(len(vals), np.nan)
    x = np.arange(n, dtype=float)
    xm = x.mean()
    denom = ((x - xm) ** 2).sum()
    for i in range(n - 1, len(vals)):
        w = vals[i - n + 1:i + 1]
        if np.isnan(w).any() or w[-1] == 0:
            continue
        k = ((x - xm) * (w - w.mean())).sum() / denom
        out[i] = k / w[-1] * 100
    return pd.Series(out, index=series.index)


def _stage(price, ma, slope):
    if np.isnan(ma) or np.isnan(slope):
        return None
    up = price > ma
    if up and slope > FLAT_BAND:
        return "STAGE2"
    if up:
        return "STAGE3"
    if slope < -FLAT_BAND:
        return "STAGE4"
    return "STAGE1"


def tradable_mask(df: pd.DataFrame) -> pd.Series:
    return (df["Open"] > 0) & (df["High"] > 0) & (df["Low"] > 0) & (df["Volume"] > 0)


def compute_indicators(df: pd.DataFrame, bench_close: pd.Series | None = None) -> pd.DataFrame:
    df = df.sort_index().copy()
    ok = tradable_mask(df)
    # 거래정지 봉(가격 0)은 지표 계산에서 제외하고 직전 값으로 유지한다.
    c = df["Close"].where(ok)
    h = df["High"].where(ok)
    lo = df["Low"].where(ok)
    v = df["Volume"].where(ok)
    out = pd.DataFrame(index=df.index)
    out["tradable"] = ok
    for n in (10, 21, 50, 150, 200):
        out[f"ma{n}"] = c.rolling(n, min_periods=n).mean().ffill()
    prev_c = c.shift(1)
    tr = pd.concat([h - lo, (h - prev_c).abs(), (lo - prev_c).abs()], axis=1).max(axis=1)
    out["atr14"] = tr.rolling(14, min_periods=14).mean().ffill()
    # 직전 20거래일 평균 (당일 제외) — '직전 20거래일 평균 대비 거래량' 정의
    out["vol_ma20_prev"] = v.shift(1).rolling(20, min_periods=20).mean().ffill()

    # RS: 종목/벤치마크 비율과 50일 평균. 약화 = 비율이 50일 평균 아래 & 10일 전보다 낮음
    if bench_close is not None:
        b = bench_close.reindex(df.index).ffill()
        ratio = (c / b).ffill()
        out["rs_ratio"] = ratio
        out["rs_ma50"] = ratio.rolling(50, min_periods=50).mean()
        out["rs_weak"] = (ratio < out["rs_ma50"]) & (ratio < ratio.shift(10))
    else:
        out["rs_ratio"] = np.nan
        out["rs_ma50"] = np.nan
        out["rs_weak"] = False

    # 주봉 (완료 주만)
    wk_key = df.index.to_period("W-FRI")
    daily = pd.DataFrame({"c": c, "wk": wk_key}, index=df.index)
    last_day = daily.groupby("wk").apply(lambda g: g.index.max())
    wclose = daily.groupby("wk")["c"].last()
    ma30w = wclose.rolling(30, min_periods=30).mean()
    slope = _rolling_slope_pct(ma30w)
    stage = pd.Series([_stage(p, m, s) for p, m, s in zip(wclose, ma30w, slope)], index=wclose.index)
    weekly = pd.DataFrame({"ma30w": ma30w, "ma30w_slope": slope, "stage_w": stage,
                           "complete_on": last_day})
    # 데이터의 마지막 주는 금요일 봉이 없으면 아직 진행 중인 주로 보고 버린다.
    if len(weekly) and df.index.max().dayofweek < 4:
        weekly = weekly.iloc[:-1]
    w_vals = weekly.set_index("complete_on")
    for col in ("ma30w", "ma30w_slope", "stage_w"):
        out[col] = w_vals[col].reindex(df.index).ffill()
    out["week_complete"] = out.index.isin(w_vals.index)
    return out
