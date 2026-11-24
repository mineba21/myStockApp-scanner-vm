"""Exit Strategy Backtest CLI.

    .venv/bin/python -m backtest.exit_strategy.run audit
    .venv/bin/python -m backtest.exit_strategy.run fetch   --market US|KR [--refresh]
    .venv/bin/python -m backtest.exit_strategy.run replay  --market US|KR
    .venv/bin/python -m backtest.exit_strategy.run backtest
    .venv/bin/python -m backtest.exit_strategy.run report
    .venv/bin/python -m backtest.exit_strategy.run all

실제 주문·운영 DB 접근 없음. 스캐너 모듈은 격리된 임시 SQLite 환경에서만 import 된다.
"""
from __future__ import annotations

import argparse
import json
import logging
import pickle
from pathlib import Path

import pandas as pd

from .src.common import OUTPUT_DIR, load_config, scanner_threshold_snapshot

log = logging.getLogger("exit_backtest")


def _cache(cfg) -> Path:
    return Path(cfg["data"]["cache_dir"])


def cmd_fetch(cfg, market, refresh):
    from .src.market_data_loader import ensure_market_data
    from .src.signal_loader import load_snapshot_signals

    snap = load_snapshot_signals(cfg["snapshot"]["mcp_dump"])
    extra = sorted(snap.loc[snap["market"] == market, "ticker"].unique())
    p = ensure_market_data(cfg, market, refresh=refresh, extra_tickers=extra)
    log.info("%s frames=%d failures=%d", market, len(p["frames"]), len(p["failures"]))


def cmd_replay(cfg, market, tickers=None):
    from .src.market_data_loader import load_cached
    from .src.signal_loader import reconstruct_signals

    payload = load_cached(cfg["data"]["cache_dir"], market)
    rec = reconstruct_signals(payload, cfg, tickers=tickers)
    out = _cache(cfg) / f"reconstructed_{market}.pkl"
    rec.to_pickle(out)
    log.info("%s reconstructed rows=%d → %s", market, len(rec), out)


def replay_validation(cfg, snap: pd.DataFrame) -> dict:
    """운영 MCP 행이 현재 로직 replay 로 재현되는지 (signal_key · 탐지일 · pivot/stop 수준)."""
    rows, details = 0, []
    matched = level_ok = level_n = date_ok = 0
    for m in ("US", "KR"):
        f = _cache(cfg) / f"reconstructed_{m}.pkl"
        if not f.exists():
            continue
        rec = pd.read_pickle(f)
        last_day = pd.to_datetime(rec["detect_date"]).max()
        sm = snap[(snap["market"] == m) & (pd.to_datetime(snap["signal_date"]) <= last_day)]
        by_key = {k: g for k, g in rec.groupby("signal_key")}
        for _, r in sm.iterrows():
            rows += 1
            g = by_key.get(r["signal_key"])
            hit = g is not None
            matched += hit
            d = {"market": m, "signal_key": r["signal_key"], "matched": bool(hit)}
            if hit:
                first = g.sort_values("detect_date").iloc[0]
                if pd.notna(r["first_detected_at"]):
                    local = r["first_detected_at"] + (pd.Timedelta(hours=9) if m == "KR" else -pd.Timedelta(hours=5))
                    same = local.normalize() == pd.Timestamp(first["detect_date"])
                    date_ok += bool(same)
                    d["detect_date_match"] = bool(same)
                lv = []
                for col in ("pivot_price", "stop_loss"):
                    a, b = r.get(col), first.get(col)
                    if pd.notna(a) and pd.notna(b) and a:
                        lv.append(abs(b / a - 1) <= 0.02)
                if lv:
                    level_n += 1
                    level_ok += all(lv)
                    d["level_match"] = all(lv)
            details.append(d)
    return {"comparable": rows, "matched": matched, "match_rate": matched / rows if rows else None,
            "detect_date_match": date_ok, "level_match_rate": level_ok / level_n if level_n else None,
            "note": "운영 행은 재스캔마다 pivot·stop 이 갱신되므로 수준 비교는 ±2% 허용. "
                    "불일치 원인: 운영 .env 미확인 임계값, 수정주가 재계산, 로직 변경 이력.",
            "details": details}


def cmd_audit(cfg):
    from .src.data_audit import CODE_FACTS, audit_reconstructed, audit_snapshot
    from .src.market_data_loader import load_cached
    from .src.signal_loader import load_snapshot_signals

    snap = load_snapshot_signals(cfg["snapshot"]["mcp_dump"])
    have = {}
    failures = {}
    for m in ("US", "KR"):
        p = load_cached(cfg["data"]["cache_dir"], m)
        if p:
            have[m] = set(p["frames"])
            failures[m] = {"universe": len(p["meta"]), "frames": len(p["frames"]),
                           "failures": p["failures"], "price_basis": p["price_basis"],
                           "fetched_at": p["fetched_at"]}
    rec = {}
    for m in ("US", "KR"):
        f = _cache(cfg) / f"reconstructed_{m}.pkl"
        if f.exists():
            rec[m] = audit_reconstructed(pd.read_pickle(f))
    audit = {"code_facts": CODE_FACTS, "snapshot": audit_snapshot(snap, have or None),
             "ohlcv": failures, "reconstructed": rec,
             "replay_validation": replay_validation(cfg, snap),
             "scanner_thresholds_used_in_replay": scanner_threshold_snapshot()}
    st = _cache(cfg) / "signals_table.pkl"
    if st.exists():
        s = pd.read_pickle(st)
        s["reason"] = s["exclude_reason"].fillna("INCLUDED").astype(str).str.split(":").str[0]
        audit["exclusions"] = (s.groupby(["market", "source", "reason"]).size().rename("n")
                               .reset_index().to_dict("records"))
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_DIR / "data_audit.json", "w", encoding="utf-8") as fh:
        json.dump(audit, fh, ensure_ascii=False, indent=2, default=str)
    log.info("data_audit.json written")
    return audit


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["audit", "fetch", "replay", "backtest", "stage2", "report", "all"])
    ap.add_argument("--market", choices=["US", "KR"], default=None)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--config", default=None)
    ap.add_argument("--tickers", default=None, help="replay 디버그용 쉼표 목록")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(a.config)
    markets = [a.market] if a.market else ["US", "KR"]
    if a.cmd in ("fetch", "all"):
        for m in markets:
            cmd_fetch(cfg, m, a.refresh)
    if a.cmd in ("replay", "all"):
        for m in markets:
            cmd_replay(cfg, m, a.tickers.split(",") if a.tickers else None)
    if a.cmd in ("backtest", "all"):
        from .src.backtest_engine import run_backtest
        run_backtest(cfg, markets)
    if a.cmd == "stage2":
        from .src.backtest_engine import resume_stage2
        resume_stage2(cfg)
    if a.cmd in ("audit", "all"):
        cmd_audit(cfg)
    if a.cmd in ("report", "all"):
        from .src.report_generator import build_report
        build_report(cfg)


if __name__ == "__main__":
    main()
