"""Entry Strategy Backtest CLI.

.venv/bin/python -m backtest.entry_strategy.run all        # P0~P5 (보고서 포함)
.venv/bin/python -m backtest.entry_strategy.run p7         # P7 Heat·위험 예산 추가 검증
.venv/bin/python -m backtest.entry_strategy.run report     # 저장된 결과로 HTML 만 다시 생성
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import pickle
import time
from dataclasses import asdict, replace

import numpy as np
import pandas as pd

from .src import data as D
from .src import experiments as EX
from .src import metrics as MT
from .src import pipeline as P
from .src.pipeline import C, Spec

log = logging.getLogger("entry_strategy")
OUT = D.OUTPUT_DIR


def _json(obj, path):
    def conv(o):
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.floating,)):
            return None if not math.isfinite(o) else float(o)
        if isinstance(o, (np.bool_,)):
            return bool(o)
        if isinstance(o, (pd.Timestamp,)):
            return str(o.date())
        if isinstance(o, float) and not math.isfinite(o):
            return None
        return str(o)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1, default=conv)


def equity_frame(specs: dict) -> pd.DataFrame:
    rows = []
    cal = C.mkt.calendar
    for name, sp in specs.items():
        res = P.run_spec(sp)
        s, e = res["start"], res["end"]
        eq = res["equity"]
        rows.append(pd.DataFrame({"date": cal[s:e + 1], "series": name, "equity": eq,
                                  "drawdown": eq / np.maximum.accumulate(eq) - 1,
                                  "invested": res["invested"], "positions": res["positions"],
                                  "heat_current": res["heat_current"], "heat_entry": res["heat_entry"]}))
    s, e = C.cuts["full"]
    spy = C.mkt.regime["spy_close"].to_numpy()[s:e + 1]
    eq = spy / spy[0] * C.cfg["profiles"]["P_C"]["equity"]
    rows.append(pd.DataFrame({"date": cal[s:e + 1], "series": "BENCH_SPY", "equity": eq,
                              "drawdown": eq / np.maximum.accumulate(eq) - 1}))
    return pd.concat(rows, ignore_index=True)


def trade_log(specs: dict) -> pd.DataFrame:
    rows = []
    cal = C.mkt.calendar
    cand = C.cand.set_index("cid")
    for name, sp in specs.items():
        res = P.run_spec(sp)
        for t in res["trades"] + res["open"]:
            c = cand.loc[t["cid"]]
            rows.append({"series": name, "ticker": t["ticker"], "signal_type": c["signal_type"],
                         "grade": c["grade"], "signal_quality": c["signal_quality"],
                         "market_condition": c["market_condition"], "detect_date": str(c["detect_date"].date()),
                         "entry_date": str(cal[t["entry_ci"]].date()), "exit_date": str(cal[t["exit_ci"]].date()),
                         "qty": t["qty"], "entry_px": t["entry_px"], "exit_px": t["exit_px"], "stop": t["stop"],
                         "pnl": t["pnl"], "R": t["R"], "ret": t["ret"], "reason": t["reason"],
                         "hold_days": t["hold_days"], "open": t["open"]})
    return pd.DataFrame(rows)


def regime_perf(specs: dict) -> pd.DataFrame:
    """시장 환경(결정일 운영 시장 상태·SPY>MA50) 별 거래 성과 + 연도별."""
    rows = []
    reg = C.mkt.regime
    cand = C.cand.set_index("cid")
    for name, sp in specs.items():
        res = P.run_spec(sp)
        t = pd.DataFrame(res["trades"] + res["open"])
        if t.empty:
            continue
        dec = cand.loc[t["cid"], "decision_ci"].to_numpy()
        t["market_condition"] = reg["market_condition"].to_numpy()[dec]
        t["spy_above_ma50"] = reg["spy_above_ma50"].to_numpy()[dec]
        t["year"] = C.mkt.calendar[t["entry_ci"].to_numpy()].year
        for col in ("market_condition", "spy_above_ma50", "year"):
            for b, g in t.groupby(col):
                gp, gl = g.loc[g.pnl > 0, "pnl"].sum(), -g.loc[g.pnl < 0, "pnl"].sum()
                rows.append({"series": name, "dimension": col, "bucket": str(b), "trades": len(g),
                             "avg_R": g["R"].mean(), "pnl": g["pnl"].sum(),
                             "profit_factor": gp / gl if gl > 0 else None,
                             "stop_rate": g["reason"].isin(MT.STOP_REASONS).mean()})
    return pd.DataFrame(rows)


def stage_all(args):
    t0 = time.time()
    cfg = D.load_config()
    P.prepare(cfg)
    OUT.mkdir(parents=True, exist_ok=True)
    steps = []

    # P0
    audit = P.data_audit()
    _json(audit, OUT / "data_audit.json")
    C.cand.to_csv(OUT / "candidates.csv", index=False)
    steps.append(("P0", "data_audit.json, candidates.csv", len(C.cand)))
    log.info("P0 done %.0fs", time.time() - t0)

    # P1
    fs = P.factor_study()
    fs.to_csv(OUT / "factor_study.csv", index=False)
    cg = P.controlled_grade_quality()
    cg.to_csv(OUT / "grade_quality_controlled.csv", index=False)
    C.score_fit = EX.fit_scores(end_ci=C.cuts["train"][1])
    C.score_fit["table"].to_csv(OUT / "score_fit_train.csv", index=False)
    steps.append(("P1", "factor_study.csv, grade_quality_controlled.csv, score_fit_train.csv", len(fs)))
    log.info("P1 done %.0fs; SC1=%s SC3=%s", time.time() - t0, C.score_fit["SC1"], C.score_fit["SC3"])

    # P2
    et = P.entry_timing_study()
    et.to_csv(OUT / "entry_timing.csv", index=False)
    steps.append(("P2", "entry_timing.csv", len(et)))
    log.info("P2 done %.0fs", time.time() - t0)

    # P3 (선택 전 등록부)
    reg0 = EX.registry()
    summary = EX.run_registry(reg0)
    log.info("P3 registry %d runs %.0fs", len(reg0), time.time() - t0)
    sel, vtab = EX.select(summary)
    vtab.to_csv(OUT / "validation_selection.csv", index=False)
    log.info("selected %s", sel)
    reg1 = [x for x in EX.registry(sel) if x not in set(reg0)]
    summary = pd.concat([summary, EX.run_registry(reg1)], ignore_index=True)

    # 무작위 기준선
    rc = cfg["random_baseline"]
    rspecs = []
    for pool, mk in [("F0", "M0"), ("F1", "M0"), ("F1", "M1"), ("F1", "M2"), ("F1", "M3")]:
        for n in (1, 2, 3):
            for seg in ("validation", "test", "full"):
                rspecs.append(Spec(pool=pool, market=mk, topn=n, segment=seg))
    rspecs.append(replace(Spec(pool=sel.pool, market=sel.market, topn=sel.topn), segment="full", horizon="UNL"))
    rnd = P.random_baseline(rspecs, rc["runs"], rc["seed"])
    rnd.to_csv(OUT / "random_baseline.csv", index=False)
    log.info("random baseline %d runs %.0fs", len(rnd), time.time() - t0)

    # 순위 능력 비교표 (같은 후보군·시점·자금·위험)
    rank_rows = []
    for _, r in summary[(summary["role"].isin(["eligible", "baseline_E00"])) & (summary["horizon"] == "H60")
                        & (summary["profile"] == "P_C") & (summary["topn"] <= 3)].iterrows():
        d = rnd[(rnd["pool"] == r["pool"]) & (rnd["market"] == r["market"]) & (rnd["topn"] == r["topn"])
                & (rnd["segment"] == r["segment"]) & (rnd["horizon"] == "H60")]
        if d.empty:
            continue
        rank_rows.append({"pool": r["pool"], "market": r["market"], "rank": r["rank"], "topn": r["topn"],
                          "segment": r["segment"], "total_return": r["total_return"], "mdd": r["mdd"],
                          "profit_factor": r["profit_factor"], "trades": r["trades"],
                          "random_median": d["total_return"].median(),
                          "random_p25": d["total_return"].quantile(.25), "random_p75": d["total_return"].quantile(.75),
                          "random_percentile": P.percentile_of(r["total_return"], d["total_return"].to_numpy()),
                          "random_pf_median": d["profit_factor"].median()})
    rank_tab = pd.DataFrame(rank_rows)
    rank_tab.to_csv(OUT / "rank_vs_random.csv", index=False)

    # P4 검증
    base = Spec(topn=sel.topn)
    wf = EX.walk_forward(sel, base, runs=rc["runs"])
    wf.to_csv(OUT / "walk_forward.csv", index=False)
    diff = (EX.daily_returns(replace(sel, segment="test")) - EX.daily_returns(replace(base, segment="test"))).to_numpy()
    bcfg = cfg["bootstrap"]
    boot = MT.block_bootstrap_diff(diff, bcfg["block_days"], bcfg["samples"], bcfg["seed"])
    verdict = EX.judge(sel, summary, rnd, wf, boot)
    log.info("verdict %s", verdict["status"])

    summary.to_csv(OUT / "strategy_summary.csv", index=False)
    port = summary[summary["segment"] == "full"]
    port.to_csv(OUT / "portfolio_summary.csv", index=False)
    key = {
        "BASE_E00_N1": Spec(topn=1), "BASE_E00_N2": Spec(topn=2), "BASE_E00_N3": Spec(topn=3),
        "BASE_E00_ALL": Spec(topn=999), "E01_FIXED10x10": Spec(sizing="fixed"),
        "F1_GRADE_N1": Spec(pool="F1", topn=1), "F1_GRADE_N2": Spec(pool="F1", topn=2),
        "F1_GRADE_N3": Spec(pool="F1", topn=3),
        "SELECTED": sel, "SELECTED_UNL": replace(sel, horizon="UNL"), "SELECTED_H120": replace(sel, horizon="H120"),
        "BASE_UNL": replace(base, horizon="UNL"), "BASE_H120": replace(base, horizon="H120"),
    }
    eqf = equity_frame(key)
    eqf.to_csv(OUT / "daily_equity.csv", index=False)
    tl = trade_log({k: key[k] for k in ("SELECTED", "SELECTED_UNL", f"BASE_E00_N{sel.topn}" if sel.topn <= 3 else "BASE_E00_ALL", "BASE_UNL")})
    tl.to_csv(OUT / "trade_log.csv", index=False)
    rp = regime_perf({"SELECTED": sel, "BASELINE": base, "SELECTED_UNL": replace(sel, horizon="UNL"),
                      "BASE_UNL": replace(base, horizon="UNL")})
    rp.to_csv(OUT / "regime_performance.csv", index=False)

    n_exp = {"registry_runs": int(len(summary)), "random_runs": int(len(rnd)),
             "walk_forward_windows": int(len(wf)), "walk_forward_random_runs": int(len(wf) * rc["runs"]),
             "eligible_combinations": int(len(cfg["selection"]["eligible_pools"]) * len(cfg["selection"]["eligible_ranks"])
                                          * len(cfg["selection"]["eligible_topn"])),
             "by_role": summary["role"].value_counts().to_dict()}
    verdict["experiment_counts"] = n_exp
    verdict["score_fit_train"] = {"SC1": C.score_fit["SC1"], "SC2": C.score_fit["SC2"], "SC3": C.score_fit["SC3"]}
    _json(verdict, OUT / "verdict.json")
    from .src import recommend
    rec = recommend.build(verdict, fs, summary, rank_tab, et)
    _json(rec, OUT / "recommended_entry_rules.json")
    steps += [("P3", "strategy_summary.csv, portfolio_summary.csv, daily_equity.csv, trade_log.csv, random_baseline.csv, rank_vs_random.csv", len(summary)),
              ("P4", "validation_selection.csv, walk_forward.csv, verdict.json", len(wf))]
    _json({"steps": steps, "elapsed_s": time.time() - t0}, OUT / "run_meta.json")
    log.info("all done %.0fs", time.time() - t0)
    stage_report(args)


def stage_posthoc(args):
    cfg = D.load_config()
    P.prepare(cfg)
    C.score_fit = EX.fit_scores(end_ci=C.cuts["train"][1])
    df = EX.posthoc(cfg["random_baseline"]["runs"])
    df.to_csv(OUT / "posthoc_exploration.csv", index=False)
    extra = {"POSTHOC_BULL_HARDEXCL_N2": Spec(pool="HARD_EXCL", market="M1", topn=2),
             "POSTHOC_BULL_HARDEXCL_N2_UNL": Spec(pool="HARD_EXCL", market="M1", topn=2, horizon="UNL"),
             "BASE_UNL_HEAT_ENTRY": Spec(topn=1, horizon="UNL", heat="entry"),
             "BASE_UNL": Spec(topn=1, horizon="UNL")}
    eqf = equity_frame(extra)
    eqf[eqf["series"] != "BENCH_SPY"].to_csv(OUT / "posthoc_equity.csv", index=False)
    import json as _j
    verdict = _j.load(open(OUT / "verdict.json", encoding="utf-8"))
    from .src import recommend
    rec = recommend.build(verdict, pd.read_csv(OUT / "factor_study.csv"), pd.read_csv(OUT / "strategy_summary.csv"),
                          pd.read_csv(OUT / "rank_vs_random.csv"), pd.read_csv(OUT / "entry_timing.csv"), posthoc=df)
    _json(rec, OUT / "recommended_entry_rules.json")
    log.info("posthoc %d rows", len(df))


def stage_p7(args):
    """P7 Heat·위험 예산 추가 검증 (configs p7 사전 등록 그리드)."""
    from .src import p7
    t0 = time.time()
    cfg = D.load_config()
    P.prepare(cfg)
    specs = p7.registry()
    log.info("p7 registry %d runs", len(specs))
    reg = p7.run_registry(specs)
    reg.to_csv(OUT / "p7_summary.csv", index=False)
    log.info("p7 registry done (%.0fs)", time.time() - t0)
    rnd = p7.random_b02()
    rnd.to_csv(OUT / "p7_random_b02.csv", index=False)
    cmp_ = p7.b01_vs_random(reg[reg.group == "main"], rnd)
    cmp_.to_csv(OUT / "p7_b01_vs_b02.csv", index=False)
    p7.daily_series().to_csv(OUT / "p7_daily.csv", index=False)
    p7.unl_open_positions().to_csv(OUT / "p7_unl_open_positions.csv", index=False)
    _json(p7.conclusions(reg, cmp_), OUT / "p7_conclusions.json")
    _json({"registry_runs": len(specs), "random_runs": int(len(rnd)),
           "by_group": reg["group"].value_counts().to_dict(), "seconds": time.time() - t0},
          OUT / "p7_meta.json")
    log.info("p7 done: %d runs + %d random (%.0fs)", len(specs), len(rnd), time.time() - t0)


def stage_p7_conclusions(args):
    """저장된 P7 결과로 결론 JSON 만 다시 만든다."""
    from .src import p7
    cfg = D.load_config()
    C.cfg = cfg
    reg = pd.read_csv(OUT / "p7_summary.csv")
    _json(p7.conclusions(reg, pd.read_csv(OUT / "p7_b01_vs_b02.csv")), OUT / "p7_conclusions.json")


def stage_report(args):
    from .src import report
    path = report.build()
    log.info("report → %s", path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["all", "posthoc", "p7", "p7c", "report"])
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    {"all": stage_all, "posthoc": stage_posthoc, "p7": stage_p7, "p7c": stage_p7_conclusions, "report": stage_report}[args.stage](args)


if __name__ == "__main__":
    main()
