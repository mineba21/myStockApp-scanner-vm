"""AUTO BUY MONITOR 조회 (읽기 전용 연결, 파일·테이블을 만들지 않는다). 가상 체결 결과는 실제 체결이 아니다.

결과는 현재 run(``ab_runs.status='CURRENT'``) 기준이다. 결측 복구로 재계산된 이전 run 은 이력으로만 보여 준다.
"""
from __future__ import annotations

import json
import math
import statistics
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from trading.auto_buy import settings as S
from trading.auto_buy.shadow.schema import ro_connect, schema_status

VIRTUAL_NOTICE = ("모든 수치는 가상 계좌(SHADOW)이며 실제 주문·체결·실현손익이 아닙니다. "
                  "가상 체결은 일봉 기준(DAILY_T1_100_v1)이라 장중 호가·부분체결·주문 거부를 재현하지 않습니다.")
S1_LIMIT_NOTICE = ("S1 한계: 운영 DB outbox 없이 별도 SHADOW 저널만으로는 신호 수집의 완전성을 보장하지 못합니다. "
                   "Scanner 가 같은 신호 행을 재스캔 때 덮어쓰므로, 수집이 늦거나 실패한 스캔의 일부 신호는 복구할 수 "
                   "없습니다. 불완전·복구 불가 수집은 아래에 표시하고, 확정할 수 없는 신호는 매수 판단에서 뺍니다.")
MILESTONES = S.S5_CRITERIA["milestones"]
STOP_REASONS = ("STOP", "STOP_GAP", "STOP_DAY0")


def _loads(x, default):
    try:
        return json.loads(x) if x else default
    except ValueError:
        return default


def _current_run(db) -> Optional[int]:
    r = db.execute("SELECT run_id FROM ab_runs WHERE status='CURRENT'").fetchone()
    return r[0] if r else None


# ── heartbeat ─────────────────────────────────────────────────────────

def expected_slot(now: datetime, minutes: int) -> Optional[datetime]:
    """스케줄러 cron(월~토 17:00~23:59 ET, N분 간격) 기준 now 이전 마지막 예정 실행 시각 (UTC)."""
    from trading.auto_stop.us_calendar import NEW_YORK
    t = now.astimezone(NEW_YORK)
    for back in range(0, 8):
        d = (t - timedelta(days=back)).date()
        if d.weekday() == 6:
            continue
        slots = [datetime(d.year, d.month, d.day, h, m, tzinfo=NEW_YORK)
                 for h in range(17, 24) for m in range(0, 60, minutes)]
        past = [s for s in slots if s <= t]
        if past:
            return past[-1].astimezone(timezone.utc)
    return None


def heartbeat(db, settings, now: Optional[datetime] = None) -> dict:
    from trading.auto_buy.shadow import calendar as CAL
    from trading.auto_buy.shadow import inputs as IN
    now = now or datetime.now(timezone.utc)
    lj = _rt(db, "last_job") or {}
    at = lj.get("at")
    slot = expected_slot(now, settings.job_minutes)
    out = {"last_job_at": at, "expected_slot": slot.isoformat() if slot else None,
           "age_sec": round(now.timestamp() - at) if at else None, "waiting": lj.get("waiting")}
    if slot is None:
        out["status"] = "NOT_EXPECTED"
    elif at is None or at < slot.timestamp() - 120:
        out["status"] = "STALE"
    else:
        out["status"] = "OK"
    last = CAL.last_closed_session(now, settings)
    run = _current_run(db)
    out["last_session"] = last.isoformat() if last else None
    if last is not None and run is not None:
        processed = db.execute("SELECT MAX(day) FROM ab_days WHERE run_id=?", (run,)).fetchone()[0]
        out["last_processed_day"] = processed
        cutoff = IN.bars_cutoff(last, settings)
        due = cutoff is not None and now >= cutoff + timedelta(minutes=30)
        out["processing"] = ("LAGGING" if due and (processed or "") < last.isoformat()
                             else "CURRENT" if (processed or "") >= last.isoformat() else "WAITING")
        out["bars_cutoff"] = cutoff.isoformat() if cutoff else None
        s = CAL.session(last, settings)
        out["last_session_early_close"] = bool(s and s.close_at.astimezone(timezone.utc) != CAL.at_et(last, "16:00"))
    return out


def _rt(db, key):
    r = db.execute("SELECT value FROM ab_runtime WHERE key=?", (key,)).fetchone()
    return _loads(r[0], None) if r else None


def health(settings: Optional[S.AutoBuySettings] = None) -> dict:
    settings = settings or S.AutoBuySettings.from_env()
    st = schema_status(settings.journal_path) if settings.journal_path else schema_status(None)
    out = {"mode": settings.mode, "requested_mode": settings.requested_mode, "live_requested": settings.live_requested,
           "live_supported": False, "config_error": settings.config_error, "shadow_enabled": settings.shadow_enabled,
           "strategy_version": S.STRATEGY_VERSION, "config_hash": S.config_hash(), "fill_model": S.FILL_MODEL,
           "journal_configured": bool(settings.journal_path), **{k: st[k] for k in
           ("journal_exists", "schema_ready", "schema_version", "expected_schema_version", "migration_required",
            "schema_error")}, "real_order_path": "없음 (OFF/SHADOW 전용 구현, 주문 모듈 미사용)",
           "notice": VIRTUAL_NOTICE, "s1_limit_notice": S1_LIMIT_NOTICE}
    try:
        from trading.auto_buy.version import logic_hash
        out["logic_hash"] = logic_hash()
    except Exception as exc:
        out["logic_hash"] = None
        out["logic_hash_error"] = type(exc).__name__
    if st["schema_ready"]:
        db = ro_connect(settings.journal_path)
        try:
            rt = {r["key"]: _loads(r["value"], None) for r in db.execute("SELECT key, value FROM ab_runtime")}
            for k in ("last_job", "last_capture", "last_error", "collection_started_at", "shadow_start_day",
                      "scan_reconciliation", "version_block", "replay_in_progress"):
                out[k] = rt.get(k)
            lj = rt.get("last_job") or {}
            out["heartbeat_age_sec"] = round(time.time() - lj["at"]) if lj.get("at") else None
            out["heartbeat"] = heartbeat(db, settings)
            out["non_virtual_rows"] = sum(db.execute(f"SELECT COUNT(*) FROM {t} WHERE is_virtual != 1").fetchone()[0]
                                          for t in ("ab_orders", "ab_fill_ledger", "ab_positions", "ab_stop_events"))
            run = _current_run(db)
            out["current_run"] = run
            out["virtual_orders"] = db.execute("SELECT COUNT(*) FROM ab_orders WHERE run_id=?", (run,)).fetchone()[0]
            out["recorded_logic_hashes"] = [r[0] for r in db.execute(
                "SELECT DISTINCT logic_hash FROM ab_accounts WHERE strategy_version=?", (S.STRATEGY_VERSION,))]
            out["building_runs"] = db.execute("SELECT COUNT(*) FROM ab_runs WHERE status='BUILDING'").fetchone()[0]
        finally:
            db.close()
    return out


# ── 계좌 지표 ──────────────────────────────────────────────────────────

def _mdd(eq: list) -> float:
    peak, mdd = -math.inf, 0.0
    for v in eq:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1)
    return mdd


def account_metrics(db, acc, run) -> dict:
    daily = db.execute("SELECT * FROM ab_daily WHERE run_id=? AND account_id=? ORDER BY day",
                       (run, acc["id"])).fetchall()
    ar = db.execute("SELECT * FROM ab_account_runs WHERE run_id=? AND account_id=?", (run, acc["id"])).fetchone()
    eq = [d["equity"] for d in daily if d["equity"] is not None]
    base = ar["initial_cash"] if ar else None
    closed = db.execute("SELECT * FROM ab_positions WHERE run_id=? AND account_id=? AND state='CLOSED'",
                        (run, acc["id"])).fetchall()
    opened = db.execute("SELECT * FROM ab_positions WHERE run_id=? AND account_id=? AND state='OPEN'",
                        (run, acc["id"])).fetchall()
    gw = sum(p["pnl"] for p in closed if p["pnl"] > 0)
    gl = -sum(p["pnl"] for p in closed if p["pnl"] < 0)
    rs = [p["r_multiple"] for p in closed if p["r_multiple"] is not None]
    open_r, open_pnl = [], 0.0
    for p in opened:
        px = p["last_close"] or p["entry_price"]
        val = p["qty"] * px * (1 - S.COSTS["slippage_bp"] / 1e4) * (1 - S.COSTS["sell_commission"])
        open_pnl += val - p["cost"]
        if p["risk0"] > 0:
            open_r.append((val - p["cost"]) / p["risk0"])
    oc = {r["state"]: r["n"] for r in db.execute(
        "SELECT state, COUNT(*) n FROM ab_orders WHERE run_id=? AND account_id=? GROUP BY state", (run, acc["id"]))}
    blocks = db.execute("SELECT reason, COUNT(*) n FROM ab_decisions WHERE run_id=? AND account_id=? "
                        "AND outcome!='ORDERED' GROUP BY reason ORDER BY n DESC", (run, acc["id"])).fetchall()
    last = daily[-1] if daily else None
    pf_open_w = gw + max(open_pnl, 0.0)
    pf_open_l = gl + max(-open_pnl, 0.0)
    return {
        "id": acc["id"], "profile": acc["profile"], "strategy": acc["strategy"], "seed": acc["seed"],
        "start_day": acc["start_day"], "initial": base, "days": len(daily),
        "incomplete_days": sum(1 for d in daily if d["data_status"] != "COMPLETE"),
        "last_day": last["day"] if last else None,
        "equity": eq[-1] if eq else None,
        "cum_return": (eq[-1] / base - 1) if eq and base else None,
        "mdd": _mdd(eq) if eq else None,
        "closed_trades": len(closed), "open_positions": len(opened),
        "pf": (gw / gl) if gl > 0 else (None if not closed else math.inf if gw > 0 else None),
        "pf_incl_open": (pf_open_w / pf_open_l) if pf_open_l > 0 else (math.inf if pf_open_w > 0 else None),
        "open_pnl": open_pnl if opened else 0.0,
        "avg_r_closed": sum(rs) / len(rs) if rs else None,
        "avg_r_incl_open": (sum(rs) + sum(open_r)) / (len(rs) + len(open_r)) if rs or open_r else None,
        "r_values_incl_open": rs + open_r,
        "stop_rate": sum(1 for p in closed if p["exit_reason"] in STOP_REASONS) / len(closed) if closed else None,
        "orders": sum(oc.values()), "order_states": oc,
        "fill_rate": (oc.get("FILLED", 0) + oc.get("PARTIAL", 0)) / max(1, sum(v for k, v in oc.items()
                                                                               if k not in ("PENDING",))),
        "entry_heat": last["entry_heat"] if last else None, "current_heat": last["current_heat"] if last else None,
        "current_heat_level": last["current_heat_level"] if last else None,
        "sleeve_budget": last["sleeve_budget"] if last else None, "sleeve_used": last["sleeve_used"] if last else None,
        "sleeve_usage": (last["sleeve_used"] / last["sleeve_budget"]) if last and last["sleeve_budget"] else None,
        "cash": last["cash"] if last else None, "reserved": last["reserved"] if last else None,
        "max_pos_w": last["max_pos_w"] if last else None, "max_sector_w": last["max_sector_w"] if last else None,
        "data_status": last["data_status"] if last else None,
        "unresolved_positions": last["unresolved_positions"] if last else 0,
        "unresolved_orders": last["unresolved_orders"] if last else 0,
        "unresolved_value": last["unresolved_value"] if last else 0.0,
        "stress_full_loss": last["stress_full_loss"] if last else 0.0,
        "stress_equity_after": (last["equity"] - last["stress_full_loss"]) if last and last["equity"] is not None
        else None,
        "safety_blocks": _loads(last["safety_blocks"], []) if last else [],
        "account_level": _loads(last["account_level"], {}) if last else {},
        "block_reasons": {r["reason"]: r["n"] for r in blocks},
    }


def _pct_rank(x, arr) -> Optional[float]:
    arr = [a for a in arr if a is not None]
    if x is None or not arr:
        return None
    return 100.0 * sum(1 for a in arr if a <= x) / len(arr)


def _ci95(vals: list) -> Optional[list]:
    if len(vals) < 2:
        return None
    m, sd = statistics.fmean(vals), statistics.stdev(vals)
    h = 1.96 * sd / math.sqrt(len(vals))
    return [m - h, m + h]


def s5_panel(db, metrics: list, b02: dict, period: dict, safety_violations: int) -> dict:
    """S5 기준(평가 시작 전 고정)에 대한 현재 상태. 판정은 고정: NOT_RECOMMENDED 유지, B01 = NEED_MORE_DATA."""
    crit = S.S5_CRITERIA
    out = {"criteria": crit, "profiles": {}, "verdict": "NOT_RECOMMENDED", "b01_status": "NEED_MORE_DATA",
           "note": "기준 충족 여부는 검토 자료일 뿐 LIVE 승인 조건이 아닙니다 (별도 승인 필요)."}
    regimes = {}
    for r in db.execute("SELECT s.market_condition mc, COUNT(DISTINCT d.day) n FROM ab_day_inputs d "
                        "JOIN ab_signal_snapshots s ON s.scan_log_id=d.decision_scan_log_id "
                        "WHERE d.decision_status='FROZEN' GROUP BY s.market_condition"):
        regimes[r["mc"] or "UNKNOWN"] = r["n"]
    out["regime_days"] = regimes
    tot = sum(regimes.values())
    out["regime_skew"] = (max(regimes.values()) / tot) if tot else None
    for prof in S.PROFILES:
        m = {x["strategy"]: x for x in metrics if x["profile"] == prof and x["strategy"] != "B02"}
        b01, b00n = m.get("B01"), m.get("B00N")
        if not b01 or not b00n:
            continue
        def ge(a, b):
            return None if a is None or b is None else a >= b
        mdd_w = None
        if b01["mdd"] is not None and b00n["mdd"] is not None:
            mdd_w = (b00n["mdd"] - b01["mdd"]) * 100          # 양수 = B01 이 더 깊다 (pp)
        checks = {
            "trading_days": period["trading_days"],
            "milestone": ("60일" if period["trading_days"] >= crit["milestones"]["interim_days"] else
                          "20일" if period["trading_days"] >= crit["milestones"]["integrity_days"] else
                          "5일" if period["trading_days"] >= crit["milestones"]["safety_days"] else "5일 미만"),
            "b01_closed_trades": b01["closed_trades"],
            "trades_enough": b01["closed_trades"] >= crit["milestones"]["min_b01_closed_trades"],
            "return_ge_b00n": ge(b01["cum_return"], b00n["cum_return"]),
            "pf_gt_1": None if b01["pf_incl_open"] is None else b01["pf_incl_open"] > 1.0,
            "pf_ge_b00n": ge(b01["pf_incl_open"], b00n["pf_incl_open"]),
            "avg_r_gt_0": None if b01["avg_r_incl_open"] is None else b01["avg_r_incl_open"] > 0,
            "avg_r_ge_b00n": ge(b01["avg_r_incl_open"], b00n["avg_r_incl_open"]),
            "mdd_worsening_pp": mdd_w,
            "mdd_ok": None if mdd_w is None else mdd_w <= crit["performance"]["mdd_worsening_max_pp"],
            "b02_percentile": b02.get(prof, {}).get("b01_percentile"),
            "ranking_review": (None if b02.get(prof, {}).get("b01_percentile") is None else
                               b02[prof]["b01_percentile"] >= crit["performance"]["b02_percentile_rank_review"]),
            "avg_r_ci95": _ci95(b01["r_values_incl_open"]),
            "safety_violations": safety_violations,
            "data_incomplete_days": period["data_incomplete_days"],
            "filter_effect_return": (None if b01["cum_return"] is None or b00n["cum_return"] is None
                                     else b01["cum_return"] - b00n["cum_return"]),
            "ranking_effect_vs_b02_median": (None if b01["cum_return"] is None or
                                             b02.get(prof, {}).get("median_return") is None
                                             else b01["cum_return"] - b02[prof]["median_return"]),
        }
        out["profiles"][prof] = checks
    return out


def summary(settings: Optional[S.AutoBuySettings] = None) -> dict:
    settings = settings or S.AutoBuySettings.from_env()
    out = {"health": health(settings), "milestones": MILESTONES, "notice": VIRTUAL_NOTICE,
           "s1_limit_notice": S1_LIMIT_NOTICE}
    if not out["health"]["schema_ready"]:
        return out
    db = ro_connect(settings.journal_path)
    try:
        run = _current_run(db)
        accs = db.execute("SELECT * FROM ab_accounts WHERE strategy_version=? ORDER BY id",
                          (S.STRATEGY_VERSION,)).fetchall()
        metrics = [account_metrics(db, a, run) for a in accs] if run is not None else []
        out["accounts"] = [{k: v for k, v in m.items() if not k.startswith("_") and k != "r_values_incl_open"}
                           for m in metrics if m["strategy"] != "B02"]
        out["b02"] = {}
        for prof in S.PROFILES:
            b02 = [m for m in metrics if m["profile"] == prof and m["strategy"] == "B02"]
            b01 = next((m for m in metrics if m["profile"] == prof and m["strategy"] == "B01"), None)
            rets = [m["cum_return"] for m in b02]
            valid = sorted(r for r in rets if r is not None)
            out["b02"][prof] = {
                "accounts": len(b02), "median_return": valid[len(valid) // 2] if valid else None,
                "min_return": valid[0] if valid else None, "max_return": valid[-1] if valid else None,
                "b01_percentile": _pct_rank(b01["cum_return"] if b01 else None, rets),
                "median_closed_trades": sorted(m["closed_trades"] for m in b02)[len(b02) // 2] if b02 else None,
            }
        days = db.execute("SELECT * FROM ab_days WHERE run_id=? ORDER BY day", (run,)).fetchall()
        period = {"trading_days": len(days), "first_day": days[0]["day"] if days else None,
                  "last_day": days[-1]["day"] if days else None,
                  "data_incomplete_days": sum(1 for d in days if d["status"] != "COMPLETE"),
                  "no_decision_days": sum(1 for d in days if d["decision_status"] != "FROZEN"),
                  "replay_in_progress": out["health"].get("replay_in_progress") is not None
                  or out["health"].get("building_runs", 0) > 0}
        period["complete"] = (period["data_incomplete_days"] == 0 and not period["replay_in_progress"]
                              and not any(m["unresolved_positions"] or m["unresolved_orders"] for m in metrics))
        period["basis"] = "모든 전략 동일 기준: 같은 거래일 입력(결정 스캔·일봉·계좌 스냅샷)을 공유한다"
        b01_closed = {m["profile"]: m["closed_trades"] for m in metrics if m["strategy"] == "B01"}
        out["validation"] = {**period, "b01_closed_trades": b01_closed,
                             "notes": "40건 확보만으로 LIVE 를 자동 승인하지 않는다. 표본 부족·시장 편중이면 기간을 연장한다."}
        out["s5"] = s5_panel(db, metrics, out["b02"], period, out["health"].get("non_virtual_rows") or 0)
        out["days"] = [dict(d) | {"notes": _loads(d["notes"], [])} for d in days[-30:]]
        out["day_inputs"] = [dict(r) | {"account_snapshot": None} for r in db.execute(
            "SELECT day, decision_status, decision_scan_log_id, frozen_by, signal_set_hash, account_status, "
            "account_reason, logic_hash, config_hash, NULL account_snapshot FROM ab_day_inputs "
            "ORDER BY day DESC LIMIT 30")]
        caps = db.execute("SELECT * FROM ab_scan_captures ORDER BY scan_log_id DESC LIMIT 40").fetchall()
        out["captures"] = [dict(c) for c in caps]
        out["capture_stats"] = dict(db.execute(
            "SELECT COUNT(*) total, SUM(complete=0) incomplete, SUM(source='CATCHUP') catchup, "
            "SUM(scan_status='ABANDONED') abandoned, SUM(recoverability='UNRECOVERABLE') unrecoverable, "
            "SUM(late_after_decision) late, SUM(rows_captured) rows FROM ab_scan_captures").fetchone())
        out["scan_reconciliation"] = out["health"].get("scan_reconciliation")
        out["late_scans"] = [dict(e) | {"details": _loads(e["details"], {})} for e in db.execute(
            "SELECT id, at, message, details FROM ab_events WHERE kind='LATE_SCAN_AFTER_DECISION' "
            "ORDER BY id DESC LIMIT 20")]
        out["unresolved"] = unresolved(db, run, settings)
        elig = db.execute(
            "SELECT s.trading_day day, e.pool, e.eligible, e.reasons FROM ab_eligibility e "
            "JOIN ab_signal_snapshots s ON s.id=e.snapshot_id WHERE e.strategy_version=? "
            "AND s.trading_day >= COALESCE((SELECT MIN(day) FROM (SELECT DISTINCT trading_day day FROM "
            "ab_signal_snapshots ORDER BY day DESC LIMIT 20)), '')", (S.STRATEGY_VERSION,)).fetchall()
        reasons = {"B00": {}, "B01": {}}
        eligible = {"B00": 0, "B01": 0}
        for e in elig:
            if e["eligible"]:
                eligible[e["pool"]] += 1
            for r in _loads(e["reasons"], []):
                reasons[e["pool"]][r] = reasons[e["pool"]].get(r, 0) + 1
        out["eligibility"] = {"eligible": eligible, "reasons": reasons, "window": "최근 20 판단일"}
        out["runs"] = [dict(r) | {"summary": _loads(r["summary"], {})} for r in db.execute(
            "SELECT run_id, status, reason, parent_run, through_day, logic_hash, config_hash, created_at, completed_at, "
            "summary FROM ab_runs ORDER BY run_id DESC LIMIT 20")]
        out["events"] = [dict(e) | {"details": _loads(e["details"], {})} for e in db.execute(
            "SELECT * FROM ab_events ORDER BY id DESC LIMIT 30").fetchall()]
    finally:
        db.close()
    return out


def unresolved(db, run, settings, now: Optional[datetime] = None) -> dict:
    """가격 미확정 포지션·체결 불명 주문. 삭제·위험 0 처리 없이 따로 보여 주고 전액 손실 스트레스를 계산한다."""
    from trading.auto_buy.shadow import calendar as CAL
    last = CAL.last_closed_session(now or datetime.now(timezone.utc), settings)
    pos = []
    for p in db.execute("SELECT * FROM ab_positions WHERE run_id=? AND state='OPEN' AND price_status='UNRESOLVED' "
                        "ORDER BY account_id, ticker", (run,)):
        since = date.fromisoformat(p["unresolved_since"])
        n = len(CAL.trading_days_between(since, last, settings)) if last else 0
        val = p["qty"] * (p["last_close"] or p["entry_price"])
        pos.append({"account_id": p["account_id"], "ticker": p["ticker"], "qty": p["qty"],
                    "entry_price": p["entry_price"], "stop_price": p["stop_price"], "last_close": p["last_close"],
                    "last_close_day": p["last_close_day"], "unresolved_since": p["unresolved_since"],
                    "missing_trading_days": n, "value_at_last_close": val,
                    "entry_risk": max(0.0, p["entry_price"] - p["stop_price"]) * p["qty"],
                    "classification": ("NO_DATA_SUSPECTED_HALT_OR_DELISTING" if n >= S.LONG_MISSING_DAYS
                                       else "GAP_PROVIDER_OR_HALT"),
                    "evidence": "가격·청산 근거 없음 (체결가를 만들지 않음)"})
    orders = [dict(o) for o in db.execute(
        "SELECT account_id, ticker, target_day, qty, limit_price, stop_price FROM ab_orders "
        "WHERE run_id=? AND state='FILL_UNKNOWN' ORDER BY target_day", (run,))]
    tickers = sorted({p["ticker"] for p in pos} | {o["ticker"] for o in orders})
    return {"positions": pos, "orders": orders, "tickers": tickers,
            "stress_full_loss_total": sum(p["value_at_last_close"] for p in pos)
            + sum(o["qty"] * o["limit_price"] for o in orders),
            "policy": "미확정 포지션은 신규 매수를 막는다. 차단 해제 조건·장기 미확정 처리(거래정지·상장폐지 확인 시 "
                      "평가 방법)는 별도 정책 승인 사항이며 이번 구현은 판정하지 않는다."}

def account_detail(account_id: str, settings: Optional[S.AutoBuySettings] = None) -> Optional[dict]:
    settings = settings or S.AutoBuySettings.from_env()
    if not schema_status(settings.journal_path)["schema_ready"]:
        return None
    db = ro_connect(settings.journal_path)
    try:
        acc = db.execute("SELECT * FROM ab_accounts WHERE id=?", (account_id,)).fetchone()
        run = _current_run(db)
        if acc is None or run is None:
            return None
        m = account_metrics(db, acc, run)
        a = (run, account_id)
        return {
            "run_id": run,
            "metrics": {k: v for k, v in m.items() if not k.startswith("_") and k != "r_values_incl_open"},
            "notice": VIRTUAL_NOTICE,
            "daily": [dict(d) for d in db.execute(
                "SELECT day, data_status, equity, cash, invested, reserved, entry_heat, current_heat, "
                "current_heat_level, sleeve_budget, sleeve_used, max_pos_w, max_sector_w, n_positions, n_pending, "
                "unresolved_positions, unresolved_orders, unresolved_value, stress_full_loss, safety_blocks "
                "FROM ab_daily WHERE run_id=? AND account_id=? ORDER BY day", a).fetchall()],
            "orders": [dict(o) for o in db.execute(
                "SELECT id, ticker, sector, decision_day, target_day, decision_price, limit_price, stop_price, qty, "
                "filled_qty, avg_fill_price, state, fill_kind, is_virtual, fill_model FROM ab_orders "
                "WHERE run_id=? AND account_id=? ORDER BY id DESC LIMIT 100", a).fetchall()],
            "fills": [dict(f) for f in db.execute(
                "SELECT f.order_id, o.ticker, f.day, f.price, f.applied_delta, f.basis, f.bar_open, f.bar_high, "
                "f.bar_low, f.bar_close, f.bar_provider, f.bar_fetched_at, f.bar_input_rev FROM ab_fill_ledger f "
                "JOIN ab_orders o ON o.id=f.order_id WHERE o.run_id=? AND o.account_id=? ORDER BY f.day DESC LIMIT 100",
                a).fetchall()],
            "positions": [dict(p) for p in db.execute(
                "SELECT id, ticker, sector, entry_day, entry_price, stop_price, qty, cost, last_close, price_status, "
                "unresolved_since, state, exit_day, exit_price, exit_reason, pnl, r_multiple, is_virtual "
                "FROM ab_positions WHERE run_id=? AND account_id=? ORDER BY id DESC LIMIT 100", a).fetchall()],
            "stops": [dict(s) | {"details": _loads(s["details"], {})} for s in db.execute(
                "SELECT e.day, p.ticker, e.reason, e.stop_price, e.exit_price, e.qty, e.cancelled_buy_qty, e.details "
                "FROM ab_stop_events e JOIN ab_positions p ON p.id=e.position_id WHERE e.run_id=? AND e.account_id=? "
                "ORDER BY e.id DESC LIMIT 100", a).fetchall()],
            "decisions": [dict(d) for d in db.execute(
                "SELECT d.day, s.ticker, s.signal_type, s.grade, s.signal_quality, d.outcome, d.reason, d.qty "
                "FROM ab_decisions d JOIN ab_signal_snapshots s ON s.id=d.snapshot_id WHERE d.run_id=? "
                "AND d.account_id=? ORDER BY d.day DESC, d.rank_value LIMIT 100", a).fetchall()],
        }
    finally:
        db.close()


def signals(day: Optional[str] = None, settings: Optional[S.AutoBuySettings] = None) -> Optional[dict]:
    settings = settings or S.AutoBuySettings.from_env()
    if not schema_status(settings.journal_path)["schema_ready"]:
        return None
    db = ro_connect(settings.journal_path)
    try:
        if not day:
            r = db.execute("SELECT MAX(trading_day) FROM ab_signal_snapshots").fetchone()
            day = r[0]
        di = db.execute("SELECT decision_status, decision_scan_log_id FROM ab_day_inputs WHERE day=?",
                        (day,)).fetchone()
        rows = db.execute(
            "SELECT s.id, s.scan_log_id, s.ticker, s.signal_type, s.signal_date, s.grade, s.signal_quality, "
            "s.market_condition, s.strict_passed, s.first_strict_pass, s.first_pass_basis, s.is_first_detection, "
            "s.decision_price, s.stop_pct, s.ext_pct, s.sector_name, s.intraday, s.late_after_decision, "
            "(SELECT decision_role FROM ab_eligibility WHERE snapshot_id=s.id AND pool='B00' AND strategy_version=?) "
            "role, "
            "(SELECT eligible FROM ab_eligibility WHERE snapshot_id=s.id AND pool='B00' AND strategy_version=?) b00, "
            "(SELECT reasons FROM ab_eligibility WHERE snapshot_id=s.id AND pool='B00' AND strategy_version=?) b00r, "
            "(SELECT eligible FROM ab_eligibility WHERE snapshot_id=s.id AND pool='B01' AND strategy_version=?) b01, "
            "(SELECT reasons FROM ab_eligibility WHERE snapshot_id=s.id AND pool='B01' AND strategy_version=?) b01r "
            "FROM ab_signal_snapshots s WHERE s.trading_day=? ORDER BY s.strict_passed DESC, s.ticker, s.scan_log_id "
            "LIMIT 500", (S.STRATEGY_VERSION,) * 5 + (day,)).fetchall()
        return {"day": day, "decision_status": di["decision_status"] if di else None,
                "decision_scan_log_id": di["decision_scan_log_id"] if di else None,
                "rows": [dict(r) | {"b00r": _loads(r["b00r"], None), "b01r": _loads(r["b01r"], None)}
                         for r in rows]}
    finally:
        db.close()
