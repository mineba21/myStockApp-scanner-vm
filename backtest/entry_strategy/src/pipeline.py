"""P0~P5 실행 오케스트레이션."""
from __future__ import annotations

import json
import logging
import math
import multiprocessing as mp
import pickle
import time
from dataclasses import dataclass, asdict, replace
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from . import data as D
from . import fills as FL
from . import metrics as MT
from . import portfolio as PF
from . import strategies as ST

log = logging.getLogger("entry_strategy")

TIMINGS = ["T0", "T1_050", "T1_100", "T2", "T3"]
HORIZONS = ["H60", "H120", "UNL"]
SEGMENTS = ["train", "validation", "test", "full"]


# ── 상태 (프로세스 전역: fork 워커가 공유) ──────────────────────────
class Ctx:
    cfg: dict
    mkt: D.Market
    cand: pd.DataFrame
    sig: pd.DataFrame
    tas: dict
    orders: Dict[str, pd.DataFrame]
    exits: Dict[tuple, pd.DataFrame]
    feats: Dict[str, pd.DataFrame]
    pools: Dict[str, Dict[str, np.ndarray]]
    mkts: Dict[str, Dict[str, np.ndarray]]
    cuts: dict
    score_fit: dict
    ranks: Dict[str, np.ndarray]
    si_cache: Dict[tuple, PF.SimInput] = {}


C = Ctx()


@dataclass(frozen=True)
class Spec:
    profile: str = "P_C"
    pool: str = "F0"
    market: str = "M0"
    rank: str = "GRADE"
    topn: int = 999
    timing: str = "T1_100"
    horizon: str = "H60"
    scenario: str = "conservative"
    heat: str = "current"
    segment: str = "full"
    sizing: str = "risk"
    slip_bp: int = 10
    seed: Optional[int] = None

    def key(self) -> str:
        n = "ALL" if self.topn >= 999 else str(self.topn)
        base = f"{self.pool}|{self.market}|{self.rank}|N{n}"
        return base


def costs(slip_bp: int) -> dict:
    c = dict(C.cfg["costs"])
    c["slippage_bp"] = slip_bp
    return c


def profile(name: str) -> PF.Profile:
    p = dict(C.cfg["profiles"][name])
    p.pop("label", None)
    p.update(C.cfg["profiles"]["common"])
    return PF.Profile(**p)


# ── P0 ────────────────────────────────────────────────────────────

def prepare(cfg: dict) -> None:
    D.isolate()
    C.cfg = cfg
    t0 = time.time()
    C.mkt = D.load_market(cfg)
    C.sig = D.load_signals(cfg)
    C.cand = D.build_candidates(C.sig, C.mkt)
    C.tas = FL.ticker_arrays(C.mkt.frames, C.mkt.calendar)
    C.cuts = D.chronological_cuts(C.cand, C.mkt.calendar)
    C.orders, C.exits, C.feats, C.pools, C.mkts = {}, {}, {}, {}, {}
    cache = D.CACHE_DIR / "orders_exits.pkl"
    if cache.exists():
        with open(cache, "rb") as fh:
            saved = pickle.load(fh)
        if saved.get("n_cand") == len(C.cand):
            C.orders, C.exits = saved["orders"], saved["exits"]
    if not C.orders:
        for tm in TIMINGS:
            C.orders[tm] = FL.build_orders(C.cand, C.tas, tm, cfg)
            hz_list = HORIZONS if tm == "T1_100" else ["H60", "UNL"]
            for hz in hz_list:
                for sc in (["conservative", "next_day"] if tm == "T1_100" else ["conservative"]):
                    C.exits[(tm, hz, sc)] = FL.build_exits(C.cand, C.orders[tm], C.tas, hz, sc)
            log.info("orders/exits %s done (%.0fs)", tm, time.time() - t0)
        with open(cache, "wb") as fh:
            pickle.dump({"n_cand": len(C.cand), "orders": C.orders, "exits": C.exits}, fh)
    for tm in TIMINGS:
        f = ST.features_for_timing(C.cand, C.orders[tm])
        C.feats[tm] = f
        C.pools[tm] = ST.pool_masks(f, cfg)
        C.mkts[tm] = ST.market_masks(f, C.mkt.regime)
    log.info("prepare done: %d candidates (%.0fs)", len(C.cand), time.time() - t0)


def sim_input(timing: str, horizon: str, scenario: str) -> PF.SimInput:
    k = (timing, horizon, scenario)
    if k not in C.si_cache:
        C.si_cache[k] = PF.make_input(C.cand, C.orders[timing], C.exits[k])
    return C.si_cache[k]


def data_audit() -> dict:
    cand, sig, cal = C.cand, C.sig, C.mkt.calendar
    cuts = {k: [str(cal[a].date()), str(cal[b].date())] for k, (a, b) in C.cuts.items()}
    feats = ["rs_value", "volume_ratio", "weekly_volume_ratio", "ext_pct", "stop_pct", "slope150",
             "atr_pct", "dist_52w_high", "ma150_ext_pct"]
    # 미래 정보 점검: 결정 봉 ≤ 탐지일, 체결 봉 > 결정 봉
    o = C.orders["T1_100"]
    dec_dates = cal[o["decision_ci"].to_numpy()]
    fill_ok = o.dropna(subset=["fill_ci"])
    checks = {
        "decision_bar_le_detect_date": bool((dec_dates <= pd.to_datetime(cand.set_index("cid").loc[o.index, "detect_date"])).all()),
        "fill_after_decision_all_timings": {tm: bool((C.orders[tm].dropna(subset=["fill_ci"])["fill_ci"]
                                                      > C.orders[tm].dropna(subset=["fill_ci"])["decision_ci"]).all())
                                            for tm in TIMINGS},
        "exit_on_or_after_fill": bool(all(
            ((ex["exit_ci"].dropna() >= C.orders[k[0]].loc[ex["exit_ci"].dropna().index, "fill_ci"]).all())
            for k, ex in C.exits.items())),
        "signal_key_unique": bool(cand["signal_key"].is_unique),
    }
    return {
        "data_quality": "RECONSTRUCTED (현재 스캐너 로직 replay). VERIFIED_HISTORICAL 0건.",
        "source_signals": {"rows_us": int(len(sig)), "strict_passed": int((sig["strict_filter_passed"] == True).sum()),  # noqa: E712
                           "first_detection_rows": int((sig["first_seen"] == True).sum())},  # noqa: E712
        "candidates": int(len(cand)),
        "candidate_snapshot": {"strict_pass_equals_first_detection": int(cand["first_seen"].sum()),
                               "strict_pass_after_first_detection": int((~cand["first_seen"]).sum())},
        "detect_range": [str(cand["detect_date"].min().date()), str(cand["detect_date"].max().date())],
        "calendar_end": str(cal[-1].date()),
        "by_type": cand["signal_type"].value_counts().to_dict(),
        "by_grade": cand["grade"].value_counts().to_dict(),
        "by_quality": cand["signal_quality"].value_counts().to_dict(),
        "by_type_grade": {f"{a}/{b}": int(v) for (a, b), v in cand.groupby(["signal_type", "grade"]).size().items()},
        "by_market_condition": cand["market_condition"].value_counts().to_dict(),
        "by_year": {int(k): int(v) for k, v in cand.groupby(cand["detect_date"].dt.year).size().items()},
        "per_day": {"days": int(cand["detect_date"].nunique()),
                    "median": float(cand.groupby("detect_date").size().median()),
                    "max": int(cand.groupby("detect_date").size().max()),
                    "days_ge3": int((cand.groupby("detect_date").size() >= 3).sum())},
        "warning_flags": {k: int(cand[k].sum()) for k in D.WARN_PATTERNS},
        "feature_missing_rate": {k: float(cand[k].isna().mean()) for k in feats},
        "split_cuts": cuts,
        "split_counts": {k: int(((cand["decision_ci"] >= a) & (cand["decision_ci"] <= b)).sum())
                         for k, (a, b) in C.cuts.items()},
        "order_status": {tm: C.orders[tm]["status"].value_counts().to_dict() for tm in TIMINGS},
        "lookahead_checks": checks,
        "limitations": [
            "현재 S&P500+Nasdaq100 구성종목 유니버스 → 생존 편향 (절대 수익률 과대 가능, 전략 간 상대 비교 위주 해석)",
            "기존 exit_strategy 전체 기간 결과를 이미 본 뒤의 설계 → Test 구간은 완전한 미관측 데이터가 아님",
            "비용은 가정값 (편도 0.25% + 10bp slippage)",
            "일봉만 사용 → 장중 순서 불명 구간은 보수/대안 가정 병기",
            "suggested_qty 과거값은 보유 상태 의존이라 재구성 불가 → 운영 calculate_position 으로 재계산",
            "Sector Stage 는 매수 조건에서 제외 (표시 전용 정책 유지)",
        ],
    }


# ── P1 신호 단위 분석 ───────────────────────────────────────────────

def signal_outcomes(timing="T1_100", horizon="H60", scenario="conservative", end_ci=None) -> pd.DataFrame:
    """체결된 후보의 1주 기준 결과. end_ci 가 있으면 그날 이후 가격 미참조(절단 평가)."""
    o = C.orders[timing]
    rows = []
    c = costs(C.cfg["costs"]["slippage_bp"])
    hz = FL.HORIZON_DAYS[horizon]
    for r in C.cand.itertuples(index=False):
        od = o.loc[r.cid]
        if od["status"] != "FILLED":
            continue
        ta = C.tas[r.ticker]
        if end_ci is not None and od["fill_ci"] > end_ci:
            continue
        end_pos = None
        if end_ci is not None:
            end_pos = int(np.searchsorted(ta.cal_idx, end_ci, side="right") - 1)
        e = FL.s01_exit(ta, int(od["fill_pos"]), r.stop, hz, scenario, end_pos)
        px = e["exit_px"] if e["exit_pos"] is not None else e["mark_px"]
        out = FL.trade_outcome(od["fill_px"], r.stop, px, c)
        rows.append({"cid": r.cid, **out, "reason": e["reason"], "day0": e["day0"]})
    res = pd.DataFrame(rows)
    return C.cand.merge(res, on="cid")


def _bucket_stats(df: pd.DataFrame, col: str, label: str, extra: dict) -> List[dict]:
    rows = []
    for b, g in df.groupby(col, dropna=False, observed=True):
        rows.append({"analysis": label, "bucket": str(b), **extra, **MT.trade_stats(g)})
    return rows


def factor_study() -> pd.DataFrame:
    rows = []
    tr_end = C.cuts["train"][1]
    sets = {("H60", "full"): signal_outcomes(horizon="H60"),
            ("H120", "full"): signal_outcomes(horizon="H120"),
            ("UNL", "full"): signal_outcomes(horizon="UNL"),
            ("H60", "train_truncated"): signal_outcomes(horizon="H60", end_ci=tr_end)}
    for (hz, per), df in sets.items():
        if per == "train_truncated":
            df = df[df["decision_ci"] <= tr_end]
        df = df.copy()
        df["f1"] = C.pools["T1_100"]["F1"][df["cid"].to_numpy()]
        df["stop_bin"] = pd.cut(df["stop_pct"], [0, 7, 10, 12, 15, 100], labels=["≤7", "7-10", "10-12", "12-15", ">15"])
        df["ext_bin"] = pd.cut(df["ext_pct"], [-100, 0, 3, 5, 7, 100], labels=["<0", "0-3", "3-5", "5-7", ">7"])
        for c in ("rs_value", "volume_ratio", "weekly_volume_ratio", "slope150", "atr_pct", "dist_52w_high"):
            df[c + "_q"] = pd.qcut(df[c], 5, labels=False, duplicates="drop")
        df["year"] = df["detect_date"].dt.year
        df["type_grade"] = df["signal_type"] + "/" + df["grade"]
        df["type_quality"] = df["signal_type"] + "/" + df["signal_quality"]
        df["type_mc_grade"] = df["signal_type"] + "/" + df["market_condition"].astype(str) + "/" + df["grade"]
        df["type_mc_quality"] = df["signal_type"] + "/" + df["market_condition"].astype(str) + "/" + df["signal_quality"]
        df["type_stop_bin"] = df["signal_type"] + "/" + df["stop_bin"].astype(str)
        df["type_ext_bin"] = df["signal_type"] + "/" + df["ext_bin"].astype(str)
        ex = {"horizon": hz, "period": per}
        rows.append({"analysis": "all", "bucket": "strict_all", **ex, **MT.trade_stats(df)})
        for col, lab in [("signal_type", "type"), ("grade", "grade"), ("signal_quality", "quality"),
                         ("type_grade", "type×grade"), ("type_quality", "type×quality"),
                         ("type_mc_grade", "type×market×grade"), ("type_mc_quality", "type×market×quality"),
                         ("market_condition", "market_condition"), ("stop_bin", "stop_pct"),
                         ("type_stop_bin", "type×stop_pct"), ("ext_bin", "ext_pct"), ("type_ext_bin", "type×ext_pct"),
                         ("first_seen", "strict_pass_is_first_detection"), ("year", "year"), ("f1", "F1_eligible"),
                         ("rs_rising", "rs_rising"), ("base_strong", "base_strong")] + \
                        [(c + "_q", c + "_quintile") for c in ("rs_value", "volume_ratio", "weekly_volume_ratio",
                                                               "slope150", "atr_pct", "dist_52w_high")]:
            rows += _bucket_stats(df, col, lab, ex)
        for w in D.WARN_PATTERNS:
            rows += _bucket_stats(df, w, "warn:" + w, ex)
            for t, g in df.groupby("signal_type"):
                rows += _bucket_stats(g, w, f"warn:{w}|{t}", ex)
    return pd.DataFrame(rows)


def controlled_grade_quality() -> pd.DataFrame:
    """신호 유형·시장 상태 통제 OLS: R ~ type + market + grade(또는 quality). 주 단위 블록 bootstrap CI."""
    df = signal_outcomes(horizon="H60")
    df = df[np.isfinite(df["R"])].copy()
    df["week"] = df["detect_date"].dt.to_period("W").astype(str)
    out = []
    for factor, levels, ref in [("grade", ["S", "A"], "B"), ("signal_quality", ["STRONG", "WEAK"], "MODERATE")]:
        X_cols = []
        X = pd.DataFrame(index=df.index)
        X["const"] = 1.0
        for t in ("BREAKOUT", "REBOUND"):
            X[f"type_{t}"] = (df["signal_type"] == t).astype(float)
        for mcond in ("NEUTRAL", "CAUTION"):
            X[f"mc_{mcond}"] = (df["market_condition"] == mcond).astype(float)
        for lv in levels:
            X[f"{factor}_{lv}"] = (df[factor] == lv).astype(float)
        X = X.loc[:, X.std() > 0].assign(const=1.0)
        y = df["R"].to_numpy()

        def fit(idx):
            b, *_ = np.linalg.lstsq(X.to_numpy()[idx], y[idx], rcond=None)
            return b
        base = fit(np.arange(len(df)))
        weeks = df["week"].to_numpy()
        uw = np.unique(weeks)
        wk_idx = {w: np.where(weeks == w)[0] for w in uw}
        rng = np.random.default_rng(5)
        bs = []
        for _ in range(500):
            pick = rng.choice(uw, size=len(uw), replace=True)
            idx = np.concatenate([wk_idx[w] for w in pick])
            bs.append(fit(idx))
        bs = np.array(bs)
        for j, col in enumerate(X.columns):
            out.append({"model": f"R ~ type + market + {factor} (ref {ref})", "term": col,
                        "coef_R": float(base[j]), "ci05": float(np.percentile(bs[:, j], 5)),
                        "ci95": float(np.percentile(bs[:, j], 95)), "n": int(len(df))})
    return pd.DataFrame(out)


def entry_timing_study() -> pd.DataFrame:
    rows = []
    for tm in TIMINGS:
        o = C.orders[tm]
        for pool in ("F0", "F1"):
            m = C.pools[tm][pool]
            sub = o.loc[C.cand["cid"].to_numpy()[m]]
            st = sub["status"].value_counts()
            df = signal_outcomes(timing=tm, horizon="H60")
            df = df[C.pools[tm][pool][df["cid"].to_numpy()]]
            fill_open = (sub["fill_kind"] == "open").mean()
            rows.append({"timing": tm, "pool": pool, "candidates": int(m.sum()),
                         "filled": int(st.get("FILLED", 0)), "unfilled": int(st.get("UNFILLED", 0)),
                         "skip_open_stop": int(st.get("SKIP_OPEN_STOP", 0)),
                         "skip_upthrust": int(st.get("SKIP_UPTHRUST", 0)),
                         "skip_no_bar": int(st.get("SKIP_NO_BAR", 0)),
                         "fill_rate": float(st.get("FILLED", 0) / max(1, m.sum())),
                         "fill_at_open_share": float(fill_open),
                         "day0_stop_rate": float(df["day0"].mean()) if len(df) else None,
                         **{f"H60_{k}": v for k, v in MT.trade_stats(df).items()}})
    # 같은 봉 체결·손절 가정 비교 (T1_100)
    for sc in ("conservative", "next_day"):
        df = signal_outcomes(timing="T1_100", horizon="H60", scenario=sc)
        rows.append({"timing": f"T1_100[{sc}]", "pool": "F0", "candidates": int(len(df)),
                     "day0_stop_rate": float(df["day0"].mean()),
                     **{f"H60_{k}": v for k, v in MT.trade_stats(df).items()}})
    return pd.DataFrame(rows)


# ── 포트폴리오 실행 ────────────────────────────────────────────────

def rank_array(name: str, timing: str, seed: Optional[int] = None) -> np.ndarray:
    if name == "GRADE":
        return ST.grade_rank(C.feats[timing]).astype(float)
    if name == "RANDOM":
        return np.random.default_rng(seed).permutation(len(C.cand)).astype(float)
    if name in ("SC1", "SC2", "SC3"):
        fit = C.score_fit
        return -ST.score(C.feats[timing], fit[name], fit["ref"])
    if name == "FIFO":
        return np.zeros(len(C.cand))
    raise ValueError(name)


def eligible_mask(spec: Spec) -> np.ndarray:
    pools = C.pools[spec.timing]
    m = np.ones(len(C.cand), bool)
    for p in spec.pool.split("+"):
        m &= pools[p]
    return m & C.mkts[spec.timing][spec.market]


def run_spec(spec: Spec, rank_override: Optional[np.ndarray] = None, seg: Optional[tuple] = None) -> dict:
    si = sim_input(spec.timing, spec.horizon, spec.scenario)
    s, e = seg or C.cuts[spec.segment]
    rank = rank_override if rank_override is not None else rank_array(spec.rank, spec.timing, spec.seed)
    res = PF.run(si, C.mkt.closes, s, e, eligible_mask(spec), rank, spec.topn, profile(spec.profile),
                 costs(spec.slip_bp), heat_def=spec.heat, sizing_mode=spec.sizing)
    return res


def summarize(spec: Spec, res: dict, role: str) -> dict:
    m = MT.portfolio_metrics(res, C.mkt.calendar)
    return {"role": role, "strategy": spec.key(), **asdict(spec), **m}


def _random_worker(args):
    spec, seeds = args
    out = []
    for sd in seeds:
        res = run_spec(replace(spec, rank="RANDOM", seed=sd))
        m = MT.portfolio_metrics(res, C.mkt.calendar)
        out.append({"seed": sd, "total_return": m["total_return"], "cagr": m["cagr"], "mdd": m["mdd"],
                    "profit_factor": m["profit_factor"], "avg_R": m["avg_R"], "trades": m["trades"]})
    return spec, out


def random_baseline(specs: List[Spec], runs: int, seed0: int, workers: int = 7) -> pd.DataFrame:
    jobs = []
    chunk = max(1, runs // workers)
    for sp in specs:
        seeds = list(range(seed0, seed0 + runs))
        for i in range(0, runs, chunk):
            jobs.append((sp, seeds[i:i + chunk]))
    rows = []
    ctx = mp.get_context("fork")
    with ctx.Pool(workers) as pool:
        for sp, out in pool.imap_unordered(_random_worker, jobs):
            for r in out:
                rows.append({"pool": sp.pool, "market": sp.market, "topn": sp.topn, "segment": sp.segment,
                             "profile": sp.profile, "horizon": sp.horizon, **r})
    return pd.DataFrame(rows)


def percentile_of(value: float, dist: np.ndarray) -> float:
    dist = dist[np.isfinite(dist)]
    if not len(dist) or value is None:
        return math.nan
    return float((dist < value).mean() * 100 + (dist == value).mean() * 50)
