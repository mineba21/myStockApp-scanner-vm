"""Codex 9차 리뷰(2026-11-28): 최종 주문 상태와 종료 증거의 원자적 저장, 상충하는 최종 상태 증거 검출, 개장 시각 종료
상한 경계. 실주문·실취소 없음 (모의 브로커, 임시 SQLite, 강제 종료는 별도 프로세스)."""
import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from decimal import Decimal

import pytest

from tests.auto_stop_fakes import ts
from tests.test_auto_stop_gate_matrix import Case
from tests.test_auto_stop_review4 import pos_of, sh, ticks
from tests.test_auto_stop_review7 import D24, day24_tick, fu, open_order_23, pos23, report
from tests.test_auto_stop_review8 import legacy_review, order_fu, row, unknown_23
from trading.auto_stop import service
from trading.auto_stop.store import UNRESOLVED, AutoStopStore, final_evidence
from trading.kiwoom_orders import OrderRejected
from trading.kiwoom_readonly import KiwoomError

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def details(h, oid):
    return json.loads(h.store.order(oid)["details"] or "{}")


class _Proxy:
    def __init__(self, db, hook):
        self._db, self._hook = db, hook

    def execute(self, sql, *a):
        cur = self._db.execute(sql, *a)
        if sql.lstrip().startswith("UPDATE auto_stop_orders SET state=") and self._hook(sql, a):
            raise RuntimeError("상태 저장 중 예외 (테스트)")
        return cur

    def __getattr__(self, name):
        return getattr(self._db, name)


def fail_on_state_update(monkeypatch, states):
    """상태 UPDATE 실행 직후·커밋 전에 예외 → 같은 트랜잭션 전체 롤백."""
    orig = AutoStopStore.tx

    @contextlib.contextmanager
    def tx(self):
        with orig(self) as db:
            yield _Proxy(db, lambda sql, a: a and a[0] and a[0][0] in states)
    monkeypatch.setattr(AutoStopStore, "tx", tx)
    return lambda: monkeypatch.setattr(AutoStopStore, "tx", orig)


def restart(h):
    h.store.release_lease("w1", h.clock())
    h.engine = h.new_engine("w2")


# ═════════════════════ STEP 1: 최종 상태·종료 증거 원자 저장 ═════════════════════
@pytest.mark.parametrize("broker_state,filled", [("REJECTED", 0), ("FILLED", 10), ("CANCELLED", 0)])
def test_reconcile_final_state_save_exception_rolls_back_both(tmp_path, monkeypatch, broker_state, filled):
    h = sh(tmp_path, monkeypatch)
    pid, o = open_order_23(h)
    sells = len(h.broker.sells)
    if broker_state == "FILLED":
        h.broker.fill(o["order_number"], 10)
    else:
        h.broker.rows[o["order_number"]].update(state=broker_state, remaining_quantity=0)
    undo = fail_on_state_update(monkeypatch, {broker_state})
    h.clock.set(ts(2026, 11, 24, 9, 31))
    with pytest.raises(RuntimeError):
        h.tick()
    undo()
    after = h.store.order(o["id"])
    assert after["state"] in UNRESOLVED and "final" not in details(h, o["id"])     # 상태·증거 함께 롤백
    restart(h)
    h.clock.set(ts(2026, 11, 24, 9, 32)); h.tick()                   # 재시작 후 읽기 전용 대조로 복원
    o2 = h.store.order(o["id"])
    fin = details(h, o["id"])["final"]
    assert o2["state"] == broker_state and [f["state"] for f in fin] == [broker_state]
    assert fin[0]["at"] == ts(2026, 11, 24, 9, 32) and fin[0]["source"] == "status"
    assert "claim" in details(h, o["id"])                            # 기존 details 보존
    if broker_state == "CANCELLED":
        # 전일 주문의 당일 취소 확정은 기존 정책상 장 마감 후 소멸 → 잔여는 이월 규칙으로 새 주문 (같은 주문 재전송 아님)
        assert h.store.order(o["id"])["order_number"] == o["order_number"]
        assert all(x["id"] == o["id"] or x["created_at"] >= ts(2026, 11, 24, 9, 32) for x in h.orders())
    else:
        assert len(h.broker.sells) == sells                           # 자동 재전송 없음
    assert "unresolved_at_day_start" in order_fu(report(h, D24, ts(2026, 11, 24, 12, 0)), pid)["reasons"]
    if broker_state == "FILLED":
        assert [r["applied_delta"] for r in h.store.fill_ledger([o["id"]])] == [10]   # 체결 이중 차감 없음
        assert h.pos(pid)["remaining_managed_qty"] == 0


@pytest.mark.parametrize("path", ["status_REJECTED", "status_FILLED", "status_CANCELLED", "send_REJECTED",
                                  "send_NOT_SENT"])
def test_codex_crash_between_state_commit_and_evidence(tmp_path, monkeypatch, path):
    """Codex 반례: 최종 상태 커밋 직후·종료 증거 저장 전 중단. 증거를 별도 트랜잭션으로 쓰는 경로가 있으면 실패한다."""
    kind, state = path.split("_", 1)

    def crash(*a, **k):
        raise RuntimeError("증거 저장 전 중단 (테스트)")
    if kind == "status":
        h = sh(tmp_path, monkeypatch)
        pid, o = open_order_23(h)
        if state == "FILLED":
            h.broker.fill(o["order_number"], 10)
        else:
            h.broker.rows[o["order_number"]].update(state=state, remaining_quantity=0)
        monkeypatch.setattr(AutoStopStore, "add_order_evidence", crash)
        h.clock.set(ts(2026, 11, 24, 9, 31))
        with contextlib.suppress(RuntimeError):
            h.tick()
        o2 = h.store.order(o["id"])
        assert o2["state"] == state and [f["state"] for f in details(h, o["id"]).get("final", [])] == [state]
        assert details(h, o["id"])["final"][0]["at"] == ts(2026, 11, 24, 9, 31)    # 실제 확인 시각 보존
        monkeypatch.undo()
        assert "unresolved_at_day_start" in order_fu(report(h, D24, ts(2026, 11, 24, 12, 0)), pid)["reasons"]
    else:
        c = Case(tmp_path, monkeypatch, "ORIGINAL", "LIVE")
        c.h.broker.sell_exc = OrderRejected("[1: 거부]") if state == "REJECTED" else KiwoomError("입력 오류")
        monkeypatch.setattr(AutoStopStore, "add_order_evidence", crash)
        with contextlib.suppress(RuntimeError):
            c.run()
        o = c.h.orders()[-1]
        d = details(c.h, o["id"])
        assert o["state"] == state and d.get("reject" if state == "REJECTED" else "not_sent")
        assert [f["state"] for f in d.get("final", [])] == [state]


@pytest.mark.parametrize("exc,state,key", [(OrderRejected("[1: 거부]"), "REJECTED", "reject"),
                                           (KiwoomError("입력 오류"), "NOT_SENT", "not_sent")])
def test_send_reject_and_not_sent_store_state_and_evidence_together(tmp_path, monkeypatch, exc, state, key):
    c = Case(tmp_path, monkeypatch, "ORIGINAL", "LIVE")
    c.h.broker.sell_exc = exc
    c.run()
    o = c.h.orders()[-1]
    d = details(c.h, o["id"])
    assert o["state"] == state and d[key] and [f["state"] for f in d["final"]] == [state] and "claim" in d


@pytest.mark.parametrize("exc,state", [(OrderRejected("[1: 거부]"), "REJECTED"), (KiwoomError("입력 오류"), "NOT_SENT")])
def test_send_final_save_exception_keeps_sending_then_unknown_without_resend(tmp_path, monkeypatch, exc, state):
    c = Case(tmp_path, monkeypatch, "ORIGINAL", "LIVE")
    h = c.h
    h.broker.sell_exc = exc
    undo = fail_on_state_update(monkeypatch, {state})
    with pytest.raises(RuntimeError):
        c.run()
    undo()
    o = h.orders()[-1]
    assert o["state"] == "SENDING" and "final" not in details(h, o["id"]) and key_absent(h, o["id"])
    calls = len(h.broker.sells)
    restart(h)
    h.clock.advance(30); c.run()                                     # 기존 정책: 이전 작업자 SENDING → UNKNOWN
    assert h.store.order(o["id"])["state"] == "UNKNOWN" and len(h.orders()) == 1 and len(h.broker.sells) == calls


def key_absent(h, oid):
    d = details(h, oid)
    return "reject" not in d and "not_sent" not in d


@pytest.mark.parametrize("broker_state,filled,target,has_final", [
    ("REJECTED", 0, "REJECTED", True), ("FILLED", 10, "FILLED", True), ("CANCELLED", 0, "CANCELLED", True),
    ("OPEN", 0, "SUBMITTED", False), ("PARTIAL", 4, "PARTIAL_FILLED", False)])
def test_resolve_unknown_final_evidence_atomic(tmp_path, monkeypatch, broker_state, filled, target, has_final):
    h = sh(tmp_path, monkeypatch)
    pid, o = unknown_23(h)
    row(h, broker_state, filled)
    at = ts(2026, 11, 24, 10, 0)
    service.resolve_unknown(h.store, h.broker, o["id"], order_number="5000", confirmation_ticker="CINF", now=at)
    o2, d = h.store.order(o["id"]), details(h, o["id"])
    assert o2["state"] == target
    if has_final:
        assert d["final"] == [{"state": target, "at": at, "source": "manual_resolve", "filled_quantity": filled}]
    else:
        assert "final" not in d                                      # 접수 확인·부분체결은 최종 종료 아님


def test_resolve_unknown_save_exception_keeps_unknown(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid, o = unknown_23(h)
    row(h, "REJECTED", 0)
    undo = fail_on_state_update(monkeypatch, {"REJECTED"})
    with pytest.raises(Exception):
        service.resolve_unknown(h.store, h.broker, o["id"], order_number="5000", confirmation_ticker="CINF",
                                now=ts(2026, 11, 24, 10, 0))
    undo()
    assert h.store.order(o["id"])["state"] == "UNKNOWN" and "final" not in details(h, o["id"])


def test_conditional_transition_failure_writes_no_evidence(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid, o = open_order_23(h)
    before = h.store.order(o["id"])
    ok = h.store.transition(o["id"], ts(2026, 11, 24, 10, 0), frm="SENDING", to="REJECTED",
                            evidence=[final_evidence("REJECTED", ts(2026, 11, 24, 10, 0), "status")])
    assert ok is False and h.store.order(o["id"]) == before


def test_same_evidence_not_duplicated(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid, o = open_order_23(h)
    ev = final_evidence("CANCELLED", ts(2026, 11, 24, 10, 0), "status")
    assert h.store.transition(o["id"], ev[1]["at"], frm=UNRESOLVED, to="CANCELLED", evidence=[ev, ev])
    assert not h.store.transition(o["id"], ev[1]["at"], frm=UNRESOLVED, to="CANCELLED", evidence=[ev])
    assert details(h, o["id"])["final"] == [ev[1]]


CHILD = textwrap.dedent("""
    import contextlib, os, sys
    sys.path.insert(0, {repo!r})
    from trading.auto_stop.store import UNRESOLVED, AutoStopStore, final_evidence
    mode = sys.argv[1]
    store = AutoStopStore({path!r})
    if mode == "inside":
        orig = AutoStopStore.tx
        class P:
            def __init__(s, db): s.db = db
            def execute(s, sql, *a):
                cur = s.db.execute(sql, *a)
                if sql.lstrip().startswith("UPDATE auto_stop_orders SET state="):
                    os._exit(9)                        # 상태 UPDATE 후, COMMIT 전 강제 종료
                return cur
            def __getattr__(s, n): return getattr(s.db, n)
        @contextlib.contextmanager
        def tx(self):
            with orig(self) as db:
                yield P(db)
        AutoStopStore.tx = tx
    at = {at!r}
    store.transition({oid!r}, at, frm=UNRESOLVED, to="REJECTED", broker_state="REJECTED",
                     evidence=[final_evidence("REJECTED", at, "status", 0)])
    os._exit(9)                                        # COMMIT 직후 강제 종료
""")


@pytest.mark.parametrize("mode", ["inside", "after_commit"])
def test_process_killed_during_or_after_commit(tmp_path, monkeypatch, mode):
    h = sh(tmp_path, monkeypatch)
    pid, o = open_order_23(h)
    h.broker.rows[o["order_number"]].update(state="REJECTED", remaining_quantity=0)
    at = ts(2026, 11, 24, 9, 31)
    script = CHILD.format(repo=REPO, path=h.st.journal_path, oid=o["id"], at=at)
    proc = subprocess.run([sys.executable, "-c", script, mode], cwd=str(tmp_path), capture_output=True, timeout=60)
    assert proc.returncode == 9, proc.stderr.decode()[-500:]
    o2, d = h.store.order(o["id"]), details(h, o["id"])
    if mode == "inside":
        assert o2["state"] == "SUBMITTED" and "final" not in d       # 롤백: 미확정 유지
        restart(h)
        h.clock.set(ts(2026, 11, 24, 9, 32)); h.tick()               # 재시작 후 읽기 전용 대조
        o2, d = h.store.order(o["id"]), details(h, o["id"])
        assert o2["state"] == "REJECTED" and d["final"][0]["at"] == ts(2026, 11, 24, 9, 32)
    else:
        assert o2["state"] == "REJECTED" and d["final"] == [{"state": "REJECTED", "at": at, "source": "status",
                                                             "filled_quantity": 0}]
    assert len(h.orders()) == 1                                       # 재전송 없음
    assert "unresolved_at_day_start" in order_fu(report(h, D24, ts(2026, 11, 24, 12, 0)), pid)["reasons"]


def test_repeated_reconcile_does_not_double_count_fills(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid, o = open_order_23(h)
    h.broker.fill(o["order_number"], 10)
    for k in range(4):
        day24_tick(h, ts(2026, 11, 24, 9, 31 + k))
    assert [r["applied_delta"] for r in h.store.fill_ledger([o["id"]])] == [10]
    assert len(details(h, o["id"])["final"]) == 1 and h.pos(pid)["remaining_managed_qty"] == 0


# ═════════════════════ STEP 2: 상충하는 최종 상태 증거 ═════════════════════
D24_10, D24_11 = ts(2026, 11, 24, 10, 0), ts(2026, 11, 24, 11, 0)


def finalize(h, o, state, at=D24_11):
    """테스트 데이터 준비: 최종 상태 + 그 상태의 종료 기록 (보고서 판정 대상, 저장 경로와 무관)."""
    assert h.store.transition(o["id"], at, frm=UNRESOLVED, to=state)
    h.store.add_order_evidence(o["id"], at, "final", {"state": state, "at": at, "source": "status"})


def reasons(h, pid):
    r = order_fu(report(h, D24, ts(2026, 11, 24, 12, 0)), pid)
    return r["reasons"] if r else None


@pytest.mark.parametrize("setup,final_state", [
    ("cancelled_alert", "REJECTED"), ("expired_alert", "FILLED"), ("full_ledger", "CANCELLED"),
    ("reject_evidence", "FILLED"), ("not_sent_evidence", "CANCELLED")])
def test_conflicting_final_evidence(tmp_path, monkeypatch, setup, final_state):
    h = sh(tmp_path, monkeypatch)
    pid, o = open_order_23(h)
    oid = o["id"]
    if setup == "cancelled_alert":
        h.store.enqueue_alert(D24_10, f"order:{oid}:cancelled", "INFO", "취소 확정")
    elif setup == "expired_alert":
        h.store.enqueue_alert(D24_10, f"order:{oid}:expired", "WARN", "소멸")
    elif setup == "full_ledger":
        h.store.apply_fill(oid, D24_10, 10, {"source": "test"})
    elif setup == "reject_evidence":
        h.store.add_order_evidence(oid, D24_10, "reject", "거부")
    else:
        h.store.add_order_evidence(oid, D24_10, "not_sent", "미전송")
    finalize(h, o, final_state)
    rs = reasons(h, pid)
    assert "final_evidence_conflict" in rs and "unresolved_at_day_start" not in rs
    assert details(h, oid)["final"][0]["state"] == final_state       # 원본 증거 보존


def test_consistent_multiple_evidence_keeps_earliest(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid, o = open_order_23(h)
    h.store.enqueue_alert(D24_10, f"order:{o['id']}:cancelled", "INFO", "취소 확정")
    h.store.audit(D24_11, "web", "resolve_unknown", o["id"], {"broker_state": "CANCELLED"})
    finalize(h, o, "CANCELLED")
    rs = reasons(h, pid)
    assert "unresolved_at_day_start" in rs and "final_evidence_conflict" not in rs


@pytest.mark.parametrize("noise", ["cancel_requests", "acceptance_audit", "other_order_alert"])
def test_non_final_or_foreign_records_are_not_conflicts(tmp_path, monkeypatch, noise):
    h = sh(tmp_path, monkeypatch)
    pid, o = open_order_23(h)
    if noise == "cancel_requests":
        for k in ("cancel_requested", "cancel_held:1", "stale"):
            h.store.enqueue_alert(D24_10, f"order:{o['id']}:{k}", "WARN", k)
    elif noise == "acceptance_audit":
        h.store.audit(D24_10, "web", "resolve_unknown", o["id"], {"broker_state": "OPEN"})
    else:
        h.store.enqueue_alert(D24_10, f"order:{o['id'] + 100}:cancelled", "INFO", "다른 주문")
    finalize(h, o, "REJECTED")
    rs = reasons(h, pid)
    assert "final_evidence_conflict" not in rs and "unresolved_at_day_start" in rs


# ═════════════════════ STEP 3: 개장 시각 종료 상한 경계 ═════════════════════
OPEN24 = ts(2026, 11, 24, 9, 30)


@pytest.mark.parametrize("bound,watched", [(OPEN24 - 60, 0), (OPEN24, 0), (OPEN24 + 60, 1)])
def test_ended_by_boundary_at_open(tmp_path, monkeypatch, bound, watched):
    h = sh(tmp_path, monkeypatch)
    pid = legacy_review(h, updated=bound)                            # 구버전 NEEDS_REVIEW, 마지막 수정 = bound
    service.disable(h.store, pid, now=ts(2026, 11, 24, 11, 0))       # 당일 관리 해제 (상한 보존)
    views = {report(h, D24, now)["watched_positions"] for now in (ts(2026, 11, 24, 12, 0), ts(2026, 11, 30, 9, 0))}
    assert views == {watched}                                        # 과거 거래일 재조회도 같음
    if watched:
        p = pos_of(report(h, D24, ts(2026, 11, 24, 12, 0)), pid)
        assert p["expected_checks"] is None                          # 상한만 있음: 측정 불가 (구간을 만들지 않음)


def test_real_trigger_at_open_still_included(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = pos23(h)
    h.broker.price = Decimal("44")
    h.broker.quote_age = 0
    h.clock.set(OPEN24); h.tick()
    assert h.event(pid)["triggered_at"] == OPEN24
    rep = report(h, D24, ts(2026, 11, 24, 12, 0))
    assert rep["watched_positions"] == 1 and pos_of(rep, pid)["expected_checks"] == 0
