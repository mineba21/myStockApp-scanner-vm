"""PR #14 재리뷰(head 9a315db) 결함 재현 테스트 — 9a315db 와 현재 코드 모두에 있는 API 만 쓴다. 각 테스트는 수정 후 기대 동작을 단언하므로 9a315db 에서 실패해야 한다."""
import sqlite3
import time
from datetime import datetime, timezone
import pytest
from tests.test_auto_buy_shadow import D3, job, prod, shadow  # noqa: F401
from tests.test_auto_buy_shadow_integrity import evening, one
from tests.test_auto_buy_shadow_review3 import _replay_ready
from trading.auto_buy import settings as S
from trading.auto_buy.shadow import inputs as IN, report as RP
from trading.auto_buy.shadow.store import connect_rw, get_runtime, set_runtime, tx


def test_repro_sequential_duplicate_replay_creates_two_runs(shadow, prod):
    full = _replay_ready(shadow, prod)
    job(shadow, prod, full, evening(D3))
    n1 = one(shadow, "SELECT COUNT(*) n FROM ab_runs")["n"]
    db = connect_rw(shadow.journal_path)
    try:
        IN.replay(db, full, "BARS_RECOVERED", shadow)          # 이미 처리된 요청을 다시 호출
    finally:
        db.close()
    assert one(shadow, "SELECT COUNT(*) n FROM ab_runs")["n"] == n1    # 기대: 새 run 없음


def test_repro_terminal_run_rows_can_be_deleted(shadow, prod):
    full = _replay_ready(shadow, prod)
    out = job(shadow, prod, full, evening(D3))
    old = out["replay"]["parent"]
    c = sqlite3.connect(shadow.journal_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):              # 기대: 종료 run 행 삭제 거부
            c.execute("DELETE FROM ab_orders WHERE run_id=?", (old,))
    finally:
        c.rollback(); c.close()


def test_repro_whole_missing_day_still_counts_streak(shadow):
    db = connect_rw(shadow.journal_path)
    try:
        with tx(db):
            db.execute("INSERT INTO ab_runs(run_id, status, reason, strategy_version, config_hash, logic_hash, created_at) "
                       "VALUES (1,'CURRENT','INITIAL',?,'x','y',0)", (S.STRATEGY_VERSION,))
            for d in ["2026-11-16", "2026-11-17", "2026-11-19", "2026-11-20", "2026-11-23"]:
                db.execute("INSERT INTO ab_days(run_id, day, status, decision_status, candidates, bars_requested, "
                           "bars_missing, notes, processed_at) VALUES (1,?,'COMPLETE','FROZEN',0,0,0,'[]',?)",
                           (d, datetime.fromisoformat(d).replace(hour=23, tzinfo=timezone.utc).timestamp()))
        c = RP.continuity(db, shadow)
    finally:
        db.close()
    print("continuity:", c["initial_window"], c["current_clean_streak"], c["functional_pass"])
    assert c["initial_window"] != "PASS" and not c["functional_pass"]    # 기대: 11-18 누락으로 통과 아님


def test_repro_stall_history_lost_after_recovery(shadow, prod):
    from tests.test_auto_buy_shadow_review4_lease import _building
    _replay_ready(shadow, prod)
    _building(shadow, {"heartbeat_at": time.time() - IN.REPLAY_LEASE_STALE_SEC - 600})
    db = connect_rw(shadow.journal_path)
    try:
        IN.reap_abandoned_builds(db)
        with tx(db):
            lz = get_runtime(db, "replay_in_progress"); lz["heartbeat_at"] = time.time()
            set_runtime(db, "replay_in_progress", lz)
        IN.reap_abandoned_builds(db)
    finally:
        db.close()
    b = RP.summary(shadow)["s5"]["profiles"]["P7C"]["safety_breakdown"]
    print("replay_stalled after recovery:", b["operational"]["replay_stalled"])
    assert b["operational"]["replay_stalled"] == 1                       # 기대: 복구 후에도 관찰 이력 1


def test_repro_migration_opens_sqlite_before_sidecar_validation(tmp_path, monkeypatch):
    import os
    from scripts import auto_buy_shadow_journal_migrate as M
    from trading.auto_buy.shadow.schema import upgrade_journal
    monkeypatch.setenv("SCANNER_ORDER_JOURNAL_PATH", str(tmp_path / "o.sqlite3"))
    j = str(tmp_path / "j.sqlite3"); upgrade_journal(j, allow_create=True); os.chmod(j, 0o600)
    os.symlink(tmp_path / "victim", j + "-wal")
    opened = []
    real = M.schema_status
    monkeypatch.setattr(M, "schema_status", lambda p: opened.append(p) or real(p))
    with pytest.raises(M.Abort):
        M.run(j, None, check=True)
    assert opened == []                                                  # 기대: 검증 전 SQLite 열지 않음


def test_repro_http_503_body_volume_zero_becomes_expired_order(shadow, prod, monkeypatch):
    """P1: HTTP 503 + 정상 형식 JSON + volume:0 이 확정 0 거래량으로 저장돼 주문이 정상 미체결 만료되던 결함."""
    from tests.test_auto_buy_shadow import D2
    from tests.test_auto_buy_shadow_integrity import _two_orders, final
    from tests.test_auto_buy_shadow_review4 import A2, chart_payload
    from tests.test_auto_buy_shadow_review5_http import R, http, ok
    from trading.auto_buy.shadow import market_data as MD
    _two_orders(shadow, prod)
    monkeypatch.setattr(MD, "_http_get", http({("AAA", D2): [ok("AAA", D2, A2 + (5e5,))],
                                               ("BBB", D2): [R(chart_payload("BBB", D2, (100.2,) * 4 + (0,)), 503)]}))
    job(shadow, prod, MD.yfinance_bars, final(D2))
    bb = one(shadow, "SELECT * FROM ab_bars WHERE ticker='BBB' AND day=?", D2)
    o = one(shadow, "SELECT * FROM ab_orders WHERE account_id='SV1:P7C:B00' AND ticker='BBB'")
    assert bb["status"] == "MISSING" and bb["volume_status"] == "UNKNOWN"          # 기대: 수집 실패 = 결측
    assert o["state"] == "FILL_UNKNOWN" and "ZERO_VOLUME" not in (o["details"] or "")
