"""자동 손절 상태 머신 (FakeBroker + 가짜 시계 + 임시 SQLite). 실계좌 주문 없음."""
from decimal import Decimal

import pytest

from tests.auto_stop_fakes import FakeBroker, Harness, NoOrderReader, ts
from trading.auto_stop.brokers import ShadowBroker
from trading.auto_stop.store import ClaimRefused, LeaseLost
from trading.kiwoom_orders import OrderRejected, OrderUnknown


@pytest.fixture
def h(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


def trigger(h, price="44.00"):
    h.broker.price = Decimal(price)
    h.tick()


# ── 기본 트리거·가격 ─────────────────────────────────────────────
def test_above_stop_sends_nothing(h):
    pid = h.add()
    h.tick(3)
    assert h.broker.sells == [] and h.pos(pid)["state"] == "ACTIVE"


def test_below_stop_sends_one_limit_order_floor_cent(h):
    pid = h.add()
    trigger(h, "44.00")
    assert len(h.broker.sells) == 1
    s = h.broker.sells[0]
    assert s["qty"] == 10 and s["price"] == Decimal("43.78")        # 44 × 0.995 = 43.78 (내림)
    o = h.orders()[0]
    assert o["state"] == "SUBMITTED" and o["kind"] == "ORIGINAL" and o["attempt_no"] == 0
    assert o["order_date"] == "2026-11-23" and o["ref_source"] == "CUR"
    assert h.pos(pid)["state"] == "EXITING"
    h.tick(5)
    assert len(h.broker.sells) == 1                                  # 미확정 주문 있으면 신규 없음


def test_event_survives_price_recovery(h):
    pid = h.add()
    trigger(h)
    h.broker.price = Decimal("60")
    h.tick(3)
    assert h.event(pid)["state"] == "OPEN" and h.pos(pid)["state"] == "EXITING"


def test_full_fill_marks_sold_and_event_done(h):
    pid = h.add()
    trigger(h)
    h.broker.fill("1000", 10)
    h.tick()
    assert h.orders()[0]["state"] == "FILLED"
    p = h.pos(pid)
    assert p["state"] == "SOLD" and p["remaining_managed_qty"] == 0 and p["total_filled_qty"] == 10
    assert h.event(pid)["state"] == "DONE"


# ── 취소 확정 후 재주문 ─────────────────────────────────────────
def test_no_replace_before_cancel_confirmed_then_replace_remaining_at_fresh_price(h):
    pid = h.add()
    trigger(h, "44.00")
    h.broker.fill("1000", 3)                    # 부분 체결
    h.tick()
    assert h.orders()[0]["state"] == "PARTIAL_FILLED"
    h.clock.advance(301)
    h.tick()
    assert h.broker.cancels == ["1000"] and h.orders()[0]["state"] == "CANCEL_PENDING"
    for _ in range(4):                          # 취소 확정 전: 신규 매도 없음
        h.clock.advance(30)
        h.tick()
    assert len(h.broker.sells) == 1
    h.broker.fill("1000", 4, state="CANCELLED")  # 취소 응답 사이 추가 체결 1주
    h.broker.price = Decimal("40.00")
    h.tick()
    assert h.orders()[0]["state"] == "CANCELLED" and h.orders()[0]["filled_qty"] == 4
    assert len(h.broker.sells) == 2
    r = h.broker.sells[1]
    assert r["qty"] == 6 and r["price"] == Decimal("39.80")         # 잔여만, 최신 시세 기준
    o2 = h.orders()[1]
    assert o2["kind"] == "REPLACE" and o2["parent_order_id"] == h.orders()[0]["id"]
    ev = h.event(pid)
    assert ev["session_replace_count"] == 1 and ev["event_replace_count"] == 1
    assert h.pos(pid)["remaining_managed_qty"] == 6


def test_replace_disabled_only_alerts_no_cancel(h):
    h.set(replace_enabled=False)
    h.add()
    trigger(h)
    h.clock.advance(400)
    h.tick(2)
    assert h.broker.cancels == [] and any(k.endswith(":stale") for k in h.alert_keys())


def test_session_replace_limit_goes_review_but_keeps_tracking(h):
    h.set(max_replaces_per_session=1)
    pid = h.add()
    trigger(h)
    h.clock.advance(301); h.tick()                         # 취소 요청
    h.broker.fill("1000", 0, state="CANCELLED"); h.tick()  # 확정 → 재주문 1회
    assert len(h.broker.sells) == 2
    h.clock.advance(301); h.tick()                         # 한도 도달
    assert h.event(pid)["state"] == "NEEDS_REVIEW"
    assert len(h.broker.cancels) == 1 and len(h.broker.sells) == 2
    assert h.orders()[1]["state"] == "SUBMITTED"
    h.broker.fill("1001", 10); h.tick()                    # 기존 주문 대조는 계속
    assert h.orders()[1]["state"] == "FILLED" and h.pos(pid)["state"] == "SOLD"


def test_event_replace_counter_survives_new_session(h):
    h.set(max_replaces_per_session=3, max_replaces_per_event=1, max_carry_over_sessions=2)
    pid = h.add()
    trigger(h)
    h.clock.advance(301); h.tick()
    h.broker.fill("1000", 0, state="CANCELLED"); h.tick()
    assert h.event(pid)["event_replace_count"] == 1
    h.clock.set(ts(2026, 11, 24, 10, 0))                   # 다음 날: 세션 카운터만 초기화
    h.tick()
    ev = h.event(pid)
    assert ev["session_replace_count"] == 0 and ev["event_replace_count"] == 1
    h.clock.advance(301); h.tick()
    assert h.event(pid)["state"] == "NEEDS_REVIEW" and len(h.broker.cancels) == 1


# ── 거부·UNKNOWN ────────────────────────────────────────────────
def test_sell_rejected_goes_review_without_repeat(h):
    pid = h.add()
    h.broker.sell_exc = OrderRejected("[1: 거부]")
    trigger(h)
    h.tick(3)
    assert h.orders()[0]["state"] == "REJECTED" and len(h.orders()) == 1
    assert h.event(pid)["state"] == "NEEDS_REVIEW" and h.pos(pid)["state"] == "NEEDS_REVIEW"


def test_sell_unknown_is_never_resent(h):
    pid = h.add()
    h.broker.sell_exc = OrderUnknown("timeout")
    trigger(h)
    h.broker.sell_exc = None
    h.clock.advance(600)
    h.tick(5)
    assert [o["state"] for o in h.orders()] == ["UNKNOWN"]
    assert any(k.endswith(":unknown") for k in h.alert_keys())
    assert h.pos(pid)["state"] == "EXITING"


def test_cancel_unknown_no_retry_then_confirmed_cancel_allows_replace(h):
    h.add()
    trigger(h)
    h.broker.cancel_exc = OrderUnknown("lost")
    h.clock.advance(301); h.tick()
    assert h.orders()[0]["state"] == "CANCEL_UNKNOWN"
    h.broker.cancel_exc = None
    h.clock.advance(400); h.tick(3)
    assert h.broker.cancels == [] and len(h.broker.sells) == 1
    h.broker.fill("1000", 0, state="CANCELLED"); h.tick()
    assert h.orders()[0]["state"] == "CANCELLED" and len(h.broker.sells) == 2


def test_cancel_rejected_but_filled_proceeds(h):
    pid = h.add()
    trigger(h)

    def reject_and_fill(*a):
        h.broker.fill("1000", 10)
        raise OrderRejected("[2: 이미 체결]")
    h.broker.cancel = reject_and_fill
    h.clock.advance(301); h.tick()
    o = h.orders()[0]
    assert o["state"] == "FILLED" and o["cancel_rejected"] == 1
    assert h.pos(pid)["state"] == "SOLD"


def test_cancel_rejected_still_open_goes_review_and_never_cancels_again(h):
    pid = h.add()
    trigger(h)
    calls = []

    def reject(*a):
        calls.append(a)
        raise OrderRejected("[3: 거부]")
    h.broker.cancel = reject
    h.clock.advance(301); h.tick()
    assert h.event(pid)["state"] == "NEEDS_REVIEW"
    h.clock.advance(600); h.tick(3)
    assert len(calls) == 1 and h.orders()[0]["cancel_rejected"] == 1
    assert h.orders()[0]["state"] == "SUBMITTED"


def test_externally_cancelled_order_goes_review(h):
    pid = h.add()
    trigger(h)
    h.broker.fill("1000", 0, state="CANCELLED")
    h.tick(2)
    assert h.event(pid)["state"] == "NEEDS_REVIEW" and len(h.broker.sells) == 1


# ── 재시작·lease·동시성 ─────────────────────────────────────────
def _claim(h, eid, owner="w1", **kw):
    args = dict(owner=owner, event_id=eid, kind="ORIGINAL", qty=10, ref_price=Decimal("44"), ref_source="CUR",
                quote_as_of=h.clock(), limit_price=Decimal("43.78"), settings=h.st, guard=lambda: None)
    args.update(kw)
    return h.store.claim_sell(h.clock(), **args)


def _open_event(h, pid):
    return h.store.create_event(h.clock(), h.pos(pid), state="OPEN", trigger_price=Decimal("44"),
                                trigger_as_of=h.clock(), session_date="2026-11-23")


def test_restart_converts_stale_sending_to_unknown_without_resend(h):
    pid = h.add()
    eid = _open_event(h, pid)
    h.store.acquire_lease("dead", h.clock(), 90)
    o = _claim(h, eid, owner="dead")
    h.store.claim_cancel  # (참조만)
    h.clock.advance(91)                       # 이전 작업자 lease 만료
    h.engine = h.new_engine("w2")
    h.broker.price = Decimal("44")
    h.tick(3)
    assert h.store.order(o["id"])["state"] == "UNKNOWN" and h.broker.sells == []


def test_restart_converts_cancel_sending_to_cancel_unknown(h):
    h.add()
    trigger(h)
    o = h.orders()[0]
    h.store.acquire_lease("dead", h.clock() + 1, 90) if False else None
    h.store.transition(o["id"], h.clock(), frm="SUBMITTED", to="CANCEL_SENDING")
    h.tick()
    assert h.store.order(o["id"])["state"] == "CANCEL_UNKNOWN" and h.broker.cancels == []


def test_lease_busy_blocks_second_runner(h):
    h.add()
    h.store.acquire_lease("other", h.clock(), 90)
    h.broker.price = Decimal("44")
    assert h.tick()["status"] == "LEASE_BUSY" and h.broker.sells == []
    h.clock.advance(91)
    assert h.tick()["status"] == "OK" and len(h.broker.sells) == 1


def test_claim_without_lease_is_refused(h):
    pid = h.add()
    eid = _open_event(h, pid)
    with pytest.raises(LeaseLost):
        _claim(h, eid, owner="nobody")


def test_double_claim_same_event_only_one_sending(h):
    pid = h.add()
    eid = _open_event(h, pid)
    h.store.acquire_lease("w1", h.clock(), 90)
    _claim(h, eid)
    with pytest.raises(ClaimRefused):
        _claim(h, eid)
    assert len(h.orders()) == 1


def test_claim_guard_rechecks_switch_inside_transaction(h):
    pid = h.add()
    eid = _open_event(h, pid)
    h.store.acquire_lease("w1", h.clock(), 90)
    with pytest.raises(ClaimRefused):
        _claim(h, eid, guard=lambda: "ACCOUNT1_ORDER_ENABLED가 꺼져 있습니다.")
    assert h.orders() == []


def test_late_response_after_lease_loss_only_records_evidence(h):
    pid = h.add()
    h.broker.price = Decimal("44")
    original_sell = h.broker.sell

    def slow_sell(*a):
        # 응답 전에 다른 작업자가 lease 를 가져가 SENDING 을 UNKNOWN 으로 처리
        h.store.release_lease("w1", h.clock())
        h.store.acquire_lease("w2", h.clock(), 90)
        o = h.store.orders(states=("SENDING",))[0]
        h.store.transition(o["id"], h.clock(), frm="SENDING", to="UNKNOWN")
        return original_sell(*a)
    h.broker.sell = slow_sell
    h.tick()
    o = h.orders()[0]
    assert o["state"] == "UNKNOWN" and "late_response" in o["details"]


# ── 수량 ────────────────────────────────────────────────────────
def test_fill_ledger_is_idempotent_and_decrease_is_anomaly(h):
    pid = h.add()
    trigger(h)
    oid = h.orders()[0]["id"]
    assert h.store.apply_fill(oid, h.clock(), 4, {}) == "APPLIED"
    assert h.store.apply_fill(oid, h.clock(), 4, {}) == "NOOP"
    assert h.pos(pid)["remaining_managed_qty"] == 6
    assert h.store.apply_fill(oid, h.clock(), 3, {}) == "ANOMALY"
    assert h.pos(pid)["remaining_managed_qty"] == 6


def test_order_filled_but_managed_remaining_sends_replace_for_rest(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    h.broker.holding["T"]["sellable"] = 6                    # 매도가능 6 < 관리 10
    pid = h.add()
    trigger(h)
    assert h.broker.sells[0]["qty"] == 6
    h.broker.holding["T"]["sellable"] = 10
    h.broker.fill("1000", 6)
    h.tick()
    p = h.pos(pid)
    assert p["state"] == "EXITING" and p["remaining_managed_qty"] == 4
    assert len(h.broker.sells) == 2 and h.broker.sells[1]["qty"] == 4
    assert h.orders()[1]["kind"] == "REPLACE"


def test_filled_with_remaining_and_replace_disabled_goes_review(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, replace_enabled=False)
    h.broker.holding["T"]["sellable"] = 6
    pid = h.add()
    trigger(h)
    h.broker.fill("1000", 6)
    h.tick()
    assert h.event(pid)["state"] == "NEEDS_REVIEW" and len(h.broker.sells) == 1


def test_sellable_zero_goes_review(h):
    h.broker.holding["T"]["sellable"] = 0
    pid = h.add()
    trigger(h)
    assert h.broker.sells == [] and h.event(pid)["state"] == "NEEDS_REVIEW"


def test_external_full_sale_closes_and_no_revival(h):
    pid = h.add()
    h.broker.holding["T"]["qty"] = 0
    h.tick()
    assert h.pos(pid)["state"] == "EXTERNAL_CLOSED"
    h.broker.holding["T"]["qty"] = 10
    h.broker.price = Decimal("40")
    h.tick(2)
    assert h.pos(pid)["state"] == "EXTERNAL_CLOSED" and h.broker.sells == []


def test_external_partial_sale_goes_review(h):
    pid = h.add()
    h.broker.holding["T"]["qty"] = 5
    h.tick()
    assert h.pos(pid)["state"] == "NEEDS_REVIEW"
    h.broker.price = Decimal("40")
    h.tick()
    assert h.broker.sells == []


def test_extra_buy_is_ignored(h):
    pid = h.add()
    h.broker.holding["T"].update(qty=20, sellable=20, avg_cost=Decimal("58"))
    trigger(h)
    assert h.broker.sells[0]["qty"] == 10 and h.pos(pid)["initial_managed_qty"] == 10


def test_split_suspected_goes_review(h):
    pid = h.add()
    h.broker.holding["T"].update(qty=20, sellable=20, avg_cost=Decimal("30"))
    h.tick()
    assert h.pos(pid)["state"] == "NEEDS_REVIEW" and h.broker.sells == []


def test_order_caps_go_review(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, max_order_qty=5)
    pid = h.add()
    trigger(h)
    assert h.broker.sells == [] and h.event(pid)["state"] == "NEEDS_REVIEW"


def test_sub_dollar_reference_goes_review(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    pid = h.add(stop="1.00")
    trigger(h, "0.95")
    assert h.broker.sells == [] and h.event(pid)["state"] == "NEEDS_REVIEW"


# ── 모드 ────────────────────────────────────────────────────────
def test_off_mode_no_quotes_no_orders_but_reconciles_live_orders(h):
    pid = h.add()
    trigger(h)
    h.set(mode="OFF")
    h.broker.fill("1000", 10)
    calls = h.broker.quote_calls
    h.tick()
    assert h.orders()[0]["state"] == "FILLED" and h.pos(pid)["state"] == "SOLD"
    pid2 = h.add(ticker="T")
    h.broker.price = Decimal("40")
    h.tick(2)
    assert h.broker.quote_calls == calls and len(h.broker.sells) == 1


def test_shadow_never_calls_real_order_path_and_is_separate_from_live(tmp_path, monkeypatch):
    env = {"KIWOOM_WEB_ENABLED": "true", "AUTO_STOP_US_MODE": "SHADOW"}
    reader = None
    h = Harness(tmp_path, monkeypatch, env=env, mode="SHADOW",
                factory=lambda mode: ShadowBroker(reader, "NEXT_QUOTE", h.clock) if mode == "SHADOW" else reader)
    reader = NoOrderReader(h.clock)          # LIVE 잔고 조회는 허용, 주문·취소 호출 시 실패
    h.broker = reader
    live_pid = h.add(mode="LIVE")                           # 같은 종목 LIVE 포지션은 감시하지 않음
    pid = h.add(mode="SHADOW")
    reader.price = Decimal("44")
    h.tick()
    o = h.orders()[0]
    assert o["mode"] == "SHADOW" and o["order_number"].startswith("V")
    assert h.pos(live_pid)["state"] == "ACTIVE"
    h.tick()                                                # NEXT_QUOTE: 같은 시세로는 체결하지 않음
    assert h.store.order(o["id"])["state"] == "SUBMITTED"
    h.clock.advance(60)                                     # 증권사 시각이 새로운 시세 ≥ 지정가 → 가상 체결
    h.tick()
    assert h.store.order(o["id"])["state"] == "FILLED" and h.pos(pid)["state"] == "SOLD"


def test_live_switch_off_creates_event_but_sends_nothing(h, monkeypatch):
    monkeypatch.setenv("ACCOUNT1_ORDER_ENABLED", "false")
    pid = h.add()
    trigger(h)
    assert h.event(pid)["state"] == "OPEN" and h.broker.sells == []
    assert any(":blocked:" in k for k in h.alert_keys())
    monkeypatch.setenv("ACCOUNT1_ORDER_ENABLED", "true")
    h.tick()
    assert len(h.broker.sells) == 1


def test_paused_position_neither_triggers_nor_sends_but_tracks(h):
    pid = h.add()
    h.store.update_position(pid, h.clock(), auto_paused=1)
    trigger(h)
    assert h.broker.sells == [] and h.pos(pid)["state"] == "ACTIVE"
    h.store.update_position(pid, h.clock(), auto_paused=0)
    h.tick()
    h.store.update_position(pid, h.clock(), auto_paused=1)
    h.broker.fill("1000", 4, state="CANCELLED")              # 외부 취소 → review, 일시중지 중 재주문 없음
    h.clock.advance(400)
    h.tick(2)
    assert len(h.broker.sells) == 1 and h.orders()[0]["filled_qty"] == 4


# ── 시세 ────────────────────────────────────────────────────────
def test_stale_quote_is_not_used(h):
    h.add()
    h.broker.quote_age = 200
    trigger(h)
    assert h.broker.sells == []


def test_quote_from_previous_day_rejected(h):
    h.add()
    h.broker.quote_age = 86400
    trigger(h)
    assert h.broker.sells == []


def test_jump_confirmed_by_requery_proceeds(h):
    pid = h.add()
    h.tick()                                    # 직전 유효값 50
    h.broker.quotes = ["30.00", "30.10", "30.10"]
    h.tick()
    assert h.event(pid)["state"] == "OPEN" and len(h.broker.sells) == 1


def test_jump_not_confirmed_goes_review(h):
    pid = h.add()
    h.tick()
    h.broker.quotes = ["30.00", "50.00"]
    h.tick()
    assert h.pos(pid)["state"] == "NEEDS_REVIEW" and h.broker.sells == []


def test_api_errors_hold_new_entries(tmp_path, monkeypatch):
    from trading.auto_stop.quotes import QuoteUnavailable
    h = Harness(tmp_path, monkeypatch, max_consecutive_errors=2)
    h.add()
    h.broker.quote_exc = QuoteUnavailable("x")
    h.tick(2)
    assert any(k.startswith("health:api_errors") for k in h.alert_keys())
    h.broker.quote_exc = None
    h.broker.price = Decimal("44")
    h.tick()
    assert h.broker.sells == []                 # 시세 오류 보류 중: 신규 진입 없음 (이번 tick 은 회복 확인만)
    h.tick()
    assert len(h.broker.sells) == 1             # 회복 확인 후 정상 진행


# ── 세션·이월 ───────────────────────────────────────────────────
def test_trigger_after_cutoff_carries_over_and_orders_next_session(h):
    pid = h.add()
    h.clock.set(ts(2026, 11, 23, 15, 56))
    trigger(h)
    assert h.event(pid)["state"] == "CARRY_OVER" and h.broker.sells == []
    h.clock.set(ts(2026, 11, 23, 16, 30)); h.tick()
    assert h.broker.sells == []
    h.clock.set(ts(2026, 11, 24, 9, 31)); h.tick()
    ev = h.event(pid)
    assert ev["carry_over_count"] == 1 and ev["session_date"] == "2026-11-24"
    assert len(h.broker.sells) == 1 and h.orders()[0]["kind"] == "ORIGINAL"


def test_no_new_orders_or_replaces_after_cutoff(h):
    h.add()
    h.clock.set(ts(2026, 11, 23, 15, 40))
    trigger(h)
    h.clock.set(ts(2026, 11, 23, 15, 56)); h.tick()
    assert h.broker.cancels == []


def test_expired_after_close_then_carry_over_order_and_carry_limit(h):
    pid = h.add()
    h.set(replace_enabled=False)
    h.clock.set(ts(2026, 11, 23, 15, 40))
    trigger(h)
    h.clock.set(ts(2026, 11, 23, 16, 5))
    h.broker.fill("1000", 2, state="CANCELLED")              # 장 마감 후 소멸
    h.tick()
    assert h.orders()[0]["state"] == "CANCELLED" and h.event(pid)["state"] == "CARRY_OVER"
    assert any(k.endswith(":expired") for k in h.alert_keys())
    h.clock.set(ts(2026, 11, 24, 9, 31)); h.tick()
    o2 = h.orders()[1]
    assert o2["kind"] == "CARRY_OVER" and o2["requested_qty"] == 8
    h.clock.set(ts(2026, 11, 24, 16, 5))
    h.broker.fill("1001", 0, state="CANCELLED")
    h.tick()
    h.clock.set(ts(2026, 11, 25, 9, 31)); h.tick()           # 이월 한도 1회 초과
    assert h.event(pid)["state"] == "NEEDS_REVIEW" and len(h.broker.sells) == 2


def test_order_alive_into_next_day_is_tracked_without_new_order(h):
    pid = h.add()
    h.set(replace_enabled=False)
    h.clock.set(ts(2026, 11, 23, 15, 40))
    trigger(h)
    h.clock.set(ts(2026, 11, 23, 16, 5)); h.tick()
    assert h.event(pid)["state"] == "OPEN"                   # 미확정이라 이월 대기 불가
    h.clock.set(ts(2026, 11, 24, 9, 31)); h.tick()
    ev = h.event(pid)
    assert ev["carry_over_count"] == 1 and len(h.broker.sells) == 1
    assert any(":overnight:" in k for k in h.alert_keys())


def test_holiday_no_quotes(h):
    h.add()
    h.clock.set(ts(2026, 11, 26, 11, 0))                     # 추수감사절
    h.broker.price = Decimal("40")
    h.tick()
    assert h.broker.quote_calls == 0


# ── 외부 충돌 ───────────────────────────────────────────────────
def test_scanner_buy_in_flight_blocks_live_sell(h):
    import sqlite3
    db = sqlite3.connect(h.st.journal_path)
    db.execute("CREATE TABLE scanner_buy_orders (account_profile TEXT, ticker TEXT, state TEXT, broker_state TEXT)")
    db.execute("INSERT INTO scanner_buy_orders VALUES ('account1','T','UNKNOWN',NULL)")
    db.commit(); db.close()
    h.add()
    trigger(h)
    assert h.broker.sells == [] and any(":buy_in_flight:" in k for k in h.alert_keys())


def test_external_open_order_same_ticker_goes_review(h):
    pid = h.add()
    h.broker.external = [{"order_number": "555", "state": "OPEN", "ticker": "T"}]
    trigger(h)
    assert h.broker.sells == [] and h.event(pid)["state"] == "NEEDS_REVIEW"


# ── 알림 ────────────────────────────────────────────────────────
def test_alert_dedup_and_retry(h):
    assert h.store.enqueue_alert(h.clock(), "k1", "INFO", "a") is True
    assert h.store.enqueue_alert(h.clock(), "k1", "INFO", "a") is False
    results = iter([False, True])
    h.engine.notifier = lambda s, t: next(results)
    h.engine._flush_alerts()
    assert h.store.recent_alerts()[0]["state"] == "FAILED"
    h.engine._flush_alerts()
    assert h.store.recent_alerts()[0]["state"] == "SENT"


def test_registration_blocked_while_unresolved_same_ticker(h):
    pid = h.add()
    trigger(h)
    h.store.update_position(pid, h.clock(), state="DISABLED")
    with pytest.raises(ClaimRefused):
        h.add()


def test_lease_lost_mid_tick_stops_all_writes(h):
    h.add()
    h.broker.price = Decimal("44")
    real_quote = h.broker.quote

    def steal(*a):
        h.store.release_lease("w1", h.clock())
        h.store.acquire_lease("w2", h.clock(), 90)
        return real_quote(*a)
    h.broker.quote = steal
    assert h.tick()["status"] == "LEASE_LOST"
    assert h.broker.sells == [] and h.store.runtime_get("heartbeat_at") is None
    assert h.sent_alerts == [] and h.store.events() == []
    assert h.store.positions()[0]["last_quote_price"] is None


def test_carry_over_not_counted_while_mode_differs(h):
    pid = h.add()
    h.clock.set(ts(2026, 11, 23, 15, 56))
    trigger(h)
    h.set(mode="OFF")
    h.clock.set(ts(2026, 11, 24, 10, 0)); h.tick()
    assert h.event(pid)["carry_over_count"] == 0 and h.event(pid)["state"] == "CARRY_OVER"
    h.set(mode="LIVE")
    h.tick()
    assert h.event(pid)["carry_over_count"] == 1 and len(h.broker.sells) == 1


def test_session_summary_after_close(h):
    h.add()
    trigger(h)
    h.clock.set(ts(2026, 11, 23, 16, 10)); h.tick()
    assert "session:2026-11-23:LIVE:summary" in h.alert_keys()
