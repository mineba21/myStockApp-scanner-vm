"""Stage 2 — Train 에서 고른 구성요소로 Hybrid 조립 → Validation 선택 → Test 1회 평가.

Test 구간은 최종 선택이 끝난 뒤 보고용으로만 계산한다.
"""
from __future__ import annotations

import json
import logging
import pickle
from dataclasses import asdict, replace
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd

from .common import OUTPUT_DIR
from .exit_rules import BREAKOUT_TYPES, ExitConfig, HardStop, TimeStop, TrendExit, catalog
from .market_data_loader import load_cached
from .parameter_search import block_bootstrap, objective
from .performance_metrics import trade_metrics
from .portfolio_simulator import run_portfolio

log = logging.getLogger(__name__)

AUTO_FAMILIES = ("S01", "S02", "S03", "S04", "S06", "S07", "S07T")
CLASSES = {"BREAKOUT_FAMILY": BREAKOUT_TYPES, "REBOUND": ("REBOUND",)}
PORTFOLIO_STRATS = ["S00", "S01", "S02_fix7", "S03_scap7", "S06_a10_d8", "S07_a10_d6_r50_trail_pct10",
                    "S09A", "S09B", "S09A_OPT", "S09B_OPT"]


def compose(cat: Dict[str, ExitConfig], best: Dict[str, str], cls: str) -> List[ExitConfig]:
    """Train 최적 구성요소로 Hybrid A/B 조립."""
    hard_fams = [f for f in ("S01", "S02", "S03") if f in best]
    hard = cat["S03_scap7"].hard
    hard_src = "default(S03_scap7)"
    if hard_fams:
        # family 간 비교는 각 family 최적 설정의 Train objective 로 한다 (호출부에서 best 에 score 동봉)
        top = max(hard_fams, key=lambda f: best.get("_score_" + f, -9))
        hard, hard_src = cat[best[top]].hard, best[top]
    bf = cat[best["S04"]].bf if "S04" in best else None
    partial = cat[best["S07"]].partial if "S07" in best else cat["S07_a10_d6_r50_trail_pct10"].partial
    trend, time = TrendExit(ma=50, consec=2), TimeStop(20, 0.02)
    meta = {"hard_from": hard_src, "bf_from": best.get("S04"), "partial_from": best.get("S07")}
    a = ExitConfig("S09A_OPT", "S09OPT", hard=hard, bf=bf, partial=partial, trend=trend, time=time,
                   review=("trend", "time"), label=f"Hybrid A (Train 선택 구성) {cls}")
    b = ExitConfig("S09B_OPT", "S09OPT", hard=hard, bf=bf, partial=partial, trend=trend, time=time,
                   label=f"Hybrid B (Train 선택 구성, 전부 매도 가정) {cls}")
    return [a, b], meta


def _family_scores(bm: pd.DataFrame, idx, best: Dict[str, str]) -> Dict[str, str]:
    out = dict(best)
    tr = bm[bm["sig_idx"].isin(idx)]
    for fam, strat in best.items():
        s = tr.loc[tr["strategy"] == strat, "return"].to_numpy()
        out["_score_" + fam] = objective(s)
    return out


def run_stage2(cfg, base: pd.DataFrame, sig: pd.DataFrame, rep: dict):
    """base: 기본 slippage·보수 시나리오·기본 horizon 결과 (analysis.base60)."""
    import gc
    from .backtest_engine import CORE, _costs, run_tasks

    H = cfg["default_horizon"]
    slip = cfg["costs"]["slippage_bp"]
    cat = {c.name: c for c in catalog()}
    rec_b = base[(base["source"] == "RECONSTRUCTED") & base["strict_filter_passed"].fillna(False)]
    costs = _costs(cfg)
    sel = rep["selection"]
    composed_meta, extra = {}, []
    for market in sel:
        trades = pickle.load(open(Path(cfg["data"]["cache_dir"]) / f"trades_{market}.pkl", "rb"))
        bm = rec_b[rec_b["market"] == market]
        idx_tr = sel[market]["idx"]["train"]
        tasks = []
        for cls, types in CLASSES.items():
            best = sel[market]["best_by_type_train"].get(cls, {})
            best = _family_scores(bm[bm["signal_type"].isin(types)], idx_tr, best)
            cfgs, meta = compose(cat, best, cls)
            composed_meta[f"{market}:{cls}"] = {**meta, "configs": {c.name: asdict(c) for c in cfgs}}
            subset = set(bm.loc[bm["signal_type"].isin(types), "sig_idx"])
            for c in cfgs:
                tasks.append((c, H, slip, "conservative", True, subset))
        res = run_tasks(trades, costs, tasks)
        del trades
        gc.collect()
        res["market"] = market
        res["sig_uid"] = market + ":" + res["sig_idx"].astype(str)
        extra.append(res)
    extra = pd.concat(extra, ignore_index=True)
    extra = extra.merge(rec_b.drop_duplicates("sig_uid")[["sig_uid"] + [c for c in rec_b.columns
                                                                        if c not in extra.columns]],
                        on="sig_uid", how="left")
    allb = pd.concat([rec_b, extra], ignore_index=True, sort=False)

    # ── 최종 선택 (Validation) → Test 보고 ──
    recs, final = [], {}
    for market in sel:
        bm = allb[allb["market"] == market]
        ix = sel[market]["idx"]
        for cls, types in CLASSES.items():
            bt = bm[bm["signal_type"].isin(types)]
            best = sel[market]["best_by_type_train"].get(cls, {})
            cands = {"S01": "S01", "S09A": "S09A", "S09A_OPT": "S09A_OPT"}
            for fam in AUTO_FAMILIES:
                if fam in best:
                    cands[fam + "*"] = best[fam]

            def seg_stats(strat, seg):
                s = bt[(bt["strategy"] == strat) & bt["sig_idx"].isin(ix[seg])]
                m = trade_metrics(s)
                return {"objective": objective(s["return"].to_numpy()), "trades": len(s),
                        "avg_return": m.get("avg_return"), "profit_factor": m.get("profit_factor"),
                        "loss_10_rate": m.get("loss_10_rate"), "worst_trade": m.get("worst_trade"),
                        "win_rate": m.get("win_rate"), "giveback_avg": m.get("giveback_avg")}

            table = {k: {seg: seg_stats(v, seg) for seg in ("train", "validation")} for k, v in cands.items()}
            valid = {k: t["validation"]["objective"] for k, t in table.items()
                     if t["validation"]["trades"] > 0 and not np.isnan(t["validation"]["objective"])}
            pick = max(valid, key=valid.get) if valid else "S01"
            pick_strat = cands[pick]
            test = {k: seg_stats(cands[k], "test") for k in (pick, "S01")}
            test["S00"] = seg_stats("S00", "test")
            full = bt[bt["strategy"] == pick_strat]
            s01 = bt[bt["strategy"] == "S01"].set_index("sig_idx")["return"]
            paired = full.set_index("sig_idx")["return"].sub(s01).dropna()
            dates = bt[bt["strategy"] == "S01"].set_index("sig_idx")["entry_date"].reindex(paired.index)
            boot = block_bootstrap(paired, dates, cfg["validation"]["bootstrap_block_days"],
                                   cfg["validation"]["bootstrap_samples"])
            te_s = bt[(bt["strategy"] == pick_strat) & bt["sig_idx"].isin(ix["test"])]
            boot_test = block_bootstrap(te_s["return"], te_s["entry_date"], cfg["validation"]["bootstrap_block_days"],
                                        cfg["validation"]["bootstrap_samples"])
            final[f"{market}:{cls}"] = {"candidates": table, "pick": pick, "pick_strategy": pick_strat,
                                        "test": test, "paired_vs_S01_full_bootstrap": boot,
                                        "test_bootstrap": boot_test,
                                        "rank_stability": sel[market]["rank_stability_train_vs_val"]}
    # ── 포트폴리오 (전체 기간, strict 통과, 60일) ──
    port_rows, port_eq = [], []
    for market in sel:
        payload = load_cached(cfg["data"]["cache_dir"], market)
        closes = {t: df["Close"] for t, df in payload["frames"].items()}
        bench = cfg["data"]["us_benchmark"] if market == "US" else cfg["data"]["kr_benchmark"]
        bm = allb[allb["market"] == market]
        start = pd.to_datetime(bm["entry_date"]).min()
        cal = payload["frames"][bench].index
        cal = cal[cal >= start]
        c = cfg["costs"][market]
        for strat in PORTFOLIO_STRATS:
            t = bm[bm["strategy"] == strat].copy()
            if t.empty or "fills" not in t or t["fills"].isna().all():
                continue
            t["priority"] = t["grade"].map({"S": 0, "A": 1, "B": 2}).fillna(3) * 10 + \
                t["signal_quality"].map({"STRONG": 0, "MODERATE": 1, "WEAK": 2}).fillna(3)
            t["last_date"] = t["horizon_end_date"]
            t = t.sort_values(["entry_date", "priority"])
            out = run_portfolio(t, closes, cal, cfg["portfolio"]["max_positions"], cfg["portfolio"]["position_pct"],
                                c["buy_commission"], c["sell_commission"] + c["sell_tax"], slip / 10000.0)
            eq = out.pop("equity"); inv = out.pop("invested"); pos = out.pop("positions")
            port_rows.append({"market": market, "strategy": strat, **out})
            port_eq.append(pd.DataFrame({"date": eq.index, "market": market, "strategy": strat,
                                         "equity": eq.values, "invested": inv.values, "positions": pos.values}))
        beq = payload["frames"][bench]["Close"].reindex(cal).ffill()
        port_eq.append(pd.DataFrame({"date": cal, "market": market, "strategy": f"BENCH_{bench}",
                                     "equity": (beq / beq.iloc[0]).values, "invested": np.nan, "positions": np.nan}))
        del payload, closes
        gc.collect()
    portfolio = pd.DataFrame(port_rows)
    daily = pd.concat(port_eq, ignore_index=True)
    daily.to_csv(OUTPUT_DIR / "daily_equity.csv", index=False)
    portfolio.to_csv(OUTPUT_DIR / "portfolio_summary.csv", index=False)

    # 확장 strategy_summary (조립 Hybrid 포함)
    opt_rows = []
    for (market, strat), s in extra.groupby(["market", "strategy"]):
        m = trade_metrics(s); m.update({"market": market, "source": "RECONSTRUCTED", "group": "B_strict",
                                        "period": "full", "horizon": H, "strategy": strat, "signal_type": "ALL"})
        opt_rows.append(m)
        for st, g in s.groupby("signal_type"):
            m = trade_metrics(g); m.update({"market": market, "source": "RECONSTRUCTED", "group": "B_strict",
                                            "period": "full", "horizon": H, "strategy": strat, "signal_type": st})
            opt_rows.append(m)
    summary = pd.concat([rep["summary"], pd.DataFrame(opt_rows)], ignore_index=True)
    summary.to_csv(OUTPUT_DIR / "strategy_summary.csv", index=False)
    rep["summary"] = summary

    # ── 사례 / 권장 규칙 / 보고서 데이터 ──
    from .case_analyzer import select_cases
    cases = select_cases(cfg, allb, base, sig)
    with open(OUTPUT_DIR / "case_study.json", "w", encoding="utf-8") as fh:
        json.dump(cases, fh, ensure_ascii=False, indent=1, default=str)
    from .recommendation import build_recommendations
    rules = build_recommendations(cfg, final, composed_meta, allb, sel, cat)
    with open(OUTPUT_DIR / "recommended_exit_rules.json", "w", encoding="utf-8") as fh:
        json.dump(rules, fh, ensure_ascii=False, indent=1, default=str)
    rep.update({"final": final, "composed": composed_meta, "portfolio": portfolio, "cases": cases,
                "rules": rules})
    allb.drop(columns=["fills"], errors="ignore").to_pickle(Path(cfg["data"]["cache_dir"]) / "results_b60.pkl")
    with open(Path(cfg["data"]["cache_dir"]) / "analysis_final.pkl", "wb") as fh:
        pickle.dump(rep, fh)
    log.info("stage2 complete")
