"""포트폴리오·거래 지표 (포트폴리오 지표는 일별 평가금액 기준)."""
from __future__ import annotations

import math
from typing import Dict, Optional

import numpy as np
import pandas as pd

STOP_REASONS = ("STOP", "STOP_GAP")


def _f(x):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else float(x)


def max_consecutive_stops(trades: pd.DataFrame) -> int:
    if trades.empty:
        return 0
    t = trades[~trades["open"]].sort_values(["exit_ci", "entry_ci"])
    best = run = 0
    for r in t["reason"]:
        run = run + 1 if r in STOP_REASONS else 0
        best = max(best, run)
    return int(best)


def portfolio_metrics(res: dict, cal: pd.DatetimeIndex, cand: Optional[pd.DataFrame] = None) -> Dict:
    s, e = res["start"], res["end"]
    dates = cal[s:e + 1]
    eq = pd.Series(res["equity"], index=dates)
    inv = pd.Series(res["invested"], index=dates)
    rets = eq.pct_change().dropna()
    years = max((dates[-1] - dates[0]).days / 365.25, 1 / 365.25)
    total = eq.iloc[-1] / eq.iloc[0] - 1 if eq.iloc[0] else 0.0
    # 첫날 평가 전 초기자금 기준
    init = eq.iloc[0] if res.get("initial") is None else res["initial"]
    cagr = (eq.iloc[-1] / init) ** (1 / years) - 1 if eq.iloc[-1] > 0 else -1.0
    dd = eq / eq.cummax() - 1
    sd = rets.std()
    tr = pd.DataFrame(res["trades"])
    op = pd.DataFrame(res["open"])
    allt = pd.concat([tr, op], ignore_index=True) if len(op) else tr
    closed = tr if len(tr) else pd.DataFrame(columns=["pnl", "R", "reason", "open"])
    gp = closed.loc[closed["pnl"] > 0, "pnl"].sum() if len(closed) else 0.0
    gl = -closed.loc[closed["pnl"] < 0, "pnl"].sum() if len(closed) else 0.0
    gp_all = allt.loc[allt["pnl"] > 0, "pnl"].sum() if len(allt) else 0.0
    gl_all = -allt.loc[allt["pnl"] < 0, "pnl"].sum() if len(allt) else 0.0
    n_entries = len(allt)
    out = {
        "total_return": _f(eq.iloc[-1] / init - 1), "cagr": _f(cagr), "mdd": _f(dd.min()),
        "sharpe": _f(rets.mean() / sd * math.sqrt(252)) if sd and sd > 0 else None,
        "calmar": _f(cagr / -dd.min()) if dd.min() < 0 else None,
        "years": years,
        "trades": int(n_entries), "trades_closed": int(len(closed)),
        "trades_per_year": n_entries / years,
        "profit_factor": _f(gp / gl) if gl > 0 else None,
        "profit_factor_incl_open": _f(gp_all / gl_all) if gl_all > 0 else None,
        "win_rate": _f((closed["pnl"] > 0).mean()) if len(closed) else None,
        "stop_rate": _f(closed["reason"].isin(STOP_REASONS).mean()) if len(closed) else None,
        "avg_R": _f(allt["R"].mean()) if n_entries else None,
        "avg_R_closed": _f(closed["R"].mean()) if len(closed) else None,
        "pnl_per_risk": _f(allt["pnl"].sum() / allt["risk0"].sum()) if n_entries and allt["risk0"].sum() > 0 else None,
        "avg_ret_trade": _f(allt["ret"].mean()) if n_entries else None,
        "avg_hold_days": _f(allt["hold_days"].mean()) if n_entries else None,
        "max_consec_stops": max_consecutive_stops(allt) if n_entries else 0,
        "capital_utilization": _f((inv / eq).mean()),
        "avg_positions": _f(np.mean(res["positions"])),
        "max_positions": int(np.max(res["positions"])) if len(res["positions"]) else 0,
        "heat_current_avg": _f(np.mean(res["heat_current"])), "heat_current_max": _f(np.max(res["heat_current"])),
        "heat_entry_avg": _f(np.mean(res["heat_entry"])), "heat_entry_max": _f(np.max(res["heat_entry"])),
        "open_at_end": int(len(op)),
        "open_unrealized_pnl": _f(op["pnl"].sum()) if len(op) else 0.0,
        "open_capital_share": _f(op["mkt_value"].sum() / eq.iloc[-1]) if len(op) else 0.0,
        "open_avg_age_days": _f(op["hold_days"].mean()) if len(op) else None,
        "pool_size": res["pool_size"],
        "opportunity_reduction": _f(1 - n_entries / res["pool_size"]) if res["pool_size"] else None,
        **{f"n_{k}": v for k, v in res["counts"].items()},
        "fill_rate": _f(res["counts"]["filled"] / res["counts"]["orders"]) if res["counts"]["orders"] else None,
    }
    for k, v in res["size_reasons"].items():
        out[f"size0_{k}"] = v
    return out


def yearly_returns(res: dict, cal: pd.DatetimeIndex, initial: float) -> Dict[int, float]:
    dates = cal[res["start"]:res["end"] + 1]
    eq = pd.Series(res["equity"], index=dates)
    out = {}
    prev = initial
    for y, g in eq.groupby(eq.index.year):
        out[int(y)] = float(g.iloc[-1] / prev - 1)
        prev = g.iloc[-1]
    return out


def block_bootstrap_diff(diff: np.ndarray, block: int = 20, n: int = 1000, seed: int = 11) -> Dict:
    """일별 수익률 차이의 평균에 대한 이동 블록 bootstrap."""
    diff = diff[np.isfinite(diff)]
    if len(diff) < block * 2:
        return {"p05": None, "p50": None, "p95": None, "prob_positive": None}
    rng = np.random.default_rng(seed)
    nb = int(math.ceil(len(diff) / block))
    means = np.empty(n)
    starts_max = len(diff) - block
    for k in range(n):
        st = rng.integers(0, starts_max + 1, size=nb)
        sample = np.concatenate([diff[s:s + block] for s in st])[:len(diff)]
        means[k] = sample.mean()
    ann = means * 252
    return {"p05": float(np.percentile(ann, 5)), "p50": float(np.percentile(ann, 50)),
            "p95": float(np.percentile(ann, 95)), "prob_positive": float((means > 0).mean())}


def trade_stats(df: pd.DataFrame, rcol="R", retcol="ret", reason_col="reason") -> Dict:
    """신호 단위(포트폴리오 제약 없음) 거래 통계."""
    if df.empty:
        return {"n": 0}
    r = df[rcol].dropna()
    ret = df[retcol].dropna()
    pos, neg = ret[ret > 0].sum(), -ret[ret < 0].sum()
    return {"n": int(len(df)), "avg_R": _f(r.mean()), "median_R": _f(r.median()),
            "avg_ret": _f(ret.mean()), "median_ret": _f(ret.median()),
            "win_rate": _f((ret > 0).mean()), "profit_factor": _f(pos / neg) if neg > 0 else None,
            "stop_rate": _f(df[reason_col].isin(STOP_REASONS).mean()),
            "loss_10_rate": _f((ret <= -0.10).mean())}
