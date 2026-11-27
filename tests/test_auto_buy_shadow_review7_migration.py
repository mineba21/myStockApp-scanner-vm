"""PR #14 Codex 재리뷰(head 36e5d2f) 반영 (3) — Migration ab0006 검증. 격리된 임시 SQLite 만 쓴다 (운영 Migration 없음).

S01 저널·운영 주문 저널 거부, 부속 파일·하드링크 보호는 기존 테스트(``review4_migration``·``review5_s5``)가 ab0006 head 에서
그대로 통과한다."""
import os
import sqlite3

import pytest

from scripts import auto_buy_shadow_journal_migrate as M
from tests.test_auto_buy_shadow_review5_s5 import _mode, _sha, _to, _triggers, menv  # noqa: F401
from trading.auto_buy.shadow.journal_migrations import ddl_v6
from trading.auto_buy.shadow.journal_migrations.ddl_v6 import DDL_V6
from trading.auto_buy.shadow.schema import EXPECTED_SCHEMA_VERSION, schema_status

V6_TRIGGERS = {s.split()[2] for s in DDL_V6 if s.startswith("CREATE TRIGGER")}


def _seed(j):
    c = sqlite3.connect(j)
    c.execute("INSERT INTO ab_runs(run_id, status, reason, strategy_version, config_hash, logic_hash, created_at) "
              "VALUES (1,'SUPERSEDED','INITIAL','SV1','x','y',0), (2,'CURRENT','REPLAY','SV1','x','y',1)")
    c.execute("INSERT INTO ab_events(at, severity, kind, message, details) VALUES (1,'ERROR','job_error','m','{}')")
    c.execute("INSERT INTO ab_day_inputs(day, decision_status, frozen_at, frozen_by, strategy_version, config_hash, "
              "logic_hash, account_status, account_reason, account_fetched_at) VALUES "
              "('2026-11-24','NO_DECISION_SCAN',0,'JOB_CUTOFF','SV1','c','l','LATE','late',1)")
    c.execute("INSERT INTO ab_bars(day, ticker, input_rev, status, provider, fetched_at, purpose) "
              "VALUES ('2026-11-24','AAA',1,'MISSING','t',0,'DAILY'), ('2026-11-24','BBB',1,'MISSING','t',0,'DAILY')")
    c.commit()
    c.close()
    os.chmod(j, 0o600)


def _rows(j):
    c = sqlite3.connect(j)
    try:
        return {t: c.execute(f"SELECT rowid, * FROM {t} ORDER BY 1").fetchall()
                for t in ("ab_runs", "ab_events", "ab_day_inputs", "ab_bars")}
    finally:
        c.close()


def test_head_is_ab0006_and_new_journal_has_contract_triggers(menv):
    assert EXPECTED_SCHEMA_VERSION == "ab0006"
    j = str(menv / "new.sqlite3")
    rep = M.run(j, str(menv / "bk"), allow_create=True)
    assert rep["result"] == "OK" and rep["schema_version"] == "ab0006" and _mode(j) == 0o600
    trig = _triggers(j)
    assert V6_TRIGGERS <= trig and "ab_eligibility_no_replace_insert" not in trig


@pytest.mark.parametrize("start", ["ab0001", "ab0005"])
def test_upgrade_to_ab0006_preserves_data_and_protects_both_connections(menv, start):
    j = str(menv / f"{start}.sqlite3")
    _to(j, start)
    if start == "ab0005":
        _seed(j)
    else:
        os.chmod(j, 0o600)
    before = _rows(j)
    rep = M.run(j, str(menv / "bk"))
    assert rep["result"] == "OK" and rep["schema_version"] == "ab0006" and _mode(rep["backup"]) == 0o600
    assert _rows(j) == before and V6_TRIGGERS <= _triggers(j)
    if start == "ab0005":
        from trading.auto_buy.shadow.store import connect_rw
        for db in (sqlite3.connect(j), connect_rw(j)):                   # 기본 연결·제품 연결 모두 보호
            try:
                with pytest.raises(sqlite3.IntegrityError, match="row identity"):   # 숨은 rowid 교체
                    db.execute("INSERT OR REPLACE INTO ab_bars(rowid, day, ticker, input_rev, status, provider, "
                               "fetched_at, purpose) VALUES (1,'2026-11-24','ZZZ',1,'OK','t',0,'DAILY')")
                with pytest.raises(sqlite3.IntegrityError, match="contract"):       # 동결 필드
                    db.execute("UPDATE ab_day_inputs SET frozen_by='edited'")
                with pytest.raises(sqlite3.IntegrityError):                          # 계좌 입력 재수정
                    db.execute("UPDATE ab_day_inputs SET account_reason='edited'")
            finally:
                db.close()
        assert _rows(j) == before
    trig = _triggers(j)
    assert M.run(j, str(menv / "bk"))["result"] == "ALREADY_CURRENT" and _triggers(j) == trig   # 재실행 멱등


def test_ab0006_failure_rolls_back_to_ab0005(menv, monkeypatch):
    j = str(menv / "fail.sqlite3")
    _to(j, "ab0005")
    _seed(j)
    trig, sha = _triggers(j), _sha(j)
    from trading.auto_buy.shadow.schema import expected_objects
    expected_objects("ab0006")                       # 기대 구조는 정상 DDL 로 (캐시), Migration 실행만 중간에 실패시킨다
    monkeypatch.setattr(ddl_v6, "DDL_V6", DDL_V6[:10] + ("CREATE TRIGGER broken BEFORE nonsense",))
    with pytest.raises(M.Abort):
        M.run(j, str(menv / "bk"))
    assert schema_status(j)["schema_version"] == "ab0005" and _triggers(j) == trig and _sha(j) == sha


@pytest.mark.parametrize("bad", ["negative_rowid", "account_partial", "capture_counts"])
def test_ab0006_refuses_data_violating_contract(menv, bad):
    j = str(menv / f"{bad}.sqlite3")
    _to(j, "ab0005")
    _seed(j)
    c = sqlite3.connect(j)
    if bad == "negative_rowid":
        c.execute("INSERT INTO ab_bars(rowid, day, ticker, input_rev, status, provider, fetched_at, purpose) "
                  "VALUES (-7,'2026-11-25','NEG',1,'MISSING','t',0,'DAILY')")
    elif bad == "account_partial":
        c.execute("INSERT INTO ab_day_inputs(day, decision_status, frozen_at, frozen_by, strategy_version, "
                  "config_hash, logic_hash, account_reason) VALUES ('2026-11-25','NO_DECISION_SCAN',0,'J','SV1',"
                  "'c','l','reason without status')")
    else:
        c.execute("INSERT INTO ab_scan_captures(scan_log_id, scan_market, scan_status, triggered_by, scan_started_at, "
                  "trading_day, intraday, source, complete, recoverability, late_after_decision, rows_captured, "
                  "strict_rows, captured_at) VALUES (9,'US','DONE','scheduler','2026-11-24T21:30:00','2026-11-24',0,"
                  "'HOOK',1,'FULL',0,3,1,0)")                            # 스냅샷 0건인데 수집 건수 3
    c.commit()
    c.close()
    sha = _sha(j)
    with pytest.raises(M.Abort):
        M.run(j, str(menv / "bk"))
    assert schema_status(j)["schema_version"] == "ab0005" and _sha(j) == sha


def test_check_mode_is_read_only_for_ab0005(menv):
    j = str(menv / "chk.sqlite3")
    _to(j, "ab0005")
    os.chmod(j, 0o600)
    sha = _sha(j)
    rep = M.run(j, str(menv / "bk"), check=True)
    assert rep["result"] == "CHECK_ONLY" and rep["dry_run"] == {"ok": True, "schema_version": "ab0006"}
    assert _sha(j) == sha and not (menv / "bk").exists()


def test_off_mode_creates_nothing(tmp_path, monkeypatch):
    from trading.auto_buy import settings as S
    from trading.auto_buy.shadow.runner import run_shadow_job
    off = tmp_path / "off" / "j.sqlite3"
    off.parent.mkdir()
    monkeypatch.setenv("AUTO_BUY_US_MODE", "OFF")
    monkeypatch.setenv("AUTO_BUY_SHADOW_JOURNAL_PATH", str(off))
    assert "skipped" in run_shadow_job(settings=S.AutoBuySettings.from_env())
    assert not any(off.parent.iterdir())
