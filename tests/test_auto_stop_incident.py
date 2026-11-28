"""Codex 독립 리뷰(2026-11-27) P1·P2·원자성 재현 테스트. FakeBroker + 가짜 시계 + 임시 SQLite. 실주문 없음.

조건은 Codex 재현과 같다: 감시 주기 60초, 30초 tick, 429 반복 시 회복 조회 백오프(120·240초).
"""
from decimal import Decimal

import pytest

from tests.auto_stop_fakes import Harness, NoOrderReader, ts
from trading.auto_stop import quote_health as QH
from trading.auto_stop import service
from trading.auto_stop.brokers import ShadowBroker
from trading.auto_stop.quotes import Quote, QuoteUnavailable
from trading.kiwoom_orders import OrderUnknown

SHADOW_ENV = {"KIWOOM_WEB_ENABLED": "true", "AUTO_STOP_US_MODE": "SHADOW"}
T0 = ts(2026, 11, 23, 10, 0)


def rl():
    return QuoteUnavailable("429", info={"category": "rate_limit", "http_status": 429})


def auth():
    return QuoteUnavailable("인증 실패", info={"category": "auth", "http_status": 401})


def unknown():
    return QuoteUnavailable("시세 조회 실패")


class Grid:
    """30초 간격 tick (응답 지연으로 시계가 앞서 있으면 그 시각부터)."""

    def __init__(self, h, t0=T0):
        self.h, self.t0, self.k = h, t0, 0

    def run(self, n):
        for _ in range(n):
            self.h.clock.set(max(self.h.clock(), self.t0 + 30 * self.k))
            self.h.tick()
            self.k += 1


def setup(h, tickers=("CINF", "EVRG"), mode="LIVE", stop="45.00"):
    ids = {}
    for t in tickers:
        h.broker.holding[t] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
        ids[t] = h.add(mode=mode, ticker=t, stop=stop)
    return ids


def inc(h):
    return h.store.runtime_get("quote_global_hold")


def held(h):
    return "quote_system" in h.engine._hold_reasons() or "quote_broad" in h.engine._hold_reasons()


def stat(h, metric, key=None, day="2026-11-23", mode="LIVE"):
    return sum(r["value"] for r in h.store.stats(day, mode) if r["metric"] == metric and (key is None or r["key"] == key))


@pytest.fixture
def h(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch, price_interval_sec=60)


def enter_incident(h, g, errors):
    h.broker.quote_exc_by = dict(errors)
    g.run(8)
    assert held(h)


# ── P1-1: 시스템 장애 미해결 상태 보존 ───────────────────────────
def test_rate_limit_backoff_is_not_recovery_evidence(h):
    ids, g = setup(h), Grid(h)
    enter_incident(h, g, {"CINF": rl(), "EVRG": auth()})
    h.broker.quote_exc_by = {"CINF": rl()}                     # EVRG 만 정상화, CINF 는 429 백오프 중
    g.run(12)
    assert held(h), "CINF 미해결인데 EVRG 정상 응답만으로 해제되면 안 된다"
    i = inc(h)
    assert i["affected"][str(ids["EVRG"])]["resolved"] is True
    assert i["affected"][str(ids["CINF"])]["resolved"] is False
    assert h.store.runtime_get("quote_health:LIVE")[str(ids["CINF"])].get("rate_limit_streak", 0) >= 2
    skipped = sum(int((r or {}).get("skipped_backoff", 0)) for r in [i["last_round"].get("skipped")])
    assert skipped >= 0 and h.broker.sells == []
    # 백오프 종료 후 다시 실패: 증거 처음부터, 보류 유지
    g.run(8)
    assert held(h) and not inc(h)["affected"][str(ids["CINF"])]["evidence_ticks"]
    h.broker.quote_exc_by = {}
    g.run(12)
    assert not held(h)
    off = h.store.audits("quote_hold_off", 0, h.clock() + 1)[-1]["details"]
    assert len(off["affected"][str(ids["CINF"])]["evidence_ticks"]) == 2


def test_backoff_skip_status_is_recorded(h):
    ids, g = setup(h), Grid(h)
    enter_incident(h, g, {"CINF": rl(), "EVRG": rl()})
    seen = set()
    for _ in range(12):
        g.run(1)
        rec = h.store.runtime_get("quote_health:LIVE")[str(ids["CINF"])]
        seen.add(rec.get("last_observation"))
    assert "skipped_backoff" in seen and "failed" in seen and held(h)


def test_time_budget_skip_is_not_recovery_evidence(h):
    ids, g = setup(h, tickers=("EVRG", "CINF")), Grid(h)     # EVRG 가 먼저 조회되도록 등록 순서
    enter_incident(h, g, {"CINF": unknown(), "EVRG": unknown()})
    h.broker.quote_exc_by = {}
    real = h.broker.quote

    def slow(ticker, exchange):
        q = real(ticker, exchange)
        if ticker == "EVRG":
            h.clock.advance(50)                                # tick 예산(45초) 소진 → 같은 tick 의 CINF 는 조회 안 함
        return q
    h.broker.quote = slow
    start = len(h.broker.quote_log)
    saw_skip = False
    for _ in range(16):
        before = len(inc(h)["affected"][str(ids["CINF"])]["evidence_ticks"]) if inc(h) else None
        g.run(1)
        cinf_calls = sum(1 for t, _ in h.broker.quote_log[start:] if t == "CINF")
        i = inc(h)
        if i is None:
            assert cinf_calls >= 2, "CINF 를 실제로 2회 조회하기 전에 해제되면 안 된다"
            break
        a = i["affected"][str(ids["CINF"])]
        if a["last_status"] == "skipped_budget":
            saw_skip = True
            assert len(a["evidence_ticks"]) == before            # 조회하지 않은 tick 은 증거가 아니다
        assert len(a["evidence_ticks"]) <= cinf_calls
    assert saw_skip and stat(h, "slot_deferred", "CINF") >= 1
    assert not held(h)


def test_per_ticker_evidence_on_different_ticks(h):
    ids, g = setup(h), Grid(h)
    enter_incident(h, g, {"CINF": unknown(), "EVRG": unknown()})
    h.broker.quote_exc_by = {"EVRG": unknown()}                # CINF 먼저 회복
    g.run(6)
    i = inc(h)
    assert i["affected"][str(ids["CINF"])]["resolved"] and not i["affected"][str(ids["EVRG"])]["resolved"]
    h.broker.quote_exc_by = {"CINF": auth()}                   # EVRG 회복, CINF 다시 SYSTEM 실패 → CINF 재검증
    g.run(4)
    i = inc(h)
    assert not i["affected"][str(ids["CINF"])]["resolved"] and held(h)
    h.broker.quote_exc_by = {}
    g.run(10)
    assert not held(h)


def test_pausing_faulted_ticker_keeps_hold_and_probes_read_only(h):
    ids, g = setup(h), Grid(h)
    enter_incident(h, g, {"CINF": unknown(), "EVRG": unknown()})
    h.store.update_position(ids["CINF"], h.clock(), auto_paused=1)
    h.broker.quote_exc_by = {"CINF": unknown()}
    g.run(10)
    assert held(h), "일시중지만으로 미해결 장애가 해제되면 안 된다"
    assert stat(h, "incident_probe", "CINF") >= 4               # 일시중지 중에도 읽기 전용 회복 조회
    h.broker.quote_exc_by = {}
    g.run(10)
    assert not held(h) and h.broker.sells == []


def test_disabled_faulted_ticker_keeps_hold_until_alternate_path_recovers(h):
    ids, g = setup(h), Grid(h)
    enter_incident(h, g, {"CINF": unknown(), "EVRG": unknown()})
    h.store.update_position(ids["CINF"], h.clock(), state="DISABLED", auto_paused=1)   # 관리 종료
    h.broker.quote_exc_by = {"CINF": unknown()}
    g.run(10)
    assert held(h), "관리 종료만으로 미해결 장애가 해제되면 안 된다"
    assert stat(h, "incident_probe", "CINF") >= 4
    h.broker.quote_exc_by = {}
    g.run(10)
    assert not held(h)
    assert len(h.store.audits("quote_hold_off", 0, h.clock() + 1)[-1]["details"]["affected"][str(ids["CINF"])]
               ["evidence_ticks"]) == 2


def test_adding_healthy_tickers_does_not_release_broad_failure(h):
    setup(h)
    g = Grid(h)
    h.broker.age_by = {"CINF": 500, "EVRG": 500}               # 응답은 오지만 둘 다 오래된 시세
    g.run(12)
    assert h.engine._hold_reasons() == ["quote_broad"]
    for t in ("A1", "A2", "A3"):                                # 정상 종목 추가 → 비율 2/5
        h.broker.holding[t] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
        h.add(ticker=t)
    g.run(6)
    assert "quote_broad" in h.engine._hold_reasons(), "분모 증가만으로 해제되면 안 된다"
    h.broker.age_by = {}
    g.run(8)
    assert h.engine._hold_reasons() == []


def test_same_tick_ok_and_system_error_does_not_count_round(h):
    setup(h)
    g = Grid(h)
    enter_incident(h, g, {"CINF": auth(), "EVRG": auth()})
    h.broker.quote_exc_by = {"CINF": auth()}
    g.run(10)
    i = inc(h)
    assert i["rounds_ok"] == 0 and held(h)


def test_restart_during_incident_preserves_scope_and_evidence(h):
    ids, g = setup(h), Grid(h)
    enter_incident(h, g, {"CINF": unknown(), "EVRG": unknown()})
    h.broker.quote_exc_by = {"CINF": unknown()}
    g.run(4)
    before = inc(h)
    h.engine = h.new_engine("w2")                               # 재시작
    g.run(1)
    after = inc(h)
    assert set(after["affected"]) == set(before["affected"]) and after["since"] == before["since"]
    assert after["affected"][str(ids["EVRG"])]["resolved"] and held(h)


def test_release_requires_clean_rounds_after_all_targets_resolved(h):
    ids, g = setup(h, tickers=("CINF",)), Grid(h)
    enter_incident(h, g, {"CINF": auth()})
    h.broker.quote_exc_by = {}
    g.run(2)                                                    # 첫 회복 조회 1회: 증거 1
    assert held(h) and not inc(h)["affected"][str(ids["CINF"])]["resolved"]
    g.run(2)                                                    # 서로 다른 tick 2회 → 대상·공통 경로 충족
    assert not held(h)


# ── P1-2: 재주문 목적 취소 전 검증 ──────────────────────────────
@pytest.fixture
def r(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, price_interval_sec=60)   # LIVE 대역, 재주문 스위치·계약 검증 켜짐
    pid = h.add()
    h.broker.price = Decimal("44")
    h.tick()
    assert h.orders()[0]["state"] == "SUBMITTED" and len(h.broker.sells) == 1
    h.pid = pid
    return h


def after_301(h):
    h.clock.advance(301)
    h.tick()


@pytest.mark.parametrize("make", [
    lambda h: setattr(h.broker, "quote_exc", auth()),
    lambda h: setattr(h.broker, "quote_age", 200),
    lambda h: setattr(h.broker, "quote", lambda t, e: Quote("CINX", e, Decimal("44"), h.clock() - 30, "F")),
    lambda h: setattr(h.broker, "quote", lambda t, e: Quote(t, "NY", Decimal("44"), h.clock() - 30, "F")),
    lambda h: setattr(h.broker, "quote", lambda t, e: Quote(t, e, Decimal("0"), h.clock() - 30, "F")),
    lambda h: setattr(h.broker, "price", Decimal("0.50")),
], ids=["auth", "stale", "symbol_mismatch", "exchange_mismatch", "price_zero", "below_1usd"])
def test_no_cancel_without_fresh_valid_quote(r, make):
    make(r)
    after_301(r)
    assert r.broker.cancels == [], "취소 전 시세 검증 실패 시 cancel 호출 0회"
    assert r.orders()[0]["state"] == "SUBMITTED" and len(r.broker.sells) == 1
    assert stat(r, "replace_precheck_fail") >= 1
    r.clock.advance(30); r.tick()
    assert r.broker.cancels == []


def test_no_cancel_while_global_hold(r):
    r.store.runtime_set("quote_global_hold", QH.incident_new(r.clock(), "quote_system", "auth"), r.clock())
    after_301(r)
    assert r.broker.cancels == []
    ev, pos, o = r.event(r.pid), r.pos(r.pid), r.orders()[0]
    from trading.auto_stop.us_calendar import market_clock
    assert r.engine._replace_precheck(market_clock(r.clock(), r.st), ev, pos, o)[0].startswith("global_hold")


def test_no_cancel_when_kill_switch(r):
    r.store.set_kill_switch(r.clock(), True, actor="t", reason="점검")
    after_301(r)
    assert r.broker.cancels == []


def test_fill_during_cancel_then_replace_only_remaining(r):
    real_cancel = r.broker.cancel

    def cancel_with_fill(t, e, n):
        out = real_cancel(t, e, n)
        r.broker.fill(n, 4, state="CANCELLED")                  # 취소 응답 사이 추가 체결
        return out
    r.broker.cancel = cancel_with_fill
    after_301(r)
    assert r.broker.cancels == ["1000"]
    r.broker.price = Decimal("43")
    r.clock.advance(30); r.tick()
    o = r.orders()
    assert o[0]["state"] == "CANCELLED" and o[0]["filled_qty"] == 4
    assert len(r.broker.sells) == 2 and r.broker.sells[1]["qty"] == 6 and o[1]["kind"] == "REPLACE"


def test_quote_failure_after_cancel_confirmed_keeps_event_and_alerts(r):
    trigger_price = r.event(r.pid)["trigger_price"]
    after_301(r)
    assert r.broker.cancels == ["1000"]
    r.broker.fill("1000", 0, state="CANCELLED")
    r.broker.quote_exc = auth()
    r.clock.advance(30); r.tick()
    r.clock.advance(30); r.tick()
    assert len(r.broker.sells) == 1                             # 재주문 없음
    ev = r.event(r.pid)
    assert ev["state"] == "OPEN" and ev["trigger_price"] == trigger_price and ev["next_order_kind"] == "REPLACE"
    assert r.store.runtime_get(f"replace_waiting:{ev['id']}")
    assert any(":replace_waiting:" in k for k in r.alert_keys())
    assert [a["severity"] for a in r.store.recent_alerts(200) if ":replace_waiting:" in a["dedup_key"]][0] == "URGENT"
    r.broker.quote_exc = None
    r.clock.advance(60); r.tick()
    assert len(r.broker.sells) == 2 and r.store.runtime_get(f"replace_waiting:{ev['id']}") is None


def test_cancel_unknown_never_replaces(r):
    r.broker.cancel_exc = OrderUnknown("timeout")
    after_301(r)
    assert r.orders()[0]["state"] == "CANCEL_UNKNOWN"
    for _ in range(4):
        r.clock.advance(60); r.tick()
    assert len(r.broker.sells) == 1


def test_duplicate_tick_and_concurrent_worker_cancel_once(r):
    r.clock.advance(301)
    r.tick(); r.tick()                                          # 같은 시각 중복 tick
    assert r.broker.cancels == ["1000"]
    r.store.acquire_lease("other", r.clock(), 90)               # 다른 작업자가 lease 보유
    assert r.new_engine("w2").tick()["status"] == "LEASE_BUSY"
    assert r.broker.cancels == ["1000"]


def test_reserved_sellable_does_not_block_replace(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, price_interval_sec=60)
    pid = h.add()
    h.broker.price = Decimal("44")
    h.tick()
    h.broker.holding["T"]["sellable"] = 0                       # 브로커가 미체결 매도분을 매도가능에서 뺀 경우
    h.clock.advance(301); h.tick()
    assert h.broker.cancels == ["1000"]                         # 취소 후 매도가능 = min(보유, 0 + 미체결 10)
    h.broker.fill("1000", 0, state="CANCELLED")
    h.broker.holding["T"]["sellable"] = 10                      # 취소 확정 후 브로커 값 복원
    h.clock.advance(30); h.tick()
    assert len(h.broker.sells) == 2 and h.broker.sells[1]["qty"] == 10
    assert h.pos(pid)["state"] == "EXITING"


def test_shadow_virtual_cancel_and_replace_only_with_valid_quote(tmp_path, monkeypatch):
    holder = {}
    h = Harness(tmp_path, monkeypatch, env=SHADOW_ENV, mode="SHADOW", shadow_replace_enabled=True,
                price_interval_sec=60, factory=lambda mode: holder["s"])
    reader = h.broker = NoOrderReader(h.clock)                  # 실주문·실취소 호출 시 실패
    holder["s"] = shadow = ShadowBroker(reader, "NEXT_QUOTE", h.clock)
    calls = []
    real = shadow.cancel
    shadow.cancel = lambda *a: calls.append(a) or real(*a)
    pid = h.add(mode="SHADOW")
    reader.price = Decimal("44"); h.tick()
    reader.price = Decimal("40"); h.clock.advance(30); h.tick()   # 지정가 43.78 아래 → 가상 미체결
    reader.quote_exc = auth()
    h.clock.advance(301); h.tick()
    assert calls == []                                          # Codex 재현: 이전 코드는 여기서 가상 취소
    reader.quote_exc = None
    h.clock.advance(60); h.tick()
    assert len(calls) == 1
    h.clock.advance(30); h.tick()                               # 가상 취소 확정 → 최신 시세로 재주문
    orders = h.orders()
    assert orders[0]["state"] == "CANCELLED" and orders[1]["kind"] == "REPLACE"
    assert all(o["order_number"].startswith("V") for o in orders) and h.pos(pid)["state"] == "EXITING"


# ── P2: 일시중지 후 재개한 날의 슬롯 정합성 ─────────────────────────
def test_paused_then_resumed_day_slot_totals_reconcile(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, env=SHADOW_ENV, mode="SHADOW", price_interval_sec=60)
    t0 = ts(2026, 11, 23, 9, 30)
    h.clock.set(t0)
    for t in ("CINF", "EVRG"):
        h.broker.holding[t] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
    c = h.add(mode="SHADOW", ticker="CINF")
    h.add(mode="SHADOW", ticker="EVRG")
    g = Grid(h, t0)
    g.run(10)
    service.set_paused(h.store, c, True, now=h.clock())
    h.broker.age_by = {"EVRG": 500}                            # EVRG 는 종목 단위 무효 시세 (감시 실패 슬롯)
    g.run(12)
    service.set_paused(h.store, c, False, now=h.clock())
    h.broker.age_by = {}
    g.run(10)
    # 슬롯 경계에서 보고 (기존 기대 기준은 진행 중인 슬롯을 세지 않는다: 구간 ÷ 60 내림)
    rep = service.daily_report(h.store, "2026-11-23", "SHADOW", h.st, now=h.clock() + 30)
    w = rep["watch"]
    keys = ("held_slots", "paused_slots", "failed_slots", "schedule_missed")
    for p in w["positions"]:
        assert p["missed_checks"] == p["expected_checks"] - p["ok_checks"]
        assert p["missed_checks"] == sum(p[k] for k in keys)
    for k in keys + ("missed_checks", "ok_checks", "expected_checks"):
        total_key = {"missed_checks": "missed", "ok_checks": "ok", "expected_checks": "expected"}.get(k, k)
        assert w[total_key] == sum(p[k] for p in w["positions"])
    cinf = [p for p in w["positions"] if p["ticker"] == "CINF"][0]
    assert cinf["slots"]["paused"] == 6                          # 완료 슬롯의 실제 분류 (절삭 없음)
    assert cinf["paused_slots"] == 6 and cinf["held_slots"] == 0 and cinf["expected_checks"] == 16
    evrg = [p for p in w["positions"] if p["ticker"] == "EVRG"][0]
    assert evrg["failed_slots"] + evrg["held_slots"] >= 1 and evrg["paused_slots"] == 0
    assert w["paused_slots"] == 6 and "일시중지 6" in service.report_text(rep)


# ── 기존 장애 상태 변환 원자성 ───────────────────────────────────
def test_legacy_migration_is_atomic_and_exactly_once(h, monkeypatch):
    ids = setup(h)
    h.store.runtime_set("api_errors", {"quote": 6}, h.clock())
    real = h.store._audit_db

    def boom(db, now, actor, action, target, details):
        if action == "quote_health_migrate":
            raise RuntimeError("강제 종료 흉내")
        return real(db, now, actor, action, target, details)
    monkeypatch.setattr(h.store, "_audit_db", boom)
    with pytest.raises(RuntimeError):
        h.tick()
    # 부분 변환 없음
    for key in ("quote_health_version", "quote_global_hold", "quote_health:LIVE"):
        assert h.store.runtime_get(key) is None, key
    assert h.store.runtime_get("api_errors")["quote"] == 6
    assert h.store.audits("quote_hold_on", 0, h.clock() + 1) == []
    monkeypatch.setattr(h.store, "_audit_db", real)
    h.engine = h.new_engine("w2")                               # 재시작 후 재변환
    h.store.release_lease("w1", h.clock())
    for _ in range(4):
        h.tick()
        h.engine = h.new_engine("w3")                           # 반복 재시작
    assert len(h.store.audits("quote_health_migrate", 0, h.clock() + 1)) == 1
    assert len(h.store.audits("quote_hold_on", 0, h.clock() + 1)) == 1
    i = inc(h)
    assert i["reason"] == "legacy_common_counter" and set(i["affected"]) == {str(v) for v in ids.values()}
    assert h.store.runtime_get("api_errors")["quote"] == 6
    health = h.store.runtime_get("quote_health:LIVE")
    assert all(health[str(v)]["fault_reason"] in ("legacy_unattributed", None) for v in ids.values())
