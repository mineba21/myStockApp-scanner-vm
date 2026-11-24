"""STEP 9 — 단일 Interactive HTML 보고서 (외부 의존 없음, 파일 단독 실행)."""
from __future__ import annotations

import json
import math
import pickle
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from .common import OUTPUT_DIR
from .performance_metrics import trade_metrics

TEMPLATE = Path(__file__).with_name("report_template.html")


def _clean(o):
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.floating, float)):
        return None if (math.isnan(o) or math.isinf(o)) else round(float(o), 6)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (pd.Timestamp, datetime)):
        return o.strftime("%Y-%m-%d")
    if o is None or isinstance(o, (str, int, bool)):
        return o
    return str(o)


def _records(df: pd.DataFrame, cols=None) -> list:
    if df is None or len(df) == 0:
        return []
    d = df[cols] if cols else df
    return _clean(d.to_dict("records"))


def _early_exit(b60: pd.DataFrame) -> list:
    """S04 변형별: 매수 근거 훼손 매도의 효과 (S01 대비 쌍대 비교)."""
    s01 = b60[b60["strategy"] == "S01"].set_index(["market", "sig_idx"])
    rows = []
    for (market, strat), g in b60[b60["family"] == "S04"].groupby(["market", "strategy"]):
        g = g.set_index(["market", "sig_idx"])
        j = g.join(s01[["return", "exit_reason"]], rsuffix="_s01", how="inner")
        bf = j[j["exit_reason"].astype(str).str.startswith("BF_")]
        if len(j) == 0:
            continue
        diff = j["return"] - j["return_s01"]
        rows.append({
            "market": market, "strategy": strat, "applied": len(j), "bf_exits": len(bf),
            "bf_exit_rate": len(bf) / len(j),
            "bf_exit_before_loss_rate": float((bf["return"] >= -0.005).mean()) if len(bf) else None,
            "avg_return": float(j["return"].mean()), "avg_diff_vs_S01": float(diff.mean()),
            "loss_avoided_sum": float(diff[diff > 0].sum()), "improved_rate": float((diff > 0.001).mean()),
            "worsened_rate": float((diff < -0.001).mean()),
            "loss10_rate": float((j["return"] <= -0.10).mean()),
            "loss10_rate_S01": float((j["return_s01"] <= -0.10).mean()),
            "bf_post20_avg": float(bf["post_20d"].mean()) if len(bf) and bf["post_20d"].notna().any() else None,
            "bf_whipsaw_rate": float((bf["post_20d"] >= 0.10).mean()) if len(bf) else None,
            "bf_recover_rate": float(bf["post_recover_entry"].dropna().astype(float).mean())
            if len(bf) and bf["post_recover_entry"].notna().any() else None,
        })
    return rows


def _below_pivot(b60: pd.DataFrame) -> list:
    rows = []
    sub = b60[b60["signal_type"].isin(["BREAKOUT", "RE_BREAKOUT"]) & b60["strategy"].isin(["S00", "S01", "S04A_t0_wall", "S04B_t1_wall"])]
    for (market, strat, below), g in sub.groupby(["market", "strategy", sub["below_pivot_entry"].fillna(False)]):
        rows.append({"market": market, "strategy": strat, "below_pivot_entry": bool(below), "trades": len(g),
                     "avg_return": float(g["return"].mean()), "win_rate": float((g["return"] > 0).mean())})
    return rows


def _lambda_sensitivity(param: pd.DataFrame) -> list:
    """선택 기준의 꼬리 벌점 가중치(λ)를 바꿨을 때 Train 최적 설정과 그 이후 구간 성과."""
    rows = []
    auto = ("S01", "S02", "S03", "S04", "S06", "S07", "S07T")
    for market, g in param[(param["signal_type"] == "ALL") & param["family"].isin(auto)].groupby("market"):
        w = g.pivot_table(index="strategy", columns="segment", values="avg_return")
        cv = g[g["segment"] == "train"].set_index("strategy")["cvar_5"]
        l10 = g.pivot_table(index="strategy", columns="segment", values="loss_10_rate")
        for lam in (0.0, 0.25, 0.5, 1.0):
            score = w["train"] + lam * cv.reindex(w.index).clip(upper=0)
            best = score.idxmax()
            rows.append({"market": market, "lambda": lam, "strategy": best,
                         "train": w.loc[best, "train"], "validation": w.loc[best].get("validation"),
                         "test": w.loc[best].get("test"), "loss_10_test": l10.loc[best].get("test"),
                         "s01_test": w.loc["S01"].get("test") if "S01" in w.index else None})
    return rows


def build_report(cfg):
    cache = Path(cfg["data"]["cache_dir"])
    rep = pickle.load(open(cache / "analysis_final.pkl", "rb"))
    b60 = pd.read_pickle(cache / "results_b60.pkl")
    audit = json.load(open(OUTPUT_DIR / "data_audit.json", encoding="utf-8"))
    summary = rep["summary"]
    daily = pd.read_csv(OUTPUT_DIR / "daily_equity.csv", parse_dates=["date"])
    # 주간 샘플링 (용량)
    daily = daily.sort_values("date")
    wk = daily.groupby(["market", "strategy"]).apply(
        lambda g: g.set_index("date").resample("W-FRI").last().dropna(subset=["equity"]).reset_index()
    ).reset_index(drop=True)
    eq = {}
    for (m, s), g in wk.groupby(["market", "strategy"]):
        eq.setdefault(m, {})[s] = {"d": g["date"].dt.strftime("%Y-%m-%d").tolist(),
                                   "v": [round(x, 5) for x in g["equity"]]}
    labels = {}
    from .exit_rules import catalog
    for c in catalog():
        labels[c.name] = c.label
    for k, v in rep.get("composed", {}).items():
        for name, c in v["configs"].items():
            labels[name] = c["label"].rsplit(" ", 1)[0]
    sum_cols = ["market", "source", "group", "period", "horizon", "strategy", "signal_type", "trades", "win_rate",
                "avg_return", "median_return", "avg_win", "avg_loss", "profit_factor", "expectancy", "payoff_ratio",
                "avg_holding_days", "avg_cost", "worst_trade", "loss_5_rate", "loss_10_rate", "loss_20_rate", "cvar_5",
                "stop_rate", "gap_excess_avg", "gap_trades", "mae_avg", "mfe_avg", "giveback_avg", "capture_median",
                "partial_component_avg", "open_at_end_rate", "ambiguous_bars", "abnormal_entries",
                "post_5d_avg", "post_20d_avg", "post_60d_avg", "post_recover_entry_rate", "post_new_high_rate",
                "loss_exit_good_rate", "loss_exit_whipsaw_rate", "profit_exit_protected_rate",
                "profit_exit_too_early_rate"]
    sum_cols = [c for c in sum_cols if c in summary.columns]
    param = rep["param"]
    pcols = ["market", "segment", "strategy", "family", "signal_type", "objective", "trades", "avg_return",
             "win_rate", "profit_factor", "loss_10_rate", "worst_trade", "avg_holding_days", "giveback_avg",
             "capture_median"]
    pcols = [c for c in pcols if c in param.columns]
    final = rep.get("final", {})
    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "config": {"costs": cfg["costs"], "portfolio": cfg["portfolio"], "horizons": cfg["horizons"],
                   "default_horizon": cfg["default_horizon"], "replay_start": cfg["data"]["replay_start"],
                   "overrides": cfg.get("scanner_env_overrides", {})},
        "audit": audit, "labels": labels,
        "summary": _records(summary, sum_cols),
        "sensitivity": _records(rep["sensitivity"]),
        "exit_reasons": _records(rep["exit_reasons"]),
        "param": _records(param[param["family"].notna()], pcols),
        "walk_forward": _records(rep["walk_forward"]),
        "selection": _clean({m: {k: v for k, v in s.items() if k != "idx"} for m, s in rep["selection"].items()}),
        "final": _clean(final), "composed": _clean(rep.get("composed", {})),
        "portfolio": _records(rep["portfolio"]), "equity": eq,
        "lambda_sens": _clean(_lambda_sensitivity(param)) if "cvar_5" in param.columns else [],
        "early": _early_exit(b60), "below_pivot": _below_pivot(b60),
        "cases": _clean(rep["cases"]), "rules": _clean(rep["rules"]),
    }
    html = TEMPLATE.read_text(encoding="utf-8").replace(
        "/*__DATA__*/null", json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/"))
    out = OUTPUT_DIR / "exit_strategy_backtest_report.html"
    out.write_text(html, encoding="utf-8")
    return out
