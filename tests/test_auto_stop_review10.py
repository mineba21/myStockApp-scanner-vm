"""Codex 10차 리뷰(2026-11-28): 사람 확인이 필요한 주문 상태 전이의 원자성.

수동 UNKNOWN/CANCEL_UNKNOWN 해제와 엔진의 사람 확인 경로(외부 취소·증권사 거부·전송 거부·확정 미전송)에서 주문 최종
상태와 NEEDS_REVIEW(신규 주문 차단)가 함께 커밋되거나 함께 롤백되는지, 이전 버전의 부분 저장 상태가 재시작 시 자동
이월·재주문 전에 차단되는지 검증한다. 실주문·실취소 없음 (모의 브로커, 임시 SQLite, 강제 종료는 별도 프로세스)."""
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
from tests.test_auto_stop_review4 import sh
from tests.test_auto_stop_review7 import open_order_23, pos23
from tests.test_auto_stop_review8 import resolve_at, row, unknown_23
from trading.auto_stop import service
from trading.auto_stop.store import TERMINAL, UNRESOLVED, AutoStopStore, ClaimRefused, final_evidence
from trading.kiwoom_orders import OrderRejected, OrderUnknown
from trading.kiwoom_readonly import KiwoomError

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
R_IN, R_AFTER = ts(2026, 11, 23, 11, 0), ts(2026, 11, 23, 16, 30)
SAME_DAY = {R_IN: [ts(2026, 11, 23, 11, 1), ts(2026, 11, 23, 11, 10)], R_AFTER: [ts(2026, 11, 23, 16, 31)]}
NEXT_DAY = [ts(2026, 11, 24, 9, 31), ts(2026, 11, 24, 9, 40)]
PARTIAL_COMBOS = {(s, e) for s in TERMINAL for e in ("OPEN", "CARRY_OVER")}


def crash_after(monkeypatch, attr):
    """공개 저장 메서드 하나를 예외로 바꾼다 (그 앞 단계가 별도 트랜잭션이면 이미 커밋된 상태로 중단된다)."""
    def boom(*a, **k):
        raise RuntimeError(f"{attr} 직전 중단 (테스트)")
    monkeypatch.setattr(AutoStopStore, attr, boom)


def restart(h):
    h.store.release_lease("w1", h.clock())
    h.store.release_lease("w2", h.clock())
    h.engine = h.new_engine("w2")


def after_ticks(h, times):
    """재주문 위험을 드러내도록 SHADOW 재주문을 켠 채 tick. 반환: (매도 호출 증분, 취소 호출 증분)."""
    sells, cancels = len(h.broker.sells), len(h.broker.cancels)
    h.broker.price = Decimal("40")
    for t in times:
        h.set(shadow_replace_enabled=True)
        h.clock.set(t)
        h.tick()
    return len(h.broker.sells) - sells, len(h.broker.cancels) - cancels


def state_pair(h, oid, pid):
    return h.store.order(oid)["state"], h.event(pid)["state"]


def audits(h, action="resolve_unknown"):
    return h.store.audits(action, 0, ts(2027, 1, 1, 0, 0))


# ═════════════════════ STEP 1: Codex P1 반례 ═════════════════════
@pytest.mark.parametrize("at", [R_IN, R_AFTER])
def test_codex_resolve_cancelled_crash_before_review_no_carry_over(tmp_path, monkeypatch, at):
    """전날 UNKNOWN → 운영자가 CANCELLED 수동 확정 → 상태·증거 커밋 후 NEEDS_REVIEW 저장 전 중단 → 재시작."""
    h = sh(tmp_path, monkeypatch)
    pid, o = unknown_23(h)
    row(h, "CANCELLED", 0)
    crash_after(monkeypatch, "review")
    with contextlib.suppress(Exception):
        resolve_at(h, o, at)
    monkeypatch.undo()
    # 원자 처리: 전부 롤백(UNKNOWN) 또는 CANCELLED+NEEDS_REVIEW. CANCELLED+OPEN/CARRY_OVER 부분 저장 금지
    assert state_pair(h, o["id"], pid) in {("UNKNOWN", "OPEN"), ("UNKNOWN", "CARRY_OVER"),
                                            ("CANCELLED", "NEEDS_REVIEW")}
    restart(h)
    assert after_ticks(h, SAME_DAY[at] + NEXT_DAY) == (0, 0)


@pytest.mark.parametrize("at", [R_IN, R_AFTER])
def test_codex_resolve_cancelled_normal_control(tmp_path, monkeypatch, at):
    h = sh(tmp_path, monkeypatch)
    pid, o = unknown_23(h)
    row(h, "CANCELLED", 0)
    resolve_at(h, o, at)
    assert state_pair(h, o["id"], pid) == ("CANCELLED", "NEEDS_REVIEW")
    assert after_ticks(h, SAME_DAY[at] + NEXT_DAY) == (0, 0)
    assert h.event(pid)["state"] == "NEEDS_REVIEW" and len(audits(h)) == 1


# ═════════════════════ STEP 4: 엔진 사람 확인 경로 ═════════════════════
def engine_path(h, path):
    """사람 확인이 필요한 최종 상태를 만드는 tick 직전 상태를 준비하고 (그 tick 시각, 대상 주문 id 조회 함수)를 돌려준다."""
    if path in ("external_cancel", "status_rejected"):
        pid, o = open_order_23(h)
        h.broker.rows[o["order_number"]].update(
            state="CANCELLED" if path == "external_cancel" else "REJECTED", remaining_quantity=0)
        return pid, ts(2026, 11, 23, 11, 1)
    pid = pos23(h)
    h.set(shadow_replace_enabled=False)
    h.broker.price = Decimal("44")
    h.broker.sell_exc = OrderRejected("거부 (테스트)") if path == "send_rejected" else KiwoomError("입력 오류 (테스트)")
    return pid, h.clock()


@pytest.mark.parametrize("path", ["external_cancel", "status_rejected", "send_rejected", "send_not_sent"])
def test_engine_review_path_crash_before_review(tmp_path, monkeypatch, path):
    h = sh(tmp_path, monkeypatch)
    pid, at = engine_path(h, path)
    crash_after(monkeypatch, "review")
    h.clock.set(at)
    with contextlib.suppress(Exception):
        h.tick()
    monkeypatch.undo()
    h.broker.sell_exc = None
    o = h.orders()[-1]
    assert state_pair(h, o["id"], pid) not in PARTIAL_COMBOS              # 최종 상태 + 신규 주문 허용 이벤트 금지
    restart(h)
    assert after_ticks(h, [at + 60, at + 600] + NEXT_DAY) == (0, 0)
    assert h.store.order(o["id"])["order_number"] == o["order_number"]    # 원래 주문 재전송 없음


# ═════════════════════ STORE: 복합 트랜잭션 ═════════════════════
def _fail(monkeypatch, attr):
    def boom(*a, **k):
        raise RuntimeError(f"{attr} 중 예외 (테스트)")
    monkeypatch.setattr(AutoStopStore, attr, staticmethod(boom) if attr.endswith("_db") else boom)


def order_details(h, oid):
    return json.loads(h.store.order(oid)["details"] or "{}")


def _review_args(h, pid, o):
    ev = h.event(pid)
    now = ts(2026, 11, 23, 11, 1)
    return dict(oid=o["id"], now=now, frm=UNRESOLVED, to="CANCELLED", reason="외부 취소 (테스트)", event_id=ev["id"],
                position_id=pid, evidence=[final_evidence("CANCELLED", now, "status", 0)],
                alert=(f"event:{ev['id']}:review", "URGENT", "확인 필요 (테스트)"), broker_state="CANCELLED")


def test_transition_review_commits_together(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid, o = open_order_23(h)
    a = _review_args(h, pid, o)
    assert h.store.transition_review(a.pop("oid"), a.pop("now"), **a) is True
    assert state_pair(h, o["id"], pid) == ("CANCELLED", "NEEDS_REVIEW")
    assert h.pos(pid)["state"] == "NEEDS_REVIEW" and h.pos(pid)["review_reason"] == "외부 취소 (테스트)"
    assert [f["state"] for f in order_details(h, o["id"])["final"]] == ["CANCELLED"]
    assert f"event:{h.event(pid)['id']}:review" in h.alert_keys()


@pytest.mark.parametrize("attr", ["_review_db", "_enqueue_alert_db"])
def test_transition_review_rolls_back_together(tmp_path, monkeypatch, attr):
    h = sh(tmp_path, monkeypatch)
    pid, o = open_order_23(h)
    a = _review_args(h, pid, o)
    _fail(monkeypatch, attr)
    with pytest.raises(RuntimeError):
        h.store.transition_review(a.pop("oid"), a.pop("now"), **a)
    monkeypatch.undo()
    assert state_pair(h, o["id"], pid) == ("SUBMITTED", "OPEN") and h.pos(pid)["state"] == "EXITING"
    assert "final" not in order_details(h, o["id"])
    assert f"event:{h.event(pid)['id']}:review" not in h.alert_keys()


def _resolve_kwargs(h, o, snap, *, cum=4, state="CANCELLED", at=R_IN):
    return dict(expect=snap, cum_filled=cum, fill_evidence={"source": "manual_resolve"}, to=state,
                fields={"broker_state": state, "order_number": "5000", "order_date": "2026-11-23"},
                evidence=[final_evidence(state, at, "manual_resolve", cum)], review_reason="수동 해제 (테스트)",
                actor="test", audit_details=lambda ev, pos: {"event_state": ev["state"],
                                                              "remaining": pos["remaining_managed_qty"]})


def test_resolve_txn_commits_all(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid, o = unknown_23(h)
    snap = h.store.resolve_snapshot(o["id"])
    out = h.store.resolve_unknown_txn(o["id"], R_IN, **_resolve_kwargs(h, o, snap))
    assert out["state"] == "CANCELLED" and out["order_number"] == "5000" and out["filled_qty"] == 4
    assert [r["applied_delta"] for r in h.store.fill_ledger([o["id"]])] == [4]
    assert state_pair(h, o["id"], pid) == ("CANCELLED", "NEEDS_REVIEW")
    assert h.pos(pid)["state"] == "EXITING" and h.pos(pid)["remaining_managed_qty"] == 6   # 포지션 상태는 그대로
    assert [a["details"] for a in audits(h)] == [{"event_state": "NEEDS_REVIEW", "remaining": 6}]


@pytest.mark.parametrize("change", ["order.filled_qty", "order.state", "event.state", "position.remaining_managed_qty"])
def test_resolve_txn_snapshot_mismatch_writes_nothing(tmp_path, monkeypatch, change):
    h = sh(tmp_path, monkeypatch)
    pid, o = unknown_23(h)
    snap = json.loads(json.dumps(h.store.resolve_snapshot(o["id"])))
    part, key = change.split(".")
    snap[part][key] = {"order.state": "CANCEL_UNKNOWN", "event.state": "CARRY_OVER"}.get(change, 1)
    with pytest.raises(ClaimRefused, match="다시 대조"):
        h.store.resolve_unknown_txn(o["id"], R_IN, **_resolve_kwargs(h, o, snap))
    assert h.store.order(o["id"])["state"] == "UNKNOWN" and h.store.fill_ledger([o["id"]]) == []
    assert h.event(pid)["state"] == "OPEN" and h.pos(pid)["remaining_managed_qty"] == 10 and audits(h) == []


@pytest.mark.parametrize("attr", ["_review_db", "_audit_db"])
def test_resolve_txn_late_failure_rolls_back_fill_and_state(tmp_path, monkeypatch, attr):
    h = sh(tmp_path, monkeypatch)
    pid, o = unknown_23(h)
    snap = h.store.resolve_snapshot(o["id"])
    _fail(monkeypatch, attr)
    with pytest.raises(RuntimeError):
        h.store.resolve_unknown_txn(o["id"], R_IN, **_resolve_kwargs(h, o, snap))
    monkeypatch.undo()
    o2 = h.store.order(o["id"])
    assert o2["state"] == "UNKNOWN" and o2["filled_qty"] == 0 and not o2["order_number"]
    assert h.store.fill_ledger([o["id"]]) == [] and h.pos(pid)["remaining_managed_qty"] == 10
    assert h.event(pid)["state"] == "OPEN" and audits(h) == [] and "final" not in order_details(h, o["id"])


def test_public_store_wrappers_unchanged(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid, o = open_order_23(h)
    now = ts(2026, 11, 23, 11, 1)
    assert h.store.apply_fill(o["id"], now, 0, {}) == "NOOP"
    assert h.store.apply_fill(o["id"], now, 11, {}) == "ANOMALY"
    assert h.store.apply_fill(o["id"], now, 3, {}) == "APPLIED"
    assert h.store.apply_fill(o["id"], now, 2, {}) == "ANOMALY"
    assert h.store.transition(o["id"], now, frm="UNKNOWN", to="CANCELLED") is False
    assert h.store.transition(o["id"], now, frm=UNRESOLVED, to="PARTIAL_FILLED") is True
    assert h.store.enqueue_alert(now, "k1", "INFO", "t") is True and h.store.enqueue_alert(now, "k1", "INFO", "t") is False


# ═════════════════════ STEP 2·3: resolve_unknown 단일 트랜잭션·결과별 정책 ═════════════════════
def cancel_unknown_23(h):
    """접수된 전날 주문의 취소 요청 결과 불명 (CANCEL_UNKNOWN, 주문번호 있음). 테스트 임시 저널에서만 전이로 만든다."""
    pid, o = open_order_23(h)
    assert h.store.transition(o["id"], h.clock(), frm="SUBMITTED", to="CANCEL_UNKNOWN")
    return pid, h.store.order(o["id"])


def resolve_any(h, o, at, broker=None):
    number = o["order_number"] or "5000"
    return service.resolve_unknown(h.store, broker or h.broker, o["id"], order_number=number,
                                   confirmation_ticker="CINF", now=at)


def evidence_row(h, o, state, filled):
    row(h, state, filled, number=o["order_number"] or "5000")


MATRIX = {  # broker 상태, 누적 체결 → 주문 상태, 최종 증거 여부
    "OPEN": (0, "SUBMITTED", False), "PARTIAL": (4, "PARTIAL_FILLED", False), "FILLED": (10, "FILLED", True),
    "CANCELLED": (4, "CANCELLED", True), "REJECTED": (0, "REJECTED", True)}


@pytest.mark.parametrize("start", ["UNKNOWN", "CANCEL_UNKNOWN"])
@pytest.mark.parametrize("broker_state", list(MATRIX))
def test_resolve_matrix(tmp_path, monkeypatch, start, broker_state):
    h = sh(tmp_path, monkeypatch)
    pid, o = unknown_23(h) if start == "UNKNOWN" else cancel_unknown_23(h)
    filled, target, final = MATRIX[broker_state]
    ev0 = h.event(pid)
    evidence_row(h, o, broker_state, filled)
    out = resolve_any(h, o, R_IN)
    assert out["state"] == target
    d = order_details(h, o["id"])
    assert ([f["source"] for f in d.get("final", [])] == ["manual_resolve"]) is final     # 접수·부분체결은 최종 아님
    assert h.event(pid)["state"] == "NEEDS_REVIEW" and h.pos(pid)["state"] == "EXITING"   # 포지션은 바꾸지 않음
    assert [r["applied_delta"] for r in h.store.fill_ledger([o["id"]])] == ([filled] if filled else [])
    assert len(audits(h)) == 1
    n_orders = len(h.orders())
    if broker_state == "FILLED":
        assert after_ticks(h, SAME_DAY[R_IN][:1]) == (0, 0)
        assert h.pos(pid)["state"] == "SOLD" and h.event(pid)["state"] == "DONE"          # 기존 SOLD/DONE 정책
        assert h.pos(pid)["remaining_managed_qty"] == 0
    else:
        assert after_ticks(h, SAME_DAY[R_IN] + NEXT_DAY) == (0, 0)                         # 신규·재·이월 매도, 자동 취소 없음
        assert h.event(pid)["state"] == "NEEDS_REVIEW"                                     # 자동 OPEN/CARRY_OVER 전환 없음
        assert h.pos(pid)["remaining_managed_qty"] == 10 - filled
    ev = h.event(pid)
    assert (ev["triggered_at"], ev["trigger_price"], ev["stop_price"]) == (ev0["triggered_at"], ev0["trigger_price"],
                                                                            ev0["stop_price"])   # 트리거 보존
    assert len(h.orders()) == n_orders
    if broker_state in ("OPEN", "PARTIAL"):
        h.broker.fill(o["order_number"] or "5000", 7)                                       # 기존 주문 체결 대조는 계속
        h.clock.advance(60); h.tick()
        assert sum(r["applied_delta"] for r in h.store.fill_ledger([o["id"]])) == 7
        assert h.pos(pid)["remaining_managed_qty"] == 3 and h.event(pid)["state"] == "NEEDS_REVIEW"


def test_duplicate_order_number_rolls_back_fill(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid, o = unknown_23(h)
    with sqlite3.connect(h.st.journal_path) as db:          # 테스트 임시 저널: 같은 주문번호의 다른 주문 기록
        db.execute("INSERT INTO auto_stop_orders(exit_event_id,mode,account_profile,attempt_no,kind,ticker,exchange,"
                   "ref_price,ref_source,limit_price,requested_qty,remaining_order_qty,order_number,order_date,state,"
                   "created_at,updated_at) SELECT exit_event_id,mode,account_profile,99,kind,ticker,exchange,ref_price,"
                   "ref_source,limit_price,requested_qty,0,'5000','2026-11-23','CANCELLED',created_at,updated_at "
                   "FROM auto_stop_orders WHERE id=?", (o["id"],))
    row(h, "CANCELLED", 4)
    with pytest.raises(service.Refused, match="주문번호가 다른"):
        resolve_at(h, o, R_IN)
    o2 = h.store.order(o["id"])
    assert o2["state"] == "UNKNOWN" and o2["filled_qty"] == 0 and h.store.fill_ledger([o["id"]]) == []
    assert h.pos(pid)["remaining_managed_qty"] == 10 and h.event(pid)["state"] == "OPEN" and audits(h) == []


@pytest.mark.parametrize("case", ["over_requested", "below_recorded"])
def test_anomaly_fill_refuses_without_writes(tmp_path, monkeypatch, case):
    h = sh(tmp_path, monkeypatch)
    pid, o = unknown_23(h)
    if case == "below_recorded":
        assert h.store.apply_fill(o["id"], h.clock(), 4, {"source": "status"}) == "APPLIED"
        row(h, "CANCELLED", 2)
    else:
        row(h, "FILLED", 11)
    ledger = h.store.fill_ledger([o["id"]])
    with pytest.raises(service.Refused, match="체결 수량"):
        resolve_at(h, o, R_IN)
    assert h.store.order(o["id"])["state"] == "UNKNOWN" and h.store.fill_ledger([o["id"]]) == ledger
    assert h.event(pid)["state"] == "OPEN" and audits(h) == []


def test_linkage_mismatch_refused_in_lock(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid, o = unknown_23(h)
    row(h, "CANCELLED", 4)
    with sqlite3.connect(h.st.journal_path) as db:          # 손상된 연결: 포지션 티커가 주문과 다름
        db.execute("UPDATE auto_stop_positions SET ticker='OTHER' WHERE id=?", (pid,))
    with pytest.raises(service.Refused, match="연결이 맞지 않습니다"):
        resolve_at(h, o, R_IN)
    assert h.store.order(o["id"])["state"] == "UNKNOWN" and h.store.fill_ledger([o["id"]]) == [] and audits(h) == []


def test_wait_record_cleanup_failure_keeps_block(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid, o = unknown_23(h)
    row(h, "CANCELLED", 0)

    def boom(*a, **k):
        raise RuntimeError("대기 기록 정리 실패 (테스트)")
    monkeypatch.setattr(service, "reconcile_wait_records", boom)
    assert resolve_at(h, o, R_IN)["state"] == "CANCELLED"
    monkeypatch.undo()
    assert h.event(pid)["state"] == "NEEDS_REVIEW" and len(audits(h)) == 1
    restart(h)
    assert after_ticks(h, SAME_DAY[R_IN] + NEXT_DAY) == (0, 0)


# ═════════════════════ STEP 6: 동시 실행 (사전 조회와 쓰기 잠금 사이) ═════════════════════
class Hooked:
    """증권사 조회 응답 직전에 한 번 다른 작업을 끼워 넣는다 (사전 조회 → 경쟁 작업 → 쓰기 잠금)."""
    def __init__(self, broker, hook):
        self.broker, self.hook = broker, hook

    def status(self, order):
        hook, self.hook = self.hook, None
        if hook:
            hook()
        return self.broker.status(order)


def test_race_engine_reconcile_during_resolve_no_number(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid, o = unknown_23(h)
    row(h, "CANCELLED", 4)
    hook = lambda: (h.clock.set(R_IN), h.tick())                  # noqa: E731  번호 없는 UNKNOWN: 엔진은 찾지 못함
    assert resolve_any(h, o, R_IN, Hooked(h.broker, hook))["state"] == "CANCELLED"
    assert [r["applied_delta"] for r in h.store.fill_ledger([o["id"]])] == [4] and len(audits(h)) == 1
    assert h.event(pid)["state"] == "NEEDS_REVIEW"


def test_race_engine_reconcile_changes_state(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid, o = unknown_23(h)
    h.store.set_order_fields(o["id"], h.clock(), order_number="5000", order_date="2026-11-23")
    o = h.store.order(o["id"])
    row(h, "CANCELLED", 4)
    hook = lambda: (h.clock.set(R_IN), h.tick())                  # noqa: E731  엔진이 같은 주문을 먼저 대조·확정
    with pytest.raises(service.Refused):
        resolve_any(h, o, R_IN, Hooked(h.broker, hook))
    assert [r["applied_delta"] for r in h.store.fill_ledger([o["id"]])] == [4]          # 이중 반영 없음
    assert h.store.order(o["id"])["state"] == "CANCELLED" and audits(h) == []
    assert h.event(pid)["state"] == "NEEDS_REVIEW"                                      # 장중 외부 취소 정책


def test_race_double_resolve(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid, o = unknown_23(h)
    row(h, "CANCELLED", 4)
    with pytest.raises(service.Refused):
        resolve_any(h, o, R_IN, Hooked(h.broker, lambda: resolve_at(h, o, R_IN)))
    assert [r["applied_delta"] for r in h.store.fill_ledger([o["id"]])] == [4] and len(audits(h)) == 1
    assert len(order_details(h, o["id"])["final"]) == 1 and h.event(pid)["state"] == "NEEDS_REVIEW"


def test_race_fill_after_precheck(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid, o = unknown_23(h)
    row(h, "FILLED", 10)
    hook = lambda: h.store.apply_fill(o["id"], R_IN, 4, {"source": "status"})       # noqa: E731
    with pytest.raises(service.Refused, match="다시 대조"):
        resolve_any(h, o, R_IN, Hooked(h.broker, hook))
    assert [r["applied_delta"] for r in h.store.fill_ledger([o["id"]])] == [4] and audits(h) == []
    assert resolve_at(h, o, R_IN + 60)["state"] == "FILLED"                            # 다시 대조
    assert [r["applied_delta"] for r in h.store.fill_ledger([o["id"]])] == [4, 6]
    assert h.pos(pid)["remaining_managed_qty"] == 0 and h.pos(pid)["total_filled_qty"] == 10


def test_race_state_change_after_precheck(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid, o = unknown_23(h)
    row(h, "CANCELLED", 0)
    hook = lambda: h.store.transition(o["id"], R_IN, frm="UNKNOWN", to="SUBMITTED", order_number="5000",  # noqa: E731
                                      order_date="2026-11-23")
    with pytest.raises(service.Refused):
        resolve_any(h, o, R_IN, Hooked(h.broker, hook))
    assert h.store.order(o["id"])["state"] == "SUBMITTED" and "final" not in order_details(h, o["id"])
    assert audits(h) == [] and h.event(pid)["state"] == "OPEN"


def test_race_resolve_vs_next_day_carry_tick(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid, o = unknown_23(h)
    row(h, "CANCELLED", 0)
    hook = lambda: (h.clock.set(NEXT_DAY[0]), h.tick())           # noqa: E731  다음 거래일 이월 tick 이 먼저 실행
    resolve_any(h, o, NEXT_DAY[0], Hooked(h.broker, hook))
    assert state_pair(h, o["id"], pid) == ("CANCELLED", "NEEDS_REVIEW")
    assert after_ticks(h, NEXT_DAY[1:] + [ts(2026, 11, 25, 9, 31)]) == (0, 0)
    assert len(h.orders()) == 1


def test_resolve_then_next_day_tick(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid, o = unknown_23(h)
    row(h, "CANCELLED", 0)
    resolve_at(h, o, R_AFTER)
    assert after_ticks(h, NEXT_DAY) == (0, 0) and len(h.orders()) == 1


def test_transition_condition_failure(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid, o = unknown_23(h)
    row(h, "CANCELLED", 4)
    monkeypatch.setattr(AutoStopStore, "_transition_db", staticmethod(lambda *a, **k: False))
    with pytest.raises(service.Refused, match="상태가 바뀌어"):
        resolve_at(h, o, R_IN)
    monkeypatch.undo()
    assert h.store.fill_ledger([o["id"]]) == [] and h.store.order(o["id"])["state"] == "UNKNOWN" and audits(h) == []


# ═════════════════════ STEP 4: 엔진 사람 확인 경로 원자화·상황 구분 ═════════════════════
@pytest.mark.parametrize("path", ["external_cancel", "status_rejected", "send_rejected", "send_not_sent"])
def test_engine_review_path_atomic(tmp_path, monkeypatch, path):
    """상태 전이·NEEDS_REVIEW·알림이 한 트랜잭션: NEEDS_REVIEW 저장 중 예외면 주문 상태도 롤백된다."""
    h = sh(tmp_path, monkeypatch)
    pid, at = engine_path(h, path)
    _fail(monkeypatch, "_review_db")
    h.clock.set(at)
    with contextlib.suppress(RuntimeError):
        h.tick()
    monkeypatch.undo()
    h.broker.sell_exc = None
    o = h.orders()[-1]
    send = path.startswith("send")
    assert state_pair(h, o["id"], pid) == ("SENDING" if send else "SUBMITTED", "OPEN")
    assert "final" not in order_details(h, o["id"]) and f"event:{h.event(pid)['id']}:review" not in h.alert_keys()
    restart(h)
    assert after_ticks(h, [at + 60, at + 600] + NEXT_DAY) == (0, 0)
    o2 = h.store.order(o["id"])
    if send:
        assert o2["state"] == "UNKNOWN"                           # 기존 재시작 복구: 전송 결과 불명, 재전송 없음
    else:
        assert state_pair(h, o["id"], pid) == (o2["state"], "NEEDS_REVIEW") and o2["state"] in ("CANCELLED", "REJECTED")
        assert [f["source"] for f in order_details(h, o["id"])["final"]] == ["status"]


@pytest.mark.parametrize("path", ["external_cancel", "status_rejected", "send_rejected", "send_not_sent"])
def test_engine_review_path_commits_review_with_state(tmp_path, monkeypatch, path):
    h = sh(tmp_path, monkeypatch)
    pid, at = engine_path(h, path)
    h.clock.set(at)
    h.tick()
    h.broker.sell_exc = None
    o = h.orders()[-1]
    assert h.event(pid)["state"] == "NEEDS_REVIEW" and h.pos(pid)["state"] == "NEEDS_REVIEW"
    assert o["state"] in TERMINAL and f"event:{h.event(pid)['id']}:review" in h.alert_keys()


def _ours_cancel(h):
    pid, o = open_order_23(h)
    assert h.store.transition(o["id"], h.clock(), frm="SUBMITTED", to="CANCEL_PENDING")
    h.broker.rows[o["order_number"]].update(state="CANCELLED", remaining_quantity=0)
    return pid, ts(2026, 11, 23, 11, 1)


def _expired(h):
    pid, o = open_order_23(h)
    h.broker.rows[o["order_number"]].update(state="CANCELLED", remaining_quantity=0)
    return pid, NEXT_DAY[0]


@pytest.mark.parametrize("case", ["ours_for_replace", "expired_after_close", "external_cancel", "manual_resolve",
                                  "status_rejected", "send_rejected", "send_not_sent"])
def test_cancel_kinds_distinguished(tmp_path, monkeypatch, case):
    """정상 재주문용 취소·장 마감 후 소멸은 기존 자동 재주문·이월 정책 그대로, 나머지는 사람 확인."""
    h = sh(tmp_path, monkeypatch)
    if case == "manual_resolve":
        pid, o = unknown_23(h)
        row(h, "CANCELLED", 0)
        resolve_at(h, o, R_IN)
        assert h.event(pid)["state"] == "NEEDS_REVIEW"
        return
    pid, at = {"ours_for_replace": _ours_cancel, "expired_after_close": _expired}.get(
        case, lambda hh: engine_path(hh, case))(h)
    sells = len(h.broker.sells)
    h.set(shadow_replace_enabled=True)
    h.clock.set(at)
    h.tick()
    h.broker.sell_exc = None
    kinds = [x["kind"] for x in h.orders()]
    if case == "ours_for_replace":
        assert h.event(pid)["state"] == "OPEN" and kinds[-1] == "REPLACE" and len(h.broker.sells) == sells + 1
    elif case == "expired_after_close":
        assert h.event(pid)["state"] == "OPEN" and kinds[-1] == "CARRY_OVER" and len(h.broker.sells) == sells + 1
        assert any(k.endswith(":expired") for k in h.alert_keys())
    else:
        assert h.event(pid)["state"] == "NEEDS_REVIEW" and len(h.broker.sells) == sells


# ═════════════════════ STEP 5: 이전 버전 부분 저장 상태 ═════════════════════
def legacy_partial(h, *, state="CANCELLED", filled=0, final=True, audit=False, event_state="OPEN",
                   final_state=None, audit_target=None, audit_broker_state=None):
    """이전 버전 resolve_unknown 이 상태·종료 증거만 커밋하고 NEEDS_REVIEW 전에 중단된 상태 (테스트 임시 저널에서만 구성)."""
    pid, o = unknown_23(h)
    at = ts(2026, 11, 23, 10, 30)
    if filled:
        assert h.store.apply_fill(o["id"], at, filled, {"source": "manual_resolve"}) == "APPLIED"
    ev = [final_evidence(final_state or state, at, "manual_resolve", filled)] if final else None
    assert h.store.transition(o["id"], at, frm="UNKNOWN", to=state, broker_state=state, order_number="5000",
                              order_date="2026-11-23", evidence=ev)
    if audit:
        h.store.audit(at, "web", "resolve_unknown", audit_target or o["id"],
                      {"order_number": "5000", "broker_state": audit_broker_state or state})
    if event_state != "OPEN":
        assert h.store.update_event(h.event(pid)["id"], at, state=event_state)
    return pid, h.store.order(o["id"])


@pytest.mark.parametrize("event_state", ["OPEN", "CARRY_OVER"])
@pytest.mark.parametrize("evidence", ["final", "audit_only"])
def test_legacy_partial_manual_resolve_blocked(tmp_path, monkeypatch, event_state, evidence):
    h = sh(tmp_path, monkeypatch)
    pid, o = legacy_partial(h, final=evidence == "final", audit=evidence == "audit_only", event_state=event_state)
    ev0 = h.event(pid)
    restart(h)
    assert after_ticks(h, SAME_DAY[R_IN] + NEXT_DAY) == (0, 0)
    ev = h.event(pid)
    assert ev["state"] == "NEEDS_REVIEW" and "사람 확인 기록 누락" in ev["review_reason"]
    assert h.pos(pid)["state"] == "EXITING" and h.pos(pid)["remaining_managed_qty"] == 10
    assert (ev["triggered_at"], ev["trigger_price"]) == (ev0["triggered_at"], ev0["trigger_price"])
    assert [dict(x) for x in h.orders()] == [dict(h.store.order(o["id"]))] and h.store.order(o["id"])["state"] == "CANCELLED"
    assert f"event:{ev['id']}:review" in h.alert_keys()


@pytest.mark.parametrize("case", ["status_expired", "audit_other_order", "audit_non_final_state",
                                  "final_state_mismatch"])
def test_guard_ignores_unrelated_or_status_cancel(tmp_path, monkeypatch, case):
    """수동 해제 증거가 이 주문·이 상태에 대응하지 않으면 일반 취소로 본다 (기존 장 마감 후 소멸·이월 정책 유지)."""
    h = sh(tmp_path, monkeypatch)
    if case == "status_expired":
        pid, at = _expired(h)
        h.clock.set(at); h.tick()                                   # 기존 정책: 다음 날 이월 주문
        assert [x["kind"] for x in h.orders()][-1] == "CARRY_OVER" and h.event(pid)["state"] == "OPEN"
        return
    kw = {"audit_other_order": dict(final=False, audit=True, audit_target=999),
          "audit_non_final_state": dict(final=False, audit=True, audit_broker_state="OPEN"),
          "final_state_mismatch": dict(final_state="FILLED")}[case]
    pid, o = legacy_partial(h, **kw)
    restart(h)
    sells = len(h.broker.sells)
    h.clock.set(NEXT_DAY[0]); h.tick()
    assert h.event(pid)["state"] == "OPEN" and len(h.broker.sells) == sells + 1    # 차단 대상 아님 (기존 이월)


def test_legacy_filled_zero_remaining_kept(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid, o = legacy_partial(h, state="FILLED", filled=10)
    restart(h)
    assert after_ticks(h, SAME_DAY[R_IN][:1]) == (0, 0)
    assert h.pos(pid)["state"] == "SOLD" and h.event(pid)["state"] == "DONE"          # 기존 SOLD/DONE 정책


# ═════════════════════ STEP 6: 별도 프로세스 강제 종료·재시작 ═════════════════════
KILL_CHILD = textwrap.dedent("""
    import contextlib, json, os, sqlite3, sys
    sys.path.insert(0, {repo!r})
    from trading.auto_stop import service
    from trading.auto_stop.store import AutoStopStore
    point = sys.argv[1]
    row = json.loads({row!r})
    RULES = {{
        "ledger": ("after", "INSERT INTO auto_stop_fill_ledger"),
        "remaining": ("before", "UPDATE auto_stop_positions SET remaining_managed_qty"),
        "state": ("after", "UPDATE auto_stop_orders SET state="),
        "final": ("after", "UPDATE auto_stop_orders SET state="),     # details.final 은 상태와 같은 UPDATE 문
        "review_before": ("before", "UPDATE auto_stop_exit_events SET state='NEEDS_REVIEW'"),
        "review_after": ("after", "UPDATE auto_stop_exit_events SET state='NEEDS_REVIEW'"),
        "audit_before": ("before", "INSERT INTO auto_stop_audit"),
        "commit_before": ("before", "COMMIT"),
        "commit_after": ("after", "COMMIT"),
    }}
    armed = [False]                                    # 해제 트랜잭션의 주문 상태 UPDATE 를 본 뒤에만 COMMIT 감시

    class P:
        def __init__(s, db): s.db = db
        def execute(s, sql, *a):
            when, prefix = RULES[point]
            text = sql.lstrip()
            hit = text.startswith(prefix) and (prefix != "COMMIT" or armed[0])
            if hit and when == "before":
                os._exit(9)
            cur = s.db.execute(sql, *a)
            if text.startswith("UPDATE auto_stop_orders SET state="):
                armed[0] = True
            if hit and when == "after":
                os._exit(9)
            return cur
        def __getattr__(s, n): return getattr(s.db, n)

    if point in RULES:
        @contextlib.contextmanager
        def tx(self):                                  # BEGIN·COMMIT 까지 감시하는 연결 (실제 저장 로직은 그대로)
            raw = sqlite3.connect(self._uri, uri=True, timeout=15, isolation_level=None)
            raw.row_factory = sqlite3.Row
            db = P(raw)
            db.execute("PRAGMA synchronous=FULL"); db.execute("PRAGMA foreign_keys=ON"); db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                raw.execute("ROLLBACK")
                raise
            finally:
                raw.close()
        AutoStopStore.tx = tx
    elif point == "wait_before":
        service.reconcile_wait_records = lambda *a, **k: os._exit(9)   # 커밋 후·대기 기록 정리 전

    class Stub:
        def status(self, order): return dict(row)
    service.resolve_unknown(AutoStopStore({path!r}), Stub(), {oid!r}, order_number={number!r},
                            confirmation_ticker="CINF", now={at!r})
    os._exit(10)                                       # 여기까지 오면 종료 지점이 실행되지 않은 것
""")
KILL_POINTS = ["ledger", "remaining", "state", "final", "review_before", "review_after", "audit_before",
               "commit_before", "commit_after", "wait_before"]
COMMITTED = {"commit_after", "wait_before"}


def kill_resolve(h, o, point, state, filled, at=R_IN):
    number = o["order_number"] or "5000"
    evidence_row(h, o, state, filled)                  # 엔진(부모)과 운영자(자식)가 보는 증권사 증거를 같게 둔다
    script = KILL_CHILD.format(repo=REPO, row=json.dumps(h.broker.rows[number]), path=h.st.journal_path,
                               oid=o["id"], number=number, at=at)
    proc = subprocess.run([sys.executable, "-c", script, point], cwd=str(h.st.journal_path.rsplit("/", 1)[0]),
                          capture_output=True, timeout=60)
    assert proc.returncode == 9, (proc.returncode, proc.stderr.decode()[-800:])


@pytest.mark.parametrize("start", ["UNKNOWN", "CANCEL_UNKNOWN"])
@pytest.mark.parametrize("point", KILL_POINTS)
def test_resolve_killed_at(tmp_path, monkeypatch, start, point):
    """수동 해제(증거 CANCELLED, 누적 체결 4주) 중 지정 지점에서 프로세스 강제 종료 → 재시작 → 같은 날·다음 날 tick.

    롤백 쪽의 CANCEL_UNKNOWN 주문은 주문번호가 있어 엔진이 재시작 후 같은 증권사 증거로 자동 대조한다. 그 결과는
    이번 변경과 무관한 기존 정책(우리 취소 요청의 확정 → 재주문 정책)이므로, 그 경우는 매도 횟수 대신 원래 주문
    재전송 없음·체결 이중 반영 없음으로 판정한다."""
    h = sh(tmp_path, monkeypatch)
    pid, o = unknown_23(h) if start == "UNKNOWN" else cancel_unknown_23(h)
    ev0, n_orders = h.event(pid), len(h.orders())
    kill_resolve(h, o, point, "CANCELLED", 4)
    o2, d = h.store.order(o["id"]), order_details(h, o["id"])
    ledger = lambda: [r["applied_delta"] for r in h.store.fill_ledger([o["id"]])]   # noqa: E731
    if point in COMMITTED:
        assert o2["state"] == "CANCELLED" and [(f["state"], f["source"]) for f in d["final"]] == [("CANCELLED",
                                                                                                   "manual_resolve")]
        assert ledger() == [4] and h.pos(pid)["remaining_managed_qty"] == 6
        assert h.event(pid)["state"] == "NEEDS_REVIEW" and h.pos(pid)["state"] == "EXITING" and len(audits(h)) == 1
    else:
        assert o2["state"] == start and "final" not in d and o2["filled_qty"] == 0     # 부분 저장 없음
        assert ledger() == [] and h.pos(pid)["remaining_managed_qty"] == 10 and audits(h) == []
        assert h.event(pid)["state"] == "OPEN"
    assert len(h.orders()) == n_orders
    restart(h)
    moved = after_ticks(h, SAME_DAY[R_IN] + NEXT_DAY)
    ev = h.event(pid)
    assert (ev["triggered_at"], ev["trigger_price"]) == (ev0["triggered_at"], ev0["trigger_price"])
    assert h.store.order(o["id"])["order_number"] == (o["order_number"] or ("5000" if point in COMMITTED else None))
    if point in COMMITTED or start == "UNKNOWN":
        assert moved == (0, 0) and len(h.orders()) == n_orders
    assert sum(ledger()) <= 4 and h.pos(pid)["total_filled_qty"] == sum(ledger())     # 이중 차감 없음
    if point in COMMITTED:
        assert ev["state"] == "NEEDS_REVIEW" and ledger() == [4]
    elif start == "UNKNOWN":
        assert h.store.order(o["id"])["state"] == "UNKNOWN"
        assert resolve_any(h, o, NEXT_DAY[1] + 60)["state"] == "CANCELLED"            # 운영자 재시도
        assert ledger() == [4] and h.event(pid)["state"] == "NEEDS_REVIEW"
        assert after_ticks(h, [NEXT_DAY[1] + 120]) == (0, 0)


def test_full_fill_kill_keeps_terminal(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid, o = unknown_23(h)
    kill_resolve(h, o, "commit_after", "FILLED", 10)
    assert h.store.order(o["id"])["state"] == "FILLED" and h.event(pid)["state"] == "NEEDS_REVIEW"
    restart(h)
    assert after_ticks(h, SAME_DAY[R_IN][:1]) == (0, 0)
    assert h.pos(pid)["state"] == "SOLD" and h.event(pid)["state"] == "DONE"
    assert [r["applied_delta"] for r in h.store.fill_ledger([o["id"]])] == [10]
