"""실제 재구성 데이터로 SHADOW 엔진 ↔ P7 운영 정책 비교판(P7-OPS) 전체 대조 (P7C 비교용 23개 계좌).

- SHADOW 쪽: 임시 SQLite 저널(ab0002)에 백테스트 후보를 신호 스냅샷으로 넣고, 거래일마다 운영 코드 경로
  ``inputs.required_tickers → collect_bars(OHLCV 캐시 공급자) → build_inputs → process_day`` 를 그대로 실행한다.
  (결정 스캔 수집·Scanner hook·계좌 조회는 대상이 아니다. 계좌 스냅샷이 없으므로 OPS 23개 계좌는 매일 차단된다 —
  OPS 는 과거 실제 계좌 스냅샷이 없어 실제 데이터 검증 대상이 아니며 합성 입력 테스트로만 확인한다.)
- P7-OPS 쪽: ``p7_ops.run`` (독립 구현). 후보 필터는 기존 P7 정의(B00 = F0·M0, B01 = HARD_EXCL+NO_CHASE·M1)를 쓴다.
- 기존 P7 결과 파일은 읽기만 하고 바꾸지 않는다. 산출물은 output/p7_ops_* (Git 제외).

운영 DB·운영 저널·VM·주문 경로와 연결되지 않는다 (임시 디렉터리 저널, 백테스트 격리 import).
"""
from __future__ import annotations

import json
import math
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from . import data as D
from . import p7_ops as OPS
from .pipeline import C

STRATS = ("B00", "B00N", "B01")


def _flags_to_warnings(row) -> list:
    return [D.WARN_PATTERNS[k] for k in D.WARN_PATTERNS if bool(row[k])]


def build_universe(start: Optional[int] = None, end: Optional[int] = None) -> dict:
    """P7 컨텍스트(C) → 대조 입력. 후보는 결정 봉(탐지일 종가) 기준, 필터는 P7 T1_100 마스크."""
    from trading.auto_buy.settings import AutoBuySettings
    from trading.auto_buy.shadow import calendar as CAL

    cal = C.mkt.calendar
    s0, e0 = C.cuts["full"]
    start = s0 if start is None else start
    end = e0 if end is None else end
    cal_days = [d.strftime("%Y-%m-%d") for d in cal]
    # SPY 캘린더에 없는 거래소 캘린더 거래일 = 실제 휴장 (예: 2025-01-09 국가 애도일). 운영의 추가 휴장일 설정과 같다.
    exch = {d.isoformat() for d in CAL.trading_days_between(cal[start].date(), cal[end].date())}
    extra_closed = sorted(exch - set(cal_days[start:end + 1]))
    extra_open = sorted(set(cal_days[start:end + 1]) - exch)
    settings = AutoBuySettings(mode="SHADOW", requested_mode="SHADOW", journal_path="",
                               extra_closed_dates=frozenset(extra_closed))
    nxt_last = CAL.next_trading_day(cal[end].date(), settings)
    next_session = {cal_days[i]: (cal_days[i + 1] if i + 1 <= end else (nxt_last.isoformat() if nxt_last else None))
                    for i in range(start, end + 1)}
    pools = C.pools["T1_100"]
    mkts = C.mkts["T1_100"]
    b00 = pools["F0"] & mkts["M0"]
    b01 = pools["HARD_EXCL"] & pools["NO_CHASE"] & mkts["M1"]
    mc = C.mkt.regime["market_condition"].to_numpy()
    sectors = {}
    try:
        from .p7 import sectors as _sectors
        sectors = _sectors()
    except Exception:
        pass
    cands = []
    for r in C.cand.itertuples(index=False):
        i = int(r.decision_ci)
        if not (start <= i <= end):
            continue
        valid = (r.close == r.close and r.close > 0 and r.stop == r.stop and 0 < r.stop < r.close)
        row = C.cand.loc[r.cid]
        cands.append({"cid": int(r.cid), "signal_key": r.signal_key, "ticker": r.ticker, "day_i": i,
                      "day": cal_days[i], "close": float(r.close), "stop": float(r.stop), "grade": r.grade,
                      "quality": r.signal_quality, "signal_type": r.signal_type, "first_seen": bool(r.first_seen),
                      "ext_pct": None if r.ext_pct != r.ext_pct else float(r.ext_pct),
                      "stop_pct": None if r.stop_pct != r.stop_pct else float(r.stop_pct),
                      "market_condition": mc[i] if isinstance(mc[i], str) else None,
                      "warnings": _flags_to_warnings(row), "sector": sectors.get(r.ticker, "Unknown"),
                      "B00": bool(b00[r.cid] and valid), "B01": bool(b01[r.cid] and valid)})
    return {"cal": cal, "cal_days": cal_days, "start": start, "end": end, "cands": cands,
            "extra_closed": extra_closed, "extra_open": extra_open, "settings": settings,
            "next_session": next_session, "book": OPS.BarBook(C.mkt.frames, cal), "frames": C.mkt.frames}


# ── SHADOW 엔진 실행 ──────────────────────────────────────────────────

def _provider(u):
    from trading.auto_buy.shadow.vfills import Bar
    frames = u["frames"]

    def f(tickers, day):
        import pandas as pd
        out = {}
        ts = pd.Timestamp(day)
        for t in tickers:
            df = frames.get(t)
            if df is None or ts not in df.index:
                out[t] = None
                continue
            r = df.loc[day]
            out[t] = Bar(float(r["Open"]), float(r["High"]), float(r["Low"]), float(r["Close"]),
                         volume=float(r["Volume"]))
        return out
    return f


def run_shadow(u: dict, journal: str, progress: bool = False) -> dict:
    from dataclasses import replace

    from trading.auto_buy import settings as S
    from trading.auto_buy.shadow import engine as EN
    from trading.auto_buy.shadow import inputs as IN
    from trading.auto_buy.shadow.schema import upgrade_journal
    from trading.auto_buy.shadow.store import connect_rw, tx
    from trading.auto_buy.version import logic_hash

    settings = replace(u["settings"], journal_path=journal)
    upgrade_journal(journal, allow_create=True)
    db = connect_rw(journal)
    days = u["cal_days"]
    start, end = u["start"], u["end"]
    t0 = time.time()
    try:
        EN.ensure_accounts(db, days[start], settings)
        run = EN.ensure_run(db, settings)
        now, lh, ch = time.time(), logic_hash(), S.config_hash(settings)
        with tx(db):
            for i in range(start, end + 1):
                db.execute("INSERT INTO ab_scan_captures(scan_log_id, scan_market, scan_status, triggered_by, "
                           "scan_started_at, trading_day, source, complete, recoverability, captured_at) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (i + 1, "US", "DONE", "scheduler", days[i] + "T21:30:00", days[i], "HOOK", 1, "FULL", now))
            for c in u["cands"]:
                db.execute(
                    "INSERT INTO ab_signal_snapshots(scan_log_id, scan_result_id, signal_key, market, ticker, signal_type, "
                    "signal_date, trading_day, scan_status, is_first_detection, strict_passed, first_strict_pass, "
                    "first_pass_basis, grade, signal_quality, market_condition, decision_price, stop_loss, ext_pct, "
                    "stop_pct, entry_warnings, sector_name, row_sha256, row_json, capture_source, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,1,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (c["day_i"] + 1, c["cid"], c["signal_key"], "US", c["ticker"], c["signal_type"], c["day"],
                     c["day"], "DONE", int(c["first_seen"]), "FIRST_DETECTION", c["grade"], c["quality"],
                     c["market_condition"], c["close"], c["stop"], c["ext_pct"], c["stop_pct"],
                     json.dumps(c["warnings"], ensure_ascii=False), c["sector"], "backtest", "{}",
                     "BACKTEST_REPLAY", now))
            for i in range(start, end + 1):
                db.execute("INSERT INTO ab_day_inputs(day, decision_status, decision_scan_log_id, frozen_at, frozen_by, "
                           "strategy_version, config_hash, logic_hash) VALUES (?,?,?,?,?,?,?,?)",
                           (days[i], "FROZEN", i + 1, now, "BACKTEST_REPLAY", S.STRATEGY_VERSION, ch, lh))
        EN.record_eligibility(db)
        prov = _provider(u)
        for i in range(start, end + 1):
            d = days[i]
            req = IN.required_tickers(db, run, d)
            got = IN.collect_bars(db, d, req, prov, final=True)
            IN.process_day(db, run, IN.build_inputs(db, run, d, got["bars"], settings), settings)
            if progress and (i - start) % 100 == 0:
                print(f"  shadow {d} ({i - start + 1}/{end - start + 1}) {time.time() - t0:.0f}s", flush=True)
        return {"run": run, "elapsed_sec": time.time() - t0, **extract(db, run)}
    finally:
        db.close()


def extract(db, run: int) -> dict:
    from trading.auto_buy import settings as S
    out = {}
    for acc_id, prof, strat, seed in S.account_ids():
        if prof != "P7C":
            continue
        name = strat if seed is None else f"B02:s{seed:02d}"
        trades = []
        for p in db.execute("SELECT * FROM ab_positions WHERE run_id=? AND account_id=? ORDER BY entry_day, ticker",
                            (run, acc_id)):
            trades.append({"ticker": p["ticker"], "entry_day": p["entry_day"], "qty": p["qty"],
                           "entry_px": p["entry_price"], "exit_day": p["exit_day"], "exit_px": p["exit_price"],
                           "reason": p["exit_reason"], "pnl": p["pnl"], "state": p["state"],
                           "unresolved": p["price_status"] == "UNRESOLVED", "zero_volume_days": p["zero_volume_days"]})
        daily = [dict(r) for r in db.execute(
            "SELECT day, equity, cash, invested, reserved, entry_heat, current_heat, n_positions, data_status "
            "FROM ab_daily WHERE run_id=? AND account_id=? ORDER BY day", (run, acc_id))]
        orders = {r[0]: r[1] for r in db.execute(
            "SELECT state, COUNT(*) FROM ab_orders WHERE run_id=? AND account_id=? GROUP BY state", (run, acc_id))}
        decisions = {(r[0] if r[1] is None else r[1]): r[2] for r in db.execute(
            "SELECT outcome, reason, COUNT(*) FROM ab_decisions WHERE run_id=? AND account_id=? GROUP BY outcome, reason",
            (run, acc_id))}
        zero_exp = db.execute("SELECT COUNT(*) FROM ab_orders WHERE run_id=? AND account_id=? AND state='UNFILLED' "
                              "AND details LIKE '%ZERO_VOLUME_EXPIRED%'", (run, acc_id)).fetchone()[0]
        out[name] = {"trades": trades, "daily": daily, "orders": orders, "decisions": decisions,
                     "zero_volume_expired": zero_exp,
                     "cash": db.execute("SELECT cash FROM ab_account_runs WHERE run_id=? AND account_id=?",
                                        (run, acc_id)).fetchone()[0]}
    out["_days"] = [dict(r) for r in db.execute("SELECT day, status, bars_missing, notes FROM ab_days WHERE run_id=? "
                                                "AND status!='COMPLETE' ORDER BY day", (run,))]
    out["_events"] = [dict(r) for r in db.execute("SELECT kind, COUNT(*) n FROM ab_events GROUP BY kind")]
    return out


# ── P7-OPS 실행·비교 ─────────────────────────────────────────────────

def ops_profile() -> OPS.Profile:
    p = dict(C.cfg["p7"]["profiles"]["BAL"])
    p.update(C.cfg["p7"]["common"])
    return OPS.Profile(initial_cash=float(C.cfg["p7"]["equity"]), RISK_PCT=p["RISK_PCT"],
                       MAX_TOTAL_HEAT_PCT=p["MAX_TOTAL_HEAT_PCT"], MAX_POSITION_PCT=p["MAX_POSITION_PCT"],
                       min_cash_pct=p["min_cash_pct"], MIN_R_PCT=p["MIN_R_PCT"], MAX_R_PCT=p["MAX_R_PCT"])


def run_ops(u: dict) -> Dict[str, OPS.Account]:
    from trading.auto_buy.settings import B02_SEEDS
    accts = OPS.p7c_accounts(B02_SEEDS)
    OPS.run(u["cands"], u["book"], u["cal_days"], u["start"], u["end"], accts, ops_profile(), dict(C.cfg["costs"]),
            u["next_session"])
    return {a.name: a for a in accts}


def _close(a, b, tol):
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, float) and isinstance(b, float) and (math.isnan(a) or math.isnan(b)):
        return math.isnan(a) and math.isnan(b)
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def compare(sh: dict, ops: Dict[str, OPS.Account], cal_days: List[str], tol: float = 1e-9) -> dict:
    rep = {"accounts": {}, "all_match": True}
    for name, a in ops.items():
        s = sh[name]
        ot = OPS.trade_rows(a, cal_days)
        st = s["trades"]
        mism = []
        if len(ot) != len(st):
            mism.append(f"거래 수 {len(st)} vs {len(ot)}")
        for x, y in zip(st, ot):
            keys_eq = (x["ticker"], x["entry_day"], x["qty"], x["exit_day"], x["reason"], x["state"]) == \
                      (y["ticker"], y["entry_day"], y["qty"], y["exit_day"], y["reason"], y["state"])
            px_eq = _close(x["entry_px"], y["entry_px"], tol) and _close(x["exit_px"], y["exit_px"], tol) \
                and _close(x["pnl"], y["pnl"], 1e-7)
            if not (keys_eq and px_eq):
                mism.append({"shadow": x, "ops": y})
                if len(mism) > 5:
                    break
        dd = {"equity": 0.0, "cash": 0.0, "entry_heat": 0.0, "current_heat": 0.0}
        pos_mism = 0
        sd = s["daily"]
        od = a.daily
        if [d["day"] for d in sd] != [d["day"] for d in od]:
            mism.append("일별 기록 날짜 불일치")
        for x, y in zip(sd, od):
            for k in dd:
                xv, yv = x[k], y[k]
                if xv is None or yv is None or (isinstance(yv, float) and math.isnan(yv)):
                    continue
                dd[k] = max(dd[k], abs(xv - yv) / max(1.0, abs(yv)) if k in ("equity", "cash") else abs(xv - yv))
            pos_mism += int(x["n_positions"] != y["n_positions"])
        ok = not mism and pos_mism == 0 and dd["equity"] <= 1e-9 and dd["cash"] <= 1e-9 \
            and dd["entry_heat"] <= 1e-9 and dd["current_heat"] <= 1e-9
        oc = {k: v for k, v in a.decisions.items()}
        ostates: Dict[str, int] = {}
        for o in a.orders:
            ostates[o["state"]] = ostates.get(o["state"], 0) + 1
        rep["accounts"][name] = {
            "match": ok, "trades_shadow": len(st), "trades_ops": len(ot),
            "closed": sum(1 for t in st if t["state"] == "CLOSED"), "open": sum(1 for t in st if t["state"] == "OPEN"),
            "max_rel_diff_equity": dd["equity"], "max_rel_diff_cash": dd["cash"],
            "max_abs_diff_entry_heat": dd["entry_heat"], "max_abs_diff_current_heat": dd["current_heat"],
            "n_positions_mismatch_days": pos_mism, "mismatches": mism[:6],
            "final_equity_shadow": sd[-1]["equity"] if sd else None, "final_equity_ops": od[-1]["equity"] if od else None,
            "orders_shadow": s["orders"], "orders_ops": ostates,
            "decisions_shadow": s["decisions"], "decisions_ops": oc,
            "decisions_match": s["decisions"] == oc, "orders_match": s["orders"] == ostates,
            "zero_volume_expired": s["zero_volume_expired"],
            "incomplete_days_shadow": sum(1 for d in sd if d["data_status"] != "COMPLETE"),
            "incomplete_days_ops": sum(1 for d in od if d["incomplete"]),
        }
        rep["accounts"][name]["match"] = ok and rep["accounts"][name]["decisions_match"] \
            and rep["accounts"][name]["orders_match"]
        rep["all_match"] &= rep["accounts"][name]["match"]
    return rep


def ops_metrics(a: OPS.Account, initial: float) -> dict:
    eq = np.array([d["equity"] for d in a.daily], float)
    peak = np.maximum.accumulate(eq) if len(eq) else eq
    closed = [t["pnl"] for t in a.trades]
    gp, gl = sum(x for x in closed if x > 0), -sum(x for x in closed if x < 0)
    return {"trades": len(a.trades) + len(a.positions), "closed": len(a.trades), "open": len(a.positions),
            "total_return": float(eq[-1] / initial - 1) if len(eq) else None,
            "mdd": float((eq / peak - 1).min()) if len(eq) else None,
            "profit_factor_closed": gp / gl if gl > 0 else None,
            "zero_volume_hold_positions": sum(1 for t in list(a.trades) + list(a.positions.values())
                                              if t.get("zero_days")),
            "zero_volume_expired_orders": sum(1 for o in a.orders if o.get("reason") == "ZERO_VOLUME_EXPIRED"),
            "fill_unknown_orders": sum(1 for o in a.orders if o["state"] == "FILL_UNKNOWN")}


def vs_p7_original(ops: Dict[str, OPS.Account]) -> dict:
    """기존 P7(BAL·UNL·full) 결과 파일과 P7-OPS 비교 (기존 파일은 읽기만). 판정(NOT_RECOMMENDED·B01 NEED_MORE_DATA) 유지."""
    import pandas as pd
    initial = float(C.cfg["p7"]["equity"])
    out = {"note": "기존 P7 결과는 바꾸지 않았다. 차이 원인: DIFF_VS_P7 (지정가 내림·거래량 0 만료·마지막 날 판단 등). "
                   "B02 는 정의가 다르다 (P7 numpy 순열 1000회 vs SHADOW seed 20개).",
           "verdict_maintained": "NOT_RECOMMENDED", "b01_status": "NEED_MORE_DATA"}
    summ = D.OUTPUT_DIR / "p7_summary.csv"
    p7 = None
    if summ.exists():
        d = pd.read_csv(summ)
        p7 = d[(d.group == "main") & (d.segment == "full") & (d.profile == "BAL") & (d.horizon == "UNL")]
    for st in STRATS:
        row = {"ops": ops_metrics(ops[st], initial)}
        if p7 is not None and not p7[p7.strategy == st].empty:
            r = p7[p7.strategy == st].iloc[0]
            row["p7"] = {"trades": int(r["trades"]), "total_return": float(r["total_return"]), "mdd": float(r["mdd"])}
        out[st] = row
    b02 = [ops_metrics(a, initial)["total_return"] for n, a in ops.items() if n.startswith("B02")]
    out["B02"] = {"ops_seeds": len(b02), "ops_median_total_return": float(np.median(b02)),
                  "ops_min": float(min(b02)), "ops_max": float(max(b02))}
    rnd = D.OUTPUT_DIR / "p7_random_b02.csv"
    if rnd.exists():
        r = pd.read_csv(rnd)
        r = r[(r.profile == "BAL") & (r.horizon == "UNL") & (r.segment == "full")]
        out["B02"]["p7_runs"] = int(len(r))
        out["B02"]["p7_median_total_return"] = float(r["total_return"].median())
    b01 = out["B01"]["ops"]["total_return"]
    out["B01_vs_B00N_ops"] = b01 - out["B00N"]["ops"]["total_return"]
    out["B01_pct_vs_B02_ops"] = float((np.array(b02) < b01).mean() * 100)
    return out


def run_all(out_dir: Optional[Path] = None, progress: bool = True) -> dict:
    u = build_universe()
    with tempfile.TemporaryDirectory(prefix="ab_ops_parity_") as tmp:
        sh = run_shadow(u, str(Path(tmp) / "shadow_parity.sqlite3"), progress=progress)
    t0 = time.time()
    ops = run_ops(u)
    ops_sec = time.time() - t0
    rep = compare(sh, ops, u["cal_days"])
    audit = OPS.zero_volume_skip_audit(C.cand, C.orders["T1_100"], u["book"])
    rep.update({
        "period": [u["cal_days"][u["start"]], u["cal_days"][u["end"]]],
        "trading_days": u["end"] - u["start"] + 1,
        "candidates": len(u["cands"]),
        "eligible": {"B00": sum(c["B00"] for c in u["cands"]), "B01": sum(c["B01"] for c in u["cands"])},
        "extra_closed_dates": u["extra_closed"], "extra_open_dates": u["extra_open"],
        "shadow_elapsed_sec": round(sh["elapsed_sec"], 1), "ops_elapsed_sec": round(ops_sec, 1),
        "shadow_incomplete_days": sh["_days"], "shadow_events": sh["_events"],
        "p7_zero_volume_skip": {"count": audit["count"], "rows": audit["rows"][:50]},
        "vs_p7_original": vs_p7_original(ops),
        "shadow_zero_volume_hold_positions": {k: sum(1 for t in sh[k]["trades"] if t["zero_volume_days"])
                                              for k in ops},
        "diff_vs_p7": OPS.DIFF_VS_P7,
        "ops_accounts_note": "OPS 23개 계좌는 과거 실제 계좌 스냅샷이 없어 실제 데이터 대조 대상이 아님 (합성 입력 테스트로만 확인)",
    })
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "p7_ops_parity.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=str))
        import pandas as pd
        rows = []
        for name, a in ops.items():
            for t in OPS.trade_rows(a, u["cal_days"]):
                rows.append({"account": name, **t})
        pd.DataFrame(rows).to_csv(out_dir / "p7_ops_trades.csv", index=False)
    return rep
