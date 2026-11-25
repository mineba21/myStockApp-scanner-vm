"""자동매수 SHADOW 저널 접근. DDL 없음, ``mode=rw`` (파일이 없으면 실패하고 만들지 않는다)."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from trading.auto_buy.shadow.schema import schema_status


class SchemaNotReady(RuntimeError):
    pass


def connect_rw(path: str) -> sqlite3.Connection:
    st = schema_status(path)
    if not st["schema_ready"]:
        raise SchemaNotReady(st["schema_error"] or "자동매수 SHADOW 스키마가 준비되지 않았습니다.")
    uri = Path(path).resolve().as_uri() + "?mode=rw"
    db = sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


@contextmanager
def tx(db: sqlite3.Connection):
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
    except BaseException:
        db.execute("ROLLBACK")
        raise
    else:
        db.execute("COMMIT")


def set_runtime(db, key: str, value) -> None:
    db.execute("INSERT INTO ab_runtime(key, value, updated_at) VALUES (?,?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
               (key, json.dumps(value, ensure_ascii=False, default=str), time.time()))


def get_runtime(db, key: str, default=None):
    row = db.execute("SELECT value FROM ab_runtime WHERE key=?", (key,)).fetchone()
    if row is None or row[0] is None:
        return default
    try:
        return json.loads(row[0])
    except ValueError:
        return default


def event(db, severity: str, kind: str, message: str, **details) -> None:
    db.execute("INSERT INTO ab_events(at, severity, kind, message, details) VALUES (?,?,?,?,?)",
               (time.time(), severity, kind, message, json.dumps(details, ensure_ascii=False, default=str)))
