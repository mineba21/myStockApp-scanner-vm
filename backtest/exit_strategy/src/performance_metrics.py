"""성과 지표 (독립 거래 단위 / 포트폴리오 단위를 분리)."""
from __future__ import annotations

import math
from typing import Dict, Optional

import numpy as np
import pandas as pd


def _f(x) -> Optional[float]:
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or math.isinf(x) else x


def trade_metrics(df: pd.DataFrame) -> Dict:
    """독립 신호 단위 지표. df: simulate() 결과 행들."""
    n = len(df)
    if n == 0:
        return {"trades": 0}
    r = df["return"].to_numpy(dtype=float)
    wins, losses = r[r > 0], r[r <= 0]
    gross_win, gross_loss = wins.sum(), -losses.sum()
    stop_reasons = df["exit_reason"].isin(["HARD_STOP"])
    out = {
        "trades": n,
        "win_rate": float((r > 0).mean()),
        "avg_return": float(r.mean()),
        "median_return": float(np.median(r)),
        "avg_win": _f(wins.mean()) if len(wins) else None,
        "avg_loss": _f(losses.mean()) if len(losses) else None,
        "profit_factor": _f(gross_win / gross_loss) if gross_loss > 0 else None,
        "expectancy": float(r.mean()),
        "payoff_ratio": _f(wins.mean() / -losses.mean()) if len(wins) and len(losses) and losses.mean() < 0 else None,
        "avg_holding_days": float(df["holding_days"].mean()),
        "total_cost": float(df["cost"].sum()),
        "avg_cost": float(df["cost"].mean()),
        # 손실 방어
        "worst_trade": float(r.min()),
        "loss_5_rate": float((r <= -0.05).mean()),
        "loss_10_rate": float((r <= -0.10).mean()),
        "loss_20_rate": float((r <= -0.20).mean()),
        "cvar_5": float(np.sort(r)[:max(1, int(n * 0.05))].mean()),
        "stop_rate": float(stop_reasons.mean()),
        "gap_excess_avg": float(df["gap_excess"].mean()),
        "gap_trades": int((df["gap_excess"] > 0).sum()),
        "mae_avg": float(df["mae"].mean()),
        "mae_median": float(df["mae"].median()),
        # 수익 보호
        "mfe_avg": float(df["mfe"].mean()),
        "mfe_median": float(df["mfe"].median()),
        "giveback_avg": float(df["giveback"].mean()),
        "capture_median": _f(df["capture"].dropna().median()) if df["capture"].notna().any() else None,
        "partial_component_avg": float(df["partial_component"].mean()),
        "open_at_end_rate": float((df["open_qty"] > 0).mean()),
        "ambiguous_bars": int(df["ambiguous_bar"].sum()),
        "abnormal_entries": int(df["abnormal_entry"].sum()),
    }
    # 조기매도 부작용 (전량 청산 거래만, 매도 이후 경로 — 분석 전용)
    ex = df[df["open_qty"] <= 0]
    if len(ex):
        for k in (5, 10, 20, 60):
            col = f"post_{k}d"
            if col in ex:
                s = ex[col].dropna()
                out[f"post_{k}d_avg"] = _f(s.mean()) if len(s) else None
        for col in ("post_recover_entry", "post_new_high", "post_further_decline"):
            if col in ex:
                s = ex[col].dropna()
                out[col + "_rate"] = _f(s.astype(float).mean()) if len(s) else None
        loss_exit = ex[ex["return"] < 0]
        if len(loss_exit) and "post_20d" in ex:
            p = loss_exit["post_20d"].dropna()
            out["loss_exit_good_rate"] = _f((p <= -0.05).mean()) if len(p) else None      # 손절 후 추가 -5%
            out["loss_exit_whipsaw_rate"] = _f((p >= 0.10).mean()) if len(p) else None    # 손절 후 +10% 반등
        prof_exit = ex[ex["return"] > 0]
        if len(prof_exit) and "post_20d" in ex:
            p = prof_exit["post_20d"].dropna()
            out["profit_exit_protected_rate"] = _f((p < 0).mean()) if len(p) else None
            out["profit_exit_too_early_rate"] = _f((p >= 0.15).mean()) if len(p) else None
    return out


def equity_metrics(equity: pd.Series, invested: pd.Series | None = None,
                   positions: pd.Series | None = None, buys: float | None = None) -> Dict:
    """포트폴리오 일별 평가금액 기반 지표 (독립 거래 수익률 누적으로 계산하지 않는다)."""
    equity = equity.dropna()
    if len(equity) < 2:
        return {}
    rets = equity.pct_change().dropna()
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1 / 365.25)
    total = equity.iloc[-1] / equity.iloc[0] - 1
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1
    dd = equity / equity.cummax() - 1
    mdd = float(dd.min())
    sd = rets.std()
    down = rets[rets < 0].std()
    out = {
        "cumulative_return": float(total), "cagr": float(cagr), "mdd": mdd,
        "sharpe": _f(rets.mean() / sd * math.sqrt(252)) if sd > 0 else None,
        "sortino": _f(rets.mean() / down * math.sqrt(252)) if down and down > 0 else None,
        "calmar": _f(cagr / -mdd) if mdd < 0 else None,
        "years": years,
    }
    if invested is not None:
        out["capital_utilization"] = float((invested / equity).reindex(equity.index).mean())
    if positions is not None:
        out["max_concurrent_positions"] = int(positions.max())
        out["time_in_market"] = float((positions > 0).mean())
    if buys is not None:
        out["turnover_per_year"] = float(buys / equity.mean() / years)
    return out
