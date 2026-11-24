"""집계 · 선택 · 검증 · 산출물 저장."""
from __future__ import annotations

import json
import logging
import pickle
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .common import OUTPUT_DIR
from .exit_rules import BREAKOUT_TYPES, ExitConfig, catalog, hybrid_configs
from .parameter_search import (block_bootstrap, chronological_split, objective, purge_overlap,
                               rank_stability, select_by_family, walk_forward)
from .performance_metrics import trade_metrics

log = logging.getLogger(__name__)

SIG_COLS = ["market", "ticker", "name", "source", "signal_type", "signal_date", "detect_date", "first_seen",
            "strict_filter_passed", "grade", "signal_quality", "has_entry_warnings", "sector_stage",
            "market_condition", "pivot_price", "stop_loss", "ma150", "price", "rs_value", "rs_trend",
            "entry_date", "entry_idx", "below_pivot_entry", "entry_vs_pivot_pct", "stop_dist_pct",
            "stop_at_or_above_open", "exclude_reason", "factor", "factor_flag", "values_at_first_detection",
            "result_id", "signal_key", "filter_reasons", "entry_warnings", "upthrust_failed_known",
            "detect_lag_days", "available_days"]


def _jsonable(o):
    if isinstance(o, (np.floating,)):
        return None if np.isnan(o) else float(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (pd.Timestamp,)):
        return o.isoformat()
    if isinstance(o, float) and (np.isnan(o) or np.isinf(o)):
        return None
    return str(o)


def _dump(obj, name):
    with open(OUTPUT_DIR / name, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1, default=_jsonable)


def _clean_records(df: pd.DataFrame) -> list:
    return json.loads(df.to_json(orient="records", force_ascii=False, date_format="iso"))


def _period_masks(entry: pd.Series, end: pd.Timestamp) -> Dict[str, pd.Series]:
    e = pd.to_datetime(entry)
    return {"full": e.notna(), "last_1y": e >= end - pd.DateOffset(years=1),
            "last_6m": e >= end - pd.DateOffset(months=6), "last_3m": e >= end - pd.DateOffset(months=3)}


def _summ(df: pd.DataFrame, keys: dict) -> dict:
    m = trade_metrics(df)
    m.update(keys)
    return m


def prep_sig(sig: pd.DataFrame) -> pd.DataFrame:
    sig = sig.reset_index().rename(columns={"index": "sig_idx"})
    sig["sig_uid"] = sig["market"] + ":" + sig["sig_idx"].astype(str)
    sig["entry_date"] = pd.to_datetime(sig["entry_date"])
    return sig


def base60(cfg, results: pd.DataFrame, sig_p: pd.DataFrame) -> pd.DataFrame:
    """Stage 2 입력: 기본 slippage·보수 시나리오·기본 horizon 행만 신호 정보와 결합 (메모리 절감)."""
    keep = [c for c in SIG_COLS if c in sig_p.columns and c != "market"]
    f = results[(results["slip_bp"] == cfg["costs"]["slippage_bp"]) & (results["scenario"] == "conservative")
                & (results["horizon"] == cfg["default_horizon"])]
    b = f.merge(sig_p[["sig_uid"] + keep], on="sig_uid", how="left")
    b["entry_date"] = pd.to_datetime(b["entry_date"])
    return b


def analyze(cfg, results: pd.DataFrame, sig: pd.DataFrame):
    """Stage 1 분석. Stage 2 는 호출부가 전체 결과를 해제한 뒤 base60 으로 실행한다."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    sig = prep_sig(sig)
    keep = [c for c in SIG_COLS if c in sig.columns]
    r = results.merge(sig[["sig_uid"] + [c for c in keep if c != "market"]], on="sig_uid", how="left")
    r["entry_date"] = pd.to_datetime(r["entry_date"])
    base = r[(r["slip_bp"] == cfg["costs"]["slippage_bp"]) & (r["scenario"] == "conservative")].copy()
    rec = base[base["source"] == "RECONSTRUCTED"]
    snap = base[base["source"] == "SNAPSHOT_ONLY"]
    H = cfg["default_horizon"]
    data_end = pd.to_datetime(base["horizon_end_date"]).max()

    # ── historical_signals.csv ──
    sig_out = sig[["sig_uid"] + keep].copy()
    sig_out.to_csv(OUTPUT_DIR / "historical_signals.csv", index=False)

    # ── trade_results.csv (핵심 전략 + 모든 horizon, 체결 내역 제외) ──
    from .backtest_engine import CORE
    tr_cols = ["market", "ticker", "source", "signal_type", "signal_date", "detect_date", "entry_date",
               "strict_filter_passed", "grade", "signal_quality", "strategy", "horizon", "entry_price",
               "initial_stop", "exit_date", "exit_reason", "first_exit_reason", "n_fills", "return",
               "realized_return_component", "partial_component", "open_qty", "holding_days", "mfe", "mae",
               "giveback", "cost", "gap_excess", "abnormal_entry", "ambiguous_bar", "incomplete_horizon",
               "review_flags", "post_5d", "post_20d", "post_60d"]
    tr = base[base["strategy"].isin(CORE) & (base["strict_filter_passed"].fillna(False) |
                                             base["strategy"].isin(["S00", "S01"]))]
    tr[[c for c in tr_cols if c in tr.columns]].round(5).to_csv(OUTPUT_DIR / "trade_results.csv", index=False)

    # ── strategy_summary ──
    rows = []
    for market in sorted(base["market"].unique()):
        for h in cfg["horizons"]:
            rm = rec[(rec["market"] == market) & (rec["horizon"] == h)]
            groups = {"A_all": rm["first_seen"].fillna(False).astype(bool)}
            from .backtest_engine import GROUPS_STRICT
            for g, f in GROUPS_STRICT.items():
                groups[g] = f(rm.assign(has_entry_warnings=rm["has_entry_warnings"].fillna(False).astype(bool),
                                        strict_filter_passed=rm["strict_filter_passed"].fillna(False).astype(bool)))
            for g, mask in groups.items():
                sub = rm[mask]
                periods = _period_masks(sub["entry_date"], data_end) if g == "B_strict" else {"full": sub["entry_date"].notna()}
                for pname, pm in periods.items():
                    for strat, s in sub[pm].groupby("strategy"):
                        if strat not in CORE and not (g == "B_strict" and pname == "full" and h == H):
                            continue
                        rows.append(_summ(s, {"market": market, "source": "RECONSTRUCTED", "group": g,
                                              "period": pname, "horizon": h, "strategy": strat,
                                              "signal_type": "ALL"}))
                if g == "B_strict":
                    for st, sub_t in sub.groupby("signal_type"):
                        for strat, s in sub_t.groupby("strategy"):
                            rows.append(_summ(s, {"market": market, "source": "RECONSTRUCTED", "group": g,
                                                  "period": "full", "horizon": h, "strategy": strat,
                                                  "signal_type": st}))
            sm = snap[(snap["market"] == market) & (snap["horizon"] == h)]
            for strat, s in sm.groupby("strategy"):
                if strat in CORE:
                    rows.append(_summ(s, {"market": market, "source": "SNAPSHOT_ONLY", "group": "SNAP_visible",
                                          "period": "full", "horizon": h, "strategy": strat, "signal_type": "ALL"}))
    summary = pd.DataFrame(rows)
    summary.to_csv(OUTPUT_DIR / "strategy_summary.csv", index=False)

    # ── slippage / scenario 민감도 ──
    sens_rows = []
    sens = r[(r["source"] == "RECONSTRUCTED") & (r["horizon"] == H) & r["strict_filter_passed"].fillna(False)]
    for (market, strat, bp, sc), s in sens.groupby(["market", "strategy", "slip_bp", "scenario"]):
        if strat in CORE or strat.startswith("S07T"):
            m = trade_metrics(s)
            sens_rows.append({"market": market, "strategy": strat, "slip_bp": bp, "scenario": sc,
                              "trades": m["trades"], "avg_return": m["avg_return"],
                              "profit_factor": m.get("profit_factor"), "ambiguous_bars": m.get("ambiguous_bars")})
    sensitivity = pd.DataFrame(sens_rows)

    # ── exit_reason_summary ──
    ers = []
    b60 = rec[(rec["horizon"] == H) & rec["strict_filter_passed"].fillna(False)]
    for (market, strat), s in b60[b60["strategy"].isin(CORE)].groupby(["market", "strategy"]):
        for reason, g in s.groupby("exit_reason"):
            ers.append({"market": market, "strategy": strat, "exit_reason": reason, "count": len(g),
                        "share": len(g) / len(s), "avg_return": g["return"].mean(),
                        "avg_holding_days": g["holding_days"].mean(),
                        "post_20d_avg": g["post_20d"].mean() if "post_20d" in g else None})
    exit_reasons = pd.DataFrame(ers)
    exit_reasons.to_csv(OUTPUT_DIR / "exit_reason_summary.csv", index=False)

    # ── 선택 · 검증 ──
    selection = {}
    param_rows = []
    wf_all = []
    for market in sorted(b60["market"].unique()):
        bm = b60[b60["market"] == market]
        sig_m = sig[(sig["market"] == market) & (sig["source"] == "RECONSTRUCTED") & sig["strict_filter_passed"]
                    & sig["entry_date"].notna()].copy()
        ends = bm[bm["strategy"] == "S00"].set_index("sig_idx")["horizon_end_date"]
        sig_m = sig_m.set_index("sig_idx")
        sig_m["exit_date_max"] = pd.to_datetime(ends.reindex(sig_m.index))
        split = chronological_split(sig_m["entry_date"], (cfg["validation"]["train"], cfg["validation"]["validation"],
                                                          cfg["validation"]["test"]))
        idx_tr = purge_overlap(sig_m.reset_index(), split.reset_index(drop=True), "train", "validation")
        idx_tr = sig_m.index[idx_tr]
        idx_va_all = sig_m.index[split == "validation"]
        va_pos = purge_overlap(sig_m.reset_index(), split.reset_index(drop=True), "validation", "test")
        idx_va = sig_m.index[va_pos]
        idx_te = sig_m.index[split == "test"]
        bounds = {s: (str(sig_m.loc[split == s, "entry_date"].min().date()),
                      str(sig_m.loc[split == s, "entry_date"].max().date())) for s in ("train", "validation", "test")
                  if (split == s).any()}
        for seg, idx in (("train", idx_tr), ("validation", idx_va), ("test", idx_te), ("full", sig_m.index)):
            sub = bm[bm["sig_idx"].isin(idx)]
            for (strat, fam), s in sub.groupby(["strategy", "family"]):
                m = trade_metrics(s)
                param_rows.append({"market": market, "segment": seg, "strategy": strat, "family": fam,
                                   "signal_type": "ALL", "objective": objective(s["return"].to_numpy()), **m})
            for st, sub_t in sub.groupby("signal_type"):
                for (strat, fam), s in sub_t.groupby(["strategy", "family"]):
                    m = trade_metrics(s)
                    param_rows.append({"market": market, "segment": seg, "strategy": strat, "family": fam,
                                       "signal_type": st, "objective": objective(s["return"].to_numpy()),
                                       "trades": m["trades"], "avg_return": m["avg_return"],
                                       "win_rate": m["win_rate"], "profit_factor": m.get("profit_factor"),
                                       "loss_10_rate": m["loss_10_rate"], "worst_trade": m["worst_trade"],
                                       "avg_holding_days": m["avg_holding_days"]})
        min_n = cfg["validation"]["min_trades_for_selection"]
        best_all = select_by_family(bm, idx_tr, min_n)
        best_type = {}
        for cls, types in (("BREAKOUT_FAMILY", BREAKOUT_TYPES), ("REBOUND", ("REBOUND",))):
            bt = bm[bm["signal_type"].isin(types)]
            best_type[cls] = select_by_family(bt, idx_tr, max(10, min_n // 2))
        stability = rank_stability(bm, idx_tr, idx_va)
        wf = walk_forward(bm, sig_m.reset_index().set_index("sig_idx"), cfg["validation"]["walk_forward_train_months"],
                          cfg["validation"]["walk_forward_test_months"], min_n)
        wf["market"] = market
        wf_all.append(wf)
        selection[market] = {"bounds": bounds, "n_train": len(idx_tr), "n_validation": len(idx_va),
                             "n_validation_before_purge": len(idx_va_all), "n_test": len(idx_te),
                             "best_by_family_train": best_all, "best_by_type_train": best_type,
                             "rank_stability_train_vs_val": stability,
                             "idx": {"train": list(map(int, idx_tr)), "validation": list(map(int, idx_va)),
                                     "test": list(map(int, idx_te))}}
    param_df = pd.DataFrame(param_rows)
    param_df.to_csv(OUTPUT_DIR / "parameter_results.csv", index=False)
    wf_df = pd.concat(wf_all, ignore_index=True) if wf_all else pd.DataFrame()

    report = {"summary": summary, "sensitivity": sensitivity, "exit_reasons": exit_reasons,
              "param": param_df, "walk_forward": wf_df, "selection": selection}
    with open(Path(cfg["data"]["cache_dir"]) / "analysis_stage1.pkl", "wb") as fh:
        pickle.dump(report, fh)
    return report
