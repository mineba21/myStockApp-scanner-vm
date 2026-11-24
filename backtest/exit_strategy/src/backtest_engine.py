"""백테스트 오케스트레이션.

1. 신호 적재 (RECONSTRUCTED / SNAPSHOT_ONLY 분리)
2. 매수 진입 생성 (탐지일 다음 거래일 시가)
3. 전략 카탈로그 실행 (병렬)
4. 그룹·기간·유형별 집계, Train/Validation/Test 선택, walk-forward, bootstrap
5. 포트폴리오 시뮬레이션, 사례 선정, 권장 규칙 산출
"""
from __future__ import annotations

import json
import logging
import os
import pickle
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .common import OUTPUT_DIR
from .entry_builder import IndicatorCache, build_trade, mark_overlaps, snapshot_factor
from .execution_simulator import Costs, simulate
from .exit_rules import (BreakoutFailure, ExitConfig, HardStop, Partial, catalog, hybrid_configs)
from .market_data_loader import load_cached
from .parameter_search import (block_bootstrap, chronological_split, objective, purge_overlap,
                               rank_stability, select_by_family, walk_forward)
from .performance_metrics import trade_metrics
from .portfolio_simulator import run_portfolio
from .signal_loader import load_snapshot_signals, snapshot_detect_date

log = logging.getLogger(__name__)

COMPACT_COLS = ["return", "holding_days", "exit_reason", "first_exit_reason", "mfe", "mae", "giveback",
                "capture", "cost", "gap_excess", "open_qty", "partial_component",
                "realized_return_component", "abnormal_entry", "ambiguous_bar", "incomplete_horizon",
                "review_flags", "exit_date", "initial_stop", "n_fills",
                "post_5d", "post_10d", "post_20d", "post_60d", "post_recover_entry", "post_new_high",
                "post_further_decline"]

CORE = ["S00", "S01", "S02_fix7", "S03_scap7", "S03_atr2cap7", "S04B_t1_wall", "S04A_t0_w5",
        "S04F_upthrust", "S05_ma50x2", "S05_stage3", "S06_a10_d8", "S06_a10_atr3",
        "S07_a10_d6_r50_trail_pct10", "S07T_t10_r50_trail_pct10", "S08_20d_lt2", "S09A", "S09B"]

GROUPS_STRICT = {
    "B_strict": lambda s: s["strict_filter_passed"],
    "C_gradeA": lambda s: s["strict_filter_passed"] & (s["grade"] == "A"),
    "C_gradeS": lambda s: s["strict_filter_passed"] & (s["grade"] == "S"),
    "D_gradeB": lambda s: s["strict_filter_passed"] & (s["grade"] == "B"),
    "E_strong": lambda s: s["strict_filter_passed"] & (s["signal_quality"] == "STRONG"),
    "F_moderate": lambda s: s["strict_filter_passed"] & (s["signal_quality"] == "MODERATE"),
    "G_warn": lambda s: s["strict_filter_passed"] & s["has_entry_warnings"],
    "G_nowarn": lambda s: s["strict_filter_passed"] & ~s["has_entry_warnings"],
}


# ── 병렬 실행 ────────────────────────────────────────────────────

_G: dict = {}


def _init(trades, costs):
    _G["trades"] = trades
    _G["costs"] = costs


def _run_task(task):
    cfg, horizon, slip_bp, scenario, keep_fills, subset = task
    out = []
    for sig_idx, td in _G["trades"]:
        if subset is not None and sig_idx not in subset:
            continue
        base = _G["costs"][td.market]
        costs = Costs(base.buy, base.sell, slip_bp / 10000.0)
        r = simulate(td, cfg, horizon, costs, scenario=scenario)
        row = {k: r.get(k) for k in COMPACT_COLS}
        row.update({"sig_idx": sig_idx, "entry_price": r["entry_price"],
                    "horizon_end_date": td.dates[min(horizon, td.n) - 1],
                    "last_close": td.c[min(horizon, td.n) - 1]})
        if keep_fills:
            row["fills"] = r["fills"]
        out.append(row)
    if not out:
        return None
    df = pd.DataFrame(out)
    # 메모리 절감: 실수형은 float32, 반복 문자열은 category (KR 수백만 행 대비)
    for c in df.columns:
        if df[c].dtype == "float64" and c not in ("entry_price", "last_close", "initial_stop"):
            df[c] = df[c].astype("float32")
    df["strategy"] = cfg.name
    df["family"] = cfg.family
    df["horizon"] = horizon
    df["slip_bp"] = slip_bp
    df["scenario"] = scenario
    return df


def _costs(cfg) -> Dict[str, Costs]:
    c = cfg["costs"]
    return {m: Costs(c[m]["buy_commission"], c[m]["sell_commission"] + c[m]["sell_tax"] + c[m]["fx_cost"], 0.0)
            for m in ("US", "KR")}


def run_tasks(trades, costs, tasks, workers=None, chunk=3000) -> pd.DataFrame:
    """거래를 chunk 단위로 나눠 fork 워커가 해당 chunk 객체만 건드리게 한다.

    (전체 경로 리스트를 모든 워커가 순회하면 참조 카운트 갱신으로 copy-on-write 가 일어나
    워커마다 전체 사본이 생긴다 — KR 3만 건에서 swap 고갈 확인.)
    """
    import multiprocessing as mp

    workers = workers or max(1, min(8, (os.cpu_count() or 2) - 1))
    parts = []
    n_chunks = (len(trades) + chunk - 1) // chunk
    for ci in range(n_chunks):
        part = trades[ci * chunk:(ci + 1) * chunk]
        ids = {i for i, _ in part}
        sub_tasks = []
        for t in tasks:
            subset = t[5]
            if subset is None or not ids.isdisjoint(subset):
                sub_tasks.append(t)
        _init(part, costs)
        with ProcessPoolExecutor(max_workers=workers, mp_context=mp.get_context("fork")) as pool:
            for df in pool.map(_run_task, sub_tasks, chunksize=4):
                if df is not None:
                    parts.append(df)
        log.info("chunk %d/%d done (%d tasks, %d frames)", ci + 1, n_chunks, len(sub_tasks), len(parts))
    _G.clear()
    return pd.concat(parts, ignore_index=True, sort=False)


# ── 신호 → 진입 ──────────────────────────────────────────────────

def build_signal_table(cfg, market: str, payload: dict) -> pd.DataFrame:
    """RECONSTRUCTED + SNAPSHOT_ONLY 를 한 표로 (source 컬럼으로 구분, 지표 혼용 없음)."""
    rec_path = Path(cfg["data"]["cache_dir"]) / f"reconstructed_{market}.pkl"
    parts = []
    if rec_path.exists():
        rec = pd.read_pickle(rec_path)
        rec["source"] = "RECONSTRUCTED"
        parts.append(rec)
    snap = load_snapshot_signals(cfg["snapshot"]["mcp_dump"])
    snap = snap[snap["market"] == market].copy()
    snap["detect_date"] = snap.apply(lambda r: snapshot_detect_date(r, {}), axis=1)
    snap["first_seen"] = True
    parts.append(snap)
    sig = pd.concat(parts, ignore_index=True, sort=False)
    sig["has_entry_warnings"] = sig["has_entry_warnings"].fillna(False).astype(bool)
    sig["strict_filter_passed"] = sig["strict_filter_passed"].fillna(False).astype(bool)
    return sig


def build_trades(sig: pd.DataFrame, payload: dict, bench: pd.Series, max_days: int):
    cache = IndicatorCache(payload["frames"], bench)
    trades, meta_rows = [], []
    for i, row in sig.iterrows():
        df = payload["frames"].get(row["ticker"])
        m = {"sig_idx": i, "exclude_reason": None}
        if df is None:
            m["exclude_reason"] = "no_ohlcv:" + str(payload["failures"].get(row["ticker"], "not_in_cache"))
            meta_rows.append(m); continue
        if row["detect_date"] is None or pd.isna(row["detect_date"]):
            m["exclude_reason"] = "no_first_detected_at"
            meta_rows.append(m); continue
        factor, fflag = 1.0, None
        if row["source"] == "SNAPSHOT_ONLY":
            factor, fflag = snapshot_factor(row, df)
        td, tm = build_trade(row, df, cache.get(row["ticker"]), max_days, factor)
        m.update(tm)
        m["factor_flag"] = fflag
        if td is not None:
            if td.atr_at_signal is None and td.scanner_stop is None:
                m["exclude_reason"] = "no_stop_no_atr"
            trades.append((i, td))
        meta_rows.append(m)
    return trades, pd.DataFrame(meta_rows).set_index("sig_idx")


# ── 메인 ─────────────────────────────────────────────────────────

def run_backtest(cfg, markets: List[str]):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    costs = _costs(cfg)
    max_h = max(cfg["horizons"])
    post = max(cfg["post_exit_days"])
    cat = {c.name: c for c in catalog()}
    all_results, all_sig = [], []
    for market in markets:
        payload = load_cached(cfg["data"]["cache_dir"], market)
        bench_sym = cfg["data"]["us_benchmark"] if market == "US" else cfg["data"]["kr_benchmark"]
        bench = payload["frames"][bench_sym]["Close"]
        sig = build_signal_table(cfg, market, payload)
        trades, meta = build_trades(sig, payload, bench, max_h + post + 1)
        sig = sig.join(meta, how="left")
        sig["sig_uid"] = market + ":" + sig.index.astype(str)
        log.info("%s signals=%d trades=%d", market, len(sig), len(trades))
        rec_idx = set(sig.index[(sig["source"] == "RECONSTRUCTED")])
        strict_idx = set(sig.index[(sig["source"] == "RECONSTRUCTED") & sig["strict_filter_passed"]])
        snap_idx = set(sig.index[sig["source"] == "SNAPSHOT_ONLY"])
        tasks = []
        # (1) 전체 카탈로그 × 60일: strict 통과(B) + 스냅샷
        grid_subset = strict_idx | snap_idx
        for c in cat.values():
            tasks.append((c, cfg["default_horizon"], cfg["costs"]["slippage_bp"], "conservative",
                          c.name in CORE, grid_subset))
        # (2) 핵심 전략 × 20/120일, 전체 신호(A 포함)
        for name in CORE:
            for h in cfg["horizons"]:
                subset = rec_idx | snap_idx
                if h == cfg["default_horizon"]:
                    subset = rec_idx - strict_idx          # 60일 strict/스냅샷은 (1)에서 계산됨
                tasks.append((cat[name], h, cfg["costs"]["slippage_bp"], "conservative", False, subset))
        # (3) slippage 민감도
        for name in CORE:
            for bp in cfg["costs"]["slippage_sensitivity_bp"]:
                if bp != cfg["costs"]["slippage_bp"]:
                    tasks.append((cat[name], cfg["default_horizon"], bp, "conservative", False, strict_idx))
        # (4) 동일 봉 순서 불확실성: 낙관 시나리오 (목표가 부분매도 + 핵심)
        for c in cat.values():
            if c.family == "S07T" or c.name in CORE:
                tasks.append((c, cfg["default_horizon"], cfg["costs"]["slippage_bp"], "optimistic", False, strict_idx))
        res = run_tasks(trades, costs, tasks)
        res["market"] = market
        res["sig_uid"] = market + ":" + res["sig_idx"].astype(str)
        all_results.append(res)
        sig["market"] = market
        all_sig.append(sig)
        with open(Path(cfg["data"]["cache_dir"]) / f"trades_{market}.pkl", "wb") as fh:
            pickle.dump(trades, fh, protocol=pickle.HIGHEST_PROTOCOL)
        del trades, payload
        import gc
        gc.collect()
    results = pd.concat(all_results, ignore_index=True)
    sig = pd.concat(all_sig, ignore_index=False)
    results.to_pickle(Path(cfg["data"]["cache_dir"]) / "results_stage1.pkl")
    sig.to_pickle(Path(cfg["data"]["cache_dir"]) / "signals_table.pkl")
    from .analysis import analyze, base60, prep_sig
    report = analyze(cfg, results, sig)
    sig_p = prep_sig(sig)
    b60 = base60(cfg, results, sig_p)
    del results, all_results
    import gc
    gc.collect()
    from .stage2 import run_stage2
    run_stage2(cfg, b60, sig_p, report)


def resume_stage2(cfg):
    """저장된 Stage 1 결과에서 Stage 2 만 다시 실행 (시뮬레이션 재실행 없음)."""
    import gc
    from .analysis import base60, prep_sig
    from .stage2 import run_stage2
    cache = Path(cfg["data"]["cache_dir"])
    sig_p = prep_sig(pd.read_pickle(cache / "signals_table.pkl"))
    results = pd.read_pickle(cache / "results_stage1.pkl")
    b60 = base60(cfg, results, sig_p)
    del results
    gc.collect()
    report = pickle.load(open(cache / "analysis_stage1.pkl", "rb"))
    run_stage2(cfg, b60, sig_p, report)
