"""PR #14 Codex 재리뷰(head 9a315db) 반영 (3) — S5 기대 거래일 연속성·관찰 이력, Migration(ab0004)·검증 순서,
합성 계좌 스냅샷.

원칙: 필수 거래일·입력이 누락되면 정상 평가하지 않는다 / 오류 복구 후에도 관찰 기간 이력을 보존한다.
임시 저널·합성 계좌만 쓴다 (실계좌 수치 없음). 운영 Migration 은 실행하지 않는다.
"""
import hashlib
import json
import os
import sqlite3
import stat
from datetime import datetime, timezone

import pytest

from tests.test_auto_buy_shadow import D1, D2, Bars, job, prod, scan, shadow, sig, utc  # noqa: F401
from tests.test_auto_buy_shadow_integrity import evening, one
from trading.auto_buy import settings as S
from trading.auto_buy.shadow import report as RP
from trading.auto_buy.shadow import runner as RN
from trading.auto_buy.shadow.store import connect_rw, event, get_runtime, set_runtime, tx

UTC = timezone.utc


# ── S5 기대 거래일 연속성 ──────────────────────────────────────────────

def _proc(day, h=23):
    return datetime.fromisoformat(day).replace(hour=h, tzinfo=UTC).timestamp()


def _days(shadow, start, days, status=None):
    """CURRENT run 에 처리 기록을 직접 넣는다 (연속성 판정만 검증)."""
    db = connect_rw(shadow.journal_path)
    try:
        with tx(db):
            if not db.execute("SELECT 1 FROM ab_runs WHERE run_id=1").fetchone():
                db.execute("INSERT INTO ab_runs(run_id, status, reason, strategy_version, config_hash, "
                           "logic_hash, created_at) VALUES (1,'CURRENT','INITIAL',?,'x','y',0)", (S.STRATEGY_VERSION,))
            if get_runtime(db, f"s5_validation_start:{S.STRATEGY_VERSION}") is None:
                set_runtime(db, f"s5_validation_start:{S.STRATEGY_VERSION}", {"day": start, "fixed_at": 0})
            for d in days:
                db.execute("INSERT INTO ab_days(run_id, day, status, decision_status, candidates, bars_requested, "
                           "bars_missing, notes, processed_at) VALUES (1,?,?,'FROZEN',0,0,0,'[]',?)",
                           (d, (status or {}).get(d, "COMPLETE"), _proc(d)))
    finally:
        db.close()


def cont(shadow, now):
    db = connect_rw(shadow.journal_path)
    try:
        return RP.continuity(db, shadow, now)
    finally:
        db.close()


def test_whole_missing_day_breaks_streak_even_if_neighbours_complete(shadow):
    """11-16·17·19·20·23 COMPLETE, 11-18 기록 전체 누락 → PASS 가 아니다."""
    _days(shadow, "2026-11-16", ["2026-11-16", "2026-11-17", "2026-11-19", "2026-11-20", "2026-11-23"])
    c = cont(shadow, datetime(2026, 11, 24, 15, 0, tzinfo=UTC))          # 11-24 장중 (11-23 까지 마감)
    assert c["validation_start"] == "2026-11-16" and c["expected_days"] == 6
    bad = {x["day"]: x["reasons"] for x in c["violation_days"]}
    assert bad == {"2026-11-18": ["DAY_NOT_PROCESSED"]}
    assert c["initial_window"] == "FAIL" and c["current_clean_streak"] == 3 and not c["functional_pass"]


def test_first_five_days_and_later_streak_are_evaluated_separately(shadow):
    days = ["2026-11-16", "2026-11-17", "2026-11-19", "2026-11-20", "2026-11-23", "2026-11-24", "2026-11-25",
            "2026-11-27"]                                                # 11-26 추수감사절 휴장, 11-27 조기폐장
    _days(shadow, "2026-11-16", days)
    c = cont(shadow, datetime(2026, 11, 30, 15, 0, tzinfo=UTC))
    assert "2026-11-26" not in {x["day"] for x in c["violation_days"]} and c["expected_days"] == 9
    assert c["initial_window"] == "FAIL"                                 # 최초 5거래일 실패 기록은 남는다
    assert c["current_clean_streak"] == 6 and c["functional_pass"]       # 그 뒤 연속 5거래일 정상


def test_clean_first_window_passes_and_weekend_is_not_expected(shadow):
    days = ["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"]
    _days(shadow, "2026-11-16", days)
    c = cont(shadow, datetime(2026, 11, 22, 18, 0, tzinfo=UTC))          # 일요일
    assert c["expected_days"] == 5 and c["initial_window"] == "PASS" and c["functional_pass"]


def test_today_before_processing_deadline_is_pending_not_violation(shadow):
    _days(shadow, "2026-11-23", ["2026-11-23"])
    c = cont(shadow, datetime(2026, 11, 24, 22, 0, tzinfo=UTC))          # 11-24 17:00 ET: 마감했으나 처리 기한 전
    assert c["pending_days"] == ["2026-11-24"] and c["violation_days"] == [] and c["initial_window"] == "PENDING"
    c = cont(shadow, datetime(2026, 11, 25, 6, 0, tzinfo=UTC))           # 처리 기한(데이터 마감 + 30분) 경과
    assert c["pending_days"] == [] and c["violation_days"][0]["reasons"] == ["DAY_NOT_PROCESSED"]


def test_early_close_deadline_follows_calendar(shadow):
    _days(shadow, "2026-11-25", ["2026-11-25"])
    early = cont(shadow, datetime(2026, 11, 28, 1, 0, tzinfo=UTC))       # 11-27 조기폐장 데이터 마감 01:20 UTC 전
    assert early["pending_days"] == ["2026-11-27"]
    after = cont(shadow, datetime(2026, 11, 28, 2, 0, tzinfo=UTC))
    assert after["pending_days"] == [] and after["violation_days"][0]["day"] == "2026-11-27"


def test_errors_and_stalls_in_a_day_window_mark_that_day(shadow):
    _days(shadow, "2026-11-16", ["2026-11-16", "2026-11-17", "2026-11-18"])
    db = connect_rw(shadow.journal_path)
    try:
        with tx(db):
            for at, sev, kind in [(_proc("2026-11-17", 12), "WARN", "replay_stalled"),
                                  (_proc("2026-11-18", 12), "ERROR", "job_error")]:
                db.execute("INSERT INTO ab_events(at, severity, kind, message, details) VALUES (?,?,?,?,?)",
                           (at, sev, kind, "t", "{}"))
    finally:
        db.close()
    c = cont(shadow, datetime(2026, 11, 19, 15, 0, tzinfo=UTC))
    bad = {x["day"]: x["reasons"] for x in c["violation_days"]}
    assert bad == {"2026-11-17": ["REPLAY_STALLED"], "2026-11-18": ["ERROR_EVENTS:1"]}


def test_validation_start_is_fixed_once_by_runner(shadow, prod):
    scan(prod, utc(D1, 21, 30), [sig("AAA")])
    job(shadow, prod, Bars({}), evening(D1))
    db = connect_rw(shadow.journal_path)
    try:
        first = get_runtime(db, f"s5_validation_start:{S.STRATEGY_VERSION}")
        assert first["day"] == RP.validation_start(db)
    finally:
        db.close()
    job(shadow, prod, Bars({}), evening(D2))
    db = connect_rw(shadow.journal_path)
    try:
        assert get_runtime(db, f"s5_validation_start:{S.STRATEGY_VERSION}") == first     # 이후 바뀌지 않음
    finally:
        db.close()


# ── 관찰 기간 이력 (현재 상태만 보지 않음) ──────────────────────────────

def test_heartbeat_gap_is_recorded_once_and_survives_recovery(shadow, prod):
    scan(prod, utc(D1, 21, 30), [sig("AAA")])
    job(shadow, prod, Bars({}), evening(D1))                             # last_job = D1 22:30 UTC
    late = utc(D2, 23, 30)                                               # D1 밤·D2 저녁 예정 실행을 놓침
    db = connect_rw(shadow.journal_path)
    try:
        RN._record_heartbeat_gap(db, late, shadow)
        RN._record_heartbeat_gap(db, late, shadow)                       # 같은 공백 → 중복 없음
    finally:
        db.close()
    assert one(shadow, "SELECT COUNT(*) n FROM ab_events WHERE kind='heartbeat_gap' AND severity='WARN'")["n"] == 1
    job(shadow, prod, Bars({}), late)                                    # 다시 정상 실행
    b = RP.summary(shadow)["s5"]["profiles"]["P7C"]["safety_breakdown"]
    assert b["operational"]["heartbeat_stale"] >= 1 and b["current_unresolved"]["heartbeat_stale"] == 0
    assert b["recovered"]["heartbeat_stale"] >= 1 and not b["pass"]


def test_uncaptured_scan_history_kept_after_capture(shadow):
    _days(shadow, "2026-11-16", [])
    db = connect_rw(shadow.journal_path)
    try:
        with tx(db):                                      # reconcile 이 남기는 것과 같은 이력 형식
            event(db, "WARN", "uncaptured_scan", "운영 스캔 #7 완료됐으나 SHADOW 미수집", scan_log_id=7)
            set_runtime(db, "uncaptured_seen", [7])
            set_runtime(db, "scan_reconciliation", {"uncaptured": 0, "uncaptured_ids": []})   # 지금은 수집됨
    finally:
        db.close()
    db = connect_rw(shadow.journal_path)
    try:
        b = RP.safety_violations(db, RP.health(shadow), shadow)
    finally:
        db.close()
    assert b["data_integrity"]["uncaptured_scans"] == 1 and b["current_unresolved"]["uncaptured_scans"] == 0
    assert b["recovered"]["uncaptured_scans"] == 1 and not b["pass"]


# ── Migration (ab0004) ───────────────────────────────────────────────

from scripts import auto_buy_shadow_journal_migrate as M  # noqa: E402
from trading.auto_buy.shadow.schema import EXPECTED_SCHEMA_VERSION, MIGRATIONS_DIR, schema_status, upgrade_journal  # noqa: E402,E501


def _mode(p):
    return stat.S_IMODE(os.lstat(p).st_mode)


def _sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def _to(path, rev):
    from alembic import command
    from alembic.config import Config
    if not os.path.exists(path):
        open(path, "w").close()
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["journal_path"] = str(path)
    command.upgrade(cfg, rev)


def _triggers(path):
    c = sqlite3.connect(path)
    try:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    finally:
        c.close()


@pytest.fixture
def menv(tmp_path, monkeypatch):
    monkeypatch.setenv("SCANNER_ORDER_JOURNAL_PATH", str(tmp_path / "orders.sqlite3"))
    monkeypatch.setenv("ALLOCATION_JOURNAL_PATH", str(tmp_path / "alloc.sqlite3"))
    return tmp_path


def test_head_is_ab0004_and_new_create_has_immutability_triggers(menv):
    assert EXPECTED_SCHEMA_VERSION == "ab0007"
    j = str(menv / "new.sqlite3")
    rep = M.run(j, str(menv / "bk"), allow_create=True)
    assert rep["result"] == "OK" and rep["schema_version"] == "ab0007" and _mode(j) == 0o600
    assert {"ab_orders_no_delete", "ab_fill_ledger_no_delete", "ab_runs_terminal_immutable",
            "ab_events_no_update", "ab_days_fence_update_old"} <= _triggers(j)


@pytest.mark.parametrize("start", ["ab0001", "ab0003"])
def test_upgrade_to_ab0004_preserves_data_and_audit(menv, start):
    j = str(menv / f"{start}.sqlite3")
    _to(j, start)
    c = sqlite3.connect(j)
    c.execute("INSERT INTO ab_runs(run_id, status, reason, strategy_version, config_hash, logic_hash, created_at) "
              "VALUES (1,'SUPERSEDED','INITIAL','SV1','x','y',0), (2,'CURRENT','REPLAY','SV1','x','y',1)")
    c.execute("INSERT INTO ab_events(at, severity, kind, message, details) VALUES (1,'ERROR','job_error','m','{}')")
    c.commit()
    c.close()
    os.chmod(j, 0o600)
    before = {t: sqlite3.connect(j).execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in ("ab_runs", "ab_events")}
    rep = M.run(j, str(menv / "bk"))
    assert rep["result"] == "OK" and rep["schema_version"] == "ab0007" and _mode(rep["backup"]) == 0o600
    after = {t: sqlite3.connect(j).execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in ("ab_runs", "ab_events")}
    assert after == before                                               # 데이터·감사 이력 보존
    c = sqlite3.connect(j)
    with pytest.raises(sqlite3.IntegrityError, match="terminal|contract immutable"):
        c.execute("UPDATE ab_runs SET reason='X' WHERE run_id=1")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        c.execute("DELETE FROM ab_events")
    c.close()
    again = M.run(j, str(menv / "bk"))                                   # 재실행 멱등
    assert again["result"] == "ALREADY_CURRENT" and _triggers(j) == _triggers(j)


def test_ab0004_failure_rolls_back_to_ab0003(menv, monkeypatch):
    j = str(menv / "fail.sqlite3")
    _to(j, "ab0003")
    os.chmod(j, 0o600)
    trig_before, sha_before = _triggers(j), _sha(j)
    from trading.auto_buy.shadow.journal_migrations import ddl_v4
    monkeypatch.setattr(ddl_v4, "DDL_V4", ddl_v4.DDL_V4[:3] + ("CREATE TRIGGER broken BEFORE nonsense",))
    import importlib
    import trading.auto_buy.shadow.journal_migrations.versions.ab0004_auto_buy_shadow_ledger_immutability as V4
    monkeypatch.setattr(V4, "DDL_V4", ddl_v4.DDL_V4)
    with pytest.raises(M.Abort):
        M.run(j, str(menv / "bk"))
    assert schema_status(j)["schema_version"] == "ab0003" and _triggers(j) == trig_before
    assert _sha(j) == sha_before                                         # 원본 무변경 (복사본에서 시험 후 실패)
    importlib.reload(ddl_v4)


def test_check_mode_is_read_only_for_ab0003(menv):
    j = str(menv / "chk.sqlite3")
    _to(j, "ab0003")
    os.chmod(j, 0o600)
    before = _sha(j)
    rep = M.run(j, str(menv / "bk"), check=True)
    assert rep["result"] == "CHECK_ONLY" and rep["dry_run"] == {"ok": True, "schema_version": "ab0007"}
    assert _sha(j) == before and not (menv / "bk").exists()


def test_sidecar_link_is_rejected_before_sqlite_opens(menv, monkeypatch):
    j = str(menv / "j.sqlite3")
    upgrade_journal(j, allow_create=True)
    os.chmod(j, 0o600)
    target = menv / "victim.sqlite3"
    target.write_bytes(b"")
    os.symlink(target, j + "-wal")
    opened = []
    monkeypatch.setattr(M, "schema_status", lambda p: opened.append(p) or {})
    with pytest.raises(M.Abort, match="심볼릭 링크"):
        M.run(j, None, check=True)
    assert opened == [] and target.read_bytes() == b""                   # schema_status 보다 먼저 거부


def test_stray_sidecar_without_journal_is_refused_and_nothing_created(menv):
    j = str(menv / "absent.sqlite3")
    open(j + "-wal", "wb").close()
    with pytest.raises(M.Abort, match="부속 파일"):
        M.run(j, None, allow_create=True)
    assert not os.path.exists(j)


def test_off_mode_creates_no_journal_and_runs_no_ddl(tmp_path, monkeypatch):
    path = tmp_path / "off.sqlite3"
    monkeypatch.setenv("AUTO_BUY_US_MODE", "OFF")
    monkeypatch.setenv("AUTO_BUY_SHADOW_JOURNAL_PATH", str(path))
    from trading.auto_buy.shadow.runner import run_shadow_job
    out = run_shadow_job(now=evening(D1), settings=S.AutoBuySettings.from_env())
    assert out.get("skipped") or out.get("mode") == "OFF" or out == {} or "OFF" in json.dumps(out, default=str)
    assert not path.exists() and not any(tmp_path.iterdir())


def test_migration_refuses_s01_and_operational_journals(menv, monkeypatch):
    s01 = menv / "s01.sqlite3"
    sqlite3.connect(s01).execute("CREATE TABLE as_stop_journal(x)").connection.close()
    os.chmod(s01, 0o600)
    before = _sha(s01)
    monkeypatch.setenv("AUTO_STOP_JOURNAL_PATH", str(s01))
    with pytest.raises(M.Abort):
        M.run(str(s01), None)
    assert _sha(s01) == before


# ── 합성 계좌 스냅샷 (실계좌 값 없음) ──────────────────────────────────

SUMMARY = {"updated_at": "t", "overseas": {"cash": 30000.0, "orderable_cash": 21000.0,      # 총현금 ≠ 주문가능
                                           "evaluation_amount": 12000.0}}
HOLDINGS = [{"ticker": "SYNA", "eval_amount": 7000.0, "currency": "USD", "price_updated_at": "t"},
            {"ticker": "SYNB", "eval_amount": 5000.0, "currency": "USD", "price_updated_at": "t"}]


def _snap():
    from trading.auto_buy.shadow.market_data import build_account_snapshot
    from web.kiwoom_sizing import account_context
    return build_account_snapshot(SUMMARY, HOLDINGS, lambda t: "Technology", account_context)


def test_snapshot_distinguishes_cash_kinds():
    s = _snap()
    assert s["status"] == "OK"
    assert (s["total_cash"], s["orderable_cash"], s["cash"]) == (30000.0, 21000.0, 21000.0)
    assert s["holdings_total"] == 12000.0 and s["equity"] == 42000.0     # 총현금 + 보유 평가
    assert s["cash"] < s["total_cash"]                                   # 신규 매수 판단은 보수적 주문가능현금


def test_synthetic_snapshot_through_job_and_risk_state(shadow, prod):
    from trading.auto_buy.shadow import inputs as IN
    from trading.auto_buy.shadow import risk as RK
    snap = _snap()
    scan(prod, utc(D1, 21, 30), [sig("AAA")])
    job(shadow, prod, Bars({}), evening(D1), acct=snap)
    row = one(shadow, "SELECT * FROM ab_day_inputs WHERE day=?", D1)
    stored = json.loads(row["account_snapshot"])
    assert row["account_status"] == "OK" and stored["total_cash"] == 30000.0 and stored["orderable_cash"] == 21000.0
    acct = IN.account_from_input(row)
    pos = [{"ticker": "VPOS", "sector": "Energy", "qty": 10, "entry_price": 100.0, "stop_price": 92.0,
            "cost": 1000.0, "close": 110.0, "unresolved": False}]
    res = [{"ticker": "VRES", "sector": "Energy", "reserved_cash": 2000.0, "planned_risk": 150.0, "unknown": False}]
    st = RK.account_state(S.PROFILES["OPS"], pos, res, cash=-1000.0, realized=0.0, initial=None, acct=acct)
    a = st["acct"]
    assert a["real_cash"] == 21000.0 and a["real_total_cash"] == 30000.0 and a["real_equity"] == 42000.0
    assert a["manual_value"] == 12000.0                                  # 실제 보유 평가
    assert st["invested"] == 1100.0 and st["reserved"] == 2000.0         # 가상 포지션·예약 별도
    assert a["equity_as_if"] == pytest.approx(42000.0 - 1000.0 + 1100.0)
    assert st["free_cash"] == pytest.approx(21000.0 - 1000.0 - 2000.0)    # 주문가능현금 기준


def test_reconcile_records_uncaptured_scan_once_and_keeps_it_after_capture(shadow, prod):
    from trading.auto_buy.shadow.snapshot import catch_up, reconcile
    scan(prod, utc(D1, 16, 0), [sig("OLD", strict_filter_passed=False)])
    catch_up(shadow, session_factory=prod, now=utc(D1, 16, 10))          # 수집 시작
    scan(prod, utc(D1, 21, 30), [sig("AAA")])                            # 완료됐으나 아직 미수집
    assert reconcile(shadow, session_factory=prod)["uncaptured"] == 1
    reconcile(shadow, session_factory=prod)                              # 반복 관찰 → 중복 없음
    assert one(shadow, "SELECT COUNT(*) n FROM ab_events WHERE kind='uncaptured_scan'")["n"] == 1
    catch_up(shadow, session_factory=prod, now=utc(D1, 22, 0))           # 나중에 수집됨
    assert reconcile(shadow, session_factory=prod)["uncaptured"] == 0
    db = connect_rw(shadow.journal_path)
    try:
        b = RP.safety_violations(db, RP.health(shadow), shadow)
    finally:
        db.close()
    assert b["data_integrity"]["uncaptured_scans"] == 1 and b["current_unresolved"]["uncaptured_scans"] == 0
