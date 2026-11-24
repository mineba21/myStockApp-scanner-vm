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


class ClaimRefused(Exception):
    """선점 트랜잭션이 조건 불충족으로 거부됨 (아무것도 전송하지 않음)."""


class LeaseLost(Exception):
    """lease 를 잃음. 이 작업자는 더 이상 주문·상태 변경을 하지 않는다."""


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
                     json.dumps(f.get("details", {}), default=str)))
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
        sql = f"UPDATE auto_stop_positions SET {sets} WHERE id=?"
        args = list(fields.values()) + [now, pid]
        if expect_states:
            sql += f" AND state IN {_in(expect_states)}"
        with self.tx() as db:
            return db.execute(sql, args).rowcount == 1

    # ── events ──────────────────────────────────────────────────
    def create_event(self, now, position, *, state, trigger_price, trigger_as_of, session_date) -> int | None:
        with self.tx() as db:
            ok = db.execute("UPDATE auto_stop_positions SET state='EXITING', updated_at=? "
                            "WHERE id=? AND state='ACTIVE' AND auto_paused=0", (now, position["id"])).rowcount
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
                db.execute("UPDATE auto_stop_positions SET state='NEEDS_REVIEW', review_reason=?, updated_at=? "
                           "WHERE id=? AND state IN ('ACTIVE','EXITING')", (reason, now, position_id))

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

    def claim_sell(self, now, *, owner, event_id, kind, qty, ref_price, ref_source, quote_as_of, limit_price,
                   settings, guard) -> dict:
        """한 트랜잭션에서 모든 조건을 확인하고 SENDING 행을 만든다. guard()는 실거래 스위치 재검증."""
        with self.tx() as db:
            self._check_lease(db, owner, now)
            kill = self._kill(db)
            if kill:
                raise ClaimRefused(f"전체 긴급 중지 중: {kill.get('reason') or ''}")
            reason = guard()
            if reason:
                raise ClaimRefused(reason)
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
            cur = db.execute(
                '''INSERT INTO auto_stop_orders(exit_event_id,mode,account_profile,attempt_no,kind,parent_order_id,
                   ticker,exchange,ref_price,ref_source,quote_as_of,limit_price,requested_qty,remaining_order_qty,
                   state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'SENDING',?,?)''',
                (event_id, ev["mode"], "account1", attempt, kind, last["id"] if last else None, pos["ticker"],
                 pos["exchange"], str(ref_price), ref_source, quote_as_of, str(limit_price), qty, qty, now, now))
            inc = ", session_replace_count=session_replace_count+1, event_replace_count=event_replace_count+1" \
                if kind == "REPLACE" else ""
            db.execute(f"UPDATE auto_stop_exit_events SET next_order_kind='REPLACE'{inc}, updated_at=? WHERE id=?",
                       (now, event_id))
            return dict(db.execute("SELECT * FROM auto_stop_orders WHERE id=?", (cur.lastrowid,)).fetchone())

    def claim_cancel(self, now, *, owner, order_id, expect_state, guard, lease_sec=90) -> dict:
        with self.tx() as db:
            self._check_lease(db, owner, now)
            kill = self._kill(db)
            if kill:
                raise ClaimRefused(f"전체 긴급 중지 중: {kill.get('reason') or ''}")
            reason = guard()
            if reason:
                raise ClaimRefused(reason)
            self._extend_lease(db, owner, now, lease_sec)
            o = db.execute("SELECT * FROM auto_stop_orders WHERE id=?", (order_id,)).fetchone()
            if o is None or o["state"] != expect_state or o["cancel_rejected"] or not o["order_number"]:
                raise ClaimRefused("취소 조건이 맞지 않습니다.")
            details = json.loads(o["details"])
            details["cancel_from_state"] = expect_state
            db.execute("UPDATE auto_stop_orders SET state='CANCEL_SENDING', cancel_sent_at=?, details=?, updated_at=? "
                       "WHERE id=? AND state=?", (now, json.dumps(details), now, order_id, expect_state))
            return dict(db.execute("SELECT * FROM auto_stop_orders WHERE id=?", (order_id,)).fetchone())

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

    def recent_alerts(self, limit=50):
        with self.tx() as db:
            return [dict(r) for r in db.execute("SELECT * FROM auto_stop_alerts ORDER BY id DESC LIMIT ?", (limit,))]

    # ── 외부 저널 대조 ──────────────────────────────────────────
    def scanner_buy_in_flight(self, ticker) -> bool:
        """같은 SQLite 의 스캐너 매수 저널에 account1 같은 종목의 미확정·미체결 매수가 있는지."""
        with self.tx() as db:
            exists = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='scanner_buy_orders'"
                                ).fetchone()
            if not exists:
                return False
            r = db.execute("SELECT 1 FROM scanner_buy_orders WHERE account_profile='account1' AND ticker=? AND "
                           "(state IN ('SENDING','UNKNOWN') OR (state='SUBMITTED' AND "
                           "COALESCE(broker_state,'') NOT IN ('FILLED','CANCELLED','REJECTED')))",
                           (ticker,)).fetchone()
            return r is not None


def dec(value) -> Decimal:
    return Decimal(str(value))
