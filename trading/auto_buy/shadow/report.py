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
from trading.auto_buy.shadow.vfills import unit_proceeds

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
           "strategy_version": S.STRATEGY_VERSION, "config_hash": S.config_hash(settings), "fill_model": S.FILL_MODEL,
           "journal_configured": bool(settings.journal_path), **{k: st[k] for k in
           ("journal_exists", "schema_ready", "schema_version", "expected_schema_version", "migration_required",
            "schema_error")}, "real_order_path": "없음 (OFF/SHADOW 전용 구현, 주문 모듈 미사용)",
           "notice": VIRTUAL_NOTICE, "s1_limit_notice": S1_LIMIT_NOTICE,
           "costs": {"basis": S.COST_BASIS, "simulation": S.COSTS, "ops_estimate": S.OPS_COST_ESTIMATE,
                     "note": "가상 손익은 P7 과 같은 비교용 비용으로 계산한다. 운영 추정 비용은 별도 기록(미검증)이며 손익에 쓰지 않는다"},
           "policy": S.POLICY}
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
                      "scan_reconciliation", "version_block", "replay_in_progress", "signal_set_integrity",
                      "integrity_day_block"):
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
            from trading.auto_buy.shadow.inputs import replay_state
            # RUNNING = 실행권이 살아 있는 재계산, STALLED = 작업자 응답 없음(자동 탈취 안 함, 수동 확인),
            # ABANDONED_SUSPECTED = 작업자 사망 확인·실행권 없는 잔존 BUILDING (다음 job 이 FAILED 로 정리). 잔존 BUILDING 을 '재계산 중'으로 계속 표시하지 않는다.
            out["replay_state"] = replay_state(db)
            out["abandoned_replays"] = db.execute(
                "SELECT COUNT(*) FROM ab_runs WHERE status='FAILED' AND summary LIKE '%ABANDONED_BUILD%'").fetchone()[0]
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


def profit_factor(pnls) -> Optional[float]:
    """거래별 총이익 / 총손실 (P7 ``profit_factor``·``profit_factor_incl_open`` 과 같은 정의). 총손실 0 이면 None."""
    gw = sum(x for x in pnls if x > 0)
    gl = -sum(x for x in pnls if x < 0)
    return gw / gl if gl > 0 else None


PERF_FIELDS = ("days", "incomplete_days", "last_day", "equity", "cum_return", "mdd", "closed_trades", "open_positions",
               "pf", "pf_incl_open", "open_pnl", "realized_pnl", "avg_r_closed", "avg_r_incl_open", "stop_rate", "orders",
               "order_states", "fill_rate", "entry_heat", "current_heat", "current_heat_level", "sleeve_budget",
               "sleeve_used", "sleeve_usage", "cash", "reserved", "max_pos_w", "max_sector_w", "data_status",
               "unresolved_positions", "unresolved_orders", "unresolved_value", "stress_full_loss",
               "stress_equity_after", "safety_blocks", "account_level", "block_reasons")
HOLD_NOTE = ("성과 평가 보류: 무결성 오류·거래일 격리·부분 처리 상태에서는 성과 원장의 정합성을 확인할 수 없어 성과 값을 "
             "계산하지 않는다 (NULL = 확인 불가, 0 아님). 원본 주문·포지션 조회는 진단용이며 정상 성과가 아니다.")


def performance_hold(db, run) -> list:
    """성과 평가를 보류할 사유 (Codex 재리뷰 head 1906a3f P2-3). 빈 목록 = 평가 가능.
    - INTEGRITY_DAY_BLOCK: 거래일 격리 표시 (자동 해제 없음)
    - SIGNAL_SET_INTEGRITY: 무결성 오류 표시 (운영자 확인 전)
    - PARTIAL_DAY: 일 기록(ab_days) 없이 일부 계좌 기록(ab_daily)만 있는 거래일 (중단·격리)
    호출 측은 같은 읽기 트랜잭션(스냅샷) 안에서 이 함수와 성과 집계를 함께 부른다."""
    out = []
    if _rt(db, "integrity_day_block"):
        out.append("INTEGRITY_DAY_BLOCK")
    if _rt(db, "signal_set_integrity"):
        out.append("SIGNAL_SET_INTEGRITY")
    if run is not None:
        part = db.execute("SELECT DISTINCT a.day FROM ab_daily a WHERE a.run_id=? AND NOT EXISTS (SELECT 1 FROM ab_days d "
                          "WHERE d.run_id=a.run_id AND d.day=a.day) ORDER BY a.day LIMIT 10", (run,)).fetchall()
        if part:
            out.append(f"PARTIAL_DAY:{[r[0] for r in part]}")
    return out


def _held_metrics(db, acc, run, hold) -> dict:
    """성과 보류: 성과·위험 값은 NULL, 원본 기록 수는 진단용으로 따로 둔다."""
    a = (run, acc["id"])
    one = lambda q: db.execute(q, a).fetchone()[0]  # noqa: E731
    out = {"id": acc["id"], "profile": acc["profile"], "strategy": acc["strategy"], "seed": acc["seed"],
           "start_day": acc["start_day"], "initial": None, **{k: None for k in PERF_FIELDS},
           "r_values_incl_open": [], "performance_status": "UNAVAILABLE", "performance_hold_reasons": hold,
           "performance_note": HOLD_NOTE}
    out["diagnostic_raw"] = {
        "note": "진단용 원본 기록 수 (정상 성과에 포함된 거래가 아님)",
        "daily_rows": one("SELECT COUNT(*) FROM ab_daily WHERE run_id=? AND account_id=?"),
        "orders": one("SELECT COUNT(*) FROM ab_orders WHERE run_id=? AND account_id=?"),
        "positions_open": one("SELECT COUNT(*) FROM ab_positions WHERE run_id=? AND account_id=? AND state='OPEN'"),
        "positions_closed": one("SELECT COUNT(*) FROM ab_positions WHERE run_id=? AND account_id=? AND state='CLOSED'"),
        "decisions": one("SELECT COUNT(*) FROM ab_decisions WHERE run_id=? AND account_id=?")}
    return out


def account_metrics(db, acc, run, hold: Optional[list] = None) -> dict:
    hold = performance_hold(db, run) if hold is None else hold
    if hold:
        return _held_metrics(db, acc, run, hold)
    # 일 기록(ab_days)이 있는 거래일만 성과로 쓴다 — 계좌 처리 중 무결성 오류로 격리된 날(일부 계좌만 커밋)은 제외
    daily = db.execute("SELECT * FROM ab_daily WHERE run_id=? AND account_id=? AND day IN "
                       "(SELECT day FROM ab_days WHERE run_id=?) ORDER BY day", (run, acc["id"], run)).fetchall()
    ar = db.execute("SELECT * FROM ab_account_runs WHERE run_id=? AND account_id=?", (run, acc["id"])).fetchone()
    eq = [d["equity"] for d in daily if d["equity"] is not None]
    base = ar["initial_cash"] if ar else None
    closed = db.execute("SELECT * FROM ab_positions WHERE run_id=? AND account_id=? AND state='CLOSED'",
                        (run, acc["id"])).fetchall()
    opened = db.execute("SELECT * FROM ab_positions WHERE run_id=? AND account_id=? AND state='OPEN'",
                        (run, acc["id"])).fetchall()
    rs = [p["r_multiple"] for p in closed if p["r_multiple"] is not None]
    # 미청산 평가: P7 과 같이 종목별로 매도 비용(수수료+매도세+슬리피지)을 뺀 가치로 평가하고, 상계하지 않는다
    open_r, open_pnls = [], []
    for p in opened:
        px = p["last_close"] or p["entry_price"]
        pnl = p["qty"] * unit_proceeds(px) - p["cost"]
        open_pnls.append(pnl)
        if p["risk0"] > 0:
            open_r.append(pnl / p["risk0"])
    open_pnl = sum(open_pnls)
    oc = {r["state"]: r["n"] for r in db.execute(
        "SELECT state, COUNT(*) n FROM ab_orders WHERE run_id=? AND account_id=? GROUP BY state", (run, acc["id"]))}
    blocks = db.execute("SELECT reason, COUNT(*) n FROM ab_decisions WHERE run_id=? AND account_id=? "
                        "AND outcome!='ORDERED' GROUP BY reason ORDER BY n DESC", (run, acc["id"])).fetchall()
    last = daily[-1] if daily else None
    return {
        "id": acc["id"], "profile": acc["profile"], "strategy": acc["strategy"], "seed": acc["seed"],
        "start_day": acc["start_day"], "initial": base, "days": len(daily),
        "incomplete_days": sum(1 for d in daily if d["data_status"] != "COMPLETE"),
        "last_day": last["day"] if last else None,
        "equity": eq[-1] if eq else None,
        "cum_return": (eq[-1] / base - 1) if eq and base else None,
        "mdd": _mdd(eq) if eq else None,
        "closed_trades": len(closed), "open_positions": len(opened),
        "pf": profit_factor([p["pnl"] for p in closed]),
        "pf_incl_open": profit_factor([p["pnl"] for p in closed] + open_pnls),     # 미청산은 종목별로, 상계 없음
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
        "performance_status": "AVAILABLE", "performance_hold_reasons": [],
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


OPERATIONAL_SAFETY_KEYS = ("non_virtual_rows", "error_events", "heartbeat_stale", "version_block",
                           "replay_abandoned_suspected", "replay_stalled", "signal_set_integrity",
                           "integrity_day_block")
DATA_INTEGRITY_KEYS = ("abandoned_scans", "unrecoverable_scans", "uncaptured_scans", "data_incomplete_days",
                       "fill_unknown_orders", "unresolved_positions", "manual_review_bars")
OBSERVED_RUNS = "('CURRENT','SUPERSEDED')"      # 실제로 결과로 쓰였던 run (FAILED·BUILDING 재계산 잔여는 제외)


def validation_start(db) -> Optional[str]:
    """S5 검증 시작일 (runner 가 처음 정할 때 고정해 저장). 기록이 없으면 처음 처리한 거래일."""
    v = _rt(db, f"s5_validation_start:{S.STRATEGY_VERSION}")
    if isinstance(v, dict) and v.get("day"):
        return v["day"]
    r = db.execute(f"SELECT MIN(d.day) FROM ab_days d JOIN ab_runs r ON r.run_id=d.run_id "
                   f"WHERE r.status IN {OBSERVED_RUNS}").fetchone()
    return r[0] if r and r[0] else None


STREAK_WARN_KINDS = ("replay_stalled", "uncaptured_scan")      # 그날 처리 구간에 있으면 정상 운영이 아닌 WARN 이벤트


def continuity(db, settings=None, now: Optional[datetime] = None) -> dict:
    """연속 정상 운영 평가. **거래소 캘린더의 기대 거래일**(고정된 검증 시작일부터, 주말·휴장 제외, 조기폐장 반영)마다
    '처음 처리한 결과'로 판정한다 (재계산으로 나중에 복구돼도 그날은 정상 운영이 아니었다).

    거래일이 정상이려면 모두 해당해야 한다:
    - 처리 기록이 있음 (없으면 ``DAY_NOT_PROCESSED`` — 기록 전체 누락도 위반, 연속 일수 초기화)
    - 처음 처리 결과가 COMPLETE
    - 그날 결정 스캔의 중단·복구 불가 수집 없음
    - 처리 지연 없음 (데이터 마감 + 30분 이후 처리는 runner 가 ``late_processing`` 에 기록)
    - 그날 처리 구간(전 거래일 처리 ~ 이 거래일 처리)에 ERROR 이벤트·실행권 정체·미수집 스캔 경고 없음
    아직 처리 기한(데이터 마감 + 30분)이 지나지 않았고 처리 기록도 없는 날은 ``PENDING`` 으로 두고 위반으로 보지 않는다."""
    from datetime import date as _date
    from trading.auto_buy.shadow import calendar as CAL
    from trading.auto_buy.shadow.inputs import bars_cutoff
    now = CAL._utc(now or datetime.now(timezone.utc))
    first = {}
    for r in db.execute(f"SELECT d.day, d.status, d.processed_at, d.run_id FROM ab_days d JOIN ab_runs r "
                        f"ON r.run_id=d.run_id WHERE r.status IN {OBSERVED_RUNS} ORDER BY d.day, d.processed_at"):
        first.setdefault(r[0], r)
    start = validation_start(db)
    need = S.S5_CRITERIA["milestones"]["safety_days"]
    base = {"required_consecutive_clean_days": need, "validation_start": start,
            "note": "거래소 기대 거래일 기준. 최초 5거래일이 모두 정상이어야 통과. 위반·누락이 있었으면 기록을 남기고 그 "
                    "이후 연속 5거래일 정상 운영을 따로 평가한다 (복구 성공 ≠ 무오류 운영)."}
    if not start:
        return {**base, "trading_days": 0, "expected_days": 0, "initial_window": "PENDING", "current_clean_streak": 0,
                "longest_clean_streak": 0, "last_violation_day": None, "violation_days": [], "pending_days": [],
                "errors_after_last_processed_day": 0, "functional_pass": False}
    last_closed = CAL.last_closed_session(now, settings)
    last_processed = max(first) if first else None
    end_s = max([d for d in (last_closed.isoformat() if last_closed else None, last_processed) if d] or [start])
    expected = [d.isoformat() for d in CAL.trading_days_between(_date.fromisoformat(start),
                                                                _date.fromisoformat(end_s), settings)] \
        if end_s >= start else []
    bad_scan_days = {r[0] for r in db.execute(
        "SELECT DISTINCT trading_day FROM ab_scan_captures WHERE trading_day IS NOT NULL AND intraday=0 AND "
        "(scan_status='ABANDONED' OR recoverability='UNRECOVERABLE')")}
    marks = [(r[0], r[1]) for r in db.execute(
        "SELECT at, CASE WHEN severity='ERROR' THEN 'ERROR' ELSE kind END FROM ab_events WHERE severity='ERROR' OR "
        f"kind IN ({','.join(repr(k) for k in STREAK_WARN_KINDS)}) ORDER BY at")]
    late = _rt(db, "late_processing") or {}
    rows, pending, prev_at = [], [], None
    for day in expected:
        rec = first.get(day)
        if rec is None:
            due = bars_cutoff(_date.fromisoformat(day), settings)
            past = (last_processed is not None and day < last_processed) or \
                (due is not None and now >= due + timedelta(minutes=30))
            if not past:
                pending.append(day)                # 처리 기한 전: 위반으로 오판하지 않음
                continue
            rows.append({"day": day, "clean": False, "reasons": ["DAY_NOT_PROCESSED"]})
            continue
        _, status, at, _run = rec
        hits = [k for t, k in marks if (prev_at is None or t > prev_at) and t <= (at or 0)]
        reasons = []
        if status != "COMPLETE":
            reasons.append("DATA_INCOMPLETE_AT_FIRST_PROCESSING")
        if day in bad_scan_days:
            reasons.append("SCAN_ABANDONED_OR_UNRECOVERABLE")
        if hits.count("ERROR"):
            reasons.append(f"ERROR_EVENTS:{hits.count('ERROR')}")
        for k in STREAK_WARN_KINDS:
            if k in hits:
                reasons.append(k.upper())
        if day in late:
            reasons.append("LATE_PROCESSING")
        rows.append({"day": day, "clean": not reasons, "reasons": reasons})
        prev_at = at
    after = [t for t, _k in marks if prev_at is None or t > prev_at]     # 마지막 처리 이후 오류 (아직 날짜 미배정)
    longest = cur = 0
    for x in rows:
        cur = cur + 1 if x["clean"] else 0
        longest = max(longest, cur)
    if after:
        cur = 0
    init = rows[:need]
    initial = ("PASS" if len(init) == need and all(x["clean"] for x in init)
               else "PENDING" if all(x["clean"] for x in init) and not after else "FAIL")
    violations = [x for x in rows if not x["clean"]]
    return {**base, "trading_days": len(rows), "expected_days": len(expected),
            "initial_window": initial, "current_clean_streak": cur, "longest_clean_streak": longest,
            "last_violation_day": violations[-1]["day"] if violations else None,
            "violation_days": violations[-20:], "pending_days": pending, "errors_after_last_processed_day": len(after),
            "functional_pass": cur >= need}


def safety_violations(db, health: dict, settings=None, now: Optional[datetime] = None) -> dict:
    """S5 안전 위반 집계 (기준: 0건. 완화하지 않는다).

    - 관찰 기간 위반(observation period): 검증 기간 중 발생한 위반. 재계산으로 복구돼도 지우지 않는다. S5 안전 위반 수.
      운영 안전: 비가상 행, ERROR 이벤트(종류별), 현재 heartbeat 지연·버전 차단·재계산 비정상 종료 의심·응답 없음.
      데이터 완전성: 중단(ABANDONED)·복구 불가(UNRECOVERABLE)·미수집 스캔, DATA_INCOMPLETE 거래일·FILL_UNKNOWN 주문·
      가격 미확정 포지션(결과로 쓰인 모든 run 기준, 같은 거래일·신호는 한 번), 복구 기간이 지난 결측 일봉.
    - 현재 미해결(current unresolved): 지금 남아 있는 것 (현재 run 기준).
    - 복구됨(recovered): 관찰 기간에 있었으나 지금은 해소된 것 (재계산 복구 성공 — 무오류 운영과 구분).
    - 복구 불가(unrecoverable): 복구 불가 스캔·복구 기간이 지난 결측.
    - 마감 전 재시도(pre_cutoff_retries): 데이터 마감 전에 기다렸다가 정상으로 확정된 경우. 위반이 아니며 따로 표시.
    - 연속 정상 운영(continuity): 최초 5거래일 / 위반 후 연속 5거래일."""
    from trading.auto_buy.shadow.inputs import expired_missing_bars, recovery_floor
    errors = {r[0]: r[1] for r in db.execute("SELECT kind, COUNT(*) FROM ab_events WHERE severity='ERROR' GROUP BY kind")}
    rec = health.get("scan_reconciliation") or {}
    run = _current_run(db)
    one = lambda q, *a: int(db.execute(q, a).fetchone()[0] or 0)  # noqa: E731
    rs = health.get("replay_state") or {}
    try:
        from trading.auto_buy.shadow import calendar as CAL
        last = CAL.last_closed_session(datetime.now(timezone.utc), settings)
        manual = len(expired_missing_bars(db, recovery_floor(last, settings)))
    except Exception:
        manual = 0
    warn = {r[0]: r[1] for r in db.execute("SELECT kind, COUNT(*) FROM ab_events WHERE severity='WARN' AND kind IN "
                                           "('replay_stalled','heartbeat_gap','uncaptured_scan') GROUP BY kind")}
    hb_now = int((health.get("heartbeat") or {}).get("status") == "STALE")
    stalled_now = int(rs.get("status") == "STALLED")
    stall_run = (rs.get("lease") or {}).get("run_id") if stalled_now else None
    stall_recorded = stalled_now and _rt(db, "replay_stall_active") == stall_run
    # 관찰 기간 누적 (영속 이벤트 기반, 복구돼도 줄지 않음) + 아직 이벤트로 기록되기 전의 현재 상태
    op = {"non_virtual_rows": int(health.get("non_virtual_rows") or 0),
          "error_events": int(sum(errors.values())),
          "heartbeat_stale": int(warn.get("heartbeat_gap", 0)) + hb_now,
          "version_block": int(bool(health.get("version_block"))),
          "replay_abandoned_suspected": int(rs.get("status") == "ABANDONED_SUSPECTED"),
          "replay_stalled": int(warn.get("replay_stalled", 0)) + int(stalled_now and not stall_recorded),
          # 동결 신호 집합 무결성 오류 (자동으로 지우지 않음 — 운영자 확인 전까지 관찰 기간·현재 미해결 모두 위반)
          "signal_set_integrity": int(bool(health.get("signal_set_integrity"))),
          # 계좌 처리 중 무결성 오류로 격리된 거래일 (자동 해제 없음)
          "integrity_day_block": int(bool(health.get("integrity_day_block")))}
    obs_days = one(f"SELECT COUNT(DISTINCT d.day) FROM ab_days d JOIN ab_runs r ON r.run_id=d.run_id "
                   f"WHERE r.status IN {OBSERVED_RUNS} AND d.status!='COMPLETE'")
    obs_fu = one(f"SELECT COUNT(*) FROM (SELECT DISTINCT o.account_id, o.signal_key, o.target_day FROM ab_orders o "
                 f"JOIN ab_runs r ON r.run_id=o.run_id WHERE r.status IN {OBSERVED_RUNS} AND o.state='FILL_UNKNOWN')")
    obs_up = one(f"SELECT COUNT(*) FROM (SELECT DISTINCT p.account_id, p.signal_key FROM ab_positions p "
                 f"JOIN ab_runs r ON r.run_id=p.run_id WHERE r.status IN {OBSERVED_RUNS} "
                 f"AND (p.price_status='UNRESOLVED' OR p.unresolved_since IS NOT NULL))")
    di = {"abandoned_scans": one("SELECT COUNT(*) FROM ab_scan_captures WHERE scan_status='ABANDONED'"),
          "unrecoverable_scans": one("SELECT COUNT(*) FROM ab_scan_captures WHERE recoverability='UNRECOVERABLE' "
                                     "AND scan_status!='ABANDONED'"),
          "uncaptured_scans": max(int(warn.get("uncaptured_scan", 0)), int(rec.get("uncaptured") or 0)),
          "data_incomplete_days": obs_days, "fill_unknown_orders": obs_fu, "unresolved_positions": obs_up,
          "manual_review_bars": max(manual, len(_rt(db, "manual_review_flagged") or []))}
    cur = {"data_incomplete_days": one("SELECT COUNT(*) FROM ab_days WHERE run_id=? AND status!='COMPLETE'", run),
           "fill_unknown_orders": one("SELECT COUNT(*) FROM ab_orders WHERE run_id=? AND state='FILL_UNKNOWN'", run),
           "unresolved_positions": one("SELECT COUNT(*) FROM ab_positions WHERE run_id=? AND state='OPEN' "
                                       "AND price_status='UNRESOLVED'", run),
           "uncaptured_scans": int(rec.get("uncaptured") or 0), "manual_review_bars": manual,
           "heartbeat_stale": hb_now, "version_block": op["version_block"],
           "replay_abandoned_suspected": op["replay_abandoned_suspected"], "replay_stalled": stalled_now,
           "signal_set_integrity": op["signal_set_integrity"], "integrity_day_block": op["integrity_day_block"],
           "non_virtual_rows": op["non_virtual_rows"]}
    recovered = {k: max(0, di[k] - cur[k]) for k in ("data_incomplete_days", "fill_unknown_orders",
                                                     "unresolved_positions", "uncaptured_scans")}
    recovered.update({k: max(0, op[k] - cur[k]) for k in ("heartbeat_stale", "replay_stalled")})
    unrec = {"unrecoverable_scans": one("SELECT COUNT(*) FROM ab_scan_captures WHERE recoverability='UNRECOVERABLE'"),
             "manual_review_bars": manual}
    waits = {}
    for r in db.execute("SELECT value FROM ab_runtime WHERE key='pre_cutoff_waits'"):
        waits = _loads(r[0], {}) or {}
    first_status = {}
    for r in db.execute(f"SELECT d.day, d.status FROM ab_days d JOIN ab_runs r ON r.run_id=d.run_id "
                        f"WHERE r.status IN {OBSERVED_RUNS} ORDER BY d.day, d.processed_at"):
        first_status.setdefault(r[0], r[1])
    pre = {"days_with_retries": len(waits), "retries": int(sum(int(v) for v in waits.values())),
           "resolved_before_cutoff": sorted(d for d in waits if first_status.get(d) == "COMPLETE"),
           "final_incomplete": sorted(d for d in waits if first_status.get(d) == "DATA_INCOMPLETE"),
           "note": "마감 전 재시도 후 정상 확정은 위반이 아니다. 마감 후 DATA_INCOMPLETE 는 위반으로 센다."}
    out = {**op, **di, "error_kinds": errors, "operational": op, "data_integrity": di,
           "operational_total": sum(op.values()), "data_integrity_total": sum(di.values())}
    out["total"] = out["operational_total"] + out["data_integrity_total"]
    out["observation_period_total"] = out["total"]
    out["current_unresolved"] = cur
    out["current_unresolved_total"] = sum(cur.values())
    out["recovered"] = recovered
    out["recovered_total"] = sum(recovered.values())
    out["unrecoverable"] = unrec
    out["unrecoverable_total"] = sum(unrec.values())
    out["pre_cutoff_retries"] = pre
    out["continuity"] = continuity(db, settings, now)
    out["pass"] = out["total"] == 0 and out["current_unresolved_total"] == 0
    return out


def s5_panel(db, metrics: list, b02: dict, period: dict, safety: dict) -> dict:
    """S5 기준(평가 시작 전 고정)에 대한 현재 상태. 판정은 고정: NOT_RECOMMENDED 유지, B01 = NEED_MORE_DATA."""
    crit = S.S5_CRITERIA
    out = {"criteria": crit, "profiles": {}, "verdict": "NOT_RECOMMENDED", "b01_status": "NEED_MORE_DATA",
           "note": "기준 충족 여부는 검토 자료일 뿐 LIVE 승인 조건이 아닙니다 (별도 승인 필요).",
           "safety_pass": safety["pass"], "safety_violations": safety["total"], "safety_breakdown": safety}
    hold = sorted({r for m in metrics for r in (m.get("performance_hold_reasons") or [])})
    if hold:      # 성과 보류: 성과 기준 비교·순위·채택 판정을 하지 않는다 (안전 위반·격리 이력은 그대로 표시)
        out.update(performance_status="UNAVAILABLE", performance_hold_reasons=hold, adoption="UNAVAILABLE",
                   performance_note=HOLD_NOTE)
        for prof in S.PROFILES:
            out["profiles"][prof] = {"performance_status": "UNAVAILABLE", "performance_hold_reasons": hold,
                                     "trading_days": period["trading_days"], "safety_pass": safety["pass"],
                                     "safety_violations": safety["total"], "safety_breakdown": safety,
                                     "functional_5d": safety["continuity"]}
        return out
    out.update(performance_status="AVAILABLE", performance_hold_reasons=[])
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
            "safety_violations": safety["total"],
            "safety_operational": safety["operational_total"],
            "safety_data_integrity": safety["data_integrity_total"],
            "safety_pass": safety["pass"],
            "safety_current_unresolved": safety["current_unresolved_total"],
            "safety_observation_period": safety["observation_period_total"],
            "safety_recovered": safety["recovered_total"],
            "safety_unrecoverable": safety["unrecoverable_total"],
            "functional_5d": safety["continuity"],
            "safety_breakdown": safety,
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
        db.execute("BEGIN")          # 한 읽기 스냅샷: 격리·무결성 표시와 성과 집계가 같은 시점의 DB 를 본다
        for k in ("signal_set_integrity", "integrity_day_block"):
            out["health"][k] = _rt(db, k)
        run = _current_run(db)
        hold = performance_hold(db, run)
        out["performance"] = {"status": "UNAVAILABLE" if hold else "AVAILABLE", "hold_reasons": hold,
                              "note": HOLD_NOTE if hold else None}
        accs = db.execute("SELECT * FROM ab_accounts WHERE strategy_version=? ORDER BY id",
                          (S.STRATEGY_VERSION,)).fetchall()
        metrics = [account_metrics(db, a, run, hold) for a in accs] if run is not None else []
        out["accounts"] = [{k: v for k, v in m.items() if not k.startswith("_") and k != "r_values_incl_open"}
                           for m in metrics if m["strategy"] != "B02"]
        out["b02"] = {}
        for prof in S.PROFILES:
            b02 = [m for m in metrics if m["profile"] == prof and m["strategy"] == "B02"]
            b01 = next((m for m in metrics if m["profile"] == prof and m["strategy"] == "B01"), None)
            rets = [m["cum_return"] for m in b02]
            valid = sorted(r for r in rets if r is not None)
            if hold:
                out["b02"][prof] = {"accounts": len(b02), "median_return": None, "min_return": None, "max_return": None,
                                    "b01_percentile": None, "median_closed_trades": None,
                                    "performance_status": "UNAVAILABLE"}
                continue
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
                  "replay_in_progress": (out["health"].get("replay_state") or {}).get("status") == "RUNNING",
                  "replay_abandoned_suspected":
                      (out["health"].get("replay_state") or {}).get("status") == "ABANDONED_SUSPECTED",
                  "replay_stalled": (out["health"].get("replay_state") or {}).get("status") == "STALLED"}
        period["complete"] = (not hold and period["data_incomplete_days"] == 0 and not period["replay_in_progress"]
                              and not period["replay_abandoned_suspected"] and not period["replay_stalled"]
                              and not any(m["unresolved_positions"] or m["unresolved_orders"] for m in metrics))
        period["performance_status"] = "UNAVAILABLE" if hold else "AVAILABLE"
        period["basis"] = "모든 전략 동일 기준: 같은 거래일 입력(결정 스캔·일봉·계좌 스냅샷)을 공유한다"
        b01_closed = {m["profile"]: m["closed_trades"] for m in metrics if m["strategy"] == "B01"}
        out["validation"] = {**period, "b01_closed_trades": b01_closed,
                             "notes": "40건 확보만으로 LIVE 를 자동 승인하지 않는다. 표본 부족·시장 편중이면 기간을 연장한다."}
        out["s5"] = s5_panel(db, metrics, out["b02"], period, safety_violations(db, out["health"], settings))
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
    from trading.auto_buy.shadow.inputs import expired_missing_bars, recovery_floor
    last = CAL.last_closed_session(now or datetime.now(timezone.utc), settings)
    floor = recovery_floor(last, settings)
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
                    "evidence": "가격·청산 근거 없음 (체결가를 만들지 않음)",
                    "manual_review": bool(floor and since < floor)})
    orders = [dict(o) for o in db.execute(
        "SELECT account_id, ticker, target_day, qty, limit_price, stop_price FROM ab_orders "
        "WHERE run_id=? AND state='FILL_UNKNOWN' ORDER BY target_day", (run,))]
    for o in orders:
        o["manual_review"] = bool(floor and date.fromisoformat(o["target_day"]) < floor)
    tickers = sorted({p["ticker"] for p in pos} | {o["ticker"] for o in orders})
    review = sorted({p["ticker"] for p in pos if p["manual_review"]} | {o["ticker"] for o in orders if o["manual_review"]})
    return {"positions": pos, "orders": orders, "tickers": tickers, "manual_review_tickers": review,
            "manual_review_bars": expired_missing_bars(db, floor),
            "recovery_window_days": S.RECOVERY_WINDOW_DAYS, "recovery_floor": floor.isoformat() if floor else None,
            "stress_full_loss_total": sum(p["value_at_last_close"] for p in pos)
            + sum(o["qty"] * o["limit_price"] for o in orders),
            "policy": S.POLICY["unresolved_position"] + f". 자동 복구는 최근 {S.RECOVERY_WINDOW_DAYS}거래일까지이며, "
                      "그 이후에도 결측 위험·불완전 상태를 유지하고 수동 검토 대상(MANUAL_REVIEW)으로 표시한다. "
                      "거래정지·상장폐지·공급자 누락은 판정하지 않는다 (분류는 결측 기간 기준 표시)."}


def account_detail(account_id: str, settings: Optional[S.AutoBuySettings] = None) -> Optional[dict]:
    settings = settings or S.AutoBuySettings.from_env()
    if not schema_status(settings.journal_path)["schema_ready"]:
        return None
    db = ro_connect(settings.journal_path)
    try:
        db.execute("BEGIN")          # 성과 보류 판단과 원본 조회를 한 읽기 스냅샷에서
        acc = db.execute("SELECT * FROM ab_accounts WHERE id=?", (account_id,)).fetchone()
        run = _current_run(db)
        if acc is None or run is None:
            return None
        hold = performance_hold(db, run)
        m = account_metrics(db, acc, run, hold)
        a = (run, account_id)
        return {
            "run_id": run,
            "metrics": {k: v for k, v in m.items() if not k.startswith("_") and k != "r_values_incl_open"},
            "performance_status": "UNAVAILABLE" if hold else "AVAILABLE", "performance_hold_reasons": hold,
            "records_notice": ("아래 일별·주문·체결·포지션·판단은 진단용 원본 기록이며 성과 보류 중에는 정상 성과에 포함된 거래가 아니다"
                               if hold else None),
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
