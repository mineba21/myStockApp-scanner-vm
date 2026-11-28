#!/usr/bin/env python3
"""자동매수 RT1 저널 스키마 Migration (명시적 배포 작업). 스케줄러·웹 API 는 이 작업을 자동 실행하지 않는다.

  python -m scripts.auto_buy_us_journal_migrate --backup-dir /var/backups/mystockapp --check           # 사전 점검만
  python -m scripts.auto_buy_us_journal_migrate --backup-dir /var/backups/mystockapp --allow-create   # 새 저널 생성

순서: 대상 확인 → 백업(+무결성) → 기존 데이터 건수·해시 기록 → 저널 전용 Alembic upgrade → 버전·무결성·
기존 데이터 보존·신규 객체 확인. 앱을 멈춘 상태에서 실행한다. 실패해도 백업으로 자동 복원하지 않는다.
보고서에는 테이블 이름·건수·해시·스키마만 남기고 행 내용은 남기지 않는다.

종료 코드: 0 성공 · 2 사전 점검 실패 · 3 Migration 실패(롤백) · 4 사후 검증 실패
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from scripts.auto_stop_journal_backup import backup
from trading.auto_buy_us.schema import (EXPECTED_SCHEMA_VERSION, VERSION_TABLE, expected_objects, schema_status,
                                        structure_diff, upgrade_journal)


class Abort(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def _ro(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)


def snapshot(path) -> dict:
    """테이블별 건수·내용 해시와 스키마 정의. 읽기 전용."""
    db = _ro(path)
    try:
        objects = [{"type": t, "name": n, "sql": s} for t, n, s in db.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name")]
        tables = [o["name"] for o in objects if o["type"] == "table" and not o["name"].startswith("sqlite_")]
        data = {}
        for t in tables:
            h, n = hashlib.sha256(), 0
            for row in db.execute(f'SELECT * FROM "{t}" ORDER BY rowid'):
                h.update(json.dumps(row, default=str).encode())
                n += 1
            data[t] = {"rows": n, "sha256": h.hexdigest()}
        version = [r[0] for r in db.execute(f"SELECT version_num FROM {VERSION_TABLE}")] \
            if VERSION_TABLE in tables else None
        return {"objects": objects, "tables": data, "version": version,
                "integrity": db.execute("PRAGMA integrity_check").fetchone()[0]}
    finally:
        db.close()


def run(journal, backup_dir, *, allow_create=False, check=False, report_dir=None) -> dict:
    rep = {"journal": str(journal), "expected_schema_version": EXPECTED_SCHEMA_VERSION, "started_at": time.time()}
    if not journal:
        raise Abort(2, "AUTO_BUY_US_RT_JOURNAL_PATH 또는 --journal 이 필요합니다.")
    journal = str(Path(journal).resolve())
    exists = os.path.isfile(journal)
    rep["journal_exists"] = exists
    if not exists and not allow_create:
        raise Abort(2, f"저널 파일이 없습니다: {journal}. 새 저널을 만들려면 --allow-create (경로를 다시 확인할 것).")
    rep["status_before"] = schema_status(journal)
    pre = None
    if exists:
        pre = snapshot(journal)
        rep["before"] = {k: pre[k] for k in ("tables", "version", "integrity")}
        if pre["integrity"] != "ok":
            raise Abort(2, f"기존 저널 무결성 검사 실패: {pre['integrity']}")
    if check:
        rep["result"] = "CHECK_ONLY"
        return rep
    if exists:
        rep["backup"] = str(backup(journal, backup_dir))
    try:
        rep["schema_version"] = upgrade_journal(journal, allow_create=allow_create)
    except Exception as exc:
        rep["result"] = "MIGRATION_FAILED"
        rep["error"] = str(exc)
        _write(rep, report_dir or backup_dir)
        raise Abort(3, f"Migration 실패 (트랜잭션 롤백, 저널 원본 유지, 백업으로 덮어쓰지 않음): {exc}")
    try:
        problems = _verify(journal, pre, rep)
    except Exception as exc:
        problems = [f"사후 검증 중 예외: {exc}"]
    rep["result"] = "FAILED_VERIFY" if problems else "OK"
    rep["problems"] = problems
    _write(rep, report_dir or backup_dir)
    if problems:
        raise Abort(4, "사후 검증 실패 — 운영 배포를 진행하지 마세요: " + "; ".join(problems))
    return rep


def _verify(journal, pre, rep) -> list:
    post = snapshot(journal)
    rep["after"] = {k: post[k] for k in ("tables", "version", "integrity")}
    problems = []
    if post["version"] != [EXPECTED_SCHEMA_VERSION]:
        problems.append(f"버전 {post['version']} ≠ {EXPECTED_SCHEMA_VERSION}")
    if post["integrity"] != "ok":
        problems.append(f"무결성 {post['integrity']}")
    after_sql = {o["name"]: o["sql"] for o in post["objects"]}
    if pre:
        for t, v in pre["tables"].items():
            if post["tables"].get(t) != v:
                problems.append(f"기존 테이블 {t} 데이터 변경 {v} → {post['tables'].get(t)}")
        before_sql = {o["name"]: o["sql"] for o in pre["objects"]}
        for name, sql in before_sql.items():
            if after_sql.get(name) != sql:
                problems.append(f"기존 객체 정의 변경: {name}")
        rep["added_objects"] = sorted(set(after_sql) - set(before_sql))
    else:
        rep["added_objects"] = sorted(after_sql)
    missing = sorted(set(expected_objects()) - set(after_sql))
    if missing:
        problems.append(f"신규 객체 없음: {missing}")
    db = _ro(journal)
    try:
        diff = structure_diff(db)
        if diff:
            problems.append(f"구조 차이: {diff}")
    finally:
        db.close()
    st = schema_status(journal)
    if not st["schema_ready"]:
        problems.append(f"런타임 스키마 검증 실패: {st['schema_error']}")
    rep["status_after"] = st
    return problems


def _write(rep, directory):
    if not directory:
        return
    Path(directory).mkdir(parents=True, exist_ok=True)
    out = Path(directory) / f"auto_buy_us_migrate-{time.strftime('%Y%m%dT%H%M%S')}-{time.time_ns() % 10**9:09d}.json"
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    os.chmod(out, 0o600)
    rep["report_file"] = str(out)


def main(argv=None):
    p = argparse.ArgumentParser(description="자동매수 RT1 저널 스키마 Migration (명시적 배포 작업)")
    p.add_argument("--journal", default=os.getenv("AUTO_BUY_US_RT_JOURNAL_PATH", ""))
    p.add_argument("--backup-dir", required=True, help="저널 폴더와 다른 백업 폴더")
    p.add_argument("--allow-create", action="store_true", help="저널 파일이 없을 때 새로 만든다")
    p.add_argument("--check", action="store_true", help="사전 점검만 (백업·변경 없음)")
    args = p.parse_args(argv)
    try:
        rep = run(args.journal, args.backup_dir, allow_create=args.allow_create, check=args.check)
    except Abort as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return exc.code
    summary = {k: rep.get(k) for k in ("result", "journal", "schema_version", "backup", "report_file",
                                       "added_objects")}
    summary["tables_before"] = {t: v["rows"] for t, v in (rep.get("before") or {}).get("tables", {}).items()}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
