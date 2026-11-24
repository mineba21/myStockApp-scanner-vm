"""Scanner 신호 적재.

두 가지 출처를 **섞지 않고** 구분한다.

SNAPSHOT_ONLY
    Scanner MCP 로 읽은 현재 저장 행. 같은 signal_key 를 재스캔하면 price·pivot·stop 등
    지표가 덮어써지고(``scan_engine._save``), 행은 수동 삭제될 수 있으며, strict 거부 행은
    MCP 에서 보이지 않는다. ``values_at_first_detection`` 은 scan_time == first_detected_at
    (최초 저장 이후 갱신 없음)인 행만 True.

RECONSTRUCTED
    현재 스캐너 코드(``weinstein.analyze_stock`` → ``entry_control.annotate_alert_freshness``
    → ``strict_filter.apply_strict_filter``)를 과거 일봉에 날짜별로 적용해 재생성한 신호.
    각 판정일 d 에는 d 까지의 2년 창만 넘긴다 (미래 데이터 차단). 같은 signal_key 의 최초
    탐지일 스냅샷을 그대로 보존한다.

VERIFIED_HISTORICAL
    불변 이벤트 이력이 없으므로 이번 데이터에는 0건이다.
"""
from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ProcessPoolExecutor
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .common import isolate_scanner_imports

logger = logging.getLogger(__name__)

SNAPSHOT_FIELDS = [
    "signal_type", "signal_date", "stage", "price", "ma150", "pivot_price", "stop_loss",
    "support_level", "signal_quality", "grade", "volume_ratio", "weekly_volume_ratio",
    "weekly_volume_ratio_4w", "rs_value", "rs_trend", "upthrust_failed", "pivot_ext_pct",
    "cur_ext_pct", "cur_stop_pct", "market_condition",
]


# ── SNAPSHOT_ONLY ────────────────────────────────────────────────

def _utc(ts):
    if ts is None:
        return pd.NaT
    return pd.Timestamp(ts).tz_convert("UTC").tz_localize(None) if pd.Timestamp(ts).tzinfo \
        else pd.Timestamp(ts)


def load_snapshot_signals(dump_path: str) -> pd.DataFrame:
    with open(dump_path, encoding="utf-8") as fh:
        items = json.load(fh)["items"]
    rows = []
    for it in items:
        first = _utc(it.get("first_detected_at"))
        scan = _utc(it.get("scan_time"))
        rows.append({
            "source": "SNAPSHOT_ONLY",
            "result_id": it["id"],
            "market": it["market"], "ticker": it["ticker"], "name": it.get("name"),
            **{k: it.get(k) for k in SNAPSHOT_FIELDS if k in it},
            "sector_stage": it.get("sector_stage"),
            "strict_filter_passed": it.get("strict_filter_passed"),
            "strict_assessment": it.get("strict_assessment"),
            "entry_warnings": json.dumps(it.get("entry_warnings") or [], ensure_ascii=False),
            "filter_reasons": json.dumps(it.get("filter_reasons") or [], ensure_ascii=False),
            "has_entry_warnings": bool(it.get("entry_warnings")),
            "suggested_qty": it.get("suggested_qty"),
            "first_detected_at": first, "scan_time": scan,
            "values_at_first_detection": bool(pd.notna(first) and pd.notna(scan) and first == scan),
            "event_key": it.get("event_key"), "signal_key": it.get("signal_key"),
        })
    return pd.DataFrame(rows)


def snapshot_detect_date(row, scan_hours: Dict[str, int]) -> Optional[pd.Timestamp]:
    """MCP 행의 '확인 가능해진 거래일'. first_detected_at 이 없으면 None(=사용 불가).

    스캔은 정규장 마감 후 실행되므로 탐지 UTC 시각의 (시장 현지) 거래일 장마감 후에
    확인 가능하다. 진입은 그 다음 거래일 시가.
    """
    first = row["first_detected_at"]
    if pd.isna(first):
        return None
    if row["market"] == "KR":
        local = first + pd.Timedelta(hours=9)          # KST
    else:
        local = first - pd.Timedelta(hours=5)          # ET (DST 무시해도 21:30UTC 는 같은 날)
    return local.normalize()


# ── RECONSTRUCTED (Replay scan) ───────────────────────────────────

def market_condition_series(index_frames: Dict[str, pd.DataFrame], dates: pd.DatetimeIndex) -> pd.Series:
    """운영 ``market_analysis._analyze_index`` + ``_condition`` 을 날짜별로 재현."""
    isolate_scanner_imports()
    from config import MA_PERIOD
    from scanner.market_analysis import _condition
    from scanner.weinstein import stage_of, _slope

    out = {}
    for d in dates:
        stages = []
        for df in index_frames.values():
            close = df["Close"].loc[:d]
            close = close[close.index > d - pd.Timedelta(days=730)]   # 운영 get_*_ohlcv 2y
            if len(close) < MA_PERIOD:
                continue
            ma = close.rolling(MA_PERIOD, min_periods=MA_PERIOD // 2).mean()
            stages.append({"stage": stage_of(float(close.iloc[-1]), float(ma.iloc[-1]), _slope(ma))})
        out[d] = _condition(stages)
    return pd.Series(out)


def _legacy_market_allow(market_condition, signal_type) -> bool:
    from scanner.scan_engine import _get_market_filter_decision  # 함수 내부 import 만 DB 사용
    allow, _ = _get_market_filter_decision(market_condition, signal_type)
    return allow


_W: dict = {}


def _init_worker(bench: pd.Series, cond: pd.Series, window_days: int):
    isolate_scanner_imports()
    logging.disable(logging.WARNING)
    _W["bench"] = bench
    _W["cond"] = cond
    _W["window"] = pd.Timedelta(days=window_days)


def _replay_ticker(args):
    ticker, name, market, df, gics, start, end = args
    from scanner.weinstein import analyze_stock
    from scanner.entry_control import annotate_alert_freshness
    from scanner.strict_filter import apply_strict_filter
    from scanner.scan_engine import _grade
    from config import STRICT_PERSIST_REJECTED

    bench, cond, window = _W["bench"], _W["cond"], _W["window"]
    idx = df.index
    b_idx = bench.index
    state: Dict[tuple, str] = {}       # key → "seen" | "passed"
    rows: List[dict] = []
    days = idx[(idx >= start) & (idx <= end)]
    for d in days:
        lo = idx.searchsorted(d - window, side="right")
        hi = idx.searchsorted(d, side="right")
        sub = df.iloc[lo:hi]
        bsub = bench.iloc[:b_idx.searchsorted(d, side="right")]
        mc = cond.get(d)
        try:
            res = analyze_stock(sub, ticker, name, market, bsub, mc)
        except Exception:
            continue
        if not res:
            continue
        key = (res["signal_type"], res.get("signal_date"))
        if state.get(key) == "passed":
            continue          # 최초 탐지·최초 strict 통과를 모두 기록함
        allow = _legacy_market_allow(mc, res["signal_type"])
        if not allow and not STRICT_PERSIST_REJECTED:
            continue
        annotate_alert_freshness(res)
        res["_gics_sector"] = gics
        ctx = {"market_condition": mc, "sector_stage": None, "benchmark_present": True}
        passed, reasons = apply_strict_filter(res, ctx)
        first_seen = key not in state
        if not first_seen and not passed:
            continue          # 이미 최초 탐지 기록됨, 아직 통과 전
        snap = {k: res.get(k) for k in SNAPSHOT_FIELDS}
        snap.update({
            "market": market, "ticker": ticker, "name": name, "detect_date": d,
            "first_seen": first_seen,
            "grade": _grade(res), "strict_filter_passed": bool(passed),
            "filter_reasons": json.dumps(reasons, ensure_ascii=False),
            "entry_warnings": json.dumps(res.get("entry_warnings") or [], ensure_ascii=False),
            "has_entry_warnings": bool(res.get("entry_warnings")),
            "base_quality": res.get("base_quality"),
            "market_filter_allow": allow,
        })
        rows.append(snap)
        state[key] = "passed" if passed else "seen"
    return rows


def reconstruct_signals(payload: dict, cfg: dict, workers: Optional[int] = None,
                        tickers: Optional[List[str]] = None) -> pd.DataFrame:
    market = payload["market"]
    d = cfg["data"]
    frames = payload["frames"]
    bench_sym = d["us_benchmark"] if market == "US" else d["kr_benchmark"]
    cond_syms = d["us_condition_indices"] if market == "US" else d["kr_condition_indices"]
    bench = frames[bench_sym]["Close"]
    start = pd.Timestamp(d["replay_start"])
    end = pd.Timestamp(d["replay_end"]) if d.get("replay_end") else bench.index.max()
    cal = bench.index[(bench.index >= start) & (bench.index <= end)]
    cond = market_condition_series({s: frames[s] for s in cond_syms if s in frames}, cal)

    skip = set(cond_syms) | {bench_sym}
    universe = [t for t in payload["meta"] if t in frames and t not in skip
                and not payload["meta"][t].get("_extra")]
    if tickers:
        universe = [t for t in universe if t in set(tickers)]
    jobs = [(t, payload["meta"][t].get("name", t), market, frames[t],
             payload["meta"][t].get("sector"), start, end) for t in universe]
    rows: List[dict] = []
    workers = workers or max(1, (os.cpu_count() or 2) - 1)
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker,
                             initargs=(bench, cond, d["scanner_window_days"])) as pool:
        for n, out in enumerate(pool.map(_replay_ticker, jobs, chunksize=4)):
            rows.extend(out)
            if n % 100 == 0:
                logger.info("%s replay %d/%d (signals %d)", market, n, len(jobs), len(rows))
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["source"] = "RECONSTRUCTED"
    df["signal_key"] = df["market"] + ":" + df["ticker"] + ":" + df["signal_type"] + ":" + df["signal_date"].astype(str)
    df["detect_lag_days"] = (df["detect_date"] - pd.to_datetime(df["signal_date"])).dt.days
    return df.sort_values(["detect_date", "ticker"]).reset_index(drop=True)
