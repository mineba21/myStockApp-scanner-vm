"""Codex 6차 리뷰(2026-11-28): UNKNOWN 해제 후 대기 기록 정합성, 개별 집계 오류의 전체 전파, 보고일 감시 대상 선정.
실주문·실취소 없음 (모의 브로커, 임시 SQLite)."""
import sqlite3
from decimal import Decimal

import pytest

from tests.auto_stop_fakes import Harness, ts
from tests.test_auto_stop_gate_matrix import Case
from tests.test_auto_stop_review4 import DAY, legacy_pos, pos_of, sh, ticks
from trading.auto_stop import service
from trading.kiwoom_orders import OrderUnknown


# ═════════════════════ STEP 1: UNKNOWN 해제 후 대기 기록 정합성 ═════════════════════
def unknown_replacement(tmp_path, monkeypatch):
    """원주문 취소 확정 → 재주문 전송 결과 불명(UNKNOWN). 재주문 대기 기록 outcome=unknown."""
    c = Case(tmp_path, monkeypatch, "REPLACE", "LIVE")
    h = c.h
    h.broker.sell_exc = OrderUnknown("timeout")
    c.run()
    h.broker.sell_exc = None
    rep = h.orders()[-1]
    assert rep["kind"] == "REPLACE" and rep["state"] == "UNKNOWN" and len(h.orders()) == 2
    assert waiting(h, c.eid)["outcome"] == "unknown"
    return c, rep


def broker_row(h, number, state, filled, qty=10):
    h.broker.rows[number] = {"order_number": number, "ticker": "T", "side": "SELL", "quantity": qty, "state": state,
                             "filled_quantity": filled, "remaining_quantity": 0 if state in ("FILLED", "CANCELLED",
                                                                                              "REJECTED") else qty - filled}


def resolve(h, oid, number):
    return service.resolve_unknown(h.store, h.broker, oid, order_number=number, confirmation_ticker="T",
                                   now=h.clock())


def waiting(h, eid):
    return h.store.runtime_get(f"replace_waiting:{eid}")


def closed_audits(h):
    return h.store.audits("wait_record_closed", 0, 1e12)


def next_tick(c):
    c.h.clock.advance(30)
    c.run()


@pytest.mark.parametrize("broker_state,filled,order_state,remaining,record", [
    ("FILLED", 10, "FILLED", 0, None),
    ("REJECTED", 0, "REJECTED", 10, "rejected"),
    ("OPEN", 0, "SUBMITTED", 10, None),
    ("PARTIAL", 4, "PARTIAL_FILLED", 6, None),
    ("CANCELLED", 4, "CANCELLED", 6, None),
])
def test_unknown_replacement_resolved(tmp_path, monkeypatch, broker_state, filled, order_state, remaining, record):
    c, rep = unknown_replacement(tmp_path, monkeypatch)
    h = c.h
    sells = len(h.broker.sells)
    broker_row(h, "5000", broker_state, filled)
    resolve(h, rep["id"], "5000")
    for _ in range(3):                                              # 해제 후 다음 tick 들
        next_tick(c)
    o = h.store.order(rep["id"])
    assert o["state"] == order_state and len(h.orders()) == 2 and len(h.broker.sells) == sells   # 주문 수 유지
    assert h.pos(c.pid)["remaining_managed_qty"] == remaining
    w = waiting(h, c.eid)
    if record is None:
        assert w is None and closed_audits(h)
    else:                                                           # 확인된 최종 결과로 기록, 자동 재주문 없음
        assert w["status"] == "closed" and w["outcome"] == record and w["remaining"] == remaining
    ev = h.store.event(c.eid)
    if order_state == "FILLED":
        assert ev["state"] == "DONE" and h.pos(c.pid)["state"] == "SOLD"
    else:
        assert ev["state"] == "NEEDS_REVIEW"                       # 수동 해제 정책 유지 (OPEN 으로 되돌리지 않음)


def test_cancel_unknown_resolved_to_cancelled_records_replacement_need(tmp_path, monkeypatch):
    c = Case(tmp_path, monkeypatch, "CANCEL_FOR_REPLACE", "LIVE")
    h = c.h
    h.broker.cancel_exc = OrderUnknown("lost")
    c.run()
    o = h.store.order(c.order_id)
    assert o["state"] == "CANCEL_UNKNOWN"
    h.broker.cancel_exc = None
    h.broker.fill(o["order_number"], 0, state="CANCELLED")
    resolve(h, o["id"], o["order_number"])
    next_tick(c)
    assert h.store.order(o["id"])["state"] == "CANCELLED" and len(h.orders()) == 1
    assert len(h.broker.sells) == 1                                 # 원주문 1건뿐, 자동 재주문 없음
    assert h.store.event(c.eid)["state"] == "NEEDS_REVIEW" and h.pos(c.pid)["remaining_managed_qty"] == 10
    a = h.store.audits("resolve_unknown", 0, 1e12)[-1]["details"]
    assert a["replacement_needed"] is True and a["remaining"] == 10  # 취소 확정 ≠ 매도 완료


def test_record_remaining_follows_actual_quantity(tmp_path, monkeypatch):
    from tests.test_auto_stop_review5 import Fault
    c = Case(tmp_path, monkeypatch, "REPLACE", "LIVE")
    h = c.h
    Fault(h, monkeypatch, "view").apply()
    c.run()
    key = f"replace_waiting:{c.eid}"
    h.store.runtime_set(key, {**waiting(h, c.eid), "remaining": 99}, h.clock())   # 기록과 실제 잔여 불일치
    service.reconcile_wait_records(h.store, h.clock())
    assert waiting(h, c.eid)["remaining"] == 10 and waiting(h, c.eid)["status"] == "waiting"


def test_restart_between_resolve_and_cleanup(tmp_path, monkeypatch):
    c, rep = unknown_replacement(tmp_path, monkeypatch)
    h = c.h
    broker_row(h, "5000", "FILLED", 10)
    real = service.reconcile_wait_records
    monkeypatch.setattr(service, "reconcile_wait_records", lambda *a, **k: {})   # 해제 직후 프로세스 중단 흉내
    resolve(h, rep["id"], "5000")
    assert waiting(h, c.eid)["outcome"] == "unknown"
    monkeypatch.setattr(service, "reconcile_wait_records", real)
    h.store.release_lease("w1", h.clock())
    h.engine = h.new_engine("w2")                                   # 재시작 후 첫 tick 에서 정리
    next_tick(c)
    assert waiting(h, c.eid) is None and len(h.orders()) == 2


def test_cleanup_is_idempotent(tmp_path, monkeypatch):
    c, rep = unknown_replacement(tmp_path, monkeypatch)
    h = c.h
    broker_row(h, "5000", "REJECTED", 0)
    resolve(h, rep["id"], "5000")
    snap = lambda: (h.store.runtime_get(f"replace_waiting:{c.eid}"), len(closed_audits(h)))  # noqa: E731
    first = snap()
    for _ in range(3):
        service.reconcile_wait_records(h.store, h.clock() + 1)
        assert snap() == first
    h.store.update_event(c.eid, h.clock(), state="DONE")           # 사람 확인 후 종료
    for _ in range(3):
        service.reconcile_wait_records(h.store, h.clock() + 2)
    assert waiting(h, c.eid) is None and len(closed_audits(h)) == first[1] + 1   # 정리·감사 기록은 한 번


def test_stale_record_of_ended_event_is_closed(tmp_path, monkeypatch):
    from tests.test_auto_stop_review5 import Fault
    c = Case(tmp_path, monkeypatch, "REPLACE", "LIVE")
    h = c.h
    Fault(h, monkeypatch, "view").apply()
    c.run()
    assert waiting(h, c.eid)["reason"].startswith("scanner_journal_unreadable")
    h.store.update_event(c.eid, h.clock(), state="DONE")
    h.store.update_position(c.pid, h.clock(), state="DISABLED")
    service.reconcile_wait_records(h.store, h.clock())
    assert waiting(h, c.eid) is None
    assert closed_audits(h)[-1]["details"]["reason"].startswith("event_ended")


def test_old_order_id_does_not_delete_current_waiting(tmp_path, monkeypatch):
    from tests.test_auto_stop_review5 import Fault
    c = Case(tmp_path, monkeypatch, "REPLACE", "LIVE")
    h = c.h
    Fault(h, monkeypatch, "view").apply()
    c.run()                                                         # 대체 주문 대기 (원주문은 CANCELLED, 과거 주문)
    key = f"replace_waiting:{c.eid}"
    h.store.runtime_set(key, {**waiting(h, c.eid), "order_id": c.order_id}, h.clock())   # 과거 order_id 가 남은 기록
    service.reconcile_wait_records(h.store, h.clock())
    assert waiting(h, c.eid) is not None and waiting(h, c.eid)["status"] == "waiting"
    db = sqlite3.connect(h.st.journal_path)                         # 다른 이벤트의 주문 id 가 남은 기록
    foreign = db.execute("SELECT MAX(id) FROM auto_stop_orders").fetchone()[0] + 1000
    db.close()
    h.store.runtime_set(key, {**waiting(h, c.eid), "order_id": foreign}, h.clock())
    service.reconcile_wait_records(h.store, h.clock())
    assert waiting(h, c.eid)["status"] == "waiting" and len(h.orders()) == 1


# ═════════════════════ STEP 2: 개별 집계 오류의 전체 전파 ═════════════════════
def legacy(h, ok_cinf, ok_evrg, *, same_ticker=False, fail=True):
    from tests.test_auto_stop_review3 import legacy_day
    day, c, e, e2, rows = legacy_day(h, extra_same_ticker=same_ticker)
    rows = [r for r in rows if r[0] != "quote_ok"] + [("quote_ok", "CINF", ok_cinf), ("quote_ok", "EVRG", ok_evrg)]
    for metric, key, n in rows:
        h.store.bump(day, "SHADOW", metric, key, n)
    rep = service.daily_report(h.store, day, "SHADOW", h.st, now=ts(2026, 11, 25, 17, 0))
    return rep, c, e, e2


def test_codex_cinf_391_of_390_does_not_offset_evrg(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    rep, c, e, _ = legacy(h, 391, 20)
    w = rep["watch"]
    assert w["expected"] == 734 and w["ok_recorded"] == 411
    assert (w["ok"], w["missed"], w["normal_rate_pct"]) == (None, None, None)
    sub = w["measured_subtotal"]
    assert (sub["positions"], sub["ok"], sub["expected"], sub["missed"]) == (1, 20, 344, 324)
    assert w["ok_integrity"] == "invalid" and w["ok_coverage"] == "full"       # coverage 의미는 그대로
    cinf = pos_of(rep, c)
    assert cinf["ok_integrity"] == "exceeds_expected" and cinf["missed_checks"] is None
    assert any("aggregate_exceeds_expected" in q for q in cinf["data_quality"])
    assert w["api_errors"] == 6
    text = service.report_text(rep)
    assert "정상 감시 측정 불가/734" in text and "데이터 무결성 오류 1건, 원시 성공 기록 411" in text
    assert "누락 측정 불가 (측정 가능 1/2종목 소계 정상 20/344, 누락 324)" in text and "None" not in text
    html = open("web/templates/index.html", encoding="utf-8").read()
    assert 'data-testid="report-integrity"' in html and "report.watch.ok_recorded ?? '측정 불가'" in html


def test_all_within_expected_keeps_nov25_result(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    rep, c, e, _ = legacy(h, 53, 20)
    w = rep["watch"]
    assert (w["ok"], w["expected"], w["missed"], w["api_errors"]) == (73, 734, 661, 6)
    assert w["ok_integrity"] == "valid" and w["integrity_errors"] == [] and w["ok_recorded"] == 73
    assert w["measured_subtotal"]["missed"] == 661


def test_every_ticker_exceeds(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    rep, *_ = legacy(h, 391, 345)
    w = rep["watch"]
    assert w["ok"] is None and w["missed"] is None and w["ok_recorded"] == 736
    assert w["measured_subtotal"]["positions"] == 0 and len(w["integrity_errors"]) == 2


def test_legacy_ticker_group_exceeds(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    rep, c, e, e2 = legacy(h, 53, 400, same_ticker=True)
    w = rep["watch"]
    grp = w["legacy_ticker_totals"]["EVRG"]
    assert grp["integrity"] == "exceeds_expected" and grp["missed"] is None and grp["ok"] == 400
    assert w["ok"] is None and any("EVRG 티커 합계" in x for x in w["integrity_errors"])
    assert w["measured_subtotal"]["positions"] == 1 and w["measured_subtotal"]["ok"] == 53   # 유효한 CINF 만


def test_slot_v2_success_cannot_exceed_expected_slots(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    h.broker.holding["CINF"] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
    h.clock.set(ts(2026, 11, 23, 9, 25))
    pid = h.add(mode="SHADOW", ticker="CINF")
    ticks(h, 20)
    for k in range(5):                                              # 구간 밖 성공 슬롯 기록 (오류 데이터)
        h.store.stat_set(DAY, "SHADOW", "slot", f"{pid}:{ts(2026, 11, 23, 15, k)}", 1)
    rep = service.daily_report(h.store, DAY, "SHADOW", h.st, now=h.clock())
    p = pos_of(rep, pid)
    assert p["format"] == "SLOT_V2" and p["ok_checks"] <= p["expected_checks"] and p["ok_integrity"] == "valid"
    assert any(q.startswith("slot_out_of_window") for q in p["data_quality"])


def test_mixed_exceeds(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = legacy_pos(h, "CINF", ts(2026, 11, 23, 9, 58), 0, 50, end_state=None)   # 구버전 기간 2분에 50회
    h.clock.set(ts(2026, 11, 23, 10, 0))
    ticks(h, 6)
    rep = service.daily_report(h.store, DAY, "SHADOW", h.st, now=h.clock())
    p, w = pos_of(rep, pid), rep["watch"]
    assert p["format"] == "MIXED" and p["ok_integrity"] == "exceeds_expected"
    assert w["ok"] is None and w["normal_rate_pct"] is None and w["ok_recorded"] == p["ok_checks"]


def test_unmeasured_and_exceeding_mixed(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    over = legacy_pos(h, "EVRG", ts(2026, 11, 23, 9, 40), 5, 40)    # LEGACY: 5분 기대 5, 정상 40
    mism = legacy_pos(h, "CINF", ts(2026, 11, 23, 9, 58), 0, 0, end_state=None)
    h.clock.set(ts(2026, 11, 23, 10, 0))
    ticks(h, 6)
    db = sqlite3.connect(h.st.journal_path)
    db.execute("UPDATE auto_stop_daily_stats SET value=1 WHERE metric='quote_ok' AND key='CINF'")
    db.commit(); db.close()
    rep = service.daily_report(h.store, DAY, "SHADOW", h.st, now=h.clock())
    w = rep["watch"]
    assert pos_of(rep, mism)["ok_checks"] is None and pos_of(rep, over)["ok_integrity"] == "exceeds_expected"
    assert w["ok"] is None and w["missed"] is None and w["ok_coverage"] == "partial" and w["ok_integrity"] == "invalid"
    assert w["measured_subtotal"]["positions"] == 0


def test_zero_expected_positive_success(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = legacy_pos(h, "CINF", ts(2026, 11, 23, 10, 0), 0.5, 1)     # 30초 감시: 기대 0, 정상 1
    rep = service.daily_report(h.store, DAY, "SHADOW", h.st, now=ts(2026, 11, 23, 17, 0))
    p, w = pos_of(rep, pid), rep["watch"]
    assert (p["expected_checks"], p["ok_checks"], p["ok_integrity"]) == (0, 1, "exceeds_expected")
    assert w["ok"] is None and w["ok_integrity"] == "invalid"


def test_zero_success_positive_expected_is_valid(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = legacy_pos(h, "CINF", ts(2026, 11, 23, 10, 0), 30, 0)     # 30분 동안 실제로 감시 성공 0
    rep = service.daily_report(h.store, DAY, "SHADOW", h.st, now=ts(2026, 11, 23, 17, 0))
    p, w = pos_of(rep, pid), rep["watch"]
    assert (p["ok_checks"], p["expected_checks"], p["ok_integrity"]) == (0, 30, "valid")
    assert (w["ok"], w["missed"], w["normal_rate_pct"], w["ok_integrity"]) == (0, 30, 0.0, "valid")


# ═════════════════════ STEP 3: 보고일 감시 대상 선정 ═════════════════════
D24 = "2026-11-24"


def day24(h, now=ts(2026, 11, 24, 12, 0)):
    h.clock.set(ts(2026, 11, 24, 10, 0))
    h.tick()
    return service.daily_report(h.store, D24, "SHADOW", h.st, now=now)


def assert_no_targets(rep):
    w = rep["watch"]
    assert rep["watched_positions"] == 0 and w["expected"] == 0
    assert (w["ok_status"], w["ok_coverage"], w["normal_rate_pct"]) == ("no_targets", "none", None)


def watched23(h, ticker="CINF"):
    h.broker.holding[ticker] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
    h.clock.set(ts(2026, 11, 23, 10, 0))
    pid = h.add(mode="SHADOW", ticker=ticker)
    ticks(h, 4)
    return pid


def sell_out(h, pid):
    """11/23 손절 트리거 → 매도 주문 → 모의 브로커 전량 체결 → SOLD / DONE."""
    h.broker.price = Decimal("44")
    ticks(h, 2)
    for o in h.orders():
        h.broker.fill(o["order_number"], o["requested_qty"])
    ticks(h, 2)


def test_previous_day_sold_done(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = watched23(h)
    sell_out(h, pid)
    assert h.pos(pid)["state"] == "SOLD" and h.event(pid)["state"] == "DONE"
    orders_before = h.orders()
    h.broker.price = Decimal("50")
    rep = day24(h)
    assert_no_targets(rep)
    assert h.orders() == orders_before and rep["exit_followups"] == []        # 기존 주문 이력 불변


@pytest.mark.parametrize("end", ["DISABLED", "NEEDS_REVIEW"])
def test_previous_day_disabled_or_review(tmp_path, monkeypatch, end):
    h = sh(tmp_path, monkeypatch)
    pid = watched23(h)
    if end == "DISABLED":
        service.disable(h.store, pid, now=h.clock())
    else:
        h.broker.holding["CINF"]["qty"] = 4
        h.tick()
    assert h.pos(pid)["state"] == end
    assert_no_targets(day24(h))


def test_previous_day_end_then_same_ticker_reregistered(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    old = watched23(h)
    sell_out(h, old)
    h.broker.price = Decimal("50")
    h.broker.holding["CINF"] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
    h.clock.set(ts(2026, 11, 24, 9, 0))
    new = h.add(mode="SHADOW", ticker="CINF")
    rep = day24(h)
    assert [p["id"] for p in rep["watch"]["positions"]] == [new] and old != new


@pytest.mark.parametrize("event", ["register", "trigger", "disable"])
def test_intraday_events_define_window(tmp_path, monkeypatch, event):
    h = sh(tmp_path, monkeypatch)
    h.broker.holding["CINF"] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
    h.clock.set(ts(2026, 11, 24, 9, 0) if event != "register" else ts(2026, 11, 24, 11, 0))
    pid = h.add(mode="SHADOW", ticker="CINF")
    h.clock.set(ts(2026, 11, 24, 11, 0))
    ticks(h, 4)
    if event == "trigger":
        h.broker.price = Decimal("44")
        h.tick()
        end = h.event(pid)["triggered_at"]
    elif event == "disable":
        end = h.clock()
        service.disable(h.store, pid, now=end)
    rep = service.daily_report(h.store, D24, "SHADOW", h.st, now=ts(2026, 11, 24, 12, 0))
    p = pos_of(rep, pid)
    assert rep["watched_positions"] == 1
    if event == "register":
        assert p["window"]["start"] == ts(2026, 11, 24, 11, 0)
    else:
        assert p["window"]["end"] == pytest.approx(end, abs=1)


def test_trigger_exactly_at_open(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = watched23(h)
    h.broker.price = Decimal("44")
    h.broker.quote_age = 0                                          # 개장 시각 시세
    h.clock.set(ts(2026, 11, 24, 9, 30))                            # 다음 날 개장 시각 첫 감시에서 트리거
    h.tick()
    ev = h.event(pid)
    assert ev["triggered_at"] == ts(2026, 11, 24, 9, 30)
    rep = service.daily_report(h.store, D24, "SHADOW", h.st, now=ts(2026, 11, 24, 12, 0))
    p = pos_of(rep, pid)                                            # 길이 0 구간이지만 당일 이벤트 근거로 포함
    assert rep["watched_positions"] == 1 and p["expected_checks"] == 0 and rep["triggers"] == 1


def test_previous_day_event_carried_over_order_today(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    h.broker.holding["CINF"] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
    h.clock.set(ts(2026, 11, 23, 15, 40))
    pid = h.add(mode="SHADOW", ticker="CINF")
    ticks(h, 2)
    h.broker.price = Decimal("44")
    h.clock.set(ts(2026, 11, 23, 15, 55)); h.tick()                 # 마감 cutoff 이후 트리거 → 이월
    assert h.event(pid)["state"] == "CARRY_OVER"
    h.clock.set(ts(2026, 11, 24, 9, 31)); h.tick()                  # 다음 정규장 이월 주문
    rep = service.daily_report(h.store, D24, "SHADOW", h.st, now=ts(2026, 11, 24, 12, 0))
    assert_no_targets(rep)                                          # 일반 감시 대상이 아니다
    assert [f["position_id"] for f in rep["exit_followups"]] == [pid]
    assert rep["orders"]["total"] == 1 and len(h.orders()) == 1     # 당일 주문 통계는 그대로 (감시 통계와 분리)


def test_target_with_zero_success_vs_no_target(tmp_path, monkeypatch):
    from trading.kiwoom_readonly import KiwoomError
    h = sh(tmp_path, monkeypatch)
    assert_no_targets(day24(h))
    h.broker.holding["CINF"] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
    h.clock.set(ts(2026, 11, 24, 10, 5))
    pid = h.add(mode="SHADOW", ticker="CINF")
    h.broker.quote_exc_by["CINF"] = KiwoomError("timeout")
    ticks(h, 6)
    w = service.daily_report(h.store, D24, "SHADOW", h.st, now=h.clock())["watch"]
    assert w["ok_status"] == "full" and w["ok"] == 0 and w["expected"] > 0 and w["normal_rate_pct"] == 0.0
    assert pos_of({"watch": w}, pid)["ok_checks"] == 0
