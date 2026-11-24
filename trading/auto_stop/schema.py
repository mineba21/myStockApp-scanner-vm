"""자동 손절 저널 스키마: 런타임 검증(읽기 전용)과 명시적 Migration 진입점.

- 런타임(스케줄러·웹 API·AutoStopStore)은 DDL 을 실행하지 않는다. 여기의 ``schema_status`` 는 저널을
  ``mode=ro`` 로 열어 버전과 객체 존재만 확인하며, 파일이 없으면 만들지 않는다.
- 테이블·인덱스 생성은 저널 전용 Alembic 환경(``trading/auto_stop/journal_migrations``)으로만 한다.
  운영 DB 의 Alembic(``alembic/``)과 설정·버전 이력을 공유하지 않는다. 버전 표는 ``auto_stop_alembic_version``.
- 실행은 배포 작업 ``python -m scripts.auto_stop_journal_migrate`` 로만 한다 (백업·전후 검증 포함).
"""
from __future__ import annotations

import os
import re
import sqlite3
from functools import lru_cache
from pathlib import Path

EXPECTED_SCHEMA_VERSION = "as0001"
VERSION_TABLE = "auto_stop_alembic_version"
MIGRATIONS_DIR = Path(__file__).with_name("journal_migrations")
OWNED_PREFIX = "auto_stop_"


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql or "").strip()


@lru_cache(maxsize=1)
def expected_objects() -> dict:
    """v1 DDL 을 메모리 DB 에 적용해 얻은 {이름: (종류, 정규화 SQL)}. 저널 파일에는 아무것도 쓰지 않는다."""
    from trading.auto_stop.journal_migrations.ddl_v1 import DDL_V1
    db = sqlite3.connect(":memory:")
    try:
        for stmt in DDL_V1:
            db.execute(stmt)
        return {name: (kind, _norm(sql)) for kind, name, sql in db.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name LIKE 'auto_stop_%' AND sql IS NOT NULL")}
    finally:
        db.close()


@lru_cache(maxsize=1)
def expected_columns() -> dict:
    from trading.auto_stop.journal_migrations.ddl_v1 import DDL_V1
    db = sqlite3.connect(":memory:")
    try:
        for stmt in DDL_V1:
            db.execute(stmt)
        tables = [n for n, (k, _) in expected_objects().items() if k == "table"]
        return {t: {r[1] for r in db.execute(f"PRAGMA table_info({t})")} for t in tables}
    finally:
        db.close()


def owned_objects(db) -> dict:
    """저널에 있는 자동 손절 소유 객체 {이름: (종류, 정규화 SQL)} (버전 표·sqlite 자동 인덱스 제외)."""
    return {name: (kind, _norm(sql)) for kind, name, sql in db.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name LIKE 'auto_stop_%' AND sql IS NOT NULL")
        if name != VERSION_TABLE}


def structure_diff(db) -> list[str]:
    """기대 v1 구조와의 차이 목록 (빈 목록 = 정확히 일치)."""
    have, want = owned_objects(db), expected_objects()
    diff = [f"없음: {n}" for n in sorted(set(want) - set(have))]
    diff += [f"예상 밖 객체: {n}" for n in sorted(set(have) - set(want))]
    diff += [f"정의 다름: {n}" for n in sorted(set(want) & set(have)) if have[n] != want[n]]
    return diff


def _ro_connect(path: str):
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5)


def schema_status(path: str | None) -> dict:
    """런타임 스키마 상태. 읽기 전용이며 파일·테이블을 만들지 않는다. 판단할 수 없으면 준비 안 됨(fail closed)."""
    st = {"journal_exists": False, "schema_ready": False, "schema_version": None,
          "expected_schema_version": EXPECTED_SCHEMA_VERSION, "migration_required": True, "schema_error": None,
          "reconcile_compatible": False, "auto_stop_objects": 0, "live_unresolved_orders": None}
    path = str(path or "").strip()
    if not path:
        st["schema_error"] = "SCANNER_ORDER_JOURNAL_PATH가 설정되지 않았습니다."
        return st
    if not os.path.isfile(path):
        st["schema_error"] = "주문 저널 파일이 없습니다 (Migration 필요)."
        return st
    st["journal_exists"] = True
    try:
        db = _ro_connect(path)
    except sqlite3.Error as exc:
        st["schema_error"] = f"주문 저널을 읽기 전용으로 열지 못했습니다: {exc}"
        return st
    try:
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
        if VERSION_TABLE in names:
            rows = [r[0] for r in db.execute(f"SELECT version_num FROM {VERSION_TABLE}")]
            st["schema_version"] = rows[0] if len(rows) == 1 else (",".join(rows) or None)
        have = owned_objects(db)
        st["auto_stop_objects"] = len(have)
        want = expected_objects()
        cols = expected_columns()
        tables_ok = all(t in names for t in cols)
        if tables_ok:
            st["reconcile_compatible"] = all(
                cols[t] <= {r[1] for r in db.execute(f"PRAGMA table_info({t})")} for t in cols)
        if "auto_stop_orders" in names:
            try:
                st["live_unresolved_orders"] = db.execute(
                    "SELECT COUNT(*) FROM auto_stop_orders WHERE mode='LIVE' AND state IN "
                    "('SENDING','SUBMITTED','PARTIAL_FILLED','CANCEL_SENDING','CANCEL_PENDING',"
                    "'CANCEL_UNKNOWN','UNKNOWN')").fetchone()[0]
            except sqlite3.Error:
                st["live_unresolved_orders"] = None
        if st["schema_version"] == EXPECTED_SCHEMA_VERSION and set(want) <= set(have):
            st["schema_ready"], st["migration_required"] = True, False
        elif not have and st["schema_version"] is None:
            st["schema_error"] = "자동 손절 스키마가 없습니다 (Migration 필요)."
        elif st["schema_version"] is None:
            st["schema_error"] = "버전 기록 없는 자동 손절 테이블이 있습니다 (이전 자체 DDL 저널, Migration으로 구조 검증 필요)."
        elif st["schema_version"] != EXPECTED_SCHEMA_VERSION:
            st["schema_error"] = (f"스키마 버전 {st['schema_version']} ≠ 기대 {EXPECTED_SCHEMA_VERSION}. "
                                  "신규 자동 손절 주문을 차단합니다.")
        else:
            st["schema_error"] = "스키마 버전은 맞지만 필요한 객체가 없습니다: " + ", ".join(sorted(set(want) - set(have)))
    except sqlite3.Error as exc:
        st["schema_error"] = f"스키마를 확인하지 못했습니다: {exc}"
        st["schema_ready"], st["migration_required"], st["reconcile_compatible"] = False, True, False
    finally:
        db.close()
    return st


def schema_block_reason(path: str | None) -> str | None:
    st = schema_status(path)
    return None if st["schema_ready"] else (st["schema_error"] or "자동 손절 스키마가 준비되지 않았습니다.")


def upgrade_journal(path: str, *, allow_create: bool = False) -> str:
    """저널 전용 Alembic upgrade head. 배포 스크립트·테스트에서만 부른다 (런타임 자동 실행 금지)."""
    from alembic import command
    from alembic.config import Config

    path = str(Path(path).resolve())
    if not os.path.isfile(path) and not allow_create:
        raise FileNotFoundError(f"주문 저널이 없습니다: {path} (새로 만들려면 --allow-create)")
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["journal_path"] = path
    command.upgrade(cfg, "head")
    st = schema_status(path)
    if not st["schema_ready"]:
        raise RuntimeError(f"Migration 후 스키마 검증 실패: {st['schema_error']}")
    return st["schema_version"]
