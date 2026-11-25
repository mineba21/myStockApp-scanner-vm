"""자동매수 SHADOW 저널 스키마: 런타임 검증(읽기 전용)과 명시적 Migration 진입점.

- 런타임(스케줄러 job·Scanner hook·웹 API)은 DDL 을 실행하지 않고 파일도 만들지 않는다. ``schema_status`` 는
  ``mode=ro`` 로만 연다. 파일이 없으면 "Migration 필요" 를 돌려준다.
- 테이블 생성은 ``python -m scripts.auto_buy_shadow_journal_migrate`` (배포 작업) 에서만 한다.
"""
from __future__ import annotations

import os
import re
import sqlite3
from functools import lru_cache
from pathlib import Path

EXPECTED_SCHEMA_VERSION = "ab0001"
VERSION_TABLE = "ab_alembic_version"
MIGRATIONS_DIR = Path(__file__).with_name("journal_migrations")
OWNED_PREFIX = "ab_"


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql or "").strip()


@lru_cache(maxsize=1)
def expected_objects() -> dict:
    from trading.auto_buy.shadow.journal_migrations.ddl_v1 import DDL_V1
    db = sqlite3.connect(":memory:")
    try:
        for stmt in DDL_V1:
            db.execute(stmt)
        return {n: (k, _norm(s)) for k, n, s in db.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name LIKE 'ab\\_%' ESCAPE '\\' AND sql IS NOT NULL")}
    finally:
        db.close()


def _objects(db) -> list:
    return list(db.execute("SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL"))


def owned_objects(db) -> dict:
    return {n: (k, _norm(s)) for k, n, s in _objects(db) if n.startswith(OWNED_PREFIX) and n != VERSION_TABLE}


def foreign_objects(db) -> set:
    return {n for _, n, _ in _objects(db) if not n.startswith(OWNED_PREFIX) and not n.startswith("sqlite_")}


def structure_diff(db) -> list[str]:
    have, want = owned_objects(db), expected_objects()
    diff = [f"없음: {n}" for n in sorted(set(want) - set(have))]
    diff += [f"예상 밖 객체: {n}" for n in sorted(set(have) - set(want))]
    diff += [f"정의 다름: {n}" for n in sorted(set(want) & set(have)) if have[n] != want[n]]
    return diff


def ro_connect(path: str) -> sqlite3.Connection:
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    return db


def schema_status(path: str | None) -> dict:
    """읽기 전용. 파일·테이블을 만들지 않는다. 판단할 수 없으면 준비 안 됨."""
    st = {"journal_configured": bool(path), "journal_exists": False, "schema_ready": False, "schema_version": None,
          "expected_schema_version": EXPECTED_SCHEMA_VERSION, "migration_required": True, "schema_error": None}
    path = str(path or "").strip()
    if not path:
        st["schema_error"] = "AUTO_BUY_SHADOW_JOURNAL_PATH가 설정되지 않았습니다."
        return st
    if not os.path.isfile(path):
        st["schema_error"] = "자동매수 SHADOW 저널 파일이 없습니다 (Migration 필요)."
        return st
    st["journal_exists"] = True
    try:
        db = ro_connect(path)
    except sqlite3.Error as exc:
        st["schema_error"] = f"저널을 읽기 전용으로 열지 못했습니다: {exc}"
        return st
    try:
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
        if VERSION_TABLE in names:
            rows = [r[0] for r in db.execute(f"SELECT version_num FROM {VERSION_TABLE}")]
            st["schema_version"] = rows[0] if len(rows) == 1 else (",".join(rows) or None)
        diff = structure_diff(db)
        if st["schema_version"] == EXPECTED_SCHEMA_VERSION and not diff:
            st["schema_ready"], st["migration_required"] = True, False
        elif st["schema_version"] is None:
            st["schema_error"] = "자동매수 SHADOW 스키마가 없습니다 (Migration 필요)."
        elif st["schema_version"] != EXPECTED_SCHEMA_VERSION:
            st["schema_error"] = f"스키마 버전 {st['schema_version']} ≠ 기대 {EXPECTED_SCHEMA_VERSION}."
        else:
            st["schema_error"] = "스키마 구조가 v1 과 다릅니다: " + "; ".join(diff[:5])
    except sqlite3.Error as exc:
        st["schema_error"] = f"스키마를 확인하지 못했습니다: {exc}"
    finally:
        db.close()
    return st


def upgrade_journal(path: str, *, allow_create: bool = False) -> str:
    """저널 전용 Alembic upgrade head. 배포 스크립트·테스트에서만 부른다 (런타임 자동 실행 금지)."""
    from alembic import command
    from alembic.config import Config

    path = str(Path(path).resolve())
    if not os.path.isfile(path) and not allow_create:
        raise FileNotFoundError(f"자동매수 SHADOW 저널이 없습니다: {path} (새로 만들려면 --allow-create)")
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["journal_path"] = path
    command.upgrade(cfg, "head")
    st = schema_status(path)
    if not st["schema_ready"]:
        raise RuntimeError(f"Migration 후 스키마 검증 실패: {st['schema_error']}")
    return st["schema_version"]
