"""Codex 2차 리뷰(2026-11-27) P1: 최종 주문 실행 직전 공통 안전 조건 재검증. 실주문 없음.

공통 불변 조건 (docs/auto_stop_sell.md "공통 실행 게이트")
- 마감 cutoff 이후 신규 매도·재주문 목적 취소 금지
- 유효하지 않은 lease 로 주문 선점 금지
- 일시중지 종목의 신규 매도·자동 취소 금지
- 미확정 주문이 있는 상태에서 중복 매도 금지
- 사전 검증 이후 주문·체결·관리 수량이 바뀌면 기존 검증 사용 금지
- 유효하지 않은(유효기간이 지난) 시세를 주문 근거로 사용 금지
- 재주문 불가능이 사전에 확인된 기존 주문의 취소 금지

주입 지점은 실제 API 호출(잔고·시세) 도중과 선점 트랜잭션 진입 직전(잠금 대기)이다.
조건을 위반하면 cancel()/sell() 호출이 0회인지 직접 확인한다.
"""
from decimal import Decimal

import pytest

from tests.auto_stop_fakes import Harness, ts

DAY = "2026-11-23"
BEFORE_CUTOFF = ts(2026, 11, 23, 15, 54, 40)      # cutoff 15:55 (마감 5분 전)


def make(tmp_path, monkeypatch, **over):
    h = Harness(tmp_path, monkeypatch, price_interval_sec=60, **over)
    h.broker.quote_age = 5
    return h


def exiting(h, at=None):
    """청산 이벤트 OPEN (주문 없음): 다음 tick 의 _send 가 잔고 → 시세 → 선점 → sell 순서로 실행된다."""
    if at:
        h.clock.set(at)
    pid = h.add()
    h.store.create_event(h.clock(), h.pos(pid), state="OPEN", trigger_price=Decimal("44"),
                         trigger_as_of=h.clock(), session_date=DAY)
    return pid


def open_order(h, at=None, wait=301):
    """미체결 원주문 1건 (SUBMITTED). 300초 뒤 tick 에서 재주문 목적 취소 경로가 실행된다."""
    pid = h.add()
    h.broker.price = Decimal("44")
    h.tick()
    assert h.orders()[0]["state"] == "SUBMITTED"
    h.clock.set(at or (h.clock() + wait))
    return pid


def hook(h, method, fn):
    """브로커 조회 호출 도중 상태를 바꾼다 (원래 응답은 그대로 돌려준다)."""
    real = getattr(h.broker, method)

    def wrapped(*a, **k):
        out = real(*a, **k)
        fn()
        return out
    setattr(h.broker, method, wrapped)


def on_claim(h, name, fn):
    """선점 트랜잭션 진입 직전(잠금 대기)에 상태를 바꾼다."""
    real = getattr(h.store, name)

    def wrapped(*a, **k):
        fn()
        return real(*a, **k)
    setattr(h.store, name, wrapped)


def sells(h):
    return len(h.broker.sells)


# ── cutoff·정규장 ─────────────────────────────────────────────
@pytest.mark.parametrize("method", ["holdings", "quote"])
def test_sell_refused_when_cutoff_passes_during_api_call(tmp_path, monkeypatch, method):
    h = make(tmp_path, monkeypatch)
    exiting(h, BEFORE_CUTOFF)
    h.broker.price = Decimal("44")
    hook(h, method, lambda: h.clock.advance(30))              # API 응답 동안 15:55 를 넘김 (lease 는 유효)
    h.tick()
    assert sells(h) == 0 and h.orders() == []


@pytest.mark.parametrize("method", ["holdings", "quote"])
def test_cancel_refused_when_cutoff_passes_during_api_call(tmp_path, monkeypatch, method):
    h = make(tmp_path, monkeypatch)
    open_order(h, at=BEFORE_CUTOFF)
    hook(h, method, lambda: h.clock.advance(30))
    h.tick()
    assert h.broker.cancels == [] and h.orders()[0]["state"] == "SUBMITTED"


def test_cancel_refused_when_session_closes_during_quote(tmp_path, monkeypatch):
    h = make(tmp_path, monkeypatch)
    open_order(h, at=ts(2026, 11, 23, 15, 59, 50))
    h.set(session_cutoff_min=0)                               # cutoff = 마감 16:00 (정규장 종료 자체를 확인)
    hook(h, "quote", lambda: h.clock.set(ts(2026, 11, 23, 16, 0, 20)))  # 정규장 종료 (lease 유효)
    h.tick()
    assert h.broker.cancels == []


# ── lease ────────────────────────────────────────────────────
@pytest.mark.parametrize("name,path", [("claim_sell", "sell"), ("claim_cancel", "cancel")])
def test_lease_expired_while_waiting_for_db_lock(tmp_path, monkeypatch, name, path):
    h = make(tmp_path, monkeypatch)
    exiting(h) if path == "sell" else open_order(h)
    h.broker.price = Decimal("44")
    on_claim(h, name, lambda: h.clock.advance(100))           # 잠금 대기 100초 > lease 90초, 다른 작업자 없음
    out = h.tick()
    assert out["status"] == "LEASE_LOST"
    assert sells(h) == (0 if path == "sell" else 1) and h.broker.cancels == []
    assert all(o["state"] != "SENDING" for o in h.orders())


@pytest.mark.parametrize("name,path", [("claim_sell", "sell"), ("claim_cancel", "cancel")])
def test_other_worker_took_lease_before_claim(tmp_path, monkeypatch, name, path):
    h = make(tmp_path, monkeypatch)
    exiting(h) if path == "sell" else open_order(h)
    h.broker.price = Decimal("44")

    def steal():
        h.store.release_lease("w1", h.clock())
        h.store.acquire_lease("w2", h.clock(), 90)
    on_claim(h, name, steal)
    assert h.tick()["status"] == "LEASE_LOST"
    assert sells(h) == (0 if path == "sell" else 1) and h.broker.cancels == []


# ── 사전 검증 이후 상태 변경 ─────────────────────────────────────
def test_pause_after_precheck_blocks_cancel(tmp_path, monkeypatch):
    h = make(tmp_path, monkeypatch)
    pid = open_order(h)
    hook(h, "quote", lambda: h.store.update_position(pid, h.clock(), auto_paused=1))
    h.tick()
    assert h.broker.cancels == []


def test_pause_after_precheck_blocks_sell(tmp_path, monkeypatch):
    h = make(tmp_path, monkeypatch)
    pid = exiting(h)
    h.broker.price = Decimal("44")
    hook(h, "quote", lambda: h.store.update_position(pid, h.clock(), auto_paused=1))
    h.tick()
    assert sells(h) == 0


@pytest.mark.parametrize("path", ["sell", "cancel"])
def test_kill_switch_after_precheck(tmp_path, monkeypatch, path):
    h = make(tmp_path, monkeypatch)
    exiting(h) if path == "sell" else open_order(h)
    h.broker.price = Decimal("44")
    hook(h, "quote", lambda: h.store.set_kill_switch(h.clock(), True, actor="t", reason="x"))
    h.tick()
    assert sells(h) == (0 if path == "sell" else 1) and h.broker.cancels == []


def test_replace_switch_off_after_precheck(tmp_path, monkeypatch):
    h = make(tmp_path, monkeypatch)
    open_order(h)
    hook(h, "quote", lambda: h.set(replace_enabled=False))
    h.tick()
    assert h.broker.cancels == []


def test_global_hold_after_precheck_blocks_sell(tmp_path, monkeypatch):
    from trading.auto_stop import quote_health as QH
    h = make(tmp_path, monkeypatch)
    exiting(h)
    h.broker.price = Decimal("44")
    hook(h, "quote", lambda: h.store.runtime_set("quote_global_hold",
                                                 QH.incident_new(h.clock(), "quote_system", "auth"), h.clock()))
    h.tick()
    assert sells(h) == 0


def test_mode_change_after_precheck_blocks_sell(tmp_path, monkeypatch):
    h = make(tmp_path, monkeypatch)
    exiting(h)
    h.broker.price = Decimal("44")
    hook(h, "quote", lambda: h.set(mode="OFF"))
    h.tick()
    assert sells(h) == 0


def test_partial_fill_after_precheck_invalidates_cancel(tmp_path, monkeypatch):
    h = make(tmp_path, monkeypatch)
    open_order(h)
    o = h.orders()[0]
    hook(h, "quote", lambda: h.store.apply_fill(o["id"], h.clock(), 3, {"source": "test"}))
    h.tick()
    assert h.broker.cancels == []
    assert h.orders()[0]["state"] == "SUBMITTED" and h.orders()[0]["filled_qty"] == 3


def test_cumulative_fill_increase_while_partial_filled_invalidates_cancel(tmp_path, monkeypatch):
    h = make(tmp_path, monkeypatch)
    open_order(h, wait=30)
    h.broker.fill("1000", 3)
    h.tick()                                                 # PARTIAL_FILLED 3 반영 (300초 전이라 취소 없음)
    o = h.orders()[0]
    assert o["state"] == "PARTIAL_FILLED" and h.broker.cancels == []
    h.clock.advance(301)
    hook(h, "quote", lambda: h.store.apply_fill(o["id"], h.clock(), 5, {"source": "test"}))
    h.tick()
    assert h.broker.cancels == [] and h.orders()[0]["state"] == "PARTIAL_FILLED"


def test_full_fill_after_precheck_blocks_cancel(tmp_path, monkeypatch):
    h = make(tmp_path, monkeypatch)
    open_order(h)
    o = h.orders()[0]

    def filled():
        h.store.apply_fill(o["id"], h.clock(), 10, {"source": "test"})
        h.store.transition(o["id"], h.clock(), frm="SUBMITTED", to="FILLED")
    hook(h, "quote", filled)
    h.tick()
    assert h.broker.cancels == []


def test_position_review_after_precheck_blocks_cancel(tmp_path, monkeypatch):
    h = make(tmp_path, monkeypatch)
    pid = open_order(h)
    hook(h, "quote", lambda: h.store.review(h.clock(), position_id=pid, reason="보유수량 불일치"))
    h.tick()
    assert h.broker.cancels == []


@pytest.mark.parametrize("path", ["sell", "cancel"])
def test_quote_expires_before_claim(tmp_path, monkeypatch, path):
    h = make(tmp_path, monkeypatch)
    exiting(h) if path == "sell" else open_order(h)
    h.broker.price = Decimal("44")
    h.broker.quote_age = 100                                   # 검증 시점에는 유효(100초)
    on_claim(h, "claim_sell" if path == "sell" else "claim_cancel",
             lambda: h.clock.advance(30))                      # 선점(잠금 획득) 시점에는 130초 > 120초
    h.tick()
    assert sells(h) == (0 if path == "sell" else 1) and h.broker.cancels == []


def test_unresolved_order_blocks_duplicate_sell(tmp_path, monkeypatch):
    h = make(tmp_path, monkeypatch)
    pid = open_order(h)
    eid = h.event(pid)["id"]
    h.store.update_event(eid, h.clock(), next_order_kind="REPLACE")
    from trading.auto_stop.us_calendar import market_clock
    h.store.acquire_lease("w1", h.clock(), 90)
    h.engine._send(market_clock(h.clock(), h.st), h.event(pid), h.pos(pid))   # 미확정 주문이 있는데 직접 호출
    assert sells(h) == 1


def test_claim_records_sent_at_inside_claim_transaction(tmp_path, monkeypatch):
    h = make(tmp_path, monkeypatch)
    exiting(h)
    h.broker.price = Decimal("44")
    seen = {}
    real = h.broker.sell

    def sell(*a, **k):
        seen["order"] = h.orders()[0]                         # 전송 시점의 DB 상태
        return real(*a, **k)
    h.broker.sell = sell
    h.tick()
    assert seen["order"]["state"] == "SENDING" and seen["order"]["sent_at"] is not None


# ── STEP 2: 취소 전 보유수량 불일치 ─────────────────────────────
def test_holding_shortfall_without_fill_evidence_blocks_cancel(tmp_path, monkeypatch):
    h = make(tmp_path, monkeypatch)
    pid = open_order(h)                                      # 관리 잔여 10, 미체결 매도 10
    h.broker.holding["T"].update(qty=5, sellable=0)          # 브로커 보유 5주, 매도가능 0, 체결 증거 없음
    h.tick()
    assert h.broker.cancels == [], "설명되지 않는 보유 부족이면 기존 주문을 취소하지 않는다"
    assert h.orders()[0]["state"] == "SUBMITTED"
    assert any(":holding_mismatch:" in k for k in h.alert_keys())
    assert [a["severity"] for a in h.store.recent_alerts(100) if ":holding_mismatch:" in a["dedup_key"]][0] == "URGENT"
    h.broker.fill("1000", 10)                                # 기존 주문 대조는 계속
    h.clock.advance(30); h.tick()
    assert h.orders()[0]["state"] == "FILLED" and h.pos(pid)["state"] == "SOLD"


def test_full_reservation_by_open_sell_still_allows_cancel(tmp_path, monkeypatch):
    h = make(tmp_path, monkeypatch)
    open_order(h)
    h.broker.holding["T"].update(qty=10, sellable=0)         # 기존 매도가 전량 예약
    h.tick()
    assert h.broker.cancels == ["1000"]


def test_fill_evidence_explains_holding_shortfall(tmp_path, monkeypatch):
    h = make(tmp_path, monkeypatch, status_interval_sec=600)  # tick 시작 대조를 건너뛰게
    pid = open_order(h)
    h.broker.fill("1000", 5)                                  # 증권사 체결 5주 (아직 저널 미반영), 보유 5
    h.tick()
    assert h.orders()[0]["filled_qty"] == 5                   # 취소 전 강제 대조로 체결 반영
    assert h.broker.cancels == ["1000"] and h.pos(pid)["remaining_managed_qty"] == 5
