"""Codex 5차 리뷰(2026-11-28): Scanner 저널 읽기 실패 후속 처리, 장중 관리 종료 상태 근거,
전체 기대·성공 집계, LEGACY 오류 통계, 측정 불가 표시 계약. 실주문·실취소 없음 (모의 브로커, 임시 SQLite)."""
import sqlite3
from decimal import Decimal

import pytest

from tests.auto_stop_fakes import Harness, ts
from tests.test_auto_stop_gate_matrix import KINDS, Case
from tests.test_auto_stop_review4 import _FailingDb, scanner_object
from trading.auto_stop.store import AutoStopStore
from trading.kiwoom_orders import OrderRejected, OrderUnknown
from trading.kiwoom_readonly import KiwoomError
from trading.scanner_buy_orders import ScannerBuyOrders

FAULTS = ("view", "missing_column", "metadata", "query")


class Fault:
    """Scanner 저널 읽기 실패 주입·해제. view/missing_column 은 실제 객체, metadata/query 는 조회 오류."""

    def __init__(self, h, monkeypatch, kind):
        self.h, self.kind, self.on = h, kind, False
        path = h.st.journal_path
        if kind == "query":
            ScannerBuyOrders(path, broker_factory=lambda m: None)    # 표가 있어야 미확정 조회까지 간다
        real = AutoStopStore.scanner_buy_in_flight_db
        fail_on = {"metadata": "sqlite_master", "query": "main.scanner_buy_orders"}.get(kind)

        def patched(db, ticker):
            if self.on and fail_on:
                db = _FailingDb(db, fail_on)
            return real(db, ticker)
        monkeypatch.setattr(AutoStopStore, "scanner_buy_in_flight_db", staticmethod(patched))

    def apply(self, *_):
        if self.on:
            return
        self.on = True
        if self.kind in ("view", "missing_column"):
            scanner_object(self.h.st.journal_path, self.kind)

    def heal(self):
        self.on = False
        db = sqlite3.connect(self.h.st.journal_path)
        if self.kind == "view":
            db.execute("DROP VIEW scanner_buy_orders")
            db.execute("DROP TABLE scanner_rows_base")
        elif self.kind == "missing_column":
            db.execute("DROP TABLE scanner_buy_orders")
        db.commit()
        db.close()


def alerts(h, part):
    return [a for a in h.store.recent_alerts(1000) if part in a["dedup_key"]]


def waiting(h, eid):
    return h.store.runtime_get(f"replace_waiting:{eid}")


def check_blocked_record(c):
    """주문 종류별 차단 결과: 전송 0회, 이벤트·트리거·잔여 유지, 실제 사유 기록."""
    h = c.h
    assert c.count() == c.before, "sell/cancel 0회"
    ev, pos = h.store.event(c.eid), h.pos(c.pid)
    assert ev["state"] == "OPEN" and pos["state"] == "EXITING" and pos["remaining_managed_qty"] == 10
    if c.kind == "CANCEL_FOR_REPLACE":
        o = h.store.order(c.order_id)
        assert o["state"] == "SUBMITTED" and o["cancel_sent_at"] is None          # 기존 매도 주문 유지
        held = alerts(h, f"order:{c.order_id}:cancel_held")
        assert held and "scanner_journal_unreadable" in held[0]["text"]
    elif c.kind == "REPLACE":
        w = waiting(h, c.eid)
        assert w and w["reason"].startswith("scanner_journal_unreadable") and w["remaining"] == 10
        urgent = alerts(h, f"event:{c.eid}:replace_waiting")
        assert urgent and urgent[0]["severity"] == "URGENT"
    else:
        b = h.store.runtime_get(f"order_block:{c.eid}")
        assert b and b["reason"].startswith("scanner_journal_unreadable") and b["kind"] == c.kind
        assert alerts(h, f"event:{c.eid}:order_block")


# ── STEP 1: 사전 검사부터 실패 ───────────────────────────────────
@pytest.mark.parametrize("fault", FAULTS)
@pytest.mark.parametrize("kind", KINDS)
def test_unreadable_from_precheck_blocks_without_crashing(tmp_path, monkeypatch, kind, fault):
    c = Case(tmp_path, monkeypatch, kind, "LIVE")
    f = Fault(c.h, monkeypatch, fault)
    f.apply()
    out = c.run()                                                   # 예외로 tick 이 중단되지 않는다
    assert out["status"] == "OK"
    assert c.h.store.runtime_get("last_tick_ok_at") == c.h.clock()
    check_blocked_record(c)


# ── STEP 1: 사전 검사 통과 후 최종 선점 직전에 실패 ─────────────────
@pytest.mark.parametrize("fault", FAULTS)
@pytest.mark.parametrize("kind", KINDS)
def test_unreadable_between_precheck_and_claim(tmp_path, monkeypatch, kind, fault):
    c = Case(tmp_path, monkeypatch, kind, "LIVE")
    f = Fault(c.h, monkeypatch, fault)
    c.inject(f.apply)
    out = c.run()
    assert out["status"] == "OK"
    check_blocked_record(c)


# ── STEP 1: Codex 재현 — 취소 확정 후 조회 실패, 30초 간격 3회 ────────
@pytest.mark.parametrize("fault", FAULTS)
def test_cancel_confirmed_then_unreadable_three_ticks(tmp_path, monkeypatch, fault):
    c = Case(tmp_path, monkeypatch, "REPLACE", "LIVE")
    h = c.h
    f = Fault(h, monkeypatch, fault)
    f.apply()
    first = h.clock()
    for _ in range(3):
        assert c.run()["status"] == "OK"
        h.clock.advance(30)
    assert h.store.order(c.order_id)["state"] == "CANCELLED"
    assert c.count() == c.before
    w = waiting(h, c.eid)
    assert w["since"] == first and w["reason"].startswith("scanner_journal_unreadable")
    assert len(alerts(h, f"event:{c.eid}:replace_waiting")) == 1            # 같은 시간대 중복 억제
    assert h.store.event(c.eid)["state"] == "OPEN" and h.pos(c.pid)["remaining_managed_qty"] == 10


def test_waiting_persists_across_restart_and_hour_dedupe(tmp_path, monkeypatch):
    c = Case(tmp_path, monkeypatch, "REPLACE", "LIVE")
    h = c.h
    f = Fault(h, monkeypatch, "view")
    f.apply()
    first = h.clock()
    c.run()
    h.store.release_lease("w1", h.clock())
    h.engine = h.new_engine("w2")                                   # 재시작
    h.clock.advance(30); c.run()
    assert waiting(h, c.eid)["since"] == first
    assert len(alerts(h, f"event:{c.eid}:replace_waiting")) == 1
    h.clock.advance(3600); c.run()                                  # 다음 시간대: 새 알림 1건 (기존 정책)
    assert len(alerts(h, f"event:{c.eid}:replace_waiting")) == 2
    assert waiting(h, c.eid)["since"] == first


@pytest.mark.parametrize("fault", FAULTS)
def test_replace_after_journal_heals(tmp_path, monkeypatch, fault):
    c = Case(tmp_path, monkeypatch, "REPLACE", "LIVE")
    h = c.h
    f = Fault(h, monkeypatch, fault)
    f.apply()
    c.run(); h.clock.advance(30); c.run()
    n_events = len(h.store.events())
    f.heal()
    h.clock.advance(30)
    c.run()
    assert c.count() == c.before + 1                                # 모든 안전 조건 재검증 후 재주문
    new = h.orders()[-1]
    assert new["kind"] == "REPLACE" and new["state"] == "SUBMITTED"
    assert waiting(h, c.eid) is None
    assert len(h.store.events()) == n_events and h.store.event(c.eid)["state"] == "OPEN"   # 새 이벤트 없음


def test_other_events_reconcile_and_common_fault_blocks_new_orders(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, price_interval_sec=60)
    h.broker.quote_age = 5
    for t in ("U", "V"):
        h.broker.holding[t] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
    t_id = h.add(ticker="T")
    u_id = h.add(ticker="U")
    h.broker.prices.update({"T": Decimal("44"), "U": Decimal("44"), "V": Decimal("50")})
    h.tick()
    h.broker.prices.update({"T": Decimal("40"), "U": Decimal("40")})
    h.clock.advance(30); h.tick()
    h.clock.advance(301); h.tick()                                  # T·U 재주문 목적 취소 요청
    num = {o["ticker"]: o["order_number"] for o in h.orders()}
    h.broker.fill(num["T"], 0, state="CANCELLED")                   # T: 취소 확정
    h.broker.fill(num["U"], 10)                                     # U: 취소 전에 전량 체결
    v_id = h.add(ticker="V")
    h.broker.prices["V"] = Decimal("44")                            # V: 신규 트리거
    sells = len(h.broker.sells)
    f = Fault(h, monkeypatch, "view")
    f.apply()
    h.clock.advance(30)
    assert h.tick()["status"] == "OK"
    assert len(h.broker.sells) == sells                             # 공통 장애: 모든 신규 매도 차단
    assert h.pos(u_id)["state"] == "SOLD"                           # 다른 이벤트 대조·종료 판정 계속
    assert waiting(h, h.event(t_id)["id"])["reason"].startswith("scanner_journal_unreadable")
    v_ev = h.event(v_id)
    assert v_ev["state"] == "OPEN" and h.store.runtime_get(f"order_block:{v_ev['id']}")


def test_in_flight_on_one_ticker_does_not_block_other_ticker(tmp_path, monkeypatch):
    from tests.test_auto_stop_gate_matrix import scanner_row
    h = Harness(tmp_path, monkeypatch, price_interval_sec=60)
    h.broker.quote_age = 5
    h.broker.holding["U"] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
    t_id, u_id = h.add(ticker="T"), h.add(ticker="U")
    scanner_row(h.st.journal_path, state="UNKNOWN", ticker="T")
    h.broker.price = Decimal("44")
    h.tick()
    assert [s["ticker"] for s in h.broker.sells] == ["U"]
    assert h.store.runtime_get(f"order_block:{h.event(t_id)['id']}")["reason"].startswith("scanner_buy_in_flight")


# ── STEP 1: 재주문 결과 판정 (주문 행 생성 ≠ 접수) ─────────────────
@pytest.mark.parametrize("exc,state,cleared", [
    (None, "SUBMITTED", True),
    (OrderUnknown("timeout"), "UNKNOWN", False),
    (OrderRejected("[1: 거부]"), "REJECTED", False),
    (KiwoomError("입력 오류"), "NOT_SENT", False),
])
def test_replace_waiting_follows_real_order_state(tmp_path, monkeypatch, exc, state, cleared):
    c = Case(tmp_path, monkeypatch, "REPLACE", "LIVE")
    h = c.h
    f = Fault(h, monkeypatch, "view")
    f.apply()
    c.run()                                                         # 대기 시작
    f.heal()
    h.broker.sell_exc = exc
    h.clock.advance(30); c.run()
    new = h.orders()[-1]
    assert new["kind"] == "REPLACE" and new["state"] == state
    w = waiting(h, c.eid)
    if cleared:
        assert w is None
    else:
        assert w is not None and w["outcome"] == state.lower()      # 접수 확인 전에는 대기 기록을 지우지 않는다


# ═════════════════════ STEP 2: 장중 감시 종료 포지션의 상태 근거 ═════════════════════
from tests.test_auto_stop_review4 import DAY, legacy_pos, pos_of, sh, ticks  # noqa: E402
from trading.auto_stop import service  # noqa: E402

CLOSE_TICK = ts(2026, 11, 23, 16, 5)
NEXT_DAY = ts(2026, 11, 24, 10, 0)


def watched(h, ticker="CINF", at=ts(2026, 11, 23, 10, 0), n=4):
    h.broker.holding[ticker] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
    h.clock.set(at)
    pid = h.add(mode="SHADOW", ticker=ticker)
    ticks(h, n)                                                     # 마지막 정상 감시까지
    return pid


def close_day(h):
    h.clock.set(CLOSE_TICK)
    h.tick()


def view(h, pid, now=NEXT_DAY):
    rep = service.daily_report(h.store, DAY, "SHADOW", h.st, now=now)
    p = pos_of(rep, pid)
    g, G = p["gaps"], rep["watch"]["gaps"]
    return {"max": g["max_sec"], "normal": g["max_normal_sec"], "measurable": g["measurable"],
            "normal_cov": G["normal_coverage"], "cov": G["coverage"], "end": p["window"]["end"],
            "quality": p["data_quality"], "rep": rep}


def evidence(h, pid):
    return next((r["value"] for r in h.store.stats(DAY, "SHADOW")
                 if r["metric"] == "state_audit" and r["key"] == str(pid)), None)


def test_codex_disable_300s_after_unaudited_pause(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = watched(h)
    last_ok = h.clock() - 30
    h.store.update_position(pid, h.clock(), auto_paused=1)          # 감사 기록 없는 일시중지
    service.disable(h.store, pid, now=last_ok + 300)                # 300초 시점 관리 종료
    close_day(h)
    v = view(h, pid)
    assert v["max"] == pytest.approx(300, abs=31)                   # 실제 공백은 측정 유지
    assert v["normal"] is None and v["measurable"] is False
    assert v["normal_cov"] == "none" and evidence(h, pid) == 0
    assert any(q.startswith("state_unaudited") for q in v["quality"])
    summary = [a for a in h.store.recent_alerts(500) if a["dedup_key"].endswith(":summary")]
    assert summary and summary[0]["severity"] == "WARN"            # 불완전 품질을 정상(INFO)으로 표시하지 않음


def test_normal_disable_keeps_normal_measurement(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = watched(h)
    service.disable(h.store, pid, now=h.clock())
    close_day(h)
    v = view(h, pid)
    assert evidence(h, pid) == 1 and v["measurable"] is True and v["normal_cov"] == "full"
    assert v["normal"] is not None and v["normal"] <= 61


def test_disable_during_audited_pause(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = watched(h)
    service.set_paused(h.store, pid, True, now=h.clock())
    h.clock.advance(300)
    service.disable(h.store, pid, now=h.clock())
    close_day(h)
    v = view(h, pid)
    assert evidence(h, pid) == 1 and v["measurable"] is True
    assert v["max"] >= 300 and v["normal"] <= 61                    # 일시중지 공백은 정상 상태가 아니다


@pytest.mark.parametrize("state", ["symbol_fault", "global_hold"])
def test_disable_during_fault_or_hold(tmp_path, monkeypatch, state):
    h = sh(tmp_path, monkeypatch)
    pid = watched(h)
    at = h.clock()
    if state == "symbol_fault":
        h.store.audit(at, "engine", "quote_fault", "SHADOW:CINF", {"position_id": pid})
        health = h.store.runtime_get("quote_health:SHADOW")
        health[str(pid)]["state"] = "FAULT"
        h.store.runtime_set("quote_health:SHADOW", health, at)
    else:
        h.store.audit(at, "engine", "global_hold", "SHADOW", {"from": [], "to": ["quote_system"]})
    service.disable(h.store, pid, now=at + 300)
    close_day(h)
    v = view(h, pid)
    assert evidence(h, pid) == 1 and v["measurable"] is True
    assert v["max"] >= 300 and v["normal"] <= 61


def test_needs_review_ends_watch_at_review_time(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = watched(h)
    h.store.update_position(pid, h.clock(), auto_paused=1)          # 감사 기록 없는 상태 변경 후
    h.broker.holding["CINF"]["qty"] = 4                             # 원인 불명 수량 감소 → NEEDS_REVIEW
    review_at = h.clock()
    h.tick()
    assert h.pos(pid)["state"] == "NEEDS_REVIEW"
    close_day(h)
    v = view(h, pid)
    assert v["end"] == pytest.approx(review_at, abs=1)              # 감시 종료 = 확인 필요 전환 시각 (장 마감 아님)
    assert evidence(h, pid) == 0 and v["normal"] is None and v["max"] < 120


def test_trigger_records_evidence_at_watch_end(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = watched(h)
    h.broker.price = Decimal("44")                                  # 손절가 45 이하
    h.tick()
    assert h.event(pid)["state"] in ("OPEN", "DONE")
    assert evidence(h, pid) == 1                                    # 마감 전, 트리거 시점에 기록
    close_day(h)
    v = view(h, pid)
    assert v["measurable"] is True and v["end"] == pytest.approx(h.event(pid)["triggered_at"], abs=1)


def test_done_marker_without_position_evidence_is_not_measured(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = watched(h)
    service.disable(h.store, pid, now=h.clock())
    close_day(h)
    db = sqlite3.connect(h.st.journal_path)                         # 이전 버전처럼 포지션별 근거 없이 완료 표식만
    db.execute("DELETE FROM auto_stop_daily_stats WHERE metric='state_audit'")
    db.commit(); db.close()
    v = view(h, pid)
    assert any(r["metric"] == "state_audit_done" for r in h.store.stats(DAY, "SHADOW"))
    assert v["normal"] is None and v["normal_cov"] == "none" and v["max"] is not None
    assert any(q.startswith("state_evidence_missing") for q in v["quality"])


def test_requery_and_restart_keep_quality(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = watched(h)
    h.store.update_position(pid, h.clock(), auto_paused=1)
    service.disable(h.store, pid, now=h.clock() + 300)
    h.store.release_lease("w1", h.clock())
    h.engine = h.new_engine("w2")                                   # 마감 처리 전 재시작
    close_day(h)
    keyf = lambda v: (v["max"], v["normal"], v["measurable"], v["normal_cov"])  # noqa: E731
    first = keyf(view(h, pid, now=ts(2026, 11, 23, 16, 10)))
    for now in (ts(2026, 11, 23, 22, 0), NEXT_DAY, ts(2026, 11, 30, 10, 0)):
        assert keyf(view(h, pid, now=now)) == first
    assert first[1] is None and first[3] == "none"


def test_live_day_ended_position_not_judged_by_current_state(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = watched(h)
    h.store.update_position(pid, h.clock(), auto_paused=1)
    service.disable(h.store, pid, now=h.clock() + 300)
    v = view(h, pid, now=h.clock() + 600)                           # 마감 처리 전 당일
    assert v["normal"] is None and evidence(h, pid) == 0


# ═════════════════════ STEP 3: 전체 기대·성공 집계 ═════════════════════
def mixed_mismatch(h):
    pid = legacy_pos(h, "CINF", ts(2026, 11, 23, 9, 58), 0, 0, end_state=None)
    h.clock.set(ts(2026, 11, 23, 10, 0))
    ticks(h, 12)
    db = sqlite3.connect(h.st.journal_path)                         # 테스트 저널: 일계 < 신버전 상세
    db.execute("UPDATE auto_stop_daily_stats SET value=1 WHERE metric='quote_ok' AND key='CINF'")
    db.commit(); db.close()
    return pid


def test_codex_only_mixed_position_expected_kept(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = mixed_mismatch(h)
    rep = service.daily_report(h.store, DAY, "SHADOW", h.st, now=h.clock())
    p, w = pos_of(rep, pid), rep["watch"]
    assert p["format"] == "MIXED" and p["ok_checks"] is None and p["expected_checks"] == 8
    assert (w["expected"], w["ok"], w["missed"], w["normal_rate_pct"], w["ok_coverage"]) == (8, None, None, None, "none")
    assert w["ok_status"] == "unmeasured"


def test_partial_measurement_keeps_expected_and_nulls_totals(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    h.broker.holding["EVRG"] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
    pid = legacy_pos(h, "CINF", ts(2026, 11, 23, 9, 58), 0, 0, end_state=None)
    h.clock.set(ts(2026, 11, 23, 10, 0))
    other = h.add(mode="SHADOW", ticker="EVRG")
    ticks(h, 12)
    db = sqlite3.connect(h.st.journal_path)
    db.execute("UPDATE auto_stop_daily_stats SET value=1 WHERE metric='quote_ok' AND key='CINF'")
    db.commit(); db.close()
    rep = service.daily_report(h.store, DAY, "SHADOW", h.st, now=h.clock())
    w, a, b = rep["watch"], pos_of(rep, pid), pos_of(rep, other)
    assert w["expected"] == a["expected_checks"] + b["expected_checks"]
    assert (w["ok"], w["missed"], w["normal_rate_pct"]) == (None, None, None)
    assert w["ok_status"] == "partial" and w["ok_coverage"] == "partial"
    sub = w["measured_subtotal"]
    assert (sub["positions"], sub["expected"], sub["ok"]) == (1, b["expected_checks"], b["ok_checks"])
    text = service.report_text(rep)
    assert "정상 감시 측정 불가/" in text and "누락 측정 불가 (측정 가능 1/2종목 소계" in text and "None" not in text


def test_no_targets_vs_zero_success_vs_full(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    h.clock.set(ts(2026, 11, 23, 10, 0)); h.tick()
    w = service.daily_report(h.store, DAY, "SHADOW", h.st, now=h.clock())["watch"]
    assert w["ok_status"] == "no_targets" and (w["expected"], w["ok"]) == (0, 0) and w["normal_rate_pct"] is None
    h.broker.holding["CINF"] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
    pid = h.add(mode="SHADOW", ticker="CINF")
    h.broker.quote_exc_by["CINF"] = KiwoomError("timeout")          # 실제 성공 0회
    ticks(h, 6)
    w = service.daily_report(h.store, DAY, "SHADOW", h.st, now=h.clock())["watch"]
    p = pos_of({"watch": w}, pid)
    assert p["ok_checks"] == 0 and w["ok"] == 0 and w["missed"] == w["expected"] > 0
    assert w["ok_status"] == "full" and w["api_errors"] > 0


def test_legacy_same_ticker_group_counted_once(tmp_path, monkeypatch):
    from tests.test_auto_stop_review3 import legacy_day
    h = sh(tmp_path, monkeypatch)
    day, c, e, e2, rows = legacy_day(h, extra_same_ticker=True)
    for metric, key, n in rows:
        h.store.bump(day, "SHADOW", metric, key, n)
    w = service.daily_report(h.store, day, "SHADOW", h.st, now=ts(2026, 11, 25, 17, 0))["watch"]
    assert w["ok"] == 73 and w["ok_status"] == "full" and w["ok_ticker_aggregated_positions"] == 2
    assert w["legacy_ticker_totals"]["EVRG"]["ok"] == 20 and w["legacy_ticker_totals"]["EVRG"]["quote_fail"] == 4
    assert w["api_errors"] == 6                                      # 그룹은 한 번만


# ═════════════════════ STEP 4: LEGACY 오류 통계 ═════════════════════
def nov25(h):
    from tests.test_auto_stop_review3 import legacy_day
    day, c, e, _, rows = legacy_day(h)
    for metric, key, n in rows:
        h.store.bump(day, "SHADOW", metric, key, n)
    return service.daily_report(h.store, day, "SHADOW", h.st, now=ts(2026, 11, 25, 17, 0)), c, e


def test_nov25_legacy_error_stats(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    rep, c, e = nov25(h)
    w = rep["watch"]
    assert (w["ok"], w["expected"], w["missed"]) == (73, 734, 661)
    assert w["api_errors"] == 6 and rep["api"]["errors"] == 6
    assert (pos_of(rep, c)["quote_fail"], pos_of(rep, e)["quote_fail"]) == (2, 4)
    assert w["attempts"] is None and w["gaps"]["max_sec"] is None
    assert all(v is None for k, v in rep["recovery"].items() if k != "coverage")   # 저널에 없는 246회는 추가하지 않음
    assert rep["api"]["http_requests"] is None and rep["holds"]["held_triggers"] is None
    text = service.report_text(rep)
    assert "API 오류 6" in text and "일반 감시 시도 측정 불가" in text and "회복 조회 측정 불가/측정 불가" in text
    assert "None" not in text


def test_mixed_failure_stats_not_double_counted(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = legacy_pos(h, "CINF", ts(2026, 11, 23, 9, 30), 0, 10, end_state=None)
    h.store.bump(DAY, "SHADOW", "quote_fail", "CINF", 3)            # 구버전 기간 실패 3회
    h.clock.set(ts(2026, 11, 23, 10, 0))
    h.broker.exc_seq = [KiwoomError("timeout"), None, KiwoomError("timeout")]
    ticks(h, 6)
    rep = service.daily_report(h.store, DAY, "SHADOW", h.st, now=h.clock())
    p = pos_of(rep, pid)
    new = int(sum(r["value"] for r in h.store.stats(DAY, "SHADOW")
                  if r["metric"] == "pos_watch_fail" and r["key"] == str(pid)))
    ticker_total = int(sum(r["value"] for r in h.store.stats(DAY, "SHADOW")
                           if r["metric"] == "quote_fail" and r["key"] == "CINF"))
    assert new > 0 and ticker_total == 3 + new                     # 신버전은 티커 일계·포지션 상세를 함께 올린다
    assert p["quote_fail"] == 3 + new and rep["watch"]["api_errors"] == ticker_total


def test_failure_aggregate_mismatch_is_null(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = legacy_pos(h, "CINF", ts(2026, 11, 23, 9, 30), 0, 10, end_state=None)
    h.clock.set(ts(2026, 11, 23, 10, 0))
    h.broker.exc_seq = [KiwoomError("timeout"), KiwoomError("timeout")]
    ticks(h, 6)
    db = sqlite3.connect(h.st.journal_path)
    db.execute("UPDATE auto_stop_daily_stats SET value=0 WHERE metric='quote_fail' AND key='CINF'")
    db.commit(); db.close()
    rep = service.daily_report(h.store, DAY, "SHADOW", h.st, now=h.clock())
    assert pos_of(rep, pid)["quote_fail"] is None and rep["watch"]["api_errors"] is None


# ═════════════════════ STEP 5: API · Slack · 웹 표시 계약 ═════════════════════
NULLABLE_TEXT = {("watch", "ok"): "정상 감시 측정 불가/", ("watch", "attempts"): "일반 감시 시도 측정 불가",
                 ("watch", "api_errors"): "API 오류 측정 불가", ("watch", "invalid"): "무효 측정 불가",
                 ("recovery", "attempts"): "회복 조회 ", ("api", "http_requests"): "HTTP 요청 측정 불가"}


def _layers_consistent(rep):
    text = service.report_text(rep)
    assert "None" not in text
    for (sec, key), phrase in NULLABLE_TEXT.items():
        if rep[sec][key] is None:
            assert phrase in text and "측정 불가" in text, (sec, key)
    if rep["watch"]["missed"] is None:
        assert "누락 측정 불가" in text
    return text


@pytest.mark.parametrize("fmt", ["LEGACY", "MIXED", "SLOT_V2"])
def test_report_layers_agree_per_format(tmp_path, monkeypatch, fmt):
    h = sh(tmp_path, monkeypatch)
    if fmt == "LEGACY":
        rep, _, _ = nov25(h)
    elif fmt == "MIXED":
        mixed_mismatch(h)
        rep = service.daily_report(h.store, DAY, "SHADOW", h.st, now=h.clock())
    else:
        watched(h, at=ts(2026, 11, 23, 9, 25), n=24)                # 개장 전부터 신버전이 기록한 날
        rep = service.daily_report(h.store, DAY, "SHADOW", h.st, now=h.clock())
    assert rep["format"] == fmt
    text = _layers_consistent(rep)
    if fmt == "SLOT_V2":
        assert rep["watch"]["ok"] is not None and rep["api"]["http_requests"] is not None
        assert "측정 불가/" not in text.split("정상 감시 ")[1][:12]


def test_web_template_has_no_zero_default_for_evaluation_metrics():
    html = open("web/templates/index.html", encoding="utf-8").read()
    seg = html[html.index('data-testid="report-recovery"') - 2000: html.index('data-testid="report-gaps"') + 1500]
    for expr in ("report.watch.ok ", "report.watch.attempts", "report.watch.api_errors", "report.watch.invalid",
                 "report.recovery?.ok", "report.recovery?.attempts", "report.recovery?.fail",
                 "report.holds?.held_triggers", "report.api.http_requests", "report.api.http_429",
                 "w.recovery?.ok", "w.ok_checks"):
        assert expr in seg, expr
    for bad in ("report.recovery?.ok ?? 0", "report.recovery?.fail ?? 0", "report.holds?.held_triggers ?? 0",
                "report.api.http_requests ?? 0", "w.ok_checks ?? '-'", "report.watch.ok + '/'"):
        assert bad not in html, bad
