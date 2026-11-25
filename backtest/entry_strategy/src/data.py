"""P0 — 캐시 적재와 결정 시점 특징 계산.

- 신호: exit_strategy replay 결과(RECONSTRUCTED). Strict 통과 행(=최초 Strict 통과 스냅샷)만 매수 후보.
  같은 signal_key 의 최초 탐지 스냅샷(first_seen=True, 미통과)은 비교용으로만 남긴다.
- 모든 특징은 탐지일(detect_date) 종가까지의 일봉으로 계산한다. 이후 봉은 읽지 않는다.
- 운영 스캐너 모듈은 exit_strategy 의 격리 함수(임시 SQLite, 주문·알림 차단)를 거친 뒤에만 import 한다.
"""
from __future__ import annotations

import json
import math
import pickle
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pandas as pd
import yaml

PKG_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PKG_DIR.parents[1]
OUTPUT_DIR = PKG_DIR / "output"
CACHE_DIR = PKG_DIR / "data"
DEFAULT_CONFIG = PKG_DIR / "configs" / "entry_backtest.yaml"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

WARN_PATTERNS = {
    "w_hold": "판정 보류",
    "w_weekly_vol": "주봉 거래량 미달",
    "w_stop_wide": "손절폭 과대",
    "w_chase": "추격",
    "w_below_pivot": "피벗 하회",
    "w_upthrust_fail": "돌파 실패 —",
}


def load_config(path=None) -> dict:
    with open(path or DEFAULT_CONFIG, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def isolate():
    from backtest.exit_strategy.src.common import isolate_scanner_imports
    isolate_scanner_imports()


@dataclass
class Market:
    frames: Dict[str, pd.DataFrame]
    calendar: pd.DatetimeIndex
    closes: Dict[str, np.ndarray]        # 캘린더 정렬 종가 (ffill, 매수 전 NaN)
    regime: pd.DataFrame                 # 캘린더별 시장 상태 (해당일 종가 확정 기준)


def _slope_pct(vals: np.ndarray, n: int = 10) -> np.ndarray:
    from backtest.exit_strategy.src.indicators import _rolling_slope_pct
    return _rolling_slope_pct(pd.Series(vals), n).to_numpy()


def load_market(cfg: dict) -> Market:
    with open(REPO_ROOT / cfg["data"]["ohlcv_pkl"], "rb") as fh:
        payload = pickle.load(fh)
    frames = {t: df.sort_index() for t, df in payload["frames"].items()}
    bench = cfg["data"]["benchmark"]
    cal = frames[bench].index
    closes = {}
    for t, df in frames.items():
        s = df["Close"].where(df["Close"] > 0).reindex(cal).ffill()
        closes[t] = s.to_numpy(dtype=float)
    regime = build_regime(frames, cal, bench)
    return Market(frames=frames, calendar=cal, closes=closes, regime=regime)


def build_regime(frames, cal, bench) -> pd.DataFrame:
    """M1 운영 시장 상태(BULL/NEUTRAL/CAUTION/BEAR), M2 SPY>MA50, M3 유니버스 MA150 위 비율."""
    isolate()
    from backtest.exit_strategy.src.signal_loader import market_condition_series
    cache = CACHE_DIR / "regime.pkl"
    if cache.exists():
        reg = pd.read_pickle(cache)
        if len(reg) == len(cal) and reg.index.equals(cal):
            return reg
    spy = frames[bench]["Close"].reindex(cal)
    reg = pd.DataFrame(index=cal)
    reg["spy_close"] = spy
    reg["spy_ma50"] = spy.rolling(50, min_periods=50).mean()
    reg["spy_above_ma50"] = reg["spy_close"] > reg["spy_ma50"]
    above, valid = np.zeros(len(cal)), np.zeros(len(cal))
    for t, df in frames.items():
        if t in ("SPY", "QQQ"):
            continue
        c = df["Close"].where(df["Close"] > 0).reindex(cal)
        ma = c.rolling(150, min_periods=150).mean()
        ok = ma.notna() & c.notna()
        above += ((c > ma) & ok).to_numpy()
        valid += ok.to_numpy()
    reg["breadth150"] = np.where(valid > 0, above / np.maximum(valid, 1), np.nan)
    start = cal[cal >= pd.Timestamp("2020-12-01")]
    cond = market_condition_series({s: frames[s] for s in ("SPY", "QQQ") if s in frames}, start)
    reg["market_condition"] = cond.reindex(cal)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    reg.to_pickle(cache)
    return reg


def _warn_flags(raw) -> dict:
    try:
        items = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except ValueError:
        items = []
    out = {k: False for k in WARN_PATTERNS}
    for w in items:
        for k, pat in WARN_PATTERNS.items():
            if pat in w:
                out[k] = True
    return out


def load_signals(cfg: dict) -> pd.DataFrame:
    sig = pd.read_pickle(REPO_ROOT / cfg["data"]["signals_pkl"])
    sig = sig[sig["market"] == cfg["data"]["market"]].copy()
    sig["detect_date"] = pd.to_datetime(sig["detect_date"])
    return sig.reset_index(drop=True)


def build_candidates(sig: pd.DataFrame, mkt: Market) -> pd.DataFrame:
    """Strict 통과 행 → 결정 시점(탐지일 종가) 특징. 미래 봉 미사용."""
    cand = sig[sig["strict_filter_passed"] == True].copy()       # noqa: E712
    assert cand["signal_key"].is_unique, "같은 signal_key 의 Strict 통과 행은 1개여야 한다"
    first_seen_map = sig[sig["first_seen"] == True].set_index("signal_key")["detect_date"]  # noqa: E712
    cand["first_detect_date"] = cand["signal_key"].map(first_seen_map)
    cal = mkt.calendar
    rows = []
    ind_cache: Dict[str, dict] = {}
    for r in cand.itertuples(index=False):
        df = mkt.frames.get(r.ticker)
        if df is None:
            continue
        if r.ticker not in ind_cache:
            c = df["Close"].where(df["Close"] > 0)
            h = df["High"].where(df["High"] > 0)
            lo = df["Low"].where(df["Low"] > 0)
            ma150 = c.rolling(150, min_periods=150).mean()
            prev = c.shift(1)
            tr = pd.concat([h - lo, (h - prev).abs(), (lo - prev).abs()], axis=1).max(axis=1)
            ind_cache[r.ticker] = {
                "close": c.to_numpy(), "ma150": ma150.to_numpy(),
                "slope150": _slope_pct(ma150.to_numpy()),
                "atr14": tr.rolling(14, min_periods=14).mean().to_numpy(),
                "hi252": h.rolling(252, min_periods=120).max().to_numpy(),
                "index": df.index,
            }
        ic = ind_cache[r.ticker]
        pos = ic["index"].searchsorted(r.detect_date, side="right") - 1   # 탐지일(또는 직전) 봉
        if pos < 0:
            continue
        close = float(ic["close"][pos])
        stop = float(r.stop_loss) if r.stop_loss else math.nan
        pivot = float(r.pivot_price) if r.pivot_price else math.nan
        flags = _warn_flags(r.entry_warnings)
        ci = int(cal.searchsorted(ic["index"][pos]))
        rows.append({
            "signal_key": r.signal_key, "ticker": r.ticker, "signal_type": r.signal_type,
            "signal_date": pd.Timestamp(r.signal_date), "detect_date": r.detect_date,
            "decision_ci": ci, "ticker_pos": pos,
            "first_seen": bool(r.first_seen), "first_detect_date": r.first_detect_date,
            "grade": r.grade, "signal_quality": r.signal_quality,
            "market_condition": r.market_condition, "base_strong": float(r.base_quality == "STRONG"),
            "rs_value": r.rs_value, "rs_trend": r.rs_trend,
            "rs_rising": float(r.rs_trend == "RISING"),
            "volume_ratio": r.volume_ratio, "weekly_volume_ratio": r.weekly_volume_ratio,
            "close": close, "stop": stop, "pivot": pivot,
            "ext_pct": (close / pivot - 1) * 100 if pivot == pivot and pivot > 0 else math.nan,
            "stop_pct": (1 - stop / close) * 100 if stop == stop and close > 0 else math.nan,
            "ma150_ext_pct": (close / ic["ma150"][pos] - 1) * 100 if ic["ma150"][pos] == ic["ma150"][pos] else math.nan,
            "slope150": ic["slope150"][pos],
            "atr_pct": ic["atr14"][pos] / close * 100 if close else math.nan,
            "dist_52w_high": (close / ic["hi252"][pos] - 1) * 100 if ic["hi252"][pos] == ic["hi252"][pos] else math.nan,
            **flags,
        })
    out = pd.DataFrame(rows).sort_values(["decision_ci", "ticker"]).reset_index(drop=True)
    out["cid"] = np.arange(len(out))
    return out


def chronological_cuts(cand: pd.DataFrame, cal: pd.DatetimeIndex, fracs=(0.6, 0.2, 0.2)):
    """탐지일 고유값 기준 60/20/20 경계 (캘린더 인덱스). exit_strategy.parameter_search 와 같은 규칙."""
    uniq = np.sort(cand["detect_date"].unique())
    n = len(uniq)
    c1 = pd.Timestamp(uniq[int(n * fracs[0])])
    c2 = pd.Timestamp(uniq[int(n * (fracs[0] + fracs[1]))])
    start = int(cal.searchsorted(pd.Timestamp(uniq[0])))
    return {
        "train": (start, int(cal.searchsorted(c1)) - 1),
        "validation": (int(cal.searchsorted(c1)), int(cal.searchsorted(c2)) - 1),
        "test": (int(cal.searchsorted(c2)), len(cal) - 1),
        "full": (start, len(cal) - 1),
    }
