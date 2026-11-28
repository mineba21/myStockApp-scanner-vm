"""자동 손절 원장·주문 저널 (SCANNER_ORDER_JOURNAL_PATH SQLite). 스키마는 저널 전용 Alembic(trading/auto_stop/schema.py).

모든 상태 변경은 BEGIN IMMEDIATE 트랜잭션 안의 조건부 UPDATE 로만 한다.
주문·취소 선점은 같은 트랜잭션에서 lease 소유권, 미확정 주문 부재, 재주문·이월 한도를 함께 검증한다.
이 저널을 삭제·초기화하거나 과거 사본으로 덮어쓰면 중복 전송 방지가 깨진다 (AGENTS.md).
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

UNRESOLVED = ("SENDING", "SUBMITTED", "PARTIAL_FILLED", "CANCEL_SENDING", "CANCEL_PENDING",
              "CANCEL_UNKNOWN", "UNKNOWN")
TERMINAL = ("FILLED", "CANCELLED", "REJECTED", "NOT_SENT")
ORDER_STATES = UNRESOLVED + TERMINAL
POSITION_LIVE_STATES = ("ACTIVE", "EXITING", "NEEDS_REVIEW")
POSITION_STATES = POSITION_LIVE_STATES + ("SOLD", "EXTERNAL_CLOSED", "DISABLED")
EVENT_OPEN_STATES = ("OPEN", "CARRY_OVER")
EVENT_STATES = EVENT_OPEN_STATES + ("DONE", "NEEDS_REVIEW")
ORDER_KINDS = ("ORIGINAL", "REPLACE", "CARRY_OVER")


def _in(values) -> str:
    return "(" + ",".join(f"'{v}'" for v in values) + ")"


# 최초 영구 감시 종료 시각: 일반 감시(ACTIVE)가 끝나는 상태 전이와 같은 UPDATE 문에서 한 번만 기록한다.
# SET 식의 state 는 갱신 전 값이므로 ACTIVE 에서 벗어날 때만 남고, 이후 전이(NEEDS_REVIEW → DISABLED 등)·체결·
# 사용자 조작·재시작으로 바뀌지 않는다. 전이가 실패하면(행 0) 함께 남지 않는다. 기존 details 열 사용 (DDL 없음).
WATCH_END_SET = ("details=CASE WHEN state='ACTIVE' AND json_extract(details,'$.watch_ended_at') IS NULL "
                 "THEN json_set(COALESCE(NULLIF(details,''),'{}'),'$.watch_ended_at',?,'$.watch_end_reason',?) "
                 "ELSE details END")


class ClaimRefused(Exception):
    """선점 트랜잭션이 조건 불충족으로 거부됨 (아무것도 전송하지 않음)."""


class LeaseLost(Exception):
    """lease 를 잃음. 이 작업자는 더 이상 주문·상태 변경을 하지 않는다."""


class ScannerJournalUnreadable(Exception):
    """Scanner 매수 저널을 읽을 수 없음 (스키마 불일치·조회 오류). 미확정 주문 없음으로 보지 않는다."""


# Scanner 매수의 미확정 정의 (trading/scanner_buy_orders.py): 전송 중·접수 불명(SENDING/UNKNOWN), 그리고
# 접수(SUBMITTED)됐지만 브로커 최종 상태(FILLED/CANCELLED/REJECTED)가 확인되지 않은 주문(미체결·부분체결 포함).
# PREPARED(미리보기, 미전송)·BLOCKED·REJECTED·ABANDONED·RESOLVED_MANUAL 은 증권사에 살아 있는 주문이 아니다.
SCANNER_IN_FLIGHT_SQL = ("SELECT 1 FROM main.scanner_buy_orders WHERE account_profile='account1' AND ticker=? AND "
                         "(state IN ('SENDING','UNKNOWN') OR (state='SUBMITTED' AND "
                         "COALESCE(broker_state,'') NOT IN ('FILLED','CANCELLED','REJECTED'))) LIMIT 1")


def _row(r):
    return dict(r) if r is not None else None


class AutoStopStore:
    """스키마는 저널 전용 Migration(scripts.auto_stop_journal_migrate)이 만든다. 생성자는 DDL 을 실행하지 않고,
    연결은 mode=rw 라 저널 파일이 없으면 새로 만들지 않고 실패한다. 호출 전 schema.schema_status 로 확인한다."""

    def __init__(self, path):
        self.path = str(path)
        self._uri = Path(self.path).resolve().as_uri() + "?mode=rw"

    @contextmanager
    def tx(self):
        db = sqlite3.connect(self._uri, uri=True, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    # ── lease ────────────────────────────────────────────────────
    @staticmethod
    def _lease(db):
        r = db.execute("SELECT value FROM auto_stop_runtime WHERE key='lease'").fetchone()
        return json.loads(r["value"]) if r and r["value"] else None

    def acquire_lease(self, owner, now, seconds) -> bool:
        with self.tx() as db:
            cur = self._lease(db)
            if cur and cur["owner"] != owner and cur["until"] > now:
                return False
            db.execute("INSERT OR REPLACE INTO auto_stop_runtime(key,value,updated_at) VALUES('lease',?,?)",
                       (json.dumps({"owner": owner, "until": now + seconds}), now))
            return True

    def renew_lease(self, owner, now, seconds) -> None:
        with self.tx() as db:
            self._check_lease(db, owner, now)
            db.execute("UPDATE auto_stop_runtime SET value=?, updated_at=? WHERE key='lease'",
                       (json.dumps({"owner": owner, "until": now + seconds}), now))

    def release_lease(self, owner, now) -> None:
        with self.tx() as db:
            cur = self._lease(db)
            if cur and cur["owner"] == owner:
                db.execute("DELETE FROM auto_stop_runtime WHERE key='lease'")

    def _check_lease(self, db, owner, now):
        cur = self._lease(db)
        if not cur or cur["owner"] != owner or cur["until"] <= now:
            raise LeaseLost("lease 소유권이 없습니다.")

    @staticmethod
    def _extend_lease(db, owner, now, seconds):
        """전송 직전 선점과 같은 트랜잭션에서 lease 를 연장해 HTTP 요청 동안 소유권 창을 확보한다."""
        db.execute("UPDATE auto_stop_runtime SET value=?, updated_at=? WHERE key='lease'",
                   (json.dumps({"owner": owner, "until": now + seconds}), now))

    # ── 전체 긴급 중지 (영속) ─────────────────────────────────────
    @staticmethod
    def _kill(db):
        r = db.execute("SELECT value FROM auto_stop_runtime WHERE key='kill_switch'").fetchone()
        v = json.loads(r["value"]) if r and r["value"] else None
        return v if v and v.get("on") else None

    def kill_switch(self):
        with self.tx() as db:
            return self._kill(db)

    def set_kill_switch(self, now, on: bool, *, actor, reason):
        value = {"on": bool(on), "at": now, "by": actor, "reason": reason}
        with self.tx() as db:
            db.execute("INSERT OR REPLACE INTO auto_stop_runtime(key,value,updated_at) VALUES('kill_switch',?,?)",
                       (json.dumps(value), now))
            db.execute("INSERT INTO auto_stop_audit(at,actor,action,target,details) VALUES(?,?,?,?,?)",
                       (now, actor, "kill_switch_on" if on else "kill_switch_off", "global",
                        json.dumps({"reason": reason})))
        return value

    # ── 일별 운영 통계 ───────────────────────────────────────────
    def bump(self, day, mode, metric, key="", n=1):
        with self.tx() as db:
            db.execute("INSERT INTO auto_stop_daily_stats(day,mode,metric,key,value) VALUES(?,?,?,?,?) "
                       "ON CONFLICT(day,mode,metric,key) DO UPDATE SET value=value+excluded.value",
                       (day, mode, metric, key, n))

    def stat_max(self, day, mode, metric, value, key=""):
        with self.tx() as db:
            db.execute("INSERT INTO auto_stop_daily_stats(day,mode,metric,key,value) VALUES(?,?,?,?,?) "
                       "ON CONFLICT(day,mode,metric,key) DO UPDATE SET value=MAX(value, excluded.value)",
                       (day, mode, metric, key, value))

    def stats(self, day, mode):
        with self.tx() as db:
            return [dict(r) for r in db.execute(
                "SELECT metric,key,value FROM auto_stop_daily_stats WHERE day=? AND mode=?", (day, mode))]

    # ── runtime / audit ─────────────────────────────────────────
    def runtime_get(self, key, default=None):
        with self.tx() as db:
            r = db.execute("SELECT value FROM auto_stop_runtime WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r and r["value"] is not None else default

    def runtime_set(self, key, value, now):
        with self.tx() as db:
            db.execute("INSERT OR REPLACE INTO auto_stop_runtime(key,value,updated_at) VALUES(?,?,?)",
                       (key, json.dumps(value), now))

    def runtime_keys(self, prefixes):
        """값이 있는(null 이 아닌) runtime 키 목록 (접두어 일치, 읽기 전용)."""
        out = []
        with self.tx() as db:
            for prefix in prefixes:
                out += [r["key"] for r in db.execute(
                    "SELECT key FROM auto_stop_runtime WHERE substr(key, 1, ?)=? AND value IS NOT NULL AND value!='null' "
                    "ORDER BY key", (len(prefix), prefix))]
        return out

    def runtime_txn(self, now, keys, fn):
        """여러 runtime 키를 한 BEGIN IMMEDIATE 트랜잭션에서 읽고 바꾼다 (DDL 없음).

        fn(values: dict, db) → (updates: dict, audits: list[(actor, action, target, details)]) 이거나 None.
        fn 이나 쓰기 도중 예외가 나면 전부 롤백된다 (부분 변경 없음). 반환: updates (None 이면 변경 없음)."""
        with self.tx() as db:
            values = {}
            for key in keys:
                r = db.execute("SELECT value FROM auto_stop_runtime WHERE key=?", (key,)).fetchone()
                values[key] = json.loads(r["value"]) if r and r["value"] is not None else None
            result = fn(values, db)
            if not result:
                return None
            updates, audits = result
            for key, value in (updates or {}).items():
                db.execute("INSERT OR REPLACE INTO auto_stop_runtime(key,value,updated_at) VALUES(?,?,?)",
                           (key, json.dumps(value, default=str), now))
            for actor, action, target, details in (audits or []):
                self._audit_db(db, now, actor, action, target, details)
            return updates

    @staticmethod
    def _audit_db(db, now, actor, action, target, details):
        db.execute("INSERT INTO auto_stop_audit(at,actor,action,target,details) VALUES(?,?,?,?,?)",
                   (now, actor, action, str(target), json.dumps(details, default=str)))

    def audit(self, now, actor, action, target, details):
        with self.tx() as db:
            db.execute("INSERT INTO auto_stop_audit(at,actor,action,target,details) VALUES(?,?,?,?,?)",
                       (now, actor, action, str(target), json.dumps(details, default=str)))

    def audits(self, action, since, until):
        with self.tx() as db:
            rows = db.execute("SELECT at,actor,action,target,details FROM auto_stop_audit "
                              "WHERE action=? AND at>=? AND at<? ORDER BY id", (action, since, until)).fetchall()
        return [{**dict(r), "details": json.loads(r["details"])} for r in rows]

    # ── positions ───────────────────────────────────────────────
    def insert_position(self, now, **f) -> int:
        with self.tx() as db:
            active = db.execute(f"SELECT COUNT(*) c FROM auto_stop_positions WHERE mode=? AND state IN "
                                f"{_in(POSITION_LIVE_STATES)}", (f["mode"],)).fetchone()["c"]
            if active >= f.pop("max_active"):
                raise ClaimRefused("자동 손절 등록 종목 수 한도를 넘었습니다.")
            busy = db.execute(f"SELECT 1 FROM auto_stop_orders WHERE mode=? AND ticker=? AND state IN "
                              f"{_in(UNRESOLVED)}", (f["mode"], f["ticker"])).fetchone()
            if busy:
                raise ClaimRefused("같은 종목의 미확정 자동 손절 주문이 있어 등록할 수 없습니다.")
            try:
                cur = db.execute(
                    '''INSERT INTO auto_stop_positions(mode,account_profile,market,ticker,exchange,source,
                       source_ref,scan_result_id,signal_snapshot,stop_price,initial_managed_qty,
                       remaining_managed_qty,broker_qty_at_register,avg_cost_at_register,state,
                       review_reason,last_quote_price,last_quote_at,created_at,updated_at,details)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (f["mode"], "account1", "US", f["ticker"], f["exchange"], f["source"], f.get("source_ref"),
                     f["scan_result_id"], json.dumps(f["signal_snapshot"], default=str), str(f["stop_price"]),
                     f["qty"], f["qty"], f["broker_qty"], f.get("avg_cost"), f.get("state", "ACTIVE"),
                     f.get("review_reason"), f.get("quote_price"), f.get("quote_at"), now, now,
                     json.dumps(_initial_details(f, now), default=str)))
            except sqlite3.IntegrityError as exc:
                raise ClaimRefused("이미 자동 손절이 등록된 종목입니다.") from exc
            return cur.lastrowid

    def position(self, pid):
        with self.tx() as db:
            return _row(db.execute("SELECT * FROM auto_stop_positions WHERE id=?", (pid,)).fetchone())

    def positions(self, mode=None, states=None):
        sql, args = "SELECT * FROM auto_stop_positions WHERE 1=1", []
        if mode:
            sql += " AND mode=?"; args.append(mode)
        if states:
            sql += f" AND state IN {_in(states)}"
        with self.tx() as db:
            return [dict(r) for r in db.execute(sql + " ORDER BY id", args)]

    def update_position(self, pid, now, *, expect_states=None, **fields) -> bool:
        if not fields:
            return False
        sets = ", ".join(f"{k}=?" for k in fields) + ", updated_at=?"
        args = list(fields.values()) + [now]
        if "state" in fields and fields["state"] != "ACTIVE":
            sets += ", " + WATCH_END_SET
            args += [now, f"state:{fields['state']}"]
        sql = f"UPDATE auto_stop_positions SET {sets} WHERE id=?"
        args.append(pid)
        if expect_states:
            sql += f" AND state IN {_in(expect_states)}"
        with self.tx() as db:
            return db.execute(sql, args).rowcount == 1

    # ── events ──────────────────────────────────────────────────
    def create_event(self, now, position, *, state, trigger_price, trigger_as_of, session_date) -> int | None:
        with self.tx() as db:
            ok = db.execute(f"UPDATE auto_stop_positions SET state='EXITING', updated_at=?, {WATCH_END_SET} "
                            "WHERE id=? AND state='ACTIVE' AND auto_paused=0",
                            (now, now, "trigger", position["id"])).rowcount
            if ok != 1:
                return None
            cur = db.execute(
                '''INSERT INTO auto_stop_exit_events(position_id,mode,state,triggered_at,trigger_price,
                   trigger_as_of,stop_price,session_date,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (position["id"], position["mode"], state, now, str(trigger_price), trigger_as_of,
                 position["stop_price"], session_date, now, now))
            return cur.lastrowid

    def event(self, eid):
        with self.tx() as db:
            return _row(db.execute("SELECT * FROM auto_stop_exit_events WHERE id=?", (eid,)).fetchone())

    def events(self, states=None, mode=None):
        sql, args = "SELECT * FROM auto_stop_exit_events WHERE 1=1", []
        if states:
            sql += f" AND state IN {_in(states)}"
        if mode:
            sql += " AND mode=?"; args.append(mode)
        with self.tx() as db:
            return [dict(r) for r in db.execute(sql + " ORDER BY id", args)]

    def open_event_for(self, pid):
        with self.tx() as db:
            return _row(db.execute(f"SELECT * FROM auto_stop_exit_events WHERE position_id=? AND state IN "
                                   f"{_in(EVENT_OPEN_STATES + ('NEEDS_REVIEW',))} ORDER BY id DESC LIMIT 1",
                                   (pid,)).fetchone())

    def update_event(self, eid, now, *, expect_states=None, **fields) -> bool:
        sets = ", ".join(f"{k}=?" for k in fields) + ", updated_at=?"
        sql = f"UPDATE auto_stop_exit_events SET {sets} WHERE id=?"
        args = list(fields.values()) + [now, eid]
        if expect_states:
            sql += f" AND state IN {_in(expect_states)}"
        with self.tx() as db:
            return db.execute(sql, args).rowcount == 1

    def review(self, now, *, event_id=None, position_id=None, reason):
        """신규 전송을 멈추고 사람 확인을 요구한다. 미확정 주문 대조는 계속된다."""
        with self.tx() as db:
            if event_id:
                db.execute(f"UPDATE auto_stop_exit_events SET state='NEEDS_REVIEW', review_reason=?, updated_at=? "
                           f"WHERE id=? AND state IN {_in(EVENT_OPEN_STATES)}", (reason, now, event_id))
            if position_id:
                db.execute(f"UPDATE auto_stop_positions SET state='NEEDS_REVIEW', review_reason=?, updated_at=?, "
                           f"{WATCH_END_SET} WHERE id=? AND state IN ('ACTIVE','EXITING')",
                           (reason, now, now, "state:NEEDS_REVIEW", position_id))

    # ── orders ──────────────────────────────────────────────────
    def orders(self, *, event_id=None, states=None, mode=None):
        sql, args = "SELECT * FROM auto_stop_orders WHERE 1=1", []
        if event_id is not None:
            sql += " AND exit_event_id=?"; args.append(event_id)
        if states:
            sql += f" AND state IN {_in(states)}"
        if mode:
            sql += " AND mode=?"; args.append(mode)
        with self.tx() as db:
            return [dict(r) for r in db.execute(sql + " ORDER BY id", args)]

    def order(self, oid):
        with self.tx() as db:
            return _row(db.execute("SELECT * FROM auto_stop_orders WHERE id=?", (oid,)).fetchone())

    @staticmethod
    def rt(db, key, default=None):
        """트랜잭션 안에서 runtime 값 읽기 (공통 실행 게이트용)."""
        r = db.execute("SELECT value FROM auto_stop_runtime WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r and r["value"] is not None else default

    def claim_sell(self, now, *, owner, event_id, kind, qty, ref_price, ref_source, quote_as_of, limit_price,
                   settings, guard, clock=None, gate=None, sent_evidence=None) -> dict:
        """한 트랜잭션에서 모든 조건을 확인하고 SENDING 행을 만든다. guard()는 실거래 스위치 재검증.

        clock 을 주면 쓰기 잠금(BEGIN IMMEDIATE)을 얻은 뒤의 실제 현재 시각으로 lease·게이트를 판단한다 (잠금 대기
        동안 흐른 시간을 반영). gate(db, now) 는 공통 실행 게이트(정규장·cutoff·모드·보류·스냅샷·시세 신선도)이며
        사유 문자열을 돌려주면 선점을 거부한다. 선점과 같은 트랜잭션에서 sent_at·증거를 기록하므로 커밋 직후 곧바로
        증권사 요청을 보낸다 (선점과 전송 사이에 다른 DB·API 작업이 없다)."""
        with self.tx() as db:
            if clock is not None:
                now = clock()
            self._check_lease(db, owner, now)
            kill = self._kill(db)
            if kill:
                raise ClaimRefused(f"전체 긴급 중지 중: {kill.get('reason') or ''}")
            reason = guard()
            if reason:
                raise ClaimRefused(reason)
            if gate is not None:
                reason = gate(db, now)
                if reason:
                    raise ClaimRefused(reason)
                # 게이트가 이 트랜잭션에서 읽은 최신 설정으로 한도·lease 를 판단한다 (서로 다른 시점의 설정 금지).
                settings = getattr(gate, "settings", None) or settings
            self._extend_lease(db, owner, now, settings.lease_sec)
            ev = db.execute("SELECT * FROM auto_stop_exit_events WHERE id=?", (event_id,)).fetchone()
            if ev is None or ev["state"] != "OPEN":
                raise ClaimRefused("청산 이벤트가 OPEN 이 아닙니다.")
            pos = db.execute("SELECT * FROM auto_stop_positions WHERE id=?", (ev["position_id"],)).fetchone()
            if pos["state"] != "EXITING" or pos["auto_paused"]:
                raise ClaimRefused("포지션이 청산 진행 상태가 아니거나 일시중지되었습니다.")
            if qty < 1 or qty > pos["remaining_managed_qty"]:
                raise ClaimRefused("주문 수량이 관리 잔여 수량과 맞지 않습니다.")
            if pos["initial_managed_qty"] != pos["remaining_managed_qty"] + pos["total_filled_qty"]:
                raise ClaimRefused("관리 수량 무결성 위반")
            busy = db.execute(f"SELECT 1 FROM auto_stop_orders WHERE mode=? AND ticker=? AND state IN "
                              f"{_in(UNRESOLVED)}", (ev["mode"], pos["ticker"])).fetchone()
            if busy:
                raise ClaimRefused("같은 종목의 미확정 주문이 있습니다.")
            last = db.execute("SELECT * FROM auto_stop_orders WHERE exit_event_id=? ORDER BY attempt_no DESC LIMIT 1",
                              (event_id,)).fetchone()
            if last is not None and last["state"] not in ("FILLED", "CANCELLED"):
                raise ClaimRefused(f"직전 주문 상태({last['state']})로는 새 주문을 낼 수 없습니다.")
            if kind != ev["next_order_kind"]:
                raise ClaimRefused("주문 종류가 이벤트 상태와 다릅니다.")
            if kind == "REPLACE":
                if ev["session_replace_count"] >= settings.max_replaces_per_session:
                    raise ClaimRefused("세션 재주문 한도 도달")
                if ev["event_replace_count"] >= settings.max_replaces_per_event:
                    raise ClaimRefused("이벤트 재주문 한도 도달")
            attempt = 0 if last is None else last["attempt_no"] + 1
            details = json.dumps(sent_evidence or {}, default=str)
            cur = db.execute(
                '''INSERT INTO auto_stop_orders(exit_event_id,mode,account_profile,attempt_no,kind,parent_order_id,
                   ticker,exchange,ref_price,ref_source,quote_as_of,limit_price,requested_qty,remaining_order_qty,
                   state,sent_at,details,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'SENDING',?,?,?,?)''',
                (event_id, ev["mode"], "account1", attempt, kind, last["id"] if last else None, pos["ticker"],
                 pos["exchange"], str(ref_price), ref_source, quote_as_of, str(limit_price), qty, qty,
                 now if clock is not None else None, details, now, now))
            inc = ", session_replace_count=session_replace_count+1, event_replace_count=event_replace_count+1" \
                if kind == "REPLACE" else ""
            db.execute(f"UPDATE auto_stop_exit_events SET next_order_kind='REPLACE'{inc}, updated_at=? WHERE id=?",
                       (now, event_id))
            return dict(db.execute("SELECT * FROM auto_stop_orders WHERE id=?", (cur.lastrowid,)).fetchone())

    def claim_cancel(self, now, *, owner, order_id, expect_state, guard, lease_sec=90, clock=None, gate=None,
                     evidence=None) -> dict:
        """재주문 목적 취소 선점. clock·gate 의미는 claim_sell 과 같다. 사전 검증 스냅샷이 최신 DB 상태와 다르면
        gate 가 거부한다 (상태가 같은 PARTIAL_FILLED 여도 누적 체결·잔량이 바뀌었으면 거부)."""
        with self.tx() as db:
            if clock is not None:
                now = clock()
            self._check_lease(db, owner, now)
            kill = self._kill(db)
            if kill:
                raise ClaimRefused(f"전체 긴급 중지 중: {kill.get('reason') or ''}")
            reason = guard()
            if reason:
                raise ClaimRefused(reason)
            if gate is not None:
                reason = gate(db, now)
                if reason:
                    raise ClaimRefused(reason)
                eff = getattr(gate, "settings", None)
                lease_sec = eff.lease_sec if eff is not None else lease_sec
            self._extend_lease(db, owner, now, lease_sec)
            o = db.execute("SELECT * FROM auto_stop_orders WHERE id=?", (order_id,)).fetchone()
            if o is None or o["state"] != expect_state or o["cancel_rejected"] or not o["order_number"]:
                raise ClaimRefused("취소 조건이 맞지 않습니다.")
            details = json.loads(o["details"])
            details["cancel_from_state"] = expect_state
            if evidence:
                details.setdefault("cancel_claim", []).append(evidence)
            db.execute("UPDATE auto_stop_orders SET state='CANCEL_SENDING', cancel_sent_at=?, details=?, updated_at=? "
                       "WHERE id=? AND state=?", (now, json.dumps(details, default=str), now, order_id, expect_state))
            return dict(db.execute("SELECT * FROM auto_stop_orders WHERE id=?", (order_id,)).fetchone())

    def stat_set(self, day, mode, metric, key, value):
        """일별 통계 한 행을 지정 값으로 쓴다 (감시 슬롯 최종 분류·성공 시각 등, 재기록 시 덮어씀)."""
        with self.tx() as db:
            db.execute("INSERT INTO auto_stop_daily_stats(day,mode,metric,key,value) VALUES(?,?,?,?,?) "
                       "ON CONFLICT(day,mode,metric,key) DO UPDATE SET value=excluded.value",
                       (day, mode, metric, key, value))

    def stat_min(self, day, mode, metric, key, value):
        """기존 값과 비교해 작은 값만 남긴다 (예: 상태 근거 1=일치·0=불일치 — 한 번이라도 불일치면 0 유지)."""
        with self.tx() as db:
            db.execute("INSERT INTO auto_stop_daily_stats(day,mode,metric,key,value) VALUES(?,?,?,?,?) "
                       "ON CONFLICT(day,mode,metric,key) DO UPDATE SET value=MIN(value, excluded.value)",
                       (day, mode, metric, key, value))

    def stat_set_if_absent(self, day, mode, metric, key, value) -> bool:
        """처음 한 번만 기록한다 (예: 그날 신규 슬롯 형식 기록 시작 시각)."""
        with self.tx() as db:
            return db.execute("INSERT OR IGNORE INTO auto_stop_daily_stats(day,mode,metric,key,value) "
                              "VALUES(?,?,?,?,?)", (day, mode, metric, key, value)).rowcount == 1

    def stat_set_many(self, rows):
        """여러 통계 행을 한 트랜잭션에서 쓴다. rows: [(day, mode, metric, key, value)]."""
        if not rows:
            return
        with self.tx() as db:
            db.executemany("INSERT INTO auto_stop_daily_stats(day,mode,metric,key,value) VALUES(?,?,?,?,?) "
                           "ON CONFLICT(day,mode,metric,key) DO UPDATE SET value=excluded.value", rows)

    def transition(self, oid, now, *, frm, to, **fields) -> bool:
        """조건부 상태 전이. frm 은 상태 하나 또는 튜플."""
        frm = (frm,) if isinstance(frm, str) else tuple(frm)
        extra = "".join(f", {k}=?" for k in fields)
        with self.tx() as db:
            return db.execute(f"UPDATE auto_stop_orders SET state=?{extra}, updated_at=? WHERE id=? AND state IN "
                              f"{_in(frm)}", [to, *fields.values(), now, oid]).rowcount == 1

    def add_order_evidence(self, oid, now, key, value):
        with self.tx() as db:
            r = db.execute("SELECT details FROM auto_stop_orders WHERE id=?", (oid,)).fetchone()
            details = json.loads(r["details"]) if r else {}
            details.setdefault(key, []).append(value)
            db.execute("UPDATE auto_stop_orders SET details=?, updated_at=? WHERE id=?",
                       (json.dumps(details, default=str), now, oid))

    def set_order_fields(self, oid, now, **fields):
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as db:
            db.execute(f"UPDATE auto_stop_orders SET {sets}, updated_at=? WHERE id=?", [*fields.values(), now, oid])

    def apply_fill(self, oid, now, cum_filled: int, evidence: dict) -> str:
        """증권사 누적 체결 수량을 증분으로만 반영한다 (중복 조회에도 1회 차감)."""
        with self.tx() as db:
            o = db.execute("SELECT * FROM auto_stop_orders WHERE id=?", (oid,)).fetchone()
            if o is None:
                return "MISSING"
            old = o["filled_qty"]
            if cum_filled == old:
                return "NOOP"
            if cum_filled < old or cum_filled > o["requested_qty"]:
                return "ANOMALY"
            delta = cum_filled - old
            ev = db.execute("SELECT position_id FROM auto_stop_exit_events WHERE id=?", (o["exit_event_id"],)).fetchone()
            try:
                db.execute("INSERT INTO auto_stop_fill_ledger(order_id,cum_filled_qty,applied_delta,applied_at,evidence) "
                           "VALUES(?,?,?,?,?)", (oid, cum_filled, delta, now, json.dumps(evidence, default=str)))
            except sqlite3.IntegrityError:
                return "NOOP"
            if db.execute("UPDATE auto_stop_orders SET filled_qty=?, remaining_order_qty=?, updated_at=? "
                          "WHERE id=? AND filled_qty=?",
                          (cum_filled, o["requested_qty"] - cum_filled, now, oid, old)).rowcount != 1:
                raise sqlite3.IntegrityError("체결 반영 경합")
            if db.execute("UPDATE auto_stop_positions SET remaining_managed_qty=remaining_managed_qty-?, "
                          "total_filled_qty=total_filled_qty+?, updated_at=? WHERE id=? AND remaining_managed_qty>=?",
                          (delta, delta, now, ev["position_id"], delta)).rowcount != 1:
                raise sqlite3.IntegrityError("관리 수량 음수")
            return "APPLIED"

    # ── alerts ──────────────────────────────────────────────────
    def enqueue_alert(self, now, key, severity, text) -> bool:
        with self.tx() as db:
            return db.execute("INSERT OR IGNORE INTO auto_stop_alerts(dedup_key,severity,text,created_at) "
                              "VALUES(?,?,?,?)", (key, severity, text, now)).rowcount == 1

    def pending_alerts(self, max_attempts=5):
        with self.tx() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM auto_stop_alerts WHERE state!='SENT' AND attempts<? ORDER BY id", (max_attempts,))]

    def mark_alert(self, aid, now, ok):
        with self.tx() as db:
            db.execute("UPDATE auto_stop_alerts SET state=?, attempts=attempts+1, sent_at=? WHERE id=?",
                       ("SENT" if ok else "FAILED", now if ok else None, aid))

    def fill_ledger(self, order_ids):
        """체결 원장 (증분 반영 이력, 읽기 전용). applied_at = 시스템이 체결을 확인·반영한 시각."""
        ids = list(order_ids)
        if not ids:
            return []
        with self.tx() as db:
            return [dict(r) for r in db.execute(
                f"SELECT order_id, cum_filled_qty, applied_delta, applied_at, evidence FROM auto_stop_fill_ledger "
                f"WHERE order_id IN ({','.join('?' * len(ids))}) ORDER BY applied_at, cum_filled_qty", ids)]

    def alerts_with_prefix(self, prefix):
        """dedup_key 접두어가 같은 알림 (읽기 전용, 생성 시각 순)."""
        with self.tx() as db:
            return [dict(r) for r in db.execute(
                "SELECT dedup_key, severity, created_at FROM auto_stop_alerts WHERE substr(dedup_key, 1, ?)=? "
                "ORDER BY created_at, id", (len(prefix), prefix))]

    def recent_alerts(self, limit=50):
        with self.tx() as db:
            return [dict(r) for r in db.execute("SELECT * FROM auto_stop_alerts ORDER BY id DESC LIMIT ?", (limit,))]

    # ── 외부 저널 대조 ──────────────────────────────────────────
    @staticmethod
    def scanner_buy_in_flight_db(db, ticker) -> bool:
        """현재 트랜잭션의 db 로 Scanner 미확정 매수를 확인한다 (새 연결·중첩 트랜잭션 없음).

        표가 없으면 False: Scanner 매수 모듈은 첫 사용 때 같은 저널에 표를 만들므로(CREATE TABLE IF NOT EXISTS),
        표가 없다는 것은 이 저널에서 Scanner 매수가 한 번도 준비되지 않았다는 뜻이다. 선점 트랜잭션은 쓰기 잠금을
        쥐고 있어 그 사이 다른 연결이 표를 만들거나 행을 넣을 수 없다. 표가 있는데 조회가 실패하면(열 누락 등)
        ScannerJournalUnreadable 로 올려 선점을 거부하게 한다."""
        # 1) 객체 존재·유형: main 스키마 메타데이터에서 SQLite 식별자 규칙(대소문자 무시)으로 찾는다.
        #    유형으로 미리 거르지 않는다 (VIEW·INDEX 등 동명 객체를 '표 없음'으로 오판하지 않게).
        try:
            objects = db.execute("SELECT type, name FROM main.sqlite_master "
                                 "WHERE name = 'scanner_buy_orders' COLLATE NOCASE").fetchall()
            temp = db.execute("SELECT type FROM temp.sqlite_master "
                              "WHERE name = 'scanner_buy_orders' COLLATE NOCASE").fetchall()
        except sqlite3.Error as exc:
            raise ScannerJournalUnreadable(f"메타데이터 조회 실패: {exc}") from exc
        types = sorted({r[0] for r in objects})
        if not types:
            if temp:
                # main 에는 없고 TEMP 에만 같은 이름이 있음: 정상 저널 구조가 아니다 (부재로 보지 않는다)
                raise ScannerJournalUnreadable("TEMP 스키마에만 scanner_buy_orders 가 있습니다")
            return False                    # 실제 부재: Scanner 매수가 이 저널에서 준비된 적 없음
        if types != ["table"]:
            raise ScannerJournalUnreadable(f"scanner_buy_orders 객체 유형이 표가 아닙니다: {types}")
        # 2) 미확정 주문: main 스키마를 명시해 TEMP 동명 객체가 가리지 못하게 한다. 열 누락·오류는 fail closed.
        try:
            return db.execute(SCANNER_IN_FLIGHT_SQL, (ticker,)).fetchone() is not None
        except sqlite3.Error as exc:
            raise ScannerJournalUnreadable(f"미확정 주문 조회 실패: {exc}") from exc

    def scanner_buy_in_flight(self, ticker) -> bool:
        """같은 SQLite 의 스캐너 매수 저널에 account1 같은 종목의 미확정·미체결 매수가 있는지."""
        with self.tx() as db:
            return self.scanner_buy_in_flight_db(db, ticker)


def _initial_details(f, now) -> dict:
    """ACTIVE 가 아닌 상태로 등록(예: LIVE 전환 시점에 이미 손절가 이하 → NEEDS_REVIEW)하면 일반 감시를 시작하지 않는다."""
    details = dict(f.get("details") or {})
    if f.get("state", "ACTIVE") != "ACTIVE":
        details.setdefault("watch_ended_at", now)
        details.setdefault("watch_end_reason", f"registered:{f['state']}")
    return details


def watch_end_of(position) -> float | None:
    """기록된 최초 영구 감시 종료 시각 (없으면 None)."""
    try:
        return json.loads(position["details"] or "{}").get("watch_ended_at")
    except (TypeError, ValueError):
        return None


def dec(value) -> Decimal:
    return Decimal(str(value))
