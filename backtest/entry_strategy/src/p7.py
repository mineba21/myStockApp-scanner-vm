"""P7 Heat·위험 예산 추가 검증 (configs p7 사전 등록 그리드만 실행)."""
from __future__ import annotations

import json
import logging
import math
import multiprocessing as mp
import pickle
from dataclasses import asdict, dataclass, replace
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from . import data as D
from . import metrics as MT
from . import pipeline as P
from . import risk_portfolio as RP
from .pipeline import C

log = logging.getLogger("entry_strategy")


@dataclass(frozen=True)
class RSpec:
    strategy: str = "B01"
    profile: str = "BAL"
    horizon: str = "UNL"
    segment: str = "full"
    slip_bp: int = 10
    min_cash: Optional[float] = None        # None = 프로필 값
    current_block: Optional[float] = None
    manual_pct: float = 0.0
    manual_risk: float = 0.0
    risk: Optional[float] = None            # OAT 덮어쓰기
    heat: Optional[float] = None
    poscap: Optional[float] = None
    random_seed: Optional[int] = None
    group: str = "main"


def sectors() -> Dict[str, str]:
    path = D.CACHE_DIR / "sectors.json"
    if path.exists():
        return json.load(open(path, encoding="utf-8"))
    with open(D.REPO_ROOT / C.cfg["data"]["ohlcv_pkl"], "rb") as fh:
        meta = pickle.load(fh)["meta"]
    out = {t: (m.get("sector") or "Unknown") for t, m in meta.items()}
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False)
    return out


_SECT: Dict[str, str] = {}


def rprofile(rs: RSpec) -> RP.RiskProfile:
    p7 = C.cfg["p7"]
    p = dict(p7["profiles"][rs.profile])
    p.pop("label", None)
    p.update(p7["common"])
    if rs.min_cash is not None:
        p["min_cash_pct"] = rs.min_cash
    if rs.risk is not None:
        p["RISK_PCT"] = rs.risk
    if rs.heat is not None:
        p["MAX_TOTAL_HEAT_PCT"] = rs.heat
    if rs.poscap is not None:
        p["MAX_POSITION_PCT"] = rs.poscap
    return RP.RiskProfile(equity=p7["equity"], current_block=rs.current_block,
                          current_warn=p7["current_heat_warn"], manual_pct=rs.manual_pct,
                          manual_risk_pct=rs.manual_risk, **p)


def strat_spec(name: str) -> P.Spec:
    s = C.cfg["p7"]["strategies"][name]
    return P.Spec(pool=s["pool"], market=s["market"], rank=s["rank"], topn=s["topn"])


def run_r(rs: RSpec) -> dict:
    global _SECT
    if not _SECT:
        _SECT = sectors()
    sp = strat_spec("B01" if rs.strategy == "B02" else rs.strategy)
    si = P.sim_input("T1_100", rs.horizon, "conservative")
    s, e = C.cuts[rs.segment]
    rank = P.rank_array("RANDOM" if rs.random_seed is not None else sp.rank, "T1_100", rs.random_seed)
    return RP.run(si, C.mkt.closes, s, e, P.eligible_mask(sp), rank, sp.topn, rprofile(rs), P.costs(rs.slip_bp),
                  _SECT, manual_series=C.mkt.regime["spy_close"].to_numpy())


def _adapt(res: dict) -> dict:
    r = res["rec"]
    return {"equity": r["equity"], "invested": r["invested"] + r["manual_value"], "positions": res["positions"],
            "heat_current": r["heat_current"], "heat_entry": r["heat_entry"], "trades": res["trades"],
            "open": res["open"], "counts": {"orders": res["counts"]["orders"], "filled": res["counts"]["filled"]},
            "size_reasons": {}, "start": res["start"], "end": res["end"], "initial": res["initial"],
            "pool_size": res["pool_size"]}


def metrics(res: dict) -> dict:
    m = MT.portfolio_metrics(_adapt(res), C.mkt.calendar)
    r = res["rec"]
    tr = pd.DataFrame(res["trades"] + res["open"])
    gaps = tr[tr["reason"] == "STOP_GAP"] if len(tr) else tr
    hc, he = r["heat_current"], r["heat_entry"]
    with np.errstate(divide="ignore", invalid="ignore"):
        gain_share = np.where(hc > 0, np.clip((hc - he) / hc, 0, 1), np.nan)
    auto_util = (r["invested"] / r["equity"]).mean()
    m.update({
        "auto_capital_utilization": float(auto_util),
        "heat_entry_avg": float(he.mean()), "heat_entry_max": float(he.max()),
        "heat_current_avg": float(hc.mean()), "heat_current_max": float(hc.max()),
        "heat_current_gain_share": float(np.nanmean(gain_share)) if np.isfinite(gain_share).any() else None,
        "max_pos_weight_avg": float(r["max_pos_w"].mean()), "max_pos_weight_max": float(r["max_pos_w"].max()),
        "max_sector_weight_avg": float(r["max_sector_w"].mean()),
        "max_sector_weight_max": float(r["max_sector_w"].max()),
        "cash_ratio_min": float(r["cash_ratio"].min()), "cash_ratio_avg": float(r["cash_ratio"].mean()),
        "current_warn_day_share": res["warn_days"] / len(r["equity"]),
        "breached_days": res["breached_days"], "worst_day": res["worst_day"],
        "gap_stops": int(len(gaps)),
        "gap_avg_R": float(gaps["R"].mean()) if len(gaps) else None,
        "gap_worst_R": float(gaps["R"].min()) if len(gaps) else None,
        "gap_excess_loss": float(tr["gap_excess"].sum()) if len(tr) else 0.0,
        "R_p10": float(tr["R"].quantile(0.10)) if len(tr) else None,
        "R_p50": float(tr["R"].median()) if len(tr) else None,
        "R_p90": float(tr["R"].quantile(0.90)) if len(tr) else None,
        "top3_pnl_share": (float(tr["pnl"].nlargest(3).sum() / tr["pnl"].sum())
                           if len(tr) and tr["pnl"].sum() > 0 else None),
        "decided": res["counts"]["decided"],
        "orders": res["counts"]["orders"],
    })
    for k, v in res["block"].items():
        m[f"block_{k}"] = v
    for k, v in res["bind"].items():
        m[f"bind_{k}"] = v
    return m


def registry() -> List[RSpec]:
    p7 = C.cfg["p7"]
    out: List[RSpec] = []
    for st in p7["strategies"]:
        for pr in p7["profiles"]:
            for hz in p7["horizons"]:
                for seg in p7["segments"]:
                    out.append(RSpec(st, pr, hz, seg, group="main"))
    for pr in p7["min_cash_check"]:
        for st in ("B00N", "B01"):
            for hz in p7["horizons"]:
                out.append(RSpec(st, pr, hz, "full", min_cash=0.0, group="min_cash0"))
    o = p7["oat"]
    for st in o["strategies"]:
        for hz in o["horizons"]:
            for v in o["RISK_PCT"]:
                out.append(RSpec(st, o["base"], hz, risk=v, group="oat_RISK_PCT"))
            for v in o["MAX_TOTAL_HEAT_PCT"]:
                out.append(RSpec(st, o["base"], hz, heat=v, group="oat_MAX_TOTAL_HEAT_PCT"))
            for v in o["MAX_POSITION_PCT"]:
                out.append(RSpec(st, o["base"], hz, poscap=v, group="oat_MAX_POSITION_PCT"))
            for v in o["min_cash_pct"]:
                out.append(RSpec(st, o["base"], hz, min_cash=v, group="oat_min_cash_pct"))
    for cb in p7["current_heat_block"]:
        for st in ("B00N", "B01"):
            for hz in p7["horizons"]:
                out.append(RSpec(st, "BAL", hz, current_block=cb, group="current_block"))
    ms = p7["manual_holdings_stress"]
    for ex in ms["exposure_pct"]:
        for rk in ms["assumed_risk_pct"]:
            if ex == 0 and rk > 0:
                continue
            for st in ("B00N", "B01"):
                for hz in ("H60", "UNL"):
                    out.append(RSpec(st, "BAL", hz, manual_pct=ex, manual_risk=rk, group="manual"))
    for sb in p7["slippage_bp"]:
        for pr in ("CONS", "BAL", "AGG", "LEG_ENT"):
            for st in ("B00N", "B01"):
                for hz in ("H60", "UNL"):
                    out.append(RSpec(st, pr, hz, slip_bp=sb, group="slippage"))
    return out


def run_registry(specs: List[RSpec]) -> pd.DataFrame:
    rows = []
    for rs in specs:
        res = run_r(rs)
        prof = rprofile(rs)
        rows.append({**asdict(rs), "RISK_PCT": prof.RISK_PCT, "HEAT_PCT": prof.MAX_TOTAL_HEAT_PCT,
                     "POS_PCT": prof.MAX_POSITION_PCT, "MIN_CASH": prof.min_cash_pct, "heat_def": prof.heat_def,
                     **metrics(res)})
    return pd.DataFrame(rows)


def _rand_worker(args):
    rs, seeds = args
    out = []
    for sd in seeds:
        m = metrics(run_r(replace(rs, strategy="B02", random_seed=sd)))
        out.append({"seed": sd, **{k: m[k] for k in ("total_return", "cagr", "mdd", "profit_factor", "avg_R",
                                                      "trades", "calmar")}})
    return rs, out


def random_b02(workers: int = 7) -> pd.DataFrame:
    p7 = C.cfg["p7"]
    runs, s0 = p7["random_runs"], p7["random_seed"]
    jobs = []
    chunk = max(1, runs // workers)
    seeds = list(range(s0, s0 + runs))
    for pr in p7["profiles"]:
        for hz in p7["horizons"]:
            for seg in p7["segments"]:
                rs = RSpec("B02", pr, hz, seg)
                for i in range(0, runs, chunk):
                    jobs.append((rs, seeds[i:i + chunk]))
    rows = []
    with mp.get_context("fork").Pool(workers) as pool:
        for rs, out in pool.imap_unordered(_rand_worker, jobs):
            for r in out:
                rows.append({"profile": rs.profile, "horizon": rs.horizon, "segment": rs.segment, **r})
    return pd.DataFrame(rows)


def b01_vs_random(main: pd.DataFrame, rnd: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (pr, hz, seg), g in rnd.groupby(["profile", "horizon", "segment"]):
        b = main[(main.strategy == "B01") & (main.profile == pr) & (main.horizon == hz) & (main.segment == seg)]
        if b.empty:
            continue
        b = b.iloc[0]
        row = {"profile": pr, "horizon": hz, "segment": seg}
        for k in ("total_return", "profit_factor", "mdd", "calmar"):
            d = g[k].to_numpy(float)
            row[f"B01_{k}"] = b[k]
            row[f"B02_{k}_p50"] = float(np.nanmedian(d))
            row[f"B02_{k}_p05"] = float(np.nanpercentile(d, 5))
            row[f"B02_{k}_p95"] = float(np.nanpercentile(d, 95))
            row[f"B01_{k}_pct"] = P.percentile_of(b[k], d) if b[k] == b[k] and b[k] is not None else None
        rows.append(row)
    return pd.DataFrame(rows)


def daily_series() -> pd.DataFrame:
    cal = C.mkt.calendar
    rows = []
    for st in ("B00N", "B01"):
        for pr in C.cfg["p7"]["profiles"]:
            for hz in ("H60", "UNL"):
                res = run_r(RSpec(st, pr, hz, "full"))
                r = res["rec"]
                s, e = res["start"], res["end"]
                eq = r["equity"]
                rows.append(pd.DataFrame({
                    "date": cal[s:e + 1].strftime("%Y-%m-%d"), "series": f"{st}|{pr}|{hz}", "equity": eq.round(2),
                    "drawdown": (eq / np.maximum.accumulate(eq) - 1).round(5),
                    "heat_entry": r["heat_entry"].round(5), "heat_current": r["heat_current"].round(5),
                    "utilization": (r["invested"] / eq).round(4), "max_sector_w": r["max_sector_w"].round(4),
                    "positions": res["positions"]}))
    return pd.concat(rows, ignore_index=True)


def unl_open_positions() -> pd.DataFrame:
    """운영형(UNL) 기존형 현재가 Heat 에서 Heat 를 점유한 미청산 포지션."""
    cal = C.mkt.calendar
    rows = []
    for st in ("B00N", "B01"):
        for pr in ("LEG_CUR", "LEG_ENT", "BAL"):
            res = run_r(RSpec(st, pr, "UNL", "full"))
            eqv = res["rec"]["equity"][-1]
            for t in res["open"]:
                rows.append({"strategy": st, "profile": pr, "ticker": t["ticker"],
                             "sector": _SECT.get(t["ticker"], "Unknown"),
                             "entry_date": str(cal[t["entry_ci"]].date()), "hold_days": t["hold_days"],
                             "entry_px": t["entry_px"], "stop": t["stop"], "last_px": t["exit_px"], "qty": t["qty"],
                             "R_now": t["R"], "weight": t["mkt_value"] / eqv,
                             "current_risk_pct": max(0, t["exit_px"] - t["stop"]) * t["qty"] / eqv,
                             "entry_risk_pct": max(0, t["entry_px"] - t["stop"]) * t["qty"] / eqv})
    return pd.DataFrame(rows)


def conclusions(reg: pd.DataFrame, cmp_: pd.DataFrame) -> dict:
    """P7 결론 수치 (결과 파일에서 직접 계산; 보고서·문서가 같은 값을 쓴다)."""
    m = reg[(reg.group == "main") & (reg.segment == "full")]

    def g(st, pr, hz, k):
        x = m[(m.strategy == st) & (m.profile == pr) & (m.horizon == hz)]
        return None if x.empty or x.iloc[0][k] != x.iloc[0][k] else float(x.iloc[0][k])

    unl_cur = m[(m.profile == "LEG_CUR") & (m.horizon == "UNL")]
    unl_ent = m[(m.profile.isin(["LEG_ENT", "BAL"])) & (m.horizon == "UNL")]
    cb = reg[(reg.group == "current_block") & (reg.horizon == "UNL")]
    man = reg[(reg.group == "manual") & (reg.manual_pct > 0)]
    b01_h = cmp_[(cmp_.segment == "full")]
    return {
        "verdict_maintained": "NOT_RECOMMENDED",
        "b01_status": "NEED_MORE_DATA (전향적 SHADOW 전용)",
        "unl_current_heat_trades": [int(unl_cur.trades.min()), int(unl_cur.trades.max())],
        "unl_current_heat_gain_share": [float(unl_cur.heat_current_gain_share.min()),
                                        float(unl_cur.heat_current_gain_share.max())],
        "unl_entry_heat_trades": [int(unl_ent.trades.min()), int(unl_ent.trades.max())],
        "unl_entry_open_capital_share": [float(unl_ent.open_capital_share.min()),
                                         float(unl_ent.open_capital_share.max())],
        "unl_entry_max_sector": [float(unl_ent.max_sector_weight_max.min()),
                                 float(unl_ent.max_sector_weight_max.max())],
        "unl_entry_mdd": [float(unl_ent.mdd.min()), float(unl_ent.mdd.max())],
        "current_block_unl_trades": {str(k if k == k else "none"): int(v) for k, v in
                                     cb[cb.strategy == "B00N"].groupby(cb.current_block.fillna(-1))["trades"].first()
                                     .rename(index={-1: float("nan")}).items()},
        "manual30_risk15_trades": int(man[man.manual_risk == 15].trades.max()),
        "manual30_risk0_trades": [int(man[man.manual_risk == 0].trades.min()), int(man[man.manual_risk == 0].trades.max())],
        "b01_vs_b02_full_pct": {f"{r.profile}|{r.horizon}": r.B01_total_return_pct for r in b01_h.itertuples()},
        "b01_vs_b01hx_unl_bal": [g("B01", "BAL", "UNL", "total_return"), g("B01HX", "BAL", "UNL", "total_return")],
        "trades_per_year_bal": {f"{st}|{hz}": g(st, "BAL", hz, "trades_per_year")
                                for st in ("B00N", "B01") for hz in ("H60", "UNL")},
        "recommended_shadow_profile": "BAL (거래당 0.75%, Entry Heat 4%, 종목 15%, 현금 하한 20%) + CONS 병행 선택",
    }
