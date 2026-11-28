"""PR #14 Codex 재리뷰(head c27d9d0) 반영 (3) — Migration ab0005 검증. 격리된 임시 SQLite 만 쓴다 (운영 Migration 없음)."""
import os
import sqlite3

import pytest

from scripts import auto_buy_shadow_journal_migrate as M
from tests.test_auto_buy_shadow_review5_s5 import _mode, _sha, _to, _triggers, menv  # noqa: F401
from trading.auto_buy.shadow.journal_migrations.ddl_v5 import DDL_V5
from trading.auto_buy.shadow.schema import EXPECTED_SCHEMA_VERSION, schema_status

V5_TRIGGERS = {s.split()[2] for s in DDL_V5} - {"ab_eligibility_no_replace_insert"}   # ab0006 이 판정 충돌 정책으로 교체


def _seed(j):
    c = sqlite3.connect(j)
    c.execute("INSERT INTO ab_runs(run_id, status, reason, strategy_version, config_hash, logic_hash, created_at) "
              "VALUES (1,'SUPERSEDED','INITIAL','SV1','x','y',0), (2,'CURRENT','REPLAY','SV1','x','y',1)")
    c.execute("INSERT INTO ab_events(at, severity, kind, message, details) VALUES (1,'ERROR','job_error','m','{}')")
    c.commit()
    c.close()
    os.chmod(j, 0o600)


def _rows(j):
    c = sqlite3.connect(j)
    try:
        return {t: c.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in ("ab_runs", "ab_events")}
    finally:
        c.close()


def test_head_is_ab0005_and_new_journal_has_v5_triggers(menv):
    assert EXPECTED_SCHEMA_VERSION == "ab0007"
    j = str(menv / "new.sqlite3")
    rep = M.run(j, str(menv / "bk"), allow_create=True)
    assert rep["result"] == "OK" and rep["schema_version"] == "ab0007" and _mode(j) == 0o600
    assert V5_TRIGGERS <= _triggers(j)


@pytest.mark.parametrize("start", ["ab0001", "ab0004"])
def test_upgrade_to_ab0005_preserves_ledger_and_audit(menv, start):
    j = str(menv / f"{start}.sqlite3")
    _to(j, start)
    _seed(j)
    before = _rows(j)
    rep = M.run(j, str(menv / "bk"))
    assert rep["result"] == "OK" and rep["schema_version"] == "ab0007" and _mode(rep["backup"]) == 0o600
    assert _rows(j) == before and V5_TRIGGERS <= _triggers(j)
    c = sqlite3.connect(j)                                               # 설정 없는 연결에서도 교체 차단
    try:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            c.execute("INSERT OR REPLACE INTO ab_events SELECT * FROM ab_events WHERE id=1")
        with pytest.raises(sqlite3.IntegrityError, match="immutable|terminal"):
            c.execute("REPLACE INTO ab_runs SELECT * FROM ab_runs WHERE run_id=1")
    finally:
        c.close()
    assert _rows(j) == before
    trig = _triggers(j)
    assert M.run(j, str(menv / "bk"))["result"] == "ALREADY_CURRENT" and _triggers(j) == trig   # 재실행 멱등


def test_ab0005_failure_rolls_back_to_ab0004(menv, monkeypatch):
    j = str(menv / "fail.sqlite3")
    _to(j, "ab0004")
    os.chmod(j, 0o600)
    trig, sha = _triggers(j), _sha(j)
    from trading.auto_buy.shadow.journal_migrations import ddl_v5      # Alembic 이 리비전 파일을 새로 읽으며 가져가는 값
    monkeypatch.setattr(ddl_v5, "DDL_V5", DDL_V5[:4] + ("CREATE TRIGGER broken BEFORE nonsense",))
    with pytest.raises(M.Abort):
        M.run(j, str(menv / "bk"))
    assert schema_status(j)["schema_version"] == "ab0004" and _triggers(j) == trig and _sha(j) == sha


def test_check_mode_is_read_only_for_ab0004(menv):
    j = str(menv / "chk.sqlite3")
    _to(j, "ab0004")
    os.chmod(j, 0o600)
    sha = _sha(j)
    rep = M.run(j, str(menv / "bk"), check=True)
    assert rep["result"] == "CHECK_ONLY" and rep["dry_run"] == {"ok": True, "schema_version": "ab0007"}
    assert _sha(j) == sha and not (menv / "bk").exists()


def test_migration_connection_sets_recursive_triggers(menv, monkeypatch):
    """Migration(Alembic) 쓰기 연결도 제품 연결과 같은 설정 (env.py 가 확인 후 진행)."""
    from sqlalchemy.engine import Connection
    seen = []
    real = Connection.exec_driver_sql

    def spy(self, sql, *a, **k):
        res = real(self, sql, *a, **k)
        if sql.strip().upper() == "PRAGMA RECURSIVE_TRIGGERS":
            seen.append(res.scalar())
            return real(self, sql, *a, **k)
        return res
    monkeypatch.setattr(Connection, "exec_driver_sql", spy)
    j = str(menv / "p.sqlite3")
    _to(j, "ab0005")
    assert seen == [1]


def test_symlinked_journal_and_off_mode_still_protected(menv, tmp_path, monkeypatch):
    j = str(menv / "j.sqlite3")
    _to(j, "ab0004")
    os.chmod(j, 0o600)
    link = str(menv / "link.sqlite3")
    os.symlink(j, link)
    with pytest.raises(M.Abort, match="심볼릭 링크"):
        M.run(link, None)
    assert schema_status(j)["schema_version"] == "ab0004"                # 링크를 따라가 올리지 않음
    from trading.auto_buy import settings as S
    from trading.auto_buy.shadow.runner import run_shadow_job
    off = tmp_path / "off" / "j.sqlite3"
    off.parent.mkdir()
    monkeypatch.setenv("AUTO_BUY_US_MODE", "OFF")
    monkeypatch.setenv("AUTO_BUY_SHADOW_JOURNAL_PATH", str(off))
    run_shadow_job(settings=S.AutoBuySettings.from_env())
    assert not any(off.parent.iterdir())
