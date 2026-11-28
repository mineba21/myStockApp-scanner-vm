"""후보 필터(pool)·시장 필터·순위 정의와 Auto Buy Score.

- 필터는 결정 시점 값만 쓴다 (T2 는 확인일 종가 기준 dec_ext/dec_stop 사용).
- 기존 Grade·Signal Quality 계산은 바꾸지 않고 저장된 값을 그대로 읽는다.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

BO = ("BREAKOUT", "RE_BREAKOUT")


def features_for_timing(cand: pd.DataFrame, orders: pd.DataFrame) -> pd.DataFrame:
    f = cand.copy()
    o = orders.reindex(cand["cid"])
    f["dec_ext_pct"] = o["dec_ext_pct"].to_numpy(float)
    f["dec_stop_pct"] = o["dec_stop_pct"].to_numpy(float)
    f["dec_ci"] = o["decision_ci"].to_numpy(int)
    return f


def f1_mask(f: pd.DataFrame, cfg: dict) -> np.ndarray:
    p = cfg["f1"]
    ext, stop = f["dec_ext_pct"], f["dec_stop_pct"]
    bo = f["signal_type"].isin(BO)
    bo_ok = bo & ext.notna() & (ext >= 0) & (ext <= p["max_ext_pct"]) & (stop <= p["max_stop_pct"]) \
        & ~f["w_upthrust_fail"]
    rb_ok = (f["signal_type"] == "REBOUND") & (stop <= p["max_stop_pct"]) \
        & (f["ma150_ext_pct"] <= p["rebound_max_ma150_ext_pct"])
    return (bo_ok | rb_ok).to_numpy()


def pool_masks(f: pd.DataFrame, cfg: dict) -> Dict[str, np.ndarray]:
    t, g, q = f["signal_type"], f["grade"], f["signal_quality"]
    ext, stop = f["dec_ext_pct"], f["dec_stop_pct"]
    m = {
        "F0": np.ones(len(f), bool),
        "F1": f1_mask(f, cfg),
        "GRADE_A": (g == "A").to_numpy(), "GRADE_B": (g == "B").to_numpy(), "GRADE_S": (g == "S").to_numpy(),
        "Q_STRONG": (q == "STRONG").to_numpy(), "Q_MODERATE": (q == "MODERATE").to_numpy(),
        "Q_WEAK": (q == "WEAK").to_numpy(), "Q_NOWEAK": (q != "WEAK").to_numpy(),
        "T_RE_BREAKOUT": (t == "RE_BREAKOUT").to_numpy(), "T_BREAKOUT": (t == "BREAKOUT").to_numpy(),
        "T_REBOUND": (t == "REBOUND").to_numpy(), "T_NO_BREAKOUT": (t != "BREAKOUT").to_numpy(),
    }
    for x in (7, 10, 12, 15):
        m[f"STOP_LE_{x}"] = (stop <= x).to_numpy()
    for x in (3, 5, 7):
        m[f"EXT_LE_{x}"] = (ext.isna() | (ext <= x)).to_numpy()
    for w in ("w_hold", "w_weekly_vol", "w_stop_wide", "w_chase", "w_below_pivot", "w_upthrust_fail"):
        m[f"NO_{w[2:].upper()}"] = (~f[w]).to_numpy()
    # 사후 탐색용 (2026-11-25 결과 확인 후 추가, 채택 근거로 쓰지 않음): 신호 단위에서 일관되게 나빴던 구간만 제외
    m["HARD_EXCL"] = (f["first_seen"] & (ext.isna() | (ext >= 0)) & (stop <= 15)).to_numpy()
    m["NO_CRITICAL_WARN"] = (~(f["w_stop_wide"] | f["w_chase"] | f["w_below_pivot"] | f["w_upthrust_fail"])).to_numpy()
    return m


def market_masks(f: pd.DataFrame, regime: pd.DataFrame) -> Dict[str, np.ndarray]:
    ci = f["dec_ci"].to_numpy()
    mc = regime["market_condition"].to_numpy()[ci]
    return {
        "M0": np.ones(len(f), bool),
        "M1": mc == "BULL",
        "M2": regime["spy_above_ma50"].to_numpy()[ci].astype(bool),
        "M3": np.nan_to_num(regime["breadth150"].to_numpy()[ci]) > 0.5,
    }


def grade_rank(f: pd.DataFrame) -> np.ndarray:
    """운영 portfolio 우선순위와 같은 Grade → Signal Quality (동률은 티커 순)."""
    g = f["grade"].map({"S": 0, "A": 1, "B": 2}).fillna(3).to_numpy()
    q = f["signal_quality"].map({"STRONG": 0, "MODERATE": 1, "WEAK": 2}).fillna(3).to_numpy()
    return g * 10 + q


# ── Auto Buy Score ────────────────────────────────────────────────

def _spearman(x: np.ndarray, y: np.ndarray) -> Tuple[float, float, int]:
    ok = np.isfinite(x) & np.isfinite(y)
    n = int(ok.sum())
    if n < 30:
        return math.nan, math.nan, n
    rx = pd.Series(x[ok]).rank().to_numpy()
    ry = pd.Series(y[ok]).rank().to_numpy()
    r = float(np.corrcoef(rx, ry)[0, 1])
    if not math.isfinite(r) or abs(r) >= 1:
        return r, 0.0, n
    t = r * math.sqrt((n - 2) / (1 - r * r))
    p = math.erfc(abs(t) / math.sqrt(2))          # 정규 근사 (n 큼)
    return r, p, n


def feature_column(f: pd.DataFrame, name: str) -> np.ndarray:
    if name == "ext_pct":
        return f["dec_ext_pct"].to_numpy(float)
    if name == "stop_pct":
        return f["dec_stop_pct"].to_numpy(float)
    return f[name].astype(float).to_numpy()


def fit_score(f_train: pd.DataFrame, r_train: np.ndarray, types: np.ndarray, cfg: dict) -> dict:
    """Train 거래만으로 요소 방향·포함 여부를 정한다. R 은 신호 유형별 평균을 뺀 값."""
    y = r_train.copy()
    for t in np.unique(types):
        m = types == t
        y[m] = y[m] - np.nanmean(y[m])
    rows = []
    for name in cfg["score"]["candidates"]:
        x = feature_column(f_train, name)
        r, p, n = _spearman(x, y)
        rows.append({"feature": name, "rho": r, "p": p, "n": n})
    tab = pd.DataFrame(rows)
    sel = tab[(tab["p"] < cfg["score"]["p_threshold"]) & tab["rho"].notna()]
    sc1 = {r.feature: float(np.sign(r.rho)) for r in sel.itertuples()}
    best = tab.dropna(subset=["rho"]).iloc[tab.dropna(subset=["rho"])["rho"].abs().argmax()] \
        if tab["rho"].notna().any() else None
    sc3 = {best.feature: float(np.sign(best.rho))} if best is not None else {}
    sc2 = {"rs_value": 1.0, "volume_ratio": 1.0, "ext_pct": -1.0, "stop_pct": -1.0}
    ref = {name: np.sort(feature_column(f_train, name)[np.isfinite(feature_column(f_train, name))])
           for name in cfg["score"]["candidates"]}
    return {"table": tab, "SC1": sc1, "SC2": sc2, "SC3": sc3, "ref": ref}


def score(f: pd.DataFrame, weights: Dict[str, float], ref: Dict[str, np.ndarray]) -> np.ndarray:
    if not weights:
        return np.zeros(len(f))
    parts = []
    for name, sgn in weights.items():
        x = feature_column(f, name)
        r = ref[name]
        pct = np.where(np.isfinite(x), np.searchsorted(r, x, side="right") / max(len(r), 1), 0.5)
        parts.append(pct if sgn > 0 else 1 - pct)
    return np.mean(parts, axis=0)
