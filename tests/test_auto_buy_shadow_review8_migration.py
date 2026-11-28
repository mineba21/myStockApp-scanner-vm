"""PR #14 Codex 재리뷰(head a183b99) 반영 (2) — Migration ab0007 사전 검증(P2)·업그레이드.

ab0006 사전 검사는 (rows_captured, strict_rows) = (0, 0) 이면 실제 행 수 대조를 건너뛰었다. ab0007 은 모든 수집 기록의 건수,
구성원 소속·row_sha256, FROZEN 결정 스캔의 수집 조건과 기존 동결 해시를 실제 집합과 대조하고, 맞지 않으면 전체 롤백한다.
ab0006 구조의 실제 제품 데이터 저널은 최신 제품 경로로 만든 저널에서 ab0007 객체만 걷어내 만든다 (구조 비교로 정확히 ab0006 확인).
격리된 임시 SQLite 만 쓴다 (운영 Migration 없음).
"""
import json
import os
import sqlite3

import pytest

from scripts import auto_buy_shadow_journal_migrate as M
from tests.test_auto_buy_shadow import ACCT, D1, D2, D3, Bars, job, prod, scan, shadow, sig, utc  # noqa: F401
from tests.test_auto_buy_shadow_integrity import BAR_A2, BAR_A3, BAR_B2, BAR_B3, BAR_C3, evening, final, q
from tests.test_auto_buy_shadow_review5_s5 import _mode, _sha, _to, _triggers, menv  # noqa: F401
from trading.auto_buy.shadow import integrity as IG
from trading.auto_buy.shadow.journal_migrations import ddl_v7
from trading.auto_buy.shadow.journal_migrations.ddl_v7 import DDL_V7
from trading.auto_buy.shadow.schema import EXPECTED_SCHEMA_VERSION, expected_objects, schema_status, structure_diff
from trading.auto_buy.shadow.store import connect_rw

V7_OBJECTS = {s.split()[2] for s in DDL_V7 if s.startswith(("CREATE TRIGGER", "CREATE TABLE"))}


def to_ab0006(path):
    """최신(ab0007) 저널에서 ab0007 객체만 걷어내 ab0006 구조로 되돌린다 (데이터는 제품 경로로 만든 그대로)."""
    db = sqlite3.connect(path, isolation_level=None)
    try:
        db.execute("BEGIN")
        for name in sorted(V7_OBJECTS - {"ab_capture_seals"}):
            db.execute(f"DROP TRIGGER {name}")
        db.execute("DROP TABLE ab_capture_seals")
        db.execute("UPDATE ab_alembic_version SET version_num='ab0006'")
        db.execute("COMMIT")
        assert structure_diff(db, "ab0006") == []
    finally:
        db.close()
    os.chmod(path, 0o600)


def data(path):
    db = sqlite3.connect(path)
    try:
        tabs = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'ab_%' "
                                         "AND name NOT IN ('ab_alembic_version','ab_capture_seals')")]
        return {t: db.execute(f"SELECT rowid, * FROM {t} ORDER BY rowid").fetchall() for t in tabs}
    finally:
        db.close()


@pytest.fixture
def ledger06(shadow, prod):
    """제품 경로로 만든 원장 (FROZEN 2일·빈 스캔·결측·재계산 요청) → ab0006 구조."""
    scan(prod, utc(D1, 21, 30), [sig("AAA"), sig("BBB")])
    job(shadow, prod, Bars({}), evening(D1))
    scan(prod, utc(D2, 21, 30), [sig("CCC", signal_date=D2, price=50.0, pivot_price=49.0, stop_loss=46.0)])
    job(shadow, prod, Bars({D2: {"AAA": BAR_A2}}), final(D2))
    scan(prod, utc(D2, 21, 45), [sig("ZZZ")])          # 정규 결정 이후 별도 scan_log_id 재스캔 (LATE, 보충 수집 → 불완전)
    scan(prod, utc(D3, 21, 30), [])                    # 정상 빈 결정 스캔
    job(shadow, prod, Bars({}), evening(D3))
    assert len(q(shadow, "SELECT 1 FROM ab_day_inputs WHERE decision_status='FROZEN'")) == 3
    to_ab0006(shadow.journal_path)
    return shadow


def test_head_is_ab0007_and_new_journal_has_seal_table(menv):
    assert EXPECTED_SCHEMA_VERSION == "ab0007"
    j = str(menv / "new.sqlite3")
    rep = M.run(j, str(menv / "bk"), allow_create=True)
    assert rep["result"] == "OK" and rep["schema_version"] == "ab0007" and _mode(j) == 0o600
    assert V7_OBJECTS - {"ab_capture_seals"} <= _triggers(j)


def test_ab0006_product_ledger_upgrades_with_backfilled_seals_and_works(ledger06, prod, menv):
    s = ledger06
    before = data(s.journal_path)
    assert not schema_status(s.journal_path)["schema_ready"]                   # ab0006 은 런타임이 받지 않음
    rep = M.run(s.journal_path, str(menv / "bk"))
    assert rep["result"] == "OK" and rep["schema_version"] == "ab0007" and _mode(rep["backup"]) == 0o600
    assert data(s.journal_path) == before                                      # 기존 데이터·감사 이력 그대로
    seals = q(s, "SELECT * FROM ab_capture_seals ORDER BY scan_log_id")
    caps = q(s, "SELECT * FROM ab_scan_captures ORDER BY scan_log_id")
    assert [g["scan_log_id"] for g in seals] == [c["scan_log_id"] for c in caps]
    assert all(g["source"] == "AB0007_BACKFILL" and (g["member_count"], g["strict_count"]) ==
               (c["rows_captured"], c["strict_rows"]) for g, c in zip(seals, caps))
    db = connect_rw(s.journal_path)
    try:
        for d in (D1, D2, D3):
            assert IG.decision_set_issues(db, d) == []
    finally:
        db.close()
    for kind in ("product", "raw"):                                           # 두 연결 모두 동결 집합 보호
        db = connect_rw(s.journal_path) if kind == "product" else sqlite3.connect(s.journal_path)
        try:
            sid = db.execute("SELECT decision_scan_log_id FROM ab_day_inputs WHERE day=?", (D1,)).fetchone()[0]
            cols = [r[1] for r in db.execute("PRAGMA table_info(ab_signal_snapshots)")]
            sel = ", ".join("NULL" if c == "id" else "'US:EVIL:X:1'" if c == "signal_key" else c for c in cols)
            with pytest.raises(sqlite3.IntegrityError, match="contract set"):
                db.execute(f"INSERT OR IGNORE INTO ab_signal_snapshots({', '.join(cols)}) SELECT {sel} "
                           f"FROM ab_signal_snapshots WHERE scan_log_id={sid} LIMIT 1")
        finally:
            db.close()
    # 정상 replay·CURRENT 전환 (복구된 일봉으로 재계산)
    out = job(s, prod, Bars({D2: {"BBB": BAR_B2}, D3: {"AAA": BAR_A3, "BBB": BAR_B3, "CCC": BAR_C3}}), utc(D3, 22, 0))
    assert out.get("error") is None and out["replay"]["run"]
    assert q(s, "SELECT status FROM ab_runs WHERE run_id=?", out["replay"]["run"])[0]["status"] == "CURRENT"
    trig = _triggers(s.journal_path)
    assert M.run(s.journal_path, str(menv / "bk"))["result"] == "ALREADY_CURRENT" and _triggers(s.journal_path) == trig


@pytest.mark.parametrize("start", ["ab0001", "ab0005"])
def test_upgrade_from_older_revisions(menv, start):
    j = str(menv / f"{start}.sqlite3")
    _to(j, start)
    os.chmod(j, 0o600)
    rep = M.run(j, str(menv / "bk"))
    assert rep["result"] == "OK" and rep["schema_version"] == "ab0007"
    assert schema_status(j)["schema_ready"]


def _break(path, how):
    db = sqlite3.connect(path, isolation_level=None)
    try:
        for (n,) in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
            db.execute(f"DROP TRIGGER {n}")           # 관리자 DDL 로 만든 기존 이상 데이터 (복구는 _restore_v6_triggers)
        sid1 = db.execute("SELECT decision_scan_log_id FROM ab_day_inputs WHERE day=?", (D1,)).fetchone()[0]
        empty = db.execute("SELECT decision_scan_log_id FROM ab_day_inputs WHERE day=?", (D3,)).fetchone()[0]
        cols = [r[1] for r in db.execute("PRAGMA table_info(ab_signal_snapshots)")]

        def copy_into(target, key, strict=None):
            sel = ", ".join("NULL" if c == "id" else str(target) if c == "scan_log_id" else f"'{key}'" if c == "signal_key"
                            else (str(strict) if strict is not None and c == "strict_passed" else c) for c in cols)
            src_day = db.execute("SELECT trading_day FROM ab_scan_captures WHERE scan_log_id=?", (target,)).fetchone()[0]
            sel = sel.replace("trading_day", f"'{src_day}'") if "trading_day" in cols else sel
            db.execute(f"INSERT INTO ab_signal_snapshots({', '.join(cols)}) SELECT {sel} FROM ab_signal_snapshots "
                       f"WHERE scan_log_id={sid1} LIMIT 1")
        if how == "recorded0_actual1":          # 빈 스캔(기록 0건)에 실제 1건 — ab0006 사전 검사가 건너뛰던 경우
            copy_into(empty, "US:LATE:X:1")
        elif how == "recorded1_actual0":
            db.execute("UPDATE ab_scan_captures SET rows_captured=1 WHERE scan_log_id=?", (empty,))
        elif how == "strict_only":
            db.execute("UPDATE ab_scan_captures SET strict_rows=strict_rows-1 WHERE scan_log_id=?", (sid1,))
        elif how == "frozen_hash":
            db.execute("UPDATE ab_day_inputs SET signal_set_hash='0000000000000000' WHERE day=?", (D1,))
        elif how == "row_sha":
            db.execute("UPDATE ab_signal_snapshots SET row_json='{}' WHERE scan_log_id=?", (sid1,))
        elif how == "incomplete_counts":
            db.execute("UPDATE ab_scan_captures SET complete=0, recoverability='UNRECOVERABLE', strict_rows=strict_rows+1 "
                       "WHERE scan_log_id=(SELECT MAX(scan_log_id) FROM ab_scan_captures)")
        elif how == "frozen_incomplete":
            db.execute("UPDATE ab_scan_captures SET complete=0, recoverability='PARTIAL' WHERE scan_log_id=?", (sid1,))
    finally:
        db.close()


def _restore_v6_triggers(path):
    """_break 가 떨어뜨린 ab0006 트리거를 같은 정의로 복구 (구조 검사는 통과해야 사전 검사까지 간다)."""
    want = expected_objects("ab0006")
    db = sqlite3.connect(path, isolation_level=None)
    try:
        have = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        for n, (k, sql) in want.items():
            if k == "trigger" and n not in have:
                db.execute(sql)
    finally:
        db.close()


BREAKS = {"recorded0_actual1": "기록 0/0건 ≠ 실제 1", "recorded1_actual0": "기록 1/0건 ≠ 실제 0/0",
          "strict_only": "Strict", "frozen_hash": "동결 해시", "row_sha": "row_sha256",
          "incomplete_counts": "불완전(DONE/UNRECOVERABLE)", "frozen_incomplete": "정상 완료 정규 수집이 아님"}


@pytest.mark.parametrize("how", sorted(BREAKS))
def test_precheck_rejects_inconsistent_journal_and_keeps_original(ledger06, menv, how):
    s = ledger06
    _break(s.journal_path, how)
    _restore_v6_triggers(s.journal_path)
    sha, trig = _sha(s.journal_path), _triggers(s.journal_path)
    db = sqlite3.connect(s.journal_path)
    try:
        bad = ddl_v7.precheck(db)
    finally:
        db.close()
    assert any(BREAKS[how] in b for b in bad), bad
    with pytest.raises(M.Abort):
        M.run(s.journal_path, str(menv / "bk"))
    assert schema_status(s.journal_path)["schema_version"] == "ab0006"
    assert _sha(s.journal_path) == sha and _triggers(s.journal_path) == trig       # 자동 수정 없음·원본 그대로


def test_precheck_accepts_normal_empty_and_normal_scans(ledger06):
    db = sqlite3.connect(ledger06.journal_path)
    try:
        assert ddl_v7.precheck(db) == []
        caps = {r[0]: (r[1], r[2]) for r in db.execute("SELECT scan_log_id, rows_captured, strict_rows FROM ab_scan_captures")}
    finally:
        db.close()
    assert (0, 0) in caps.values() and any(v[0] > 0 for v in caps.values())   # 빈 스캔·정상 수집 스캔 모두 포함


def test_ab0007_failure_midway_rolls_back_everything(ledger06, menv, monkeypatch):
    s = ledger06
    sha, trig = _sha(s.journal_path), _triggers(s.journal_path)
    expected_objects("ab0006")
    expected_objects("ab0007")
    monkeypatch.setattr(ddl_v7, "DDL_V7", DDL_V7[:4] + ("CREATE TRIGGER broken BEFORE nonsense",))
    with pytest.raises(M.Abort):
        M.run(s.journal_path, str(menv / "bk"))
    assert schema_status(s.journal_path)["schema_version"] == "ab0006"
    assert _sha(s.journal_path) == sha and _triggers(s.journal_path) == trig


def test_multi_step_upgrade_is_one_transaction(menv):
    """ab0005 → ab0007 은 한 트랜잭션: ab0007 사전 검사가 실패하면 ab0006 도 남지 않는다."""
    j = str(menv / "multi.sqlite3")
    _to(j, "ab0005")
    db = sqlite3.connect(j)
    db.execute("INSERT INTO ab_scan_captures(scan_log_id, scan_market, scan_status, triggered_by, scan_started_at, "
               "trading_day, source, complete, recoverability, captured_at) VALUES "
               "(1,'US','DONE','scheduler','2026-11-23T21:30:00','2026-11-23','HOOK',1,'FULL',0)")
    db.execute("INSERT INTO ab_signal_snapshots(scan_log_id, scan_result_id, signal_key, market, ticker, signal_type, "
               "signal_date, trading_day, scan_status, is_first_detection, strict_passed, first_pass_basis, row_sha256, "
               "row_json, capture_source, created_at) VALUES (1,1,'k','US','AAA','BREAKOUT','2026-11-20','2026-11-23',"
               "'DONE',1,1,'FIRST_DETECTION','x','{}','HOOK',0)")
    db.commit()
    db.close()
    os.chmod(j, 0o600)
    sha = _sha(j)
    with pytest.raises(M.Abort):
        M.run(j, str(menv / "bk"))
    assert schema_status(j)["schema_version"] == "ab0005" and _sha(j) == sha


def test_check_mode_and_off_mode(ledger06, menv, tmp_path, monkeypatch):
    s = ledger06
    sha = _sha(s.journal_path)
    rep = M.run(s.journal_path, str(menv / "bk2"), check=True)
    assert rep["result"] == "CHECK_ONLY" and rep["dry_run"] == {"ok": True, "schema_version": "ab0007"}
    assert _sha(s.journal_path) == sha and not (menv / "bk2").exists()
    from trading.auto_buy import settings as S
    from trading.auto_buy.shadow.runner import run_shadow_job
    off = tmp_path / "off" / "j.sqlite3"
    off.parent.mkdir()
    monkeypatch.setenv("AUTO_BUY_US_MODE", "OFF")
    monkeypatch.setenv("AUTO_BUY_SHADOW_JOURNAL_PATH", str(off))
    assert "skipped" in run_shadow_job(settings=S.AutoBuySettings.from_env())
    assert not any(off.parent.iterdir())
    assert json.dumps(rep, default=str)
