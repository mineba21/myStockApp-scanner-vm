"""주문 종류별 안전 조건 매트릭스 (Codex 3차 리뷰 STEP 1·2·5). 실주문·실취소 없음.

열: ORIGINAL / REPLACE / CARRY_OVER / CANCEL_FOR_REPLACE × SHADOW / LIVE
행: 아래 CONDITIONS. 모든 변경은 사전 검증이 끝난 뒤, 선점 트랜잭션 시작 직전에 주입한다.
기대: APPLY = 실제 모의 sell/cancel 0회, N_A = 차단하지 않음(이유는 docs/auto_stop_sell.md 매트릭스).
"""
import sqlite3
from dataclasses import replace
from decimal import Decimal

import pytest

from tests.auto_stop_fakes import Harness, NoOrderReader, ts
from trading.auto_stop import quote_health as QH
from trading.auto_stop.brokers import ShadowBroker
from trading.auto_stop.settings import CONTRACTS
from trading.scanner_buy_orders import ScannerBuyOrders

SHADOW_ENV = {"KIWOOM_WEB_ENABLED": "true", "AUTO_STOP_US_MODE": "SHADOW"}
KINDS = ("ORIGINAL", "REPLACE", "CARRY_OVER", "CANCEL_FOR_REPLACE")
MODES = ("SHADOW", "LIVE")


class Case:
    """주문 종류·모드별 상황을 만들고, 다음 tick 이 그 주문 종류의 선점을 실행하게 둔다."""

    def __init__(self, tmp_path, monkeypatch, kind, mode):
        self.kind, self.mode, self.monkeypatch = kind, mode, monkeypatch
        if mode == "LIVE":
            self.h = h = Harness(tmp_path, monkeypatch, price_interval_sec=60)
            h.broker.quote_age = 5
            self.reader = h.broker
        else:
            holder = {}
            self.h = h = Harness(tmp_path, monkeypatch, env=SHADOW_ENV, mode="SHADOW",
                                 shadow_replace_enabled=True, price_interval_sec=60,
                                 factory=lambda m: holder["s"])
            self.reader = h.broker = NoOrderReader(h.clock)          # 실주문·실취소 경로 호출 시 실패
            self.reader.quote_age = 5
            holder["s"] = s = ShadowBroker(self.reader, "NEXT_QUOTE", h.clock)
            self.v_sells, self.v_cancels = [], []
            rs, rc = s.sell, s.cancel
            s.sell = lambda *a: self.v_sells.append(a) or rs(*a)
            s.cancel = lambda *a: self.v_cancels.append(a) or rc(*a)
        getattr(self, "_" + kind.lower())()
        self.before = self.count()

    # 실제 모의 호출 수
    def count(self):
        if self.kind == "CANCEL_FOR_REPLACE":
            return len(self.h.broker.cancels) if self.mode == "LIVE" else len(self.v_cancels)
        return len(self.h.broker.sells) if self.mode == "LIVE" else len(self.v_sells)

    @property
    def claim(self):
        return "claim_cancel" if self.kind == "CANCEL_FOR_REPLACE" else "claim_sell"

    def _original(self):
        h = self.h
        self.pid = h.add(mode=self.mode)
        self.eid = h.store.create_event(h.clock(), h.pos(self.pid), state="OPEN", trigger_price=Decimal("44"),
                                        trigger_as_of=h.clock(), session_date="2026-11-23")
        self.reader.price = Decimal("44")

    def _open_order(self):
        h = self.h
        self.pid = h.add(mode=self.mode)
        self.reader.price = Decimal("44")
        h.tick()                                                   # 원주문
        self.reader.price = Decimal("40")                           # 지정가 아래 → 미체결 유지
        h.clock.advance(30); h.tick()
        self.eid = h.event(self.pid)["id"]
        self.order_id = h.orders()[0]["id"]

    def _cancel_for_replace(self):
        self._open_order()
        self.h.clock.advance(301)                                  # 다음 tick: 재주문 목적 취소

    def _replace(self):
        self._open_order()
        h = self.h
        h.clock.advance(301); h.tick()                             # 재주문용 취소 요청
        if self.mode == "LIVE":
            h.broker.fill(h.orders()[0]["order_number"], 0, state="CANCELLED")
        h.clock.advance(30)                                        # 다음 tick: 취소 확정 → REPLACE 매도

    def _carry_over(self):
        h = self.h
        h.clock.set(ts(2026, 11, 23, 15, 50))
        self.pid = h.add(mode=self.mode)
        self.eid = h.store.create_event(h.clock(), h.pos(self.pid), state="OPEN", trigger_price=Decimal("44"),
                                        trigger_as_of=h.clock(), session_date="2026-11-23")
        h.store.acquire_lease("setup", h.clock(), 90)
        o = h.store.claim_sell(h.clock(), owner="setup", event_id=self.eid, kind="ORIGINAL", qty=10,
                               ref_price=Decimal("44"), ref_source="CUR", quote_as_of=h.clock(),
                               limit_price=Decimal("43.78"), settings=h.st, guard=lambda: None)
        h.store.transition(o["id"], h.clock(), frm="SENDING", to="SUBMITTED", order_number="X1",
                           order_date="2026-11-23")
        h.store.transition(o["id"], h.clock(), frm="SUBMITTED", to="CANCELLED")   # 장 마감 후 소멸
        h.store.release_lease("setup", h.clock())
        h.store.update_event(self.eid, h.clock(), state="CARRY_OVER")
        self.order_id = o["id"]
        h.clock.set(ts(2026, 11, 24, 9, 31))                       # 다음 정규장: CARRY_OVER 매도
        self.reader.price = Decimal("44")

    def inject(self, fn):
        real = getattr(self.h.store, self.claim)

        def wrapped(*a, **k):
            fn(self)
            return real(*a, **k)
        setattr(self.h.store, self.claim, wrapped)

    def run(self):
        return self.h.tick()


def scanner_row(path, *, state, broker_state=None, ticker="T", account="account1"):
    """별도 SQLite 연결에서 Scanner 매수 행을 실제 커밋한다 (기존 Scanner DDL)."""
    ScannerBuyOrders(path, broker_factory=lambda m: None)
    db = sqlite3.connect(path)
    db.execute("INSERT INTO scanner_buy_orders(id,account_profile,market,currency,scan_result_id,ticker,name,"
               "exchange,quantity,price,state,order_number,broker_state,created_at,expires_at,sent_at,updated_at,"
               "details) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (f"s-{state}-{broker_state}-{ticker}-{account}", account, "US", "USD", 1, ticker, ticker, "ND", 1,
                "40", state, "77" if state == "SUBMITTED" else None, broker_state, 0, 1e12, 0, 0, "{}"))
    db.commit()
    db.close()


# ── 조건 정의: (이름, 변경 함수, 적용 판단) ───────────────────────
def _steal(c):
    c.h.store.release_lease("w1", c.h.clock())
    c.h.store.acquire_lease("w2", c.h.clock(), 90)


def _fills(c):
    h = c.h
    if c.kind == "CANCEL_FOR_REPLACE":
        h.store.apply_fill(c.order_id, h.clock(), 3, {"source": "test"})
    else:
        p = h.pos(c.pid)                                           # 다른 경로에서 체결 반영 (관리 잔여 변경)
        h.store.update_position(c.pid, h.clock(), remaining_managed_qty=p["remaining_managed_qty"] - 1,
                                total_filled_qty=p["total_filled_qty"] + 1)


def _symbol_hold(c):
    key = f"quote_health:{c.mode}"
    health = c.h.store.runtime_get(key, {}) or {}
    rec = health.get(str(c.pid)) or {"position_id": c.pid, "ticker": "T"}
    rec["state"] = QH.FAULT
    health[str(c.pid)] = rec
    c.h.store.runtime_set(key, health, c.h.clock())


def _stale(c):
    c.h.clock.advance(30)                                          # 검증 때 100초 → 선점 때 130초


ALL = lambda kind, mode: True                                     # noqa: E731
LIVE_ONLY = lambda kind, mode: mode == "LIVE"                     # noqa: E731
REPLACE_ONLY = lambda kind, mode: kind in ("REPLACE", "CANCEL_FOR_REPLACE")   # noqa: E731

CONDITIONS = {
    "lease": (_steal, ALL),
    "cutoff": (lambda c: c.h.set(session_cutoff_min=420), ALL),      # cutoff 를 현재 이전으로 (시각 경과와 같은 효과)
    "mode": (lambda c: c.h.set(mode="OFF"), ALL),
    "kill_switch": (lambda c: c.h.store.set_kill_switch(c.h.clock(), True, actor="t", reason="x"), ALL),
    "order_switch": (lambda c: c.monkeypatch.setenv("ACCOUNT1_ORDER_ENABLED", "false"), LIVE_ONLY),
    "replace_switch": (lambda c: c.h.set(replace_enabled=False, shadow_replace_enabled=False), REPLACE_ONLY),
    "replace_limit": (lambda c: c.h.set(max_replaces_per_session=0), REPLACE_ONLY),
    "api_contract": (lambda c: c.h.set(verified_contracts=frozenset()), LIVE_ONLY),
    "global_hold": (lambda c: c.h.store.runtime_set("quote_global_hold",
                                                    QH.incident_new(c.h.clock(), "quote_system", "auth"),
                                                    c.h.clock()), ALL),
    "symbol_hold": (_symbol_hold, ALL),
    "position_event_state": (lambda c: c.h.store.review(c.h.clock(), event_id=c.eid, position_id=c.pid,
                                                        reason="test"), ALL),
    "paused": (lambda c: c.h.store.update_position(c.pid, c.h.clock(), auto_paused=1), ALL),
    "fills_changed": (_fills, ALL),
    "order_cap": (lambda c: c.h.set(max_order_qty=1), ALL),
    "quote_stale": (_stale, ALL),
    "scanner_in_flight": (lambda c: scanner_row(c.h.st.journal_path, state="SENDING"), LIVE_ONLY),
}


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("kind", KINDS)
def test_baseline_each_kind_executes(tmp_path, monkeypatch, kind, mode):
    c = Case(tmp_path, monkeypatch, kind, mode)
    c.run()
    assert c.count() == c.before + 1, "대조군: 조건 변경이 없으면 해당 주문 종류가 실행된다"


@pytest.mark.parametrize("cond", sorted(CONDITIONS))
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("kind", KINDS)
def test_matrix_condition_changed_after_precheck(tmp_path, monkeypatch, kind, mode, cond):
    change, applies = CONDITIONS[cond]
    c = Case(tmp_path, monkeypatch, kind, mode)
    if cond == "quote_stale":
        c.reader.quote_age = 100
    c.inject(change)
    c.run()
    if applies(kind, mode):
        assert c.count() == c.before, f"{cond} 변경 후 {kind}/{mode} 실행 금지"
    else:
        assert c.count() == c.before + 1, f"{cond} 는 {kind}/{mode} 에 해당 없음: 차단하지 않는다"


# ── STEP 1 개별 필수 사례 ───────────────────────────────────────
def test_replace_blocked_keeps_event_and_waiting_alert(tmp_path, monkeypatch):
    c = Case(tmp_path, monkeypatch, "REPLACE", "LIVE")
    trig = c.h.event(c.pid)["trigger_price"]
    c.inject(lambda c: c.h.set(replace_enabled=False))
    c.run()
    ev = c.h.event(c.pid)
    assert c.count() == c.before and ev["state"] == "OPEN" and ev["trigger_price"] == trig
    assert c.h.store.runtime_get(f"replace_waiting:{ev['id']}")
    assert any(":replace_waiting:" in k for k in c.h.alert_keys())


@pytest.mark.parametrize("cap", ["qty", "notional"])
def test_order_caps_reduced_after_precheck(tmp_path, monkeypatch, cap):
    c = Case(tmp_path, monkeypatch, "ORIGINAL", "LIVE")
    c.inject(lambda c: c.h.set(**({"max_order_qty": 5} if cap == "qty"
                                  else {"max_order_notional_usd": Decimal("100")})))
    c.run()
    assert c.count() == c.before


def test_original_ignores_replace_switch(tmp_path, monkeypatch):
    c = Case(tmp_path, monkeypatch, "ORIGINAL", "LIVE")
    c.h.set(replace_enabled=False, shadow_replace_enabled=False)
    c.run()
    assert c.count() == c.before + 1


@pytest.mark.parametrize("change", ["contract", "carry_limit"])
def test_carry_over_policy_rechecked_at_claim(tmp_path, monkeypatch, change):
    c = Case(tmp_path, monkeypatch, "CARRY_OVER", "LIVE")
    if change == "contract":
        c.inject(lambda c: c.h.set(verified_contracts=frozenset(CONTRACTS) - {"POST_CLOSE_EXPIRY"}))
    else:
        c.inject(lambda c: c.h.set(max_carry_over_sessions=0))
    c.run()
    assert c.count() == c.before


def test_kind_mismatch_and_unknown_kind_refused(tmp_path, monkeypatch):
    from trading.auto_stop.store import ClaimRefused
    c = Case(tmp_path, monkeypatch, "ORIGINAL", "LIVE")
    h = c.h
    snap = h.engine._snapshot(h.pos(c.pid), h.event(c.pid))
    h.store.acquire_lease("w1", h.clock(), 90)
    for kind in ("REPLACE", "FOO"):
        gate = h.engine._exec_gate(kind=kind, mode="LIVE", position_id=c.pid, event_id=c.eid,
                                   quote_as_of=h.clock() - 5, snapshot=snap, qty=10, limit_price=Decimal("43.78"))
        with pytest.raises(ClaimRefused, match="kind"):
            h.store.claim_sell(h.clock(), owner="w1", event_id=c.eid, kind="ORIGINAL", qty=10,
                               ref_price=Decimal("44"), ref_source="CUR", quote_as_of=h.clock() - 5,
                               limit_price=Decimal("43.78"), settings=h.st, guard=lambda: None, clock=h.clock,
                               gate=gate)
    assert h.orders() == [] and h.broker.sells == []


def test_settings_unavailable_fails_closed(tmp_path, monkeypatch):
    c = Case(tmp_path, monkeypatch, "ORIGINAL", "LIVE")
    h, state = c.h, {"fail": False}
    real_loader = h.engine.settings_loader

    def loader():
        if state["fail"]:
            state["fail"] = False
            raise RuntimeError("env 읽기 실패")
        return real_loader()
    h.engine.settings_loader = loader
    c.inject(lambda c: state.update(fail=True))
    c.run()
    assert c.count() == c.before


@pytest.mark.parametrize("mode", MODES)
def test_shadow_and_live_cancel_for_replace_switch_off(tmp_path, monkeypatch, mode):
    c = Case(tmp_path, monkeypatch, "CANCEL_FOR_REPLACE", mode)
    c.inject(lambda c: c.h.set(replace_enabled=False, shadow_replace_enabled=False))
    c.run()
    assert c.count() == c.before


# ── STEP 2 Scanner 미확정 매수 (LIVE) ───────────────────────────
@pytest.mark.parametrize("state,broker_state", [("SENDING", None), ("UNKNOWN", None), ("SUBMITTED", "OPEN"),
                                                ("SUBMITTED", "PARTIAL"), ("SUBMITTED", None)])
@pytest.mark.parametrize("kind", KINDS)
def test_scanner_in_flight_committed_after_precheck_blocks(tmp_path, monkeypatch, kind, state, broker_state):
    c = Case(tmp_path, monkeypatch, kind, "LIVE")
    c.inject(lambda c: scanner_row(c.h.st.journal_path, state=state, broker_state=broker_state))
    c.run()
    assert c.count() == c.before


@pytest.mark.parametrize("row", [dict(state="SENDING", ticker="OTHER"), dict(state="SENDING", account="account4"),
                                 dict(state="SUBMITTED", broker_state="FILLED"),
                                 dict(state="SUBMITTED", broker_state="CANCELLED"),
                                 dict(state="SUBMITTED", broker_state="REJECTED"),
                                 dict(state="REJECTED"), dict(state="RESOLVED_MANUAL"), dict(state="PREPARED")])
@pytest.mark.parametrize("kind", ["ORIGINAL", "CANCEL_FOR_REPLACE"])
def test_scanner_rows_outside_policy_do_not_block(tmp_path, monkeypatch, kind, row):
    c = Case(tmp_path, monkeypatch, kind, "LIVE")
    c.inject(lambda c: scanner_row(c.h.st.journal_path, **row))
    c.run()
    assert c.count() == c.before + 1


def test_scanner_journal_schema_mismatch_fails_closed(tmp_path, monkeypatch):
    c = Case(tmp_path, monkeypatch, "ORIGINAL", "LIVE")

    def broken(c):
        db = sqlite3.connect(c.h.st.journal_path)
        db.execute("CREATE TABLE scanner_buy_orders (id TEXT, ticker TEXT)")   # 필수 열 누락
        db.commit(); db.close()
    c.inject(broken)
    c.run()
    assert c.count() == c.before


def test_missing_scanner_table_is_not_in_flight(tmp_path, monkeypatch):
    c = Case(tmp_path, monkeypatch, "ORIGINAL", "LIVE")
    c.run()
    assert c.count() == c.before + 1


def test_gate_uses_one_settings_snapshot_for_limits(tmp_path, monkeypatch):
    """게이트가 읽은 설정과 선점의 한도 판단이 같은 설정 객체를 쓴다."""
    c = Case(tmp_path, monkeypatch, "REPLACE", "LIVE")
    seen = []
    real_loader = c.h.engine.settings_loader

    def loader():
        s = real_loader()
        seen.append(s)
        return s
    c.h.engine.settings_loader = loader
    c.inject(lambda c: c.h.set(max_replaces_per_event=0))
    c.run()
    assert c.count() == c.before
    assert replace(seen[-1]).max_replaces_per_event == 0
