#!/usr/bin/env python3
"""자동매수 SHADOW 저널 Migration (명시적 배포 작업). 스케줄러·Scanner hook·웹 API 는 자동 실행하지 않는다.

  python -m scripts.auto_buy_shadow_journal_migrate --env-file /opt/app/.env --backup-dir DIR --check
  python -m scripts.auto_buy_shadow_journal_migrate --env-file /opt/app/.env --backup-dir DIR --check --allow-create
  python -m scripts.auto_buy_shadow_journal_migrate --env-file /opt/app/.env --backup-dir DIR --allow-create  # 최초 생성

- ``--env-file``: .env 를 셸에서 실행(source)하지 않고 ``dotenv_values`` 로 필요한 경로 키만 읽는다. 값은 출력하지 않는다.
- ``--check``: 파일·테이블을 만들지 않는다. 파일이 없으면 종료 코드 2. ``--check --allow-create`` 는 생성 가능 여부
  (상위 디렉터리 존재·쓰기 가능, 다른 저널과 겹치지 않음)만 확인하고 아무것도 만들지 않는다.
  기존 파일이 최신 스키마가 아니면 임시 사본에서 upgrade 를 시험해 실패하면 종료 코드 2 (불완전한 ab_* 스키마 등).
- 다른 저널과의 동일성은 경로(심볼릭 링크 해석)와 inode(하드링크)로 비교한다.
- 권한: 실행 중 ``umask 077``. 새 저널은 ``O_CREAT|O_EXCL`` 0600 으로 먼저 만들고(심볼릭 링크 따라가지 않음),
  완료 후 저널과 ``-journal``/``-wal``/``-shm`` 부속 파일, 보고서를 0600 으로 맞춘다.

- 대상은 AUTO_BUY_SHADOW_JOURNAL_PATH (또는 --journal). 운영 주문 저널(SCANNER_ORDER_JOURNAL_PATH)·ETF 저널과 같은 파일이면 중단.
- 파일이 없으면 --allow-create 일 때만 새로 만든다. 있으면 백업(읽기 전용 원본 → 사본, 무결성 검사) 후 upgrade.
- upgrade 는 BEGIN IMMEDIATE 트랜잭션 하나. 실패하면 롤백. 운영 DB·S01 저널 Migration 과 무관하다.
- 실패해도 백업으로 자동 복원하지 않는다.

종료 코드: 0 성공 · 2 사전 점검 실패 · 3 Migration 실패 · 4 사후 검증 실패
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from trading.auto_buy.settings import _same_file
from trading.auto_buy.shadow.schema import (EXPECTED_SCHEMA_VERSION, foreign_objects, schema_status, structure_diff,
                                            upgrade_journal)


ENV_KEYS = ("AUTO_BUY_SHADOW_JOURNAL_PATH", "SCANNER_ORDER_JOURNAL_PATH", "ALLOCATION_JOURNAL_PATH")
SIDECARS = ("-journal", "-wal", "-shm")


def load_env_file(path: str) -> dict:
    """.env 에서 경로 키만 읽어 os.environ 에 없을 때 채운다. 값은 출력·기록하지 않는다."""
    from dotenv import dotenv_values
    p = Path(path)
    if not p.is_file():
        raise Abort(2, "--env-file 이 파일이 아닙니다.")
    vals = dotenv_values(p)
    loaded = []
    for k in ENV_KEYS:
        if vals.get(k) and not os.getenv(k):
            os.environ[k] = vals[k]
            loaded.append(k)
    return {"env_keys_loaded": loaded}


def _create_exclusive(path: str) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    os.close(fd)


def dry_run_upgrade(journal: str) -> dict:
    """읽기 전용 원본을 임시 디렉터리에 복사(SQLite backup API)해 upgrade 를 시험한다. 원본은 바꾸지 않는다."""
    import tempfile
    with tempfile.TemporaryDirectory(prefix="ab_dryrun_") as tmp:
        copy = os.path.join(tmp, "journal_copy.sqlite3")
        src = sqlite3.connect(Path(journal).as_uri() + "?mode=ro", uri=True)
        dst = sqlite3.connect(copy)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        try:
            version = upgrade_journal(copy)
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        return {"ok": True, "schema_version": version}


def _secure(path: str) -> list:
    done = []
    for suffix in ("",) + SIDECARS:
        f = path + suffix
        if os.path.isfile(f) and not os.path.islink(f):
            os.chmod(f, 0o600)
            done.append(os.path.basename(f))
    return done


class Abort(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def run(journal: str, backup_dir: str, *, allow_create=False, check=False) -> dict:
    rep = {"journal": journal, "expected_schema_version": EXPECTED_SCHEMA_VERSION, "started_at": time.time()}
    if not journal or not os.path.isabs(journal):
        raise Abort(2, "AUTO_BUY_SHADOW_JOURNAL_PATH(절대경로) 또는 --journal 이 필요합니다.")
    for other in ("SCANNER_ORDER_JOURNAL_PATH", "ALLOCATION_JOURNAL_PATH"):
        if _same_file(journal, os.getenv(other, "")):
            raise Abort(2, f"대상이 {other} 와 같은 파일입니다. 자동매수 SHADOW 는 별도 파일이어야 합니다.")
    journal = str(Path(journal).resolve())
    exists = os.path.isfile(journal)
    rep["journal_exists"] = exists
    rep["status_before"] = schema_status(journal)
    if not exists and not allow_create:
        raise Abort(2, f"저널 파일이 없습니다: {journal}. 최초 생성은 --allow-create (경로 확인 후).")
    if not exists:
        parent = os.path.dirname(journal)
        if not os.path.isdir(parent):
            raise Abort(3, f"상위 디렉터리가 없습니다: {parent}")
        if not os.access(parent, os.W_OK):
            raise Abort(2, f"상위 디렉터리에 쓸 수 없습니다: {parent}")
    if exists:
        db = sqlite3.connect(Path(journal).as_uri() + "?mode=ro", uri=True)
        try:
            integ = db.execute("PRAGMA integrity_check").fetchone()[0]
            foreign = sorted(foreign_objects(db))
            rep["structure_diff_before"] = structure_diff(db)
        finally:
            db.close()
        if integ != "ok":
            raise Abort(2, f"무결성 검사 실패: {integ}")
        if foreign:
            raise Abort(2, f"자동매수 SHADOW 저널이 아닌 객체가 있습니다 (다른 DB 로 보임): {foreign[:10]}")
    if exists and not rep["status_before"]["schema_ready"]:
        # 실제 upgrade 가능 여부를 원본을 건드리지 않고 임시 사본에서 확인한다 (불완전한 ab_* 스키마 등)
        rep["dry_run"] = dry_run_upgrade(journal)
        if not rep["dry_run"]["ok"]:
            raise Abort(2, f"사본 upgrade 시험 실패 — 이 저널은 Migration 할 수 없습니다: {rep['dry_run']['error']}")
    if check:
        rep["result"] = ("CHECK_ONLY_ALREADY_CURRENT" if exists and rep["status_before"]["schema_ready"]
                         else "CHECK_ONLY" if exists else "CHECK_ONLY_WOULD_CREATE")
        return rep
    if exists and rep["status_before"]["schema_ready"]:
        rep["result"] = "ALREADY_CURRENT"
        return rep
    if exists:
        from scripts.auto_stop_journal_backup import backup
        rep["backup"] = str(backup(journal, backup_dir))
    created = False
    if not exists:
        try:
            _create_exclusive(journal)
            created = True
        except FileExistsError:
            raise Abort(2, "생성 직전에 같은 경로의 파일이 생겼습니다. 경로를 다시 확인하세요.")
    try:
        rep["schema_version"] = upgrade_journal(journal, allow_create=allow_create)
    except Exception as exc:
        rep.update(result="MIGRATION_FAILED", error=str(exc), secured_files=_secure(journal))
        _write(rep, backup_dir)
        raise Abort(3, f"Migration 실패 (롤백): {exc}")
    st = schema_status(journal)
    rep["status_after"] = st
    if not st["schema_ready"]:
        rep["result"] = "FAILED_VERIFY"
        _write(rep, backup_dir)
        raise Abort(4, f"사후 검증 실패: {st['schema_error']}")
    rep["secured_files"] = _secure(journal)
    rep["created"] = created
    rep["result"] = "OK"
    _write(rep, backup_dir)
    return rep


def _write(rep, directory):
    if not directory:
        return
    Path(directory).mkdir(parents=True, exist_ok=True)
    out = Path(directory) / f"auto_buy_shadow_migrate-{time.strftime('%Y%m%dT%H%M%S')}-{time.time_ns() % 10**9:09d}.json"
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    os.chmod(out, 0o600)
    rep["report_file"] = str(out)


def main(argv=None):
    os.umask(0o077)
    p = argparse.ArgumentParser(description="자동매수 SHADOW 저널 Migration (명시적 배포 작업)")
    p.add_argument("--journal", default=None)
    p.add_argument("--env-file", default=None, help=".env 경로 (source 하지 않고 경로 키만 읽음)")
    p.add_argument("--backup-dir", required=True)
    p.add_argument("--allow-create", action="store_true")
    p.add_argument("--check", action="store_true")
    args = p.parse_args(argv)
    try:
        if args.env_file:
            load_env_file(args.env_file)
        journal = args.journal or os.getenv("AUTO_BUY_SHADOW_JOURNAL_PATH", "")
        rep = run(journal, args.backup_dir, allow_create=args.allow_create, check=args.check)
    except Abort as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return exc.code
    print(json.dumps({k: rep.get(k) for k in ("result", "journal", "schema_version", "backup", "report_file")},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
