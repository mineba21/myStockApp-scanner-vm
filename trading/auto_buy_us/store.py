"""자동매수 SHADOW 저널 저장소: 연결·트랜잭션·lease·runtime·시스템 이벤트·API 호출 집계.

스키마는 저널 전용 Migration 이 만든다. 여기서는 DDL 을 실행하지 않고, 파일이 없으면 생성하지 않고 실패한다.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LEVELS = ("INFO", "WARN", "URGENT")
API_OUTCOMES = ("ok", "err", "http_429", "timeout", "budget_skip")
_LEASE_KEY = "lease"


class LeaseLost(Exception):
    """lease 를 잃음. 이 작업자는 더 이상 상태를 변경하지 않는다."""


class Store:
    def __init__(self, path: str, clock=time.time):
        self.path = str(path)
        self.clock = clock
        self._uri = Path(self.path).resolve().as_uri() + "?mode=rw"

    def connect(self) -> sqlite3.Connection:
        if not Path(self.path).is_file():
            raise FileNotFoundError(self.path)
        try:
            db = sqlite3.connect(self._uri, uri=True, timeout=30, isolation_level=None)
        except sqlite3.OperationalError as e:
            raise FileNotFoundError(self.path) from e
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    @contextmanager
    def tx(self):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")
        finally:
            db.close()

    @contextmanager
    def read(self):
        db = self.connect()
        try:
            db.execute("BEGIN")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")
        finally:
            db.close()

    # ── lease ────────────────────────────────────────────────────
    def _lease(self, db):
        return self.get_runtime(db, _LEASE_KEY)

    def acquire_lease(self, owner: str, now: float, lease_sec: int) -> bool:
        with self.tx() as db:
            cur = self._lease(db)
            if cur and cur["owner"] != owner and now < cur["until"]:
                return False
            self.set_runtime(db, _LEASE_KEY, {"owner": owner, "until": now + lease_sec})
            return True

    def renew_lease(self, owner: str, now: float, lease_sec: int) -> None:
        with self.tx() as db:
            cur = self._lease(db)
            if not cur or cur["owner"] != owner or now >= cur["until"]:
                raise LeaseLost("lease 소유권이 없습니다.")
            self.set_runtime(db, _LEASE_KEY, {"owner": owner, "until": now + lease_sec})

    def release_lease(self, owner: str) -> None:
        with self.tx() as db:
            cur = self._lease(db)
            if cur and cur["owner"] == owner:
                db.execute("DELETE FROM abu_runtime WHERE key=?", (_LEASE_KEY,))

    # ── runtime ──────────────────────────────────────────────────
    @staticmethod
    def get_runtime(db, key: str) -> Any:
        r = db.execute("SELECT value FROM abu_runtime WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r and r["value"] is not None else None

    @staticmethod
    def set_runtime(db, key: str, value: Any) -> None:
        db.execute("INSERT INTO abu_runtime(key,value) VALUES(?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)))

    # ── 시스템 이벤트 ────────────────────────────────────────────
    @staticmethod
    def event(db, level: str, kind: str, now: float, **details) -> None:
        if level not in LEVELS:
            raise ValueError(f"level 은 {LEVELS} 중 하나여야 합니다: {level!r}")
        db.execute("INSERT INTO abu_system_events(at,level,kind,details_json,notified) VALUES(?,?,?,?,0)",
                   (now, level, kind,
                    json.dumps(details, ensure_ascii=False, sort_keys=True, default=str)))

    # ── API 호출 집계 ────────────────────────────────────────────
    @staticmethod
    def count_api(db, now: float, api_id: str, outcome: str, n: int = 1) -> None:
        if outcome not in API_OUTCOMES:
            raise ValueError(f"outcome 은 {API_OUTCOMES} 중 하나여야 합니다: {outcome!r}")
        minute = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M")
        db.execute("INSERT INTO abu_api_calls(minute,api_id,outcome,n) VALUES(?,?,?,?) "
                   "ON CONFLICT(minute,api_id,outcome) DO UPDATE SET n = n + excluded.n",
                   (minute, api_id, outcome, n))
