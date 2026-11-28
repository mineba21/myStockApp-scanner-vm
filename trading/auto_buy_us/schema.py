"""자동매수 RT1 저널 스키마: 런타임 검증(읽기 전용)과 명시적 Migration 진입점.

- 런타임(스케줄러·웹 API·store)은 DDL 을 실행하지 않는다. ``schema_status`` 는 ``mode=ro`` 로 열어 버전과 구조만
  확인하며 파일이 없으면 만들지 않는다.
- 생성은 전용 Alembic(``trading/auto_buy_us/journal_migrations``)과 ``scripts.auto_buy_us_journal_migrate`` 로만 한다.
  버전 표는 ``abu_alembic_version`` 으로 운영 DB·S01 저널과 분리된다.
"""
from __future__ import annotations

import os
import re
import sqlite3
from functools import lru_cache
from pathlib import Path

EXPECTED_SCHEMA_VERSION = "abu0001"
VERSION_TABLE = "abu_alembic_version"
MIGRATIONS_DIR = Path(__file__).with_name("journal_migrations")
OWNED_PREFIX = "abu_"
_OWNED_SQL = "SELECT type, name, sql FROM sqlite_master WHERE name GLOB 'abu_*' AND sql IS NOT NULL"


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql or "").strip()


@lru_cache(maxsize=1)
def expected_objects() -> dict:
    """v1 DDL 을 메모리 DB 에 적용해 얻은 {이름: (종류, 정규화 SQL)}. 저널 파일에는 아무것도 쓰지 않는다."""
    from trading.auto_buy_us.journal_migrations.ddl_v1 import DDL_V1
    db = sqlite3.connect(":memory:")
    try:
        for stmt in DDL_V1:
            db.execute(stmt)
        return {name: (kind, _norm(sql)) for kind, name, sql in db.execute(_OWNED_SQL)}
    finally:
        db.close()


def owned_objects(db) -> dict:
    """저널의 abu_ 소유 객체 {이름: (종류, 정규화 SQL)} (버전 표 제외)."""
    return {name: (kind, _norm(sql)) for kind, name, sql in db.execute(_OWNED_SQL) if name != VERSION_TABLE}


def structure_diff(db) -> list[str]:
    """기대 v1 구조와의 차이 (빈 목록 = 정확히 일치)."""
    have, want = owned_objects(db), expected_objects()
    diff = [f"없음: {n}" for n in sorted(set(want) - set(have))]
    diff += [f"예상 밖 객체: {n}" for n in sorted(set(have) - set(want))]
    diff += [f"정의 다름: {n}" for n in sorted(set(want) & set(have)) if have[n] != want[n]]
    return diff


def _ro_connect(path: str):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)


def schema_status(path: str | None) -> dict:
    """런타임 스키마 상태. 읽기 전용, 파일·테이블을 만들지 않는다. 판단 불가면 준비 안 됨(fail closed)."""
    st = {"journal_exists": False, "schema_ready": False, "schema_version": None,
          "expected_schema_version": EXPECTED_SCHEMA_VERSION, "migration_required": True, "schema_error": None}
    path = str(path or "").strip()
    if not path:
        st["schema_error"] = "AUTO_BUY_US_RT_JOURNAL_PATH가 설정되지 않았습니다."
        return st
    if not os.path.isfile(path):
        st["schema_error"] = "자동매수 저널 파일이 없습니다 (Migration 필요)."
        return st
    st["journal_exists"] = True
    try:
        db = _ro_connect(path)
    except sqlite3.Error as exc:
        st["schema_error"] = f"자동매수 저널을 읽기 전용으로 열지 못했습니다: {exc}"
        return st
    try:
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
        if VERSION_TABLE in names:
            rows = [r[0] for r in db.execute(f"SELECT version_num FROM {VERSION_TABLE}")]
            st["schema_version"] = rows[0] if len(rows) == 1 else (",".join(rows) or None)
        diff = structure_diff(db)
        if st["schema_version"] == EXPECTED_SCHEMA_VERSION and not diff:
            st["schema_ready"], st["migration_required"] = True, False
        elif st["schema_version"] is None and len(diff) == len(expected_objects()):
            st["schema_error"] = "자동매수 스키마가 없습니다 (Migration 필요)."
        elif st["schema_version"] != EXPECTED_SCHEMA_VERSION:
            st["schema_error"] = (f"스키마 버전 {st['schema_version']} ≠ 기대 {EXPECTED_SCHEMA_VERSION} "
                                  "(Migration 필요).")
        else:
            st["schema_error"] = "스키마 구조가 v1 과 다릅니다: " + "; ".join(diff)
    except sqlite3.Error as exc:
        st["schema_error"] = f"스키마를 확인하지 못했습니다: {exc}"
        st["schema_ready"], st["migration_required"] = False, True
    finally:
        db.close()
    return st


def upgrade_journal(path: str, *, allow_create: bool = False) -> str:
    """저널 전용 Alembic upgrade head. 배포 스크립트·테스트에서만 부른다 (런타임 자동 실행 금지)."""
    from alembic import command
    from alembic.config import Config

    path = str(Path(path).resolve())
    if not os.path.isfile(path) and not allow_create:
        raise FileNotFoundError(f"자동매수 저널이 없습니다: {path} (새로 만들려면 --allow-create)")
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["journal_path"] = path
    command.upgrade(cfg, "head")
    st = schema_status(path)
    if not st["schema_ready"]:
        raise RuntimeError(f"Migration 후 스키마 검증 실패: {st['schema_error']}")
    return st["schema_version"]
