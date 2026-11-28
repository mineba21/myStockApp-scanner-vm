"""P3~P4 — 사전 등록 실험 실행, Validation 선택, Test 1회 평가, walk-forward, bootstrap, 판정."""
from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, replace
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from . import metrics as MT
from . import pipeline as P
from . import strategies as ST
from .pipeline import C, Spec

log = logging.getLogger("entry_strategy")

PROFILES = ["P_A", "P_B", "P_C"]
TOPN = [1, 2, 3, 999]
DIAG_POOLS = ["GRADE_A", "GRADE_B", "GRADE_S", "Q_STRONG", "Q_MODERATE", "Q_WEAK", "Q_NOWEAK",
              "T_RE_BREAKOUT", "T_BREAKOUT", "T_REBOUND", "T_NO_BREAKOUT",
              "STOP_LE_7", "STOP_LE_10", "STOP_LE_12", "STOP_LE_15", "EXT_LE_3", "EXT_LE_5", "EXT_LE_7",
              "NO_HOLD", "NO_WEEKLY_VOL", "NO_STOP_WIDE", "NO_CHASE", "NO_BELOW_PIVOT", "NO_UPTHRUST_FAIL",
              "NO_CRITICAL_WARN", "F1"]


def fit_scores(end_ci: int, start_ci: Optional[int] = None) -> dict:
    """[start, end] 결정 후보의 결과를 end 에서 절단해 Score 를 적합 (Train 밖 가격 미참조)."""
    df = P.signal_outcomes(timing="T1_100", horizon="H60", end_ci=end_ci)
    m = df["decision_ci"] <= end_ci
    if start_ci is not None:
        m &= df["decision_ci"] >= start_ci
    df = df[m & np.isfinite(df["R"])]
    f = C.feats["T1_100"].set_index("cid").loc[df["cid"]].reset_index()
    return ST.fit_score(f, df["R"].to_numpy(), df["signal_type"].to_numpy(), C.cfg)


def registry(selected: Optional[Spec] = None) -> List[Tuple[str, Spec]]:
    reg: List[Tuple[str, Spec]] = []
    for prof in PROFILES:
        for n in TOPN:
            for seg in P.SEGMENTS:
                reg.append(("baseline_E00", Spec(profile=prof, topn=n, segment=seg)))
            for hz in ("H120", "UNL"):
                for seg in ("full", "test"):
                    reg.append(("baseline_E00", Spec(profile=prof, topn=n, segment=seg, horizon=hz)))
        for hz in ("H60", "UNL"):
            reg.append(("baseline_E01_fixed10x10", Spec(profile=prof, sizing="fixed", segment="full", horizon=hz)))
    for prof in ("P_C", "P_A"):
        for pool in DIAG_POOLS:
            for n in (2, 999):
                for seg in ("full", "test"):
                    reg.append(("diagnostic_pool", Spec(profile=prof, pool=pool, topn=n, segment=seg)))
        for mk in ("M1", "M2", "M3"):
            for n in (2, 999):
                reg.append(("diagnostic_market", Spec(profile=prof, pool="F0", market=mk, topn=n, segment="full")))
    for pool in C.cfg["selection"]["eligible_pools"]:
        base_pool, mk = (pool.split("_") + ["M0"])[:2]
        for rk in C.cfg["selection"]["eligible_ranks"]:
            for n in C.cfg["selection"]["eligible_topn"]:
                for seg in P.SEGMENTS:
                    reg.append(("eligible", Spec(pool=base_pool, market=mk, rank=rk, topn=n, segment=seg)))
                for hz in ("H120", "UNL"):
                    for seg in ("full", "test"):
                        reg.append(("eligible", Spec(pool=base_pool, market=mk, rank=rk, topn=n, segment=seg, horizon=hz)))
                for prof in ("P_A", "P_B"):
                    reg.append(("eligible_other_profile", Spec(profile=prof, pool=base_pool, market=mk, rank=rk,
                                                               topn=n, segment="full")))
    for tm in P.TIMINGS:
        for n in (2, 999):
            for seg in ("full", "test"):
                reg.append(("diagnostic_timing", Spec(pool="F1", timing=tm, topn=n, segment=seg)))
            reg.append(("diagnostic_timing", Spec(pool="F1", timing=tm, topn=n, segment="full", horizon="UNL")))
    if selected is not None:
        base = Spec(topn=selected.topn)
        for sp in (selected, base):
            role = "sensitivity_selected" if sp is selected else "sensitivity_baseline"
            for seg in ("full", "test"):
                reg.append((role, replace(sp, scenario="next_day", segment=seg)))
                reg.append((role, replace(sp, heat="entry", segment=seg)))
                for sb in C.cfg["costs"]["slippage_sensitivity_bp"]:
                    reg.append((role, replace(sp, slip_bp=sb, segment=seg)))
                for prof in ("P_A", "P_B"):
                    reg.append((role, replace(sp, profile=prof, segment=seg)))
                    reg.append((role, replace(sp, profile=prof, segment=seg, horizon="UNL")))
            reg.append((role, replace(sp, scenario="next_day", segment="full", horizon="UNL")))
            reg.append((role, replace(sp, heat="entry", segment="full", horizon="UNL")))
    seen, out = set(), []
    for role, sp in reg:
        if (role, sp) in seen:
            continue
        seen.add((role, sp))
        out.append((role, sp))
    return out


def run_registry(reg) -> pd.DataFrame:
    rows = []
    for role, sp in reg:
        res = P.run_spec(sp)
        rows.append(P.summarize(sp, res, role))
    return pd.DataFrame(rows)


def objective(row) -> float:
    if row["trades"] < C.cfg["selection"]["min_validation_trades"]:
        return -math.inf
    return row["total_return"] / max(abs(row["mdd"]), 0.05)


def select(summary: pd.DataFrame) -> Tuple[Spec, pd.DataFrame]:
    v = summary[(summary["role"] == "eligible") & (summary["segment"] == "validation")
                & (summary["horizon"] == "H60") & (summary["profile"] == "P_C")].copy()
    v["objective"] = v.apply(objective, axis=1)
    v = v.sort_values("objective", ascending=False)
    top = v.iloc[0]
    spec = Spec(pool=top["pool"], market=top["market"], rank=top["rank"], topn=int(top["topn"]))
    return spec, v


def _yearly(sp: Spec) -> Dict[int, float]:
    res = P.run_spec(sp)
    return MT.yearly_returns(res, C.mkt.calendar, res["initial"])


def daily_returns(sp: Spec) -> pd.Series:
    res = P.run_spec(sp)
    s, e = res["start"], res["end"]
    eq = pd.Series(res["equity"], index=C.mkt.calendar[s:e + 1])
    return pd.concat([pd.Series([res["initial"]], index=[eq.index[0] - pd.Timedelta(days=1)]), eq]).pct_change().dropna()


def walk_forward(sp: Spec, base: Spec, runs: int = 1000) -> pd.DataFrame:
    cal = C.mkt.calendar
    wf = C.cfg["walk_forward"]
    t0 = pd.Timestamp(cal[C.cuts["full"][0]]) + pd.DateOffset(months=wf["train_months"])
    rows = []
    ws = t0
    while ws < cal[-1]:
        we = ws + pd.DateOffset(months=wf["test_months"])
        s = int(cal.searchsorted(ws)); e = min(int(cal.searchsorted(we)) - 1, len(cal) - 1)
        if e - s < 20:
            break
        tr_s = int(cal.searchsorted(ws - pd.DateOffset(months=wf["train_months"])))
        rank = None
        if sp.rank.startswith("SC"):
            fit = fit_scores(end_ci=s - 1, start_ci=tr_s)
            rank = -ST.score(C.feats[sp.timing], fit[sp.rank], fit["ref"])
            comps = fit[sp.rank]
        else:
            comps = None
        r_sel = MT.portfolio_metrics(P.run_spec(sp, rank_override=rank, seg=(s, e)), cal)
        r_base = MT.portfolio_metrics(P.run_spec(base, seg=(s, e)), cal)
        rnd = []
        for sd in range(runs):
            rr = P.run_spec(replace(sp, rank="RANDOM", seed=10_000 + sd), seg=(s, e))
            eqv = rr["equity"][-1]
            rnd.append(eqv / rr["initial"] - 1)
        rows.append({"window_start": str(cal[s].date()), "window_end": str(cal[e].date()),
                     "sel_return": r_sel["total_return"], "base_return": r_base["total_return"],
                     "sel_mdd": r_sel["mdd"], "base_mdd": r_base["mdd"],
                     "sel_trades": r_sel["trades"], "base_trades": r_base["trades"],
                     "sel_pf": r_sel["profit_factor"], "base_pf": r_base["profit_factor"],
                     "random_percentile": P.percentile_of(r_sel["total_return"], np.array(rnd)),
                     "score_components": json.dumps(comps) if comps is not None else ""})
        ws = we
    return pd.DataFrame(rows)


def judge(sp: Spec, summary: pd.DataFrame, rnd: pd.DataFrame, wf: pd.DataFrame, boot: dict) -> dict:
    acc = C.cfg["acceptance"]

    def pick(spec):
        m = summary
        for k, v in asdict(spec).items():
            if k == "seed":
                continue
            m = m[m[k] == v]
        if m.empty:
            r = P.summarize(spec, P.run_spec(spec), "adhoc")
            return r
        return m.iloc[0].to_dict()

    base = Spec(topn=sp.topn)
    sel_t, base_t = pick(replace(sp, segment="test")), pick(replace(base, segment="test"))
    sel_f, base_f = pick(replace(sp, segment="full")), pick(replace(base, segment="full"))
    sel_u, base_u = pick(replace(sp, segment="full", horizon="UNL")), pick(replace(base, segment="full", horizon="UNL"))
    rd = rnd[(rnd["pool"] == sp.pool) & (rnd["market"] == sp.market) & (rnd["topn"] == sp.topn)
             & (rnd["segment"] == "test") & (rnd["horizon"] == "H60")]
    pct_test = P.percentile_of(sel_t["total_return"], rd["total_return"].to_numpy())
    rdf = rnd[(rnd["pool"] == sp.pool) & (rnd["market"] == sp.market) & (rnd["topn"] == sp.topn)
              & (rnd["segment"] == "full") & (rnd["horizon"] == "H60")]
    pct_full = P.percentile_of(sel_f["total_return"], rdf["total_return"].to_numpy())
    ys, yb = _yearly(replace(sp, segment="full")), _yearly(replace(base, segment="full"))
    years_better = sum(1 for y in ys if ys[y] >= yb.get(y, -math.inf))
    pf = lambda x: x if x is not None and x == x else 0.0     # noqa: E731
    checks = {
        "test_cagr_ge_baseline": sel_t["cagr"] >= base_t["cagr"],
        "test_pf_ge_baseline": pf(sel_t["profit_factor"]) >= pf(base_t["profit_factor"]),
        "test_mdd_within_tolerance": sel_t["mdd"] >= base_t["mdd"] - acc["test_mdd_tolerance"],
        "random_percentile_test_ge_min": pct_test >= acc["random_percentile_min"],
        "trades_per_year_ge_min": sel_f["trades_per_year"] >= acc["min_trades_per_year"],
        "years_better_ge_min": years_better >= acc["min_years_better"],
        "walk_forward_share_better": float((wf["sel_return"] >= wf["base_return"]).mean()) >= acc["wf_min_share_better"]
        if len(wf) else False,
        "operational_unl_cagr_gt": sel_u["cagr"] > acc["operational_unl"]["cagr_gt"],
        "operational_unl_pf_gt": pf(sel_u["profit_factor_incl_open"]) > acc["operational_unl"]["pf_gt"],
        "operational_unl_mdd_within_tolerance": sel_u["mdd"] >= base_u["mdd"] - acc["operational_unl"]["mdd_tolerance_vs_baseline"],
    }
    checks = {k: bool(v) for k, v in checks.items()}
    hard = ["test_cagr_ge_baseline", "test_pf_ge_baseline", "test_mdd_within_tolerance",
            "operational_unl_cagr_gt", "operational_unl_pf_gt", "operational_unl_mdd_within_tolerance"]
    soft = [k for k in checks if k not in hard]
    if all(checks.values()):
        status = "RECOMMENDED_FOR_SHADOW"
    elif all(checks[k] for k in hard):
        status = "NEED_MORE_DATA"
    else:
        status = "NOT_RECOMMENDED"
    return {"selected": asdict(sp), "baseline": asdict(base), "status": status, "checks": checks,
            "hard_checks": hard, "soft_checks": soft,
            "random_percentile_test": pct_test, "random_percentile_full": pct_full,
            "years_better": years_better, "yearly_selected": ys, "yearly_baseline": yb,
            "bootstrap_test_daily_diff_annualized": boot,
            "test": {"selected": _slim(sel_t), "baseline": _slim(base_t)},
            "full": {"selected": _slim(sel_f), "baseline": _slim(base_f)},
            "operational_unl_full": {"selected": _slim(sel_u), "baseline": _slim(base_u)},
            "walk_forward_share_better": float((wf["sel_return"] >= wf["base_return"]).mean()) if len(wf) else None}


KEEP = ["total_return", "cagr", "mdd", "sharpe", "calmar", "profit_factor", "profit_factor_incl_open", "win_rate",
        "stop_rate", "trades", "trades_per_year", "avg_R", "pnl_per_risk", "capital_utilization",
        "opportunity_reduction", "max_consec_stops", "heat_current_avg", "heat_current_max", "open_at_end",
        "open_unrealized_pnl", "open_capital_share", "open_avg_age_days", "avg_hold_days", "fill_rate", "pool_size"]


def _slim(d: dict) -> dict:
    return {k: (None if d.get(k) is None or (isinstance(d.get(k), float) and not math.isfinite(d.get(k))) else d.get(k))
            for k in KEEP}


def posthoc(runs: int) -> pd.DataFrame:
    """사후 탐색 (결과 확인 후 정의, 채택 근거 아님). 신호 단위 통제 분석에서 가장 일관된 시장 상태 요인."""
    rows = []
    variants = [("F0", "M1"), ("HARD_EXCL", "M0"), ("HARD_EXCL", "M1")]
    for pool, mk in variants:
        for n in (1, 2, 3, 999):
            for seg in ("train", "validation", "test", "full"):
                for hz in (("H60", "UNL") if seg in ("full", "test") else ("H60",)):
                    sp = Spec(pool=pool, market=mk, topn=n, segment=seg, horizon=hz)
                    m = P.summarize(sp, P.run_spec(sp), "posthoc")
                    if hz == "H60" and n <= 3 and seg in ("test", "full"):
                        rnd = P.random_baseline([sp], runs, 7000, workers=7)
                        m["random_percentile"] = P.percentile_of(m["total_return"], rnd["total_return"].to_numpy())
                        m["random_median"] = float(rnd["total_return"].median())
                    rows.append(m)
    return pd.DataFrame(rows)
