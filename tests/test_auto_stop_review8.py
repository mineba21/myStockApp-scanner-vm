"""Codex 8차 리뷰(2026-11-28): UNKNOWN 해제와 최종 종료 증거 구분, 구버전 포지션 감시 종료 근거(정확 시각 vs 상한),
동일 티커 LEGACY 집계 귀속 불확실성. 실주문·실취소 없음 (모의 브로커, 임시 SQLite)."""
import json
import sqlite3
from decimal import Decimal

import pytest

from tests.auto_stop_fakes import ts
from tests.test_auto_stop_review4 import DAY, pos_of, sh, ticks
from tests.test_auto_stop_review7 import D24, D25, day24_tick, fu, open_order_23, pos23, report
from trading.auto_stop import service
from trading.auto_stop.store import watch_end_of, watch_ended_by_of
from trading.kiwoom_orders import OrderUnknown

D24_START = ts(2026, 11, 24, 0, 0)


# ═════════════════════ STEP 1: UNKNOWN 해제와 최종 종료 증거 ═════════════════════
def unknown_23(h):
    h.set(shadow_replace_enabled=False)
    pid = pos23(h)
    h.broker.price = Decimal("44")
    h.broker.sell_exc = OrderUnknown("timeout")
    h.tick()
    h.broker.sell_exc = None
    h.broker.price = Decimal("40")
    o = h.orders()[-1]
    assert o["state"] == "UNKNOWN"
    return pid, o


def row(h, state, filled, number="5000"):
    h.broker.rows[number] = {"order_number": number, "ticker": "CINF", "side": "SELL", "quantity": 10, "state": state,
                             "filled_quantity": filled,
                             "remaining_quantity": 0 if state in ("FILLED", "CANCELLED", "REJECTED") else 10 - filled}


def resolve_at(h, o, at, number="5000"):
    return service.resolve_unknown(h.store, h.broker, o["id"], order_number=number, confirmation_ticker="CINF", now=at)


def order_fu(rep, pid):
    f = fu(rep, pid)                                                # 이벤트가 열려 있어도 주문 활동이 없으면 None
    orders = [] if f is None else f["events"][0]["orders"]
    return orders[0] if orders else None


def test_codex_unknown_to_submitted_then_filled_keeps_past_followup(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid, o = unknown_23(h)
    row(h, "OPEN", 0)
    resolve_at(h, o, ts(2026, 11, 23, 15, 30))                      # 11/23 접수 확인 (종료 아님)
    day24_tick(h)                                                    # 11/24 대조 (미체결)
    before = order_fu(report(h, D24, ts(2026, 11, 24, 16, 30)), pid)
    assert before is not None and "unresolved_at_day_start" in before["reasons"]
    row(h, "FILLED", 10)
    day24_tick(h, ts(2026, 11, 25, 9, 31))                           # 11/25 전량 체결
    after = order_fu(report(h, D24, ts(2026, 11, 25, 12, 0)), pid)
    assert after is not None and after["reasons"] == before["reasons"] and after["order_id"] == before["order_id"]
    assert after["state_now"] == "FILLED"                           # 현재 상태 표시만 최신으로 바뀐다


def test_unknown_to_partial_then_full_fill(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid, o = unknown_23(h)
    row(h, "PARTIAL", 4)
    resolve_at(h, o, ts(2026, 11, 23, 15, 30))
    day24_tick(h)
    row(h, "FILLED", 10)
    day24_tick(h, ts(2026, 11, 25, 9, 31))
    r24 = order_fu(report(h, D24, ts(2026, 11, 25, 12, 0)), pid)
    r25 = order_fu(report(h, D25, ts(2026, 11, 25, 12, 0)), pid)
    assert "unresolved_at_day_start" in r24["reasons"] and r24["fills_applied_qty_on_day"] == 0
    assert r25["fills_applied_qty_on_day"] == 6


@pytest.mark.parametrize("state,filled", [("FILLED", 10), ("REJECTED", 0)])
def test_unknown_resolved_final_on_23_not_followup_on_24(tmp_path, monkeypatch, state, filled):
    h = sh(tmp_path, monkeypatch)
    pid, o = unknown_23(h)
    row(h, state, filled)
    resolve_at(h, o, ts(2026, 11, 23, 15, 30))                      # 11/23 최종 종료 증거
    day24_tick(h)
    assert order_fu(report(h, D24, ts(2026, 11, 24, 12, 0)), pid) is None


def test_unknown_resolved_cancelled_on_24(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid, o = unknown_23(h)
    row(h, "CANCELLED", 0)
    day24_tick(h)
    resolve_at(h, o, ts(2026, 11, 24, 10, 0))
    r = order_fu(report(h, D24, ts(2026, 11, 24, 12, 0)), pid)
    assert "unknown_resolved_on_day" in r["reasons"] and "unresolved_at_day_start" in r["reasons"]
    assert order_fu(report(h, D25, ts(2026, 11, 25, 12, 0)), pid) is None


def test_cancel_unknown_resolved_open_then_cancel_confirmed(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid, o = open_order_23(h)
    assert h.store.transition(o["id"], h.clock(), frm="SUBMITTED", to="CANCEL_UNKNOWN")   # 취소 응답 불명 상황
    service.resolve_unknown(h.store, h.broker, o["id"], order_number=o["order_number"], confirmation_ticker="CINF",
                            now=ts(2026, 11, 23, 15, 45))           # 증권사: 아직 OPEN → 접수 확인일 뿐
    assert h.store.order(o["id"])["state"] == "SUBMITTED"
    h.broker.fill(o["order_number"], 0, state="CANCELLED")
    day24_tick(h)                                                    # 11/24 취소 확정 (최종 종료)
    assert h.store.order(o["id"])["state"] == "CANCELLED"
    r = order_fu(report(h, D24, ts(2026, 11, 24, 12, 0)), pid)
    assert r is not None and "unresolved_at_day_start" in r["reasons"]


def finalize_at(h, o, state, at, *, evidence_state=None):
    h.store.transition(o["id"], at, frm=("SUBMITTED", "UNKNOWN", "PARTIAL_FILLED"), to=state)
    h.store.add_order_evidence(o["id"], at, "final", {"state": evidence_state or state, "at": at, "source": "status"})


def test_acceptance_and_final_audits_both_present(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid, o = open_order_23(h)
    h.store.audit(ts(2026, 11, 23, 15, 30), "web", "resolve_unknown", o["id"], {"broker_state": "OPEN"})
    finalize_at(h, o, "CANCELLED", ts(2026, 11, 24, 11, 0))
    h.store.audit(ts(2026, 11, 24, 11, 0), "web", "resolve_unknown", o["id"], {"broker_state": "CANCELLED"})
    assert "unresolved_at_day_start" in order_fu(report(h, D24, ts(2026, 11, 24, 12, 0)), pid)["reasons"]


def test_legacy_audit_without_broker_state_is_not_final_evidence(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid, o = open_order_23(h)
    h.store.audit(ts(2026, 11, 23, 15, 30), "web", "resolve_unknown", o["id"], {"order_number": o["order_number"]})
    h.broker.fill(o["order_number"], 10)
    day24_tick(h, ts(2026, 11, 25, 9, 31))                           # 실제 종료는 11/25
    assert "unresolved_at_day_start" in order_fu(report(h, D24, ts(2026, 11, 25, 12, 0)), pid)["reasons"]


def test_event_review_alert_of_other_order_is_not_final_evidence(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid, o = open_order_23(h)
    eid = h.event(pid)["id"]
    h.store.enqueue_alert(ts(2026, 11, 23, 15, 40), f"event:{eid}:review", "URGENT", "다른 사유의 확인 필요")
    h.broker.rows[o["order_number"]].update(state="REJECTED")
    day24_tick(h)                                                    # 이 주문의 거부 확정은 11/24
    assert h.store.order(o["id"])["state"] == "REJECTED"
    assert "unresolved_at_day_start" in order_fu(report(h, D24, ts(2026, 11, 24, 12, 0)), pid)["reasons"]


def test_contradicting_final_evidence_is_quality_error(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid, o = open_order_23(h)
    finalize_at(h, o, "FILLED", ts(2026, 11, 24, 11, 0), evidence_state="FILLED")
    h.store.audit(ts(2026, 11, 23, 15, 30), "web", "resolve_unknown", o["id"], {"broker_state": "CANCELLED"})
    r = order_fu(report(h, D24, ts(2026, 11, 24, 12, 0)), pid)
    assert r is not None and "final_evidence_conflict" in r["reasons"] and "unresolved_at_day_start" not in r["reasons"]


@pytest.mark.parametrize("at,included", [(D24_START, True), (D24_START - 1, False)])
def test_final_evidence_at_day_boundary(tmp_path, monkeypatch, at, included):
    h = sh(tmp_path, monkeypatch)
    pid, o = open_order_23(h)
    finalize_at(h, o, "CANCELLED", at)
    r = order_fu(report(h, D24, ts(2026, 11, 24, 12, 0)), pid)
    assert (r is not None) is included


def test_multi_day_reconcile_history_is_stable(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid, o = open_order_23(h)
    day24_tick(h)
    day24_tick(h, ts(2026, 11, 25, 9, 31))
    h.broker.fill(o["order_number"], 10)
    h.clock.advance(30); h.tick()
    views = [(order_fu(report(h, d, now), pid) or {}).get("reasons")
             for d in (D24, D25) for now in (ts(2026, 11, 25, 16, 30), ts(2026, 11, 30, 12, 0))]
    assert views[0] == views[1] and views[2] == views[3]
    assert "unresolved_at_day_start" in views[0] and "fill_applied_on_day" in views[2]


# ═════════════════════ STEP 2: 감시 종료 정확 시각 vs 상한 근거 ═════════════════════
def legacy_review(h, *, updated=ts(2026, 11, 23, 11, 0)):
    """종료 시각·전환 알림 없이 NEEDS_REVIEW 가 된 구버전 포지션."""
    pid = pos23(h)
    db = sqlite3.connect(h.st.journal_path)
    db.execute("UPDATE auto_stop_positions SET state='NEEDS_REVIEW', details='{}', updated_at=? WHERE id=?", (updated, pid))
    db.execute("DELETE FROM auto_stop_alerts WHERE dedup_key LIKE ?", (f"position:{pid}:%",))
    db.commit(); db.close()
    return pid


def watched(h, day, now):
    rep = report(h, day, now)
    return rep["watched_positions"], rep["watch"]["expected"]


def test_codex_legacy_review_then_next_day_disable(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = legacy_review(h)
    assert watched(h, D24, ts(2026, 11, 24, 9, 50)) == (0, 0)
    service.disable(h.store, pid, now=ts(2026, 11, 24, 10, 0))
    assert watched(h, D24, ts(2026, 11, 24, 12, 0)) == (0, 0)        # 기대 30회가 새로 생기지 않는다
    assert watch_end_of(h.pos(pid)) is None                          # 후속 해제 시각을 최초 종료로 쓰지 않음
    assert watch_ended_by_of(h.pos(pid)) == ts(2026, 11, 23, 11, 0)  # 그때 이미 종료돼 있었다는 상한


def test_legacy_review_then_pause_resume(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = legacy_review(h)
    service.set_paused(h.store, pid, True, now=ts(2026, 11, 24, 10, 0))
    service.set_paused(h.store, pid, False, now=ts(2026, 11, 24, 10, 30))
    assert watched(h, D24, ts(2026, 11, 24, 12, 0)) == (0, 0)
    assert watch_ended_by_of(h.pos(pid)) == ts(2026, 11, 23, 11, 0)


def test_active_to_disabled_is_exact(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = pos23(h)
    at = h.clock()
    service.disable(h.store, pid, now=at)
    assert watch_end_of(h.pos(pid)) == at
    assert h.store.audits("disable", 0, 1e18)[-1]["details"]["prev_state"] == "ACTIVE"
    assert pos_of(report(h, DAY, ts(2026, 11, 23, 17, 0)), pid)["window"]["end"] == at


def test_exiting_to_disabled_keeps_trigger_time(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = pos23(h)
    trig = h.clock()
    h.store.create_event(trig, h.pos(pid), state="OPEN", trigger_price=Decimal("44"), trigger_as_of=trig,
                         session_date=DAY)
    service.disable(h.store, pid, now=ts(2026, 11, 24, 10, 0))
    assert watch_end_of(h.pos(pid)) == trig and watched(h, D24, ts(2026, 11, 24, 12, 0)) == (0, 0)


def test_same_day_review_then_disable(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = pos23(h)
    h.broker.holding["CINF"]["qty"] = 4
    at = h.clock(); h.tick()
    service.disable(h.store, pid, now=at + 1800)
    assert pos_of(report(h, DAY, ts(2026, 11, 23, 17, 0)), pid)["window"]["end"] == pytest.approx(at)


def legacy_disabled(h, *, audit_details, at=ts(2026, 11, 24, 10, 0)):
    pid = pos23(h)
    db = sqlite3.connect(h.st.journal_path)
    db.execute("UPDATE auto_stop_positions SET state='DISABLED', details='{}', updated_at=? WHERE id=?", (at, pid))
    db.commit(); db.close()
    h.store.audit(at, "web", "disable", pid, audit_details)
    return pid


def test_legacy_disable_audit_without_prev_state_is_not_exact(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = legacy_disabled(h, audit_details={})
    rep = report(h, D24, ts(2026, 11, 24, 12, 0))
    p = pos_of(rep, pid)                                             # 보고일과 겹치는지 불명: 측정 불가로 유지
    assert p["expected_checks"] is None and any(q.startswith("watch_end_unverified") for q in p["data_quality"])
    assert watched(h, D25, ts(2026, 11, 25, 12, 0)) == (0, 0)        # 상한이 보고일 전 → 제외 확실


def test_prev_state_bound_proves_end_before_day(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = legacy_disabled(h, audit_details={"prev_state": "NEEDS_REVIEW", "prev_updated_at": ts(2026, 11, 23, 11, 0)})
    assert watched(h, D24, ts(2026, 11, 24, 12, 0)) == (0, 0)


def test_failed_transition_writes_no_bound(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = legacy_review(h)
    assert h.store.update_position(pid, ts(2026, 11, 24, 10, 0), expect_states=("EXITING",), state="DISABLED") is False
    assert watch_ended_by_of(h.pos(pid)) is None and h.pos(pid)["state"] == "NEEDS_REVIEW"


def test_bound_survives_restart_and_requery(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = legacy_review(h)
    service.disable(h.store, pid, now=ts(2026, 11, 24, 10, 0))
    h.store.release_lease("w1", h.clock())
    h.engine = h.new_engine("w2")
    h.clock.set(ts(2026, 11, 24, 10, 30)); ticks(h, 3)
    views = {watched(h, D24, now) for now in (ts(2026, 11, 24, 12, 0), ts(2026, 11, 25, 12, 0), ts(2026, 11, 30, 9, 0))}
    assert views == {(0, 0)} and watch_ended_by_of(h.pos(pid)) == ts(2026, 11, 23, 11, 0)


# ═════════════════════ STEP 3: 동일 티커 LEGACY 집계 귀속 ═════════════════════
def unverified_old(h, ticker="CINF", created=ts(2026, 11, 23, 9, 0), ended=ts(2026, 11, 23, 14, 0)):
    """종료 시각 미확인 이전 버전 포지션 (기록·이력 없음, 마지막 수정이 보고일 안)."""
    h.broker.holding[ticker] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
    h.clock.set(created)
    pid = h.add(mode="SHADOW", ticker=ticker)
    db = sqlite3.connect(h.st.journal_path)
    db.execute("UPDATE auto_stop_positions SET state='DISABLED', details='{}', updated_at=? WHERE id=?", (ended, pid))
    db.commit(); db.close()
    return pid


def new_pos(h, ticker="CINF", at=ts(2026, 11, 23, 14, 30)):
    h.clock.set(at)
    return h.add(mode="SHADOW", ticker=ticker)


def legacy_report(h):
    return report(h, DAY, ts(2026, 11, 23, 17, 0))


def test_codex_unverified_old_and_new_legacy(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    old = unverified_old(h)
    new = new_pos(h)
    for m, n in (("quote_ok", 12), ("quote_fail", 3), ("quote_invalid", 1)):
        h.store.bump(DAY, "SHADOW", m, "CINF", n)
    rep = legacy_report(h)
    p_new, p_old = pos_of(rep, new), pos_of(rep, old)
    assert p_new["ok_checks"] is None and p_new["missed_checks"] is None and p_new["normal_rate_pct"] is None
    assert any(q.startswith("legacy_attribution_uncertain") for q in p_new["data_quality"])
    assert p_old["ok_checks"] is None
    assert (p_new["quote_fail"], p_new["quote_invalid"]) == (None, None)
    raw = rep["ticker_daily_raw"]["CINF"]
    assert (raw["quote_ok"], raw["quote_fail"], raw["quote_invalid"], raw["attribution"]) == (12, 3, 1, "uncertain")
    w = rep["watch"]
    assert w["ok"] is None and w["measured_subtotal"]["positions"] == 0 and w["api_errors"] is None
    text = service.report_text(rep)
    assert "CINF 티커 일계 정상 12회 귀속 불가" in text and "None" not in text


def test_unverified_old_does_not_invalidate_slot_v2(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    old = unverified_old(h)
    h.store.bump(DAY, "SHADOW", "quote_ok", "CINF", 12)
    new = new_pos(h)
    ticks(h, 8)                                                      # 신버전 슬롯 (position_id 기록)
    rep = report(h, DAY, h.clock())
    p_new = pos_of(rep, new)
    assert p_new["format"] == "SLOT_V2" and p_new["ok_checks"] == p_new["slots"]["ok"] > 0
    assert pos_of(rep, old)["ok_checks"] is None and rep["ticker_daily_raw"]["CINF"]["attribution"] == "uncertain"
    assert rep["watch"]["measured_subtotal"]["ok"] == p_new["ok_checks"]


def test_unverified_old_and_mixed(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    unverified_old(h, ended=ts(2026, 11, 23, 11, 0))
    h.store.bump(DAY, "SHADOW", "quote_ok", "CINF", 12)
    mixed = new_pos(h, at=ts(2026, 11, 23, 11, 30))
    h.clock.set(ts(2026, 11, 23, 12, 0)); ticks(h, 6)                # 장중 버전 전환 → MIXED
    p = pos_of(report(h, DAY, h.clock()), mixed)
    assert p["format"] == "MIXED" and p["ok_checks"] is None
    assert any(q.startswith("legacy_attribution_uncertain") for q in p["data_quality"])


def test_several_unverified_positions(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    a = unverified_old(h, created=ts(2026, 11, 23, 9, 0), ended=ts(2026, 11, 23, 10, 30))
    b = unverified_old(h, created=ts(2026, 11, 23, 11, 0), ended=ts(2026, 11, 23, 12, 30))
    new = new_pos(h)
    h.store.bump(DAY, "SHADOW", "quote_ok", "CINF", 12)
    rep = legacy_report(h)
    assert {pos_of(rep, x)["ok_checks"] for x in (a, b, new)} == {None}
    assert sorted(rep["ticker_daily_raw"]["CINF"]["uncertain_positions"]) == sorted([a, b])


def test_old_ended_before_day_is_not_a_contributor(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    unverified_old(h, created=ts(2026, 11, 20, 10, 0), ended=ts(2026, 11, 20, 14, 0))   # 보고일 전 종료 확실
    new = new_pos(h)
    h.store.bump(DAY, "SHADOW", "quote_ok", "CINF", 12)
    rep = legacy_report(h)
    assert rep["watched_positions"] == 1 and pos_of(rep, new)["ok_checks"] == 12
    assert rep["ticker_daily_raw"]["CINF"]["attribution"] == "single"


def test_proven_attribution_and_real_zero(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = pos23(h, at=ts(2026, 11, 23, 9, 0))
    db = sqlite3.connect(h.st.journal_path)                         # 슬롯 기록 없는 LEGACY 날로 만든다
    db.execute("DELETE FROM auto_stop_daily_stats"); db.commit(); db.close()
    service.disable(h.store, pid, now=ts(2026, 11, 23, 11, 0))      # ACTIVE → DISABLED: 정확한 종료
    h.store.bump(DAY, "SHADOW", "quote_ok", "CINF", 12)
    h.broker.holding["EVRG"] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
    zero = new_pos(h, "EVRG", at=ts(2026, 11, 23, 10, 0))           # 실제 성공 0회
    rep = legacy_report(h)
    assert pos_of(rep, pid)["ok_checks"] == 12 and pos_of(rep, zero)["ok_checks"] == 0
    assert pos_of(rep, zero)["ok_integrity"] == "valid" and rep["watch"]["ok"] == 12


def test_other_ticker_subtotal_and_no_double_count(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    unverified_old(h)
    new_pos(h)
    h.broker.holding["EVRG"] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
    ev = new_pos(h, "EVRG", at=ts(2026, 11, 23, 15, 0))
    for t, n in (("CINF", 12), ("EVRG", 20)):
        h.store.bump(DAY, "SHADOW", "quote_ok", t, n)
    rep = legacy_report(h)
    w = rep["watch"]
    assert pos_of(rep, ev)["ok_checks"] == 20
    assert w["measured_subtotal"]["positions"] == 1 and w["measured_subtotal"]["ok"] == 20
    assert w["ok"] is None and w["ok_recorded"] == 20                # 원시 일계 12 는 평가 집계에 더하지 않음
    assert rep["ticker_daily_raw"]["CINF"]["quote_ok"] == 12


# ═════════════════════ STEP 4: 공통 불변 조건 ═════════════════════
def test_report_is_read_only_with_new_evidence_fields(tmp_path, monkeypatch):
    from tests.test_auto_stop_review4 import _snapshot_db
    h = sh(tmp_path, monkeypatch)
    pid, o = unknown_23(h)
    row(h, "OPEN", 0)
    resolve_at(h, o, ts(2026, 11, 23, 15, 30))
    snap = _snapshot_db(h.st.journal_path)
    for d in (DAY, D24, D25):
        report(h, d, ts(2026, 11, 25, 12, 0))
    assert _snapshot_db(h.st.journal_path) == snap
    assert json.loads(h.store.order(o["id"])["details"]).get("final") is None     # 접수 확인은 종료 기록이 아님
