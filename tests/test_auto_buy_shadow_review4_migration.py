"""PR #14 Codex 최종 재리뷰 반영 회귀 테스트 (3) — SHADOW 저널 Migration 파일 권한(0600).

임시 디렉터리의 저널만 쓴다. 운영 저널·운영 DB·S01 저널 Migration 은 건드리지 않는다.
"""
import hashlib
import os
import sqlite3
import stat

import pytest

from scripts import auto_buy_shadow_journal_migrate as M
from trading.auto_buy.shadow.schema import schema_status, upgrade_journal


def mode(p):
    return stat.S_IMODE(os.lstat(p).st_mode)


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SCANNER_ORDER_JOURNAL_PATH", str(tmp_path / "orders.sqlite3"))
    monkeypatch.setenv("ALLOCATION_JOURNAL_PATH", str(tmp_path / "alloc.sqlite3"))
    return tmp_path


def latest_0644(path):
    """최신(head) 저널을 0644 로 복원한 상황."""
    open(path, "w").close()
    upgrade_journal(str(path), allow_create=True)
    os.chmod(path, 0o644)
    return str(path)


def test_check_fails_for_restored_latest_journal_with_0644_and_changes_nothing(env):
    j = latest_0644(env / "j.sqlite3")
    assert schema_status(j)["schema_version"] == "ab0006"
    before = sha(j)
    with pytest.raises(M.Abort) as e:
        M.run(j, str(env / "bk"), check=True)
    assert e.value.code == 2 and "0600" in str(e.value) and "0o644" in str(e.value)
    assert mode(j) == 0o644 and sha(j) == before                          # 권한·데이터 변경 없음
    assert M.main(["--journal", j, "--backup-dir", str(env / "bk"), "--check"]) == 2
    assert not (env / "bk").exists()                                      # --check 는 아무것도 만들지 않음


def test_check_also_fails_when_sidecar_is_not_0600(env):
    j = latest_0644(env / "j.sqlite3")
    os.chmod(j, 0o600)
    open(j + "-wal", "wb").close()
    os.chmod(j + "-wal", 0o644)
    with pytest.raises(M.Abort, match="j.sqlite3-wal=0o644"):
        M.run(j, None, check=True)
    assert mode(j + "-wal") == 0o644


def test_rerun_on_already_current_0644_journal_secures_to_0600(env):
    j = latest_0644(env / "j.sqlite3")
    open(j + "-shm", "wb").close()
    os.chmod(j + "-shm", 0o644)
    before = sha(j)
    rep = M.run(j, str(env / "bk"))
    assert rep["result"] == "ALREADY_CURRENT" and rep["permissions_after"]["ok"]
    assert mode(j) == 0o600 and mode(j + "-shm") == 0o600 and sha(j) == before
    assert mode(rep["report_file"]) == 0o600
    assert M.run(j, None, check=True)["result"] == "CHECK_ONLY_ALREADY_CURRENT"


def test_upgrade_and_create_paths_end_0600_even_with_permissive_umask(env):
    from alembic import command
    from alembic.config import Config
    from trading.auto_buy.shadow.schema import MIGRATIONS_DIR
    old = os.umask(0)                                                     # 느슨한 umask 에서도 077 적용
    try:
        v1 = str(env / "v1.sqlite3")
        open(v1, "w").close()
        cfg = Config()
        cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
        cfg.attributes["journal_path"] = v1
        command.upgrade(cfg, "ab0001")
        os.chmod(v1, 0o644)
        rep = M.run(v1, str(env / "bk"))
        assert rep["result"] == "OK" and rep["schema_version"] == "ab0006"
        assert mode(v1) == 0o600 and mode(rep["backup"]) == 0o600 and mode(rep["report_file"]) == 0o600
        new = str(env / "new.sqlite3")
        rep = M.run(new, str(env / "bk"), allow_create=True)
        assert rep["result"] == "OK" and rep["created"] and mode(new) == 0o600
    finally:
        os.umask(old)


def test_chmod_failure_is_not_reported_as_success(env, monkeypatch):
    j = latest_0644(env / "j.sqlite3")

    def deny(*a, **k):
        raise PermissionError("denied")
    monkeypatch.setattr(M.os, "chmod", deny)
    with pytest.raises(M.Abort) as e:
        M.run(j, None)
    assert e.value.code == 4 and "권한 변경 실패" in str(e.value)


def test_never_chmods_symlinks_hardlinks_or_other_journals(env, monkeypatch):
    s01 = env / "orders.sqlite3"                     # S01 / 운영 주문 저널 (가짜)
    sqlite3.connect(s01).execute("CREATE TABLE as_orders(x)").connection.close()
    os.chmod(s01, 0o644)
    j = latest_0644(env / "j.sqlite3")
    # 1) 저널 경로 자체가 심볼릭 링크
    link = env / "link.sqlite3"
    os.symlink(j, link)
    with pytest.raises(M.Abort, match="심볼릭 링크"):
        M.run(str(link), None)
    # 2) 부속 파일이 S01 저널을 가리키는 심볼릭 링크
    os.symlink(s01, j + "-wal")
    with pytest.raises(M.Abort, match="심볼릭 링크"):
        M.run(j, None)
    os.unlink(j + "-wal")
    # 3) 하드링크된 저널 (다른 이름으로 같은 inode)
    os.link(j, env / "hard.sqlite3")
    with pytest.raises(M.Abort, match="하드링크"):
        M.run(j, None)
    os.unlink(env / "hard.sqlite3")
    # 4) 대상이 운영 주문(S01) 저널
    with pytest.raises(M.Abort, match="SCANNER_ORDER_JOURNAL_PATH"):
        M.run(str(s01), None)
    # 5) ab_ 이외 객체가 있는 파일 (다른 DB)
    other = env / "other.sqlite3"
    sqlite3.connect(other).execute("CREATE TABLE as_orders(x)").connection.close()
    os.chmod(other, 0o644)
    with pytest.raises(M.Abort, match="아닌 객체"):
        M.run(str(other), None)
    assert mode(s01) == 0o644 and mode(other) == 0o644 and mode(j) == 0o644    # 어떤 파일에도 chmod 하지 않음
