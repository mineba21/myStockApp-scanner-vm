"""Codex 7차 리뷰(2026-11-28): 이전 형식 대기 기록 order_id 호환, 최초 영구 감시 종료 시각, 전일 주문의 당일 후속 이력.
실주문·실취소 없음 (모의 브로커, 임시 SQLite)."""
import json
import sqlite3
from decimal import Decimal

import pytest

from tests.auto_stop_fakes import ts
from tests.test_auto_stop_gate_matrix import Case
from tests.test_auto_stop_review4 import DAY, pos_of, sh, ticks
from tests.test_auto_stop_review6 import broker_row, closed_audits, next_tick, resolve, unknown_replacement, waiting
from trading.auto_stop import service
from trading.auto_stop.store import watch_end_of
from trading.kiwoom_orders import OrderUnknown

D24, D25 = "2026-11-24", "2026-11-25"


# ═════════════════════ STEP 1: 이전 형식 대기 기록의 order_id 호환 ═════════════════════
def old_format(h, key, order, **extra):
    """6b09507 이 남기던 형식: since 가 주문 생성 뒤 시각, outcome=unknown, status 없음."""
    rec = {"since": order["created_at"] + 1, "at": order["created_at"] + 1, "reason": f"재주문 결과 {order['state']}",
           "ticker": order["ticker"], "remaining": 10, "outcome": "unknown", "order_id": order["id"], **extra}
    h.store.runtime_set(key, rec, h.clock())
    return rec


def snapshot(h, pid):
    return len(h.orders()), h.pos(pid)["remaining_managed_qty"]


@pytest.mark.parametrize("broker_state,filled,expect_record,remaining", [
    ("REJECTED", 0, "rejected", 10), ("FILLED", 10, None, 0), ("OPEN", 0, None, 10), ("PARTIAL", 4, None, 6)])
def test_old_format_record_with_valid_order_id(tmp_path, monkeypatch, broker_state, filled, expect_record, remaining):
    c, rep = unknown_replacement(tmp_path, monkeypatch)
    h = c.h
    key = f"replace_waiting:{c.eid}"
    old_format(h, key, rep)
    broker_row(h, "5000", broker_state, filled)
    resolve(h, rep["id"], "5000")                                   # 공식 경로 (증권사 증거)
    w = waiting(h, c.eid)
    if expect_record:
        assert w["status"] == "closed" and w["outcome"] == expect_record and "link_issue" not in w
    else:
        assert w is None and closed_audits(h)[-1]["details"]["order_id"] == rep["id"]
    assert snapshot(h, c.pid) == (2, remaining)                     # 주문 행 수·관리 잔여 보존
    assert h.store.event(c.eid)["state"] == "NEEDS_REVIEW"          # 이벤트 상태는 정리로 바뀌지 않음
    next_tick(c)
    assert snapshot(h, c.pid)[0] == 2


def waiting_case(tmp_path, monkeypatch):
    from tests.test_auto_stop_review5 import Fault
    c = Case(tmp_path, monkeypatch, "REPLACE", "LIVE")
    Fault(c.h, monkeypatch, "view").apply()
    c.run()                                                         # 대체 주문 대기 (주문 없음)
    return c


def foreign_order_id(c):
    h = c.h
    h.broker.holding["U"] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
    pid = h.add(ticker="U")
    eid = h.store.create_event(h.clock(), h.pos(pid), state="OPEN", trigger_price=Decimal("44"),
                               trigger_as_of=h.clock(), session_date="2026-11-23")
    h.store.acquire_lease("setup", h.clock(), 90)
    o = h.store.claim_sell(h.clock(), owner="setup", event_id=eid, kind="ORIGINAL", qty=1, ref_price=Decimal("44"),
                           ref_source="CUR", quote_as_of=h.clock(), limit_price=Decimal("43"), settings=h.st,
                           guard=lambda: None)
    h.store.release_lease("setup", h.clock())
    return o["id"]


@pytest.mark.parametrize("ref", ["other_event", "missing", "past_original"])
def test_invalid_order_id_does_not_fabricate_result(tmp_path, monkeypatch, ref):
    c = waiting_case(tmp_path, monkeypatch)
    h = c.h
    key = f"replace_waiting:{c.eid}"
    oid = {"other_event": lambda: foreign_order_id(c), "missing": lambda: 999999,
           "past_original": lambda: c.order_id}[ref]()
    h.store.runtime_set(key, {**waiting(h, c.eid), "order_id": oid}, h.clock())
    n = len(h.orders())
    service.reconcile_wait_records(h.store, h.clock())
    w = waiting(h, c.eid)
    assert w is not None and w["status"] == "waiting" and "outcome" not in w     # 확정 결과를 만들지 않음
    assert w["link_issue"].startswith({"other_event": "order_other_event", "missing": "order_missing",
                                       "past_original": "order_kind_mismatch"}[ref])
    assert len(h.orders()) == n


def clone_order(h, src, *, dt, state, number):
    db = sqlite3.connect(h.st.journal_path)
    cols = [r[1] for r in db.execute("PRAGMA table_info(auto_stop_orders)") if r[1] != "id"]
    row = dict(zip(cols, db.execute(f"SELECT {','.join(cols)} FROM auto_stop_orders WHERE id=?", (src["id"],)).fetchone()))
    row.update(created_at=src["created_at"] + dt, updated_at=src["created_at"] + dt, sent_at=src["created_at"] + dt,
               state=state, order_number=number, attempt_no=row["attempt_no"] + 1, parent_order_id=src["id"])
    cur = db.execute(f"INSERT INTO auto_stop_orders({','.join(row)}) VALUES({','.join('?' * len(row))})",
                     list(row.values()))
    db.commit(); oid = cur.lastrowid; db.close()
    return oid


def test_newer_replace_after_old_order_id_is_the_subject(tmp_path, monkeypatch):
    c, rep = unknown_replacement(tmp_path, monkeypatch)
    h = c.h
    h.store.transition(rep["id"], h.clock(), frm="UNKNOWN", to="CANCELLED")  # 기존 주문 종료 후 (미확정 1건 규칙)
    newer = clone_order(h, rep, dt=60, state="SUBMITTED", number="7777")    # 기존 order_id 이후 신규 REPLACE
    old_format(h, f"replace_waiting:{c.eid}", rep)
    service.reconcile_wait_records(h.store, h.clock())
    assert waiting(h, c.eid) is None and closed_audits(h)[-1]["details"]["order_id"] == newer


def test_superseded_old_order_id_does_not_delete_current_waiting(tmp_path, monkeypatch):
    c, rep = unknown_replacement(tmp_path, monkeypatch)
    h = c.h
    clone_order(h, rep, dt=60, state="CANCELLED", number="7777")
    old_format(h, f"replace_waiting:{c.eid}", rep, since=rep["created_at"] + 61)  # 대기 시작이 신규 주문 뒤
    service.reconcile_wait_records(h.store, h.clock())
    w = waiting(h, c.eid)
    assert w is not None and w["link_issue"].startswith("order_superseded") and w.get("outcome") != "rejected"


def test_old_record_without_order_id_and_current_format(tmp_path, monkeypatch):
    c = waiting_case(tmp_path, monkeypatch)
    h = c.h
    service.reconcile_wait_records(h.store, h.clock())
    assert waiting(h, c.eid)["status"] == "waiting" and "link_issue" not in waiting(h, c.eid)
    c2, rep = unknown_replacement(tmp_path / "b" if (tmp_path / "b").mkdir() is None else None, monkeypatch)
    assert waiting(c2.h, c2.eid)["since"] == rep["created_at"]        # 현재 형식: since = 주문 생성 시각
    broker_row(c2.h, "5000", "REJECTED", 0)
    resolve(c2.h, rep["id"], "5000")
    assert waiting(c2.h, c2.eid)["outcome"] == "rejected"


def test_order_block_old_format(tmp_path, monkeypatch):
    c = Case(tmp_path, monkeypatch, "ORIGINAL", "LIVE")
    h = c.h
    h.broker.sell_exc = OrderUnknown("timeout")
    c.run()
    h.broker.sell_exc = None
    o = h.orders()[-1]
    assert o["state"] == "UNKNOWN"
    key = f"order_block:{c.eid}"
    old_format(h, key, o, kind="ORIGINAL", reason="scanner_journal_unreadable: 이전 기록")
    broker_row(h, "5000", "REJECTED", 0)
    resolve(h, o["id"], "5000")
    w = h.store.runtime_get(key)
    assert w["status"] == "closed" and w["outcome"] == "rejected" and snapshot(h, c.pid) == (1, 10)


def test_old_format_cleanup_idempotent_and_after_restart(tmp_path, monkeypatch):
    c, rep = unknown_replacement(tmp_path, monkeypatch)
    h = c.h
    old_format(h, f"replace_waiting:{c.eid}", rep)
    broker_row(h, "5000", "REJECTED", 0)
    real = service.reconcile_wait_records
    monkeypatch.setattr(service, "reconcile_wait_records", lambda *a, **k: {})   # 해제 직후 중단 흉내
    resolve(h, rep["id"], "5000")
    monkeypatch.setattr(service, "reconcile_wait_records", real)
    h.store.release_lease("w1", h.clock())
    h.engine = h.new_engine("w2")
    next_tick(c)
    first = (waiting(h, c.eid), len(closed_audits(h)))
    assert first[0]["outcome"] == "rejected"
    for _ in range(3):
        service.reconcile_wait_records(h.store, h.clock() + 5)
        assert (waiting(h, c.eid), len(closed_audits(h))) == first
    assert snapshot(h, c.pid) == (2, 10)


# ═════════════════════ STEP 2: 최초 영구 감시 종료 시각 ═════════════════════
def pos23(h, ticker="CINF", at=ts(2026, 11, 23, 10, 0)):
    h.broker.holding[ticker] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
    h.clock.set(at)
    pid = h.add(mode="SHADOW", ticker=ticker)
    ticks(h, 4)
    return pid


def review_now(h, pid):
    h.broker.holding[h.pos(pid)["ticker"]]["qty"] = 4               # 원인 불명 수량 감소 → NEEDS_REVIEW
    at = h.clock()
    h.tick()
    assert h.pos(pid)["state"] == "NEEDS_REVIEW"
    return at


def report(h, day, now):
    return service.daily_report(h.store, day, "SHADOW", h.st, now=now)


def test_codex_prev_day_review_then_next_day_disable(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = pos23(h)
    at = review_now(h, pid)
    assert watch_end_of(h.pos(pid)) == at
    h.clock.set(ts(2026, 11, 24, 10, 0)); h.tick()
    assert report(h, D24, ts(2026, 11, 24, 10, 30))["watched_positions"] == 0
    service.disable(h.store, pid, now=ts(2026, 11, 24, 11, 0))
    assert report(h, D24, ts(2026, 11, 24, 12, 0))["watched_positions"] == 0       # disable 후에도 0
    assert watch_end_of(h.pos(pid)) == at                                          # 최초 종료 시각 유지


def test_prev_day_review_then_pause_resume(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = pos23(h)
    at = review_now(h, pid)
    service.set_paused(h.store, pid, True, now=ts(2026, 11, 24, 10, 0))
    service.set_paused(h.store, pid, False, now=ts(2026, 11, 24, 10, 30))
    assert report(h, D24, ts(2026, 11, 24, 12, 0))["watched_positions"] == 0 and watch_end_of(h.pos(pid)) == at


def test_prev_day_sold_then_order_updates(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = pos23(h)
    h.broker.price = Decimal("44")
    ticks(h, 2)
    for o in h.orders():
        h.broker.fill(o["order_number"], o["requested_qty"])
    ticks(h, 2)
    assert h.pos(pid)["state"] == "SOLD"
    trig = h.event(pid)["triggered_at"]
    h.store.add_order_evidence(h.orders()[0]["id"], ts(2026, 11, 24, 10, 0), "note", "갱신")
    h.store.update_position(pid, ts(2026, 11, 24, 10, 0), broker_qty_last=0)
    assert report(h, D24, ts(2026, 11, 24, 12, 0))["watched_positions"] == 0 and watch_end_of(h.pos(pid)) == trig


def test_same_day_review_then_disable_window_ends_at_review(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = pos23(h)
    at = review_now(h, pid)
    service.disable(h.store, pid, now=at + 3600)
    p = pos_of(report(h, DAY, ts(2026, 11, 23, 17, 0)), pid)
    assert p["window"]["end"] == pytest.approx(at)


def test_same_day_disable_then_next_day_report(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = pos23(h)
    service.disable(h.store, pid, now=h.clock())
    assert watch_end_of(h.pos(pid)) == h.clock()
    assert report(h, D24, ts(2026, 11, 24, 12, 0))["watched_positions"] == 0


def test_daily_close_and_pause_are_not_permanent_end(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = pos23(h)
    h.clock.set(ts(2026, 11, 23, 16, 5)); h.tick()                  # 일일 마감 처리
    service.set_paused(h.store, pid, True, now=ts(2026, 11, 24, 9, 0))
    service.set_paused(h.store, pid, False, now=ts(2026, 11, 24, 9, 10))
    h.clock.set(ts(2026, 11, 24, 10, 0)); ticks(h, 4)
    assert watch_end_of(h.pos(pid)) is None and h.pos(pid)["state"] == "ACTIVE"
    rep = report(h, D24, h.clock())
    assert rep["watched_positions"] == 1 and pos_of(rep, pid)["ok_checks"] > 0     # 다음 거래일 정상 감시 계속


def test_shadow_to_live_promotion_ends_shadow_watch(tmp_path, monkeypatch):
    from trading.auto_stop import settings as S
    h = sh(tmp_path, monkeypatch)
    pid = pos23(h)
    monkeypatch.setattr(S, "live_order_block_reason", lambda st: None)
    at = h.clock()
    service.promote(h.store, h.broker, pid, confirmation_ticker="CINF", now=at, settings=h.st)
    assert h.pos(pid)["state"] == "DISABLED" and watch_end_of(h.pos(pid)) == at
    assert report(h, D24, ts(2026, 11, 24, 12, 0))["watched_positions"] == 0


def test_end_record_survives_restart(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = pos23(h)
    at = review_now(h, pid)
    h.store.release_lease("w1", h.clock())
    h.engine = h.new_engine("w2")
    h.clock.set(ts(2026, 11, 24, 10, 0)); ticks(h, 3)
    assert watch_end_of(h.pos(pid)) == at


def test_failed_transition_leaves_no_end_time(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = pos23(h)
    assert h.store.update_position(pid, h.clock(), expect_states=("EXITING",), state="DISABLED") is False
    assert watch_end_of(h.pos(pid)) is None and h.pos(pid)["state"] == "ACTIVE"
    assert h.store.create_event(h.clock(), {**h.pos(pid), "id": pid}, state="OPEN", trigger_price=Decimal("44"),
                                trigger_as_of=h.clock(), session_date=DAY) is not None
    assert json.loads(h.pos(pid)["details"])["watch_end_reason"] == "trigger"


def strip_end(h, pid):
    db = sqlite3.connect(h.st.journal_path)                         # 이전 버전 포지션: 종료 시각 기록 없음
    db.execute("UPDATE auto_stop_positions SET details='{}' WHERE id=?", (pid,))
    db.commit(); db.close()


def test_legacy_position_end_restored_from_history(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = pos23(h)
    at = review_now(h, pid)
    service.disable(h.store, pid, now=at + 3600)
    strip_end(h, pid)
    p = pos_of(report(h, DAY, ts(2026, 11, 23, 17, 0)), pid)
    assert p["window"]["end"] == pytest.approx(at, abs=1)            # 확인 필요 알림 시각 (disable 시각 아님)
    assert any(q.startswith("watch_end_from_history") for q in p["data_quality"])
    assert report(h, D24, ts(2026, 11, 24, 12, 0))["watched_positions"] == 0


def test_legacy_position_end_unprovable(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = pos23(h)
    db = sqlite3.connect(h.st.journal_path)                         # 기록·이력 없이 종료된 이전 버전 포지션
    db.execute("UPDATE auto_stop_positions SET state='DISABLED', details='{}', updated_at=? WHERE id=?",
               (ts(2026, 11, 24, 11, 0), pid))
    db.commit(); db.close()
    rep = report(h, D24, ts(2026, 11, 24, 12, 0))
    p = pos_of(rep, pid)
    assert (p["expected_checks"], p["ok_checks"], p["gaps"]["max_sec"]) == (None, None, None)
    assert any(q.startswith("watch_end_unverified") for q in p["data_quality"])
    assert rep["watch"]["expected"] is None and rep["watch"]["ok"] is None
    text = service.report_text(rep)
    assert "정상 감시 측정 불가/측정 불가" in text and "None" not in text
    assert report(h, D25, ts(2026, 11, 25, 12, 0))["watched_positions"] == 0    # 마지막 수정 이전 → 제외 확실


def test_registered_as_needs_review_never_watched(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    h.broker.holding["CINF"] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
    h.clock.set(ts(2026, 11, 23, 10, 0))
    pid = h.add(mode="SHADOW", ticker="CINF", state="NEEDS_REVIEW", review_reason="등록 시 손절가 이하")
    assert watch_end_of(h.pos(pid)) == h.clock()
    assert report(h, DAY, ts(2026, 11, 23, 17, 0))["watched_positions"] == 0


# ═════════════════════ STEP 3: 전일 주문의 당일 후속 이력 ═════════════════════
def open_order_23(h, *, ticker="CINF"):
    """11/23 장중 트리거 → 매도 주문 SUBMITTED (지정가 아래 시세로 미체결 유지, 재주문 꺼짐)."""
    h.set(shadow_replace_enabled=False)
    pid = pos23(h, ticker)
    h.broker.price = Decimal("44")
    h.tick(); h.clock.advance(30)
    h.broker.price = Decimal("40")
    h.tick()
    o = h.orders()[-1]
    assert o["state"] == "SUBMITTED"
    return pid, o


def day24_tick(h, at=ts(2026, 11, 24, 9, 31)):
    h.clock.set(at)
    h.tick()


def fu(rep, pid):
    return next((f for f in rep["exit_followups"] if f["position_id"] == pid), None)


def test_codex_prev_submitted_filled_next_day(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid, o = open_order_23(h)
    h.broker.fill(o["order_number"], 10)
    day24_tick(h)
    h.clock.advance(30); h.tick()
    assert h.store.order(o["id"])["state"] == "FILLED" and h.event(pid)["state"] == "DONE"
    rep = report(h, D24, ts(2026, 11, 24, 12, 0))
    f = fu(rep, pid)
    assert f is not None and f["events"][0]["orders"][0]["fills_applied_qty_on_day"] == 10
    assert "fill_applied_on_day" in f["events"][0]["orders"][0]["reasons"]
    assert rep["watched_positions"] == 0 and rep["orders"]["total"] == 0           # 감시·신규 주문 수 불변
    assert rep["exit_followup_fills_applied_qty"] == 10
    item = f["events"][0]["orders"][0]["fills_on_day"][0]
    assert item["broker_fill_time"] is None and ts(2026, 11, 24, 9, 31) <= item["applied_at"]   # 실제 체결 시각 추정 없음


def test_prev_partial_then_more_fills_over_days(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid, o = open_order_23(h)
    h.broker.fill(o["order_number"], 3)
    h.clock.advance(30); h.tick()                                   # 11/23 부분체결 3
    h.broker.fill(o["order_number"], 6)
    day24_tick(h)                                                    # 11/24 +3
    for _ in range(3):
        h.clock.advance(30); h.tick()                               # 같은 체결 반복 대조
    h.broker.fill(o["order_number"], 10)
    day24_tick(h, ts(2026, 11, 25, 9, 31))                           # 11/25 +4
    r24 = report(h, D24, ts(2026, 11, 25, 12, 0))
    r25 = report(h, D25, ts(2026, 11, 25, 12, 0))
    assert fu(r24, pid)["events"][0]["orders"][0]["fills_applied_qty_on_day"] == 3
    assert fu(r25, pid)["events"][0]["orders"][0]["fills_applied_qty_on_day"] == 4
    assert [r["applied_delta"] for r in h.store.fill_ledger([o["id"]])] == [3, 3, 4]       # 중복 반영 없음


@pytest.mark.parametrize("outcome", ["CANCELLED", "REJECTED"])
def test_prev_order_cancel_or_reject_confirmed_next_day(tmp_path, monkeypatch, outcome):
    h = sh(tmp_path, monkeypatch)
    pid, o = open_order_23(h)
    h.broker.fill(o["order_number"], 0, state=outcome) if outcome == "CANCELLED" else \
        h.broker.rows[o["order_number"]].update(state="REJECTED")
    day24_tick(h)
    assert h.store.order(o["id"])["state"] == outcome
    f = fu(report(h, D24, ts(2026, 11, 24, 12, 0)), pid)
    assert f is not None and "unresolved_at_day_start" in f["events"][0]["orders"][0]["reasons"]


def test_prev_unknown_resolved_next_day(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    h.set(shadow_replace_enabled=False)
    pid = pos23(h)
    h.broker.price = Decimal("44")
    h.broker.sell_exc = OrderUnknown("timeout")
    h.tick()
    h.broker.sell_exc = None
    o = h.orders()[-1]
    assert o["state"] == "UNKNOWN"
    h.broker.rows["5000"] = {"order_number": "5000", "ticker": "CINF", "side": "SELL", "quantity": 10,
                             "state": "FILLED", "filled_quantity": 10, "remaining_quantity": 0}
    h.clock.set(ts(2026, 11, 24, 10, 0))
    service.resolve_unknown(h.store, h.broker, o["id"], order_number="5000", confirmation_ticker="CINF", now=h.clock())
    f = fu(report(h, D24, ts(2026, 11, 24, 12, 0)), pid)
    reasons = f["events"][0]["orders"][0]["reasons"]
    assert "unknown_resolved_on_day" in reasons and "fill_applied_on_day" in reasons


def test_prev_order_still_open_next_day_and_requery(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid, o = open_order_23(h)
    day24_tick(h)                                                    # 상태 변화 없는 대조
    f = fu(report(h, D24, ts(2026, 11, 24, 12, 0)), pid)
    first = f["events"][0]["orders"][0]
    assert "unresolved_at_day_start" in first["reasons"] and first["fills_applied_qty_on_day"] == 0
    h.broker.fill(o["order_number"], 10)
    day24_tick(h, ts(2026, 11, 25, 9, 31))
    again = fu(report(h, D24, ts(2026, 11, 25, 12, 0)), pid)        # 다음 날 과거 보고 재조회
    assert again["events"][0]["orders"][0]["reasons"] == first["reasons"]     # 이후 체결이 과거 보고를 바꾸지 않음
    assert again["events"][0]["orders"][0]["fills_applied_qty_on_day"] == 0


def test_prev_final_order_without_activity_excluded(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid, o = open_order_23(h)
    h.broker.fill(o["order_number"], 10)
    h.clock.advance(30); h.tick()                                   # 11/23 안에 전량 체결
    assert h.store.order(o["id"])["state"] == "FILLED"
    day24_tick(h)
    assert fu(report(h, D24, ts(2026, 11, 24, 12, 0)), pid) is None
    assert report(h, D25, ts(2026, 11, 25, 12, 0))["exit_followups"] == []


def test_prev_event_carry_over_order_today_is_followup(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    h.broker.holding["CINF"] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
    h.clock.set(ts(2026, 11, 23, 15, 40))
    pid = h.add(mode="SHADOW", ticker="CINF")
    ticks(h, 2)
    h.broker.price = Decimal("44")
    h.clock.set(ts(2026, 11, 23, 15, 55)); h.tick()
    day24_tick(h)
    f = fu(report(h, D24, ts(2026, 11, 24, 12, 0)), pid)
    assert "created_on_day" in f["events"][0]["orders"][0]["reasons"]


# ═════════════════════ STEP 4: 공통 이력 불변 조건 ═════════════════════
def test_report_generation_is_read_only_and_history_based(tmp_path, monkeypatch):
    from tests.test_auto_stop_review4 import _snapshot_db
    h = sh(tmp_path, monkeypatch)
    pid, o = open_order_23(h)
    h.broker.fill(o["order_number"], 10)
    day24_tick(h)
    h.broker.prices["EVRG"] = Decimal("50")
    review_pid = pos23(h, "EVRG", at=ts(2026, 11, 24, 10, 0))
    review_now(h, review_pid)
    before = _snapshot_db(h.st.journal_path)
    views = [json.dumps({k: report(h, d, now)[k] for k in ("watched_positions", "exit_followups")}, sort_keys=True)
             for d, now in ((D24, ts(2026, 11, 24, 16, 30)), (D24, ts(2026, 11, 25, 12, 0)),
                            (D24, ts(2026, 11, 30, 12, 0)))]
    assert len(set(views)) == 1                                     # 조회 시각이 과거 보고서를 바꾸지 않음
    service.disable(h.store, review_pid, now=ts(2026, 11, 25, 10, 0))   # 이후 상태 변경도 바꾸지 않음
    assert json.dumps({k: report(h, D24, ts(2026, 11, 25, 12, 0))[k]
                       for k in ("watched_positions", "exit_followups")}, sort_keys=True) == views[0]
    assert before                                                   # 아래: 보고서 생성 전후 저널 전체 불변
    mid = _snapshot_db(h.st.journal_path)
    for _ in range(3):
        report(h, D24, ts(2026, 11, 25, 12, 0))
    assert _snapshot_db(h.st.journal_path) == mid
