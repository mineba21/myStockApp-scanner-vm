"""SHADOW 운영 전 안전장치: 긴급 중지, 계약 게이트, lease 연장, OFF 잔고 동기화, timeout, 429, STRESS, 일일 보고."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tests.auto_stop_fakes import FakeBroker, Harness, NoOrderReader, ts
from trading.auto_stop import service
from trading.auto_stop.brokers import LiveStopSellBroker, ShadowBroker
from trading.auto_stop.settings import CONTRACTS, LIVE_REQUIRED
from trading.auto_stop.store import AutoStopStore, ClaimRefused


@pytest.fixture
def h(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


def trigger(h, price="44.00"):
    h.broker.price = Decimal(price)
    h.tick()


# ── 전체 긴급 중지 ──────────────────────────────────────────────
def test_kill_switch_blocks_new_sell_and_persists_across_restart(h):
    pid = h.add()
    service.set_kill_switch(h.store, True, confirmation="stop", reason="점검", actor="t", now=h.clock())
    reopened = AutoStopStore(h.st.journal_path)                  # 재시작 후에도 유지
    assert reopened.kill_switch()["on"] is True
    trigger(h)
    assert h.event(pid)["state"] == "OPEN" and h.broker.sells == []
    assert any(k.startswith("kill:") for k in h.alert_keys())
    service.set_kill_switch(h.store, False, confirmation="RESUME", reason="", actor="t", now=h.clock())
    h.tick()
    assert len(h.broker.sells) == 1


def test_kill_switch_keeps_reconciling_and_never_auto_cancels(h):
    pid = h.add()
    trigger(h)
    service.set_kill_switch(h.store, True, confirmation="STOP", reason="r", actor="t", now=h.clock())
    h.clock.advance(400)
    h.tick(2)
    assert h.broker.cancels == []                                # 기존 접수 주문은 취소하지 않음
    h.broker.fill("1000", 10)
    h.tick()
    assert h.orders()[0]["state"] == "FILLED" and h.pos(pid)["state"] == "SOLD"


def test_kill_switch_checked_inside_claim_transaction(h):
    pid = h.add()
    eid = h.store.create_event(h.clock(), h.pos(pid), state="OPEN", trigger_price=Decimal("44"),
                               trigger_as_of=h.clock(), session_date="2026-11-23")
    h.store.acquire_lease("w1", h.clock(), 90)
    h.store.set_kill_switch(h.clock(), True, actor="t", reason="x")
    with pytest.raises(ClaimRefused, match="긴급 중지"):
        h.store.claim_sell(h.clock(), owner="w1", event_id=eid, kind="ORIGINAL", qty=1, ref_price=Decimal("44"),
                           ref_source="CUR", quote_as_of=h.clock(), limit_price=Decimal("43.78"),
                           settings=h.st, guard=lambda: None)
    assert h.orders() == []


def test_kill_switch_requires_phrase_reason_and_is_audited(h):
    with pytest.raises(service.Refused, match="STOP"):
        service.set_kill_switch(h.store, True, confirmation="yes", reason="r", actor="t")
    with pytest.raises(service.Refused, match="사유"):
        service.set_kill_switch(h.store, True, confirmation="STOP", reason="", actor="t")
    service.set_kill_switch(h.store, True, confirmation="STOP", reason="r", actor="web:1.2.3.4")
    import sqlite3
    rows = sqlite3.connect(h.st.journal_path).execute("SELECT actor, action FROM auto_stop_audit").fetchall()
    assert ("web:1.2.3.4", "kill_switch_on") in rows


# ── 미검증 계약 게이트 ──────────────────────────────────────────
def test_live_blocked_without_read_contracts(h):
    h.set(verified_contracts=frozenset())
    pid = h.add()
    trigger(h)
    assert h.event(pid)["state"] == "OPEN" and h.broker.sells == []
    assert any(":blocked:" in k for k in h.alert_keys())


def test_live_one_share_cap_until_sell_contract_verified(h):
    h.set(verified_contracts=frozenset(LIVE_REQUIRED))
    pid = h.add()
    trigger(h)
    assert h.broker.sells == [] and "1주" in h.event(pid)["review_reason"]


def test_live_one_share_position_can_sell_before_sell_contract(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    h.set(verified_contracts=frozenset(LIVE_REQUIRED))
    h.broker.holding["T"].update(qty=1, sellable=1)
    h.add(qty=1, broker_qty=1)
    trigger(h)
    assert len(h.broker.sells) == 1 and h.broker.sells[0]["qty"] == 1


def test_single_live_position_until_sell_contract_verified(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    h.set(verified_contracts=frozenset(LIVE_REQUIRED))
    h.broker.holding["T"].update(qty=1, sellable=1)
    h.broker.holding["U"] = {"qty": 1, "sellable": 1, "avg_cost": Decimal("60")}
    first = service.promote(h.store, h.broker, h.add(mode="SHADOW", qty=1, broker_qty=1),
                            confirmation_ticker="T", now=h.clock(), settings=h.st)
    assert first["mode"] == "LIVE"
    scan = {"id": 8, "market": "US", "ticker": "U", "signal_date": "2026-11-20", "stop_loss": 45.0,
            "strict_filter_passed": True}
    with pytest.raises(service.Refused, match="하나만"):
        service.register(h.store, h.broker, scan=scan, holding_id="kiwoom:account1:US:U", managed_qty=1,
                         mode="LIVE", confirmation_ticker="U", now=h.clock(), settings=h.st)
    assert len(h.store.positions(mode="LIVE")) == 1


def test_live_cancel_blocked_without_partial_cancel_contract(h):
    h.set(verified_contracts=frozenset(CONTRACTS) - {"UST20003_PARTIAL_CANCEL"})
    h.add()
    trigger(h)
    h.clock.advance(400)
    h.tick()
    assert h.broker.cancels == []


def test_live_ignores_bid_unless_verified(h):
    from trading.auto_stop.quotes import Quote
    h.set(verified_contracts=frozenset(CONTRACTS) - {"USA20101_BID"})
    h.add()
    h.broker.quote = lambda t, e: Quote(t, e, Decimal("44.00"), h.clock() - 30, "F", Decimal("43.50"))
    h.tick()
    assert h.broker.sells[0]["price"] == Decimal("43.78") and h.orders()[0]["ref_source"] == "CUR"


def test_live_carry_over_needs_post_close_contract(h):
    h.set(verified_contracts=frozenset(CONTRACTS) - {"POST_CLOSE_EXPIRY"}, replace_enabled=False)
    pid = h.add()
    h.clock.set(ts(2026, 11, 23, 15, 40))
    trigger(h)
    h.clock.set(ts(2026, 11, 23, 16, 5))
    h.broker.fill("1000", 0, state="CANCELLED")
    h.tick()
    h.clock.set(ts(2026, 11, 24, 9, 31)); h.tick()
    assert h.event(pid)["state"] == "NEEDS_REVIEW" and len(h.broker.sells) == 1


def test_trigger_after_cutoff_without_orders_still_carries_over_in_live(h):
    h.set(verified_contracts=frozenset(CONTRACTS) - {"POST_CLOSE_EXPIRY"})
    h.add()
    h.clock.set(ts(2026, 11, 23, 15, 56))
    trigger(h)
    h.clock.set(ts(2026, 11, 24, 9, 31)); h.tick()
    assert len(h.broker.sells) == 1


# ── lease·timeout·429 ──────────────────────────────────────────
def test_claim_extends_lease_for_http_window(h):
    pid = h.add()
    eid = h.store.create_event(h.clock(), h.pos(pid), state="OPEN", trigger_price=Decimal("44"),
                               trigger_as_of=h.clock(), session_date="2026-11-23")
    h.store.acquire_lease("w1", h.clock() - 80, 90)               # 10초 남음
    h.store.claim_sell(h.clock(), owner="w1", event_id=eid, kind="ORIGINAL", qty=1, ref_price=Decimal("44"),
                       ref_source="CUR", quote_as_of=h.clock(), limit_price=Decimal("43.78"),
                       settings=h.st, guard=lambda: None)
    assert h.store.acquire_lease("w2", h.clock() + 60, 90) is False   # 연장되어 60초 뒤에도 소유


def _cfg(timeout):
    from trading.kiwoom_readonly import REAL_BASE_URL
    return SimpleNamespace(mode="real", base_url=REAL_BASE_URL, timeout_seconds=timeout)


def test_order_capable_broker_rejects_long_http_timeout():
    with pytest.raises(Exception, match="timeout"):
        LiveStopSellBroker(_cfg(60), client=SimpleNamespace(session=None), token="t", evidence=object(),
                           orders=object(), allow_orders=True)
    b = LiveStopSellBroker(_cfg(60), client=SimpleNamespace(session=None), token="t", evidence=object())
    assert b.orders is None                                         # 조회 전용은 허용


def test_http_429_is_counted_per_broker():
    class Session:
        def post(self, *a, **k):
            return SimpleNamespace(status_code=429)
    client = SimpleNamespace(session=Session())
    b = LiveStopSellBroker(_cfg(15), client=client, token="t", evidence=object())
    client.session.post("u"); client.session.post("u")
    assert b.pop_429() == 2 and b.pop_429() == 0


# ── OFF 에서도 잔고 동기화 ──────────────────────────────────────
def test_off_mode_still_syncs_live_balance_without_orders(h):
    pid = h.add()
    h.set(mode="OFF")
    h.broker.holding["T"]["qty"] = 0
    h.broker.price = Decimal("40")
    h.tick()
    assert h.pos(pid)["state"] == "EXTERNAL_CLOSED"
    assert h.broker.sells == [] and h.broker.quote_calls == 0


# ── STRESS 가상 체결 (엔진 경로) ───────────────────────────────
def test_stress_model_exercises_partial_cancel_delay_and_replace(tmp_path, monkeypatch):
    env = {"KIWOOM_WEB_ENABLED": "true", "AUTO_STOP_US_MODE": "SHADOW"}
    holder = {}
    h = Harness(tmp_path, monkeypatch, env=env, mode="SHADOW",
                factory=lambda mode: ShadowBroker(holder["r"], "STRESS", h.clock))
    holder["r"] = NoOrderReader(h.clock)
    h.broker = holder["r"]
    pid = h.add(mode="SHADOW")
    trigger(h)
    h.clock.advance(61); h.tick()
    o = h.orders()[0]
    assert o["state"] == "PARTIAL_FILLED" and o["filled_qty"] == 5
    h.clock.advance(250); h.tick()                                 # 300초 경과 → 가상 취소 요청
    assert h.store.order(o["id"])["state"] == "CANCEL_PENDING"
    h.clock.advance(30); h.tick()
    assert h.store.order(o["id"])["state"] == "CANCEL_PENDING" and len(h.orders()) == 1   # 취소 지연
    h.clock.advance(70); h.tick()
    assert h.store.order(o["id"])["state"] == "CANCELLED"
    assert len(h.orders()) == 2 and h.orders()[1]["kind"] == "REPLACE" and h.orders()[1]["requested_qty"] == 5
    h.clock.advance(60); h.tick()
    assert h.pos(pid)["state"] == "SOLD"


# ── 일일 보고 ──────────────────────────────────────────────────
def test_daily_report_without_trigger_shows_watch_health(tmp_path, monkeypatch):
    env = {"KIWOOM_WEB_ENABLED": "true", "AUTO_STOP_US_MODE": "SHADOW"}
    h = Harness(tmp_path, monkeypatch, env=env, mode="SHADOW", price_interval_sec=60)
    h.clock.set(ts(2026, 11, 23, 9, 30))
    h.add(mode="SHADOW")
    for _ in range(20):
        h.tick(); h.clock.advance(30)
    rep = service.daily_report(h.store, "2026-11-23", "SHADOW", h.st, now=h.clock())
    assert rep["trading_day"] and rep["watched_positions"] == 1 and rep["triggers"] == 0
    assert rep["watch"]["ok"] == 10 and rep["watch"]["expected"] == 10 and rep["watch"]["missed"] == 0
    assert rep["ticks"]["actual"] == 20 and rep["ticks"]["max_gap_sec"] == 30 and rep["heartbeat"]["ok"]
    assert "실제 체결률" in rep["note"]
    assert "정상 감시 10/10" in service.report_text(rep)


def test_daily_report_counts_misses_errors_and_virtual_fill_vs_stop(tmp_path, monkeypatch):
    from trading.auto_stop.quotes import QuoteUnavailable
    env = {"KIWOOM_WEB_ENABLED": "true", "AUTO_STOP_US_MODE": "SHADOW"}
    holder = {}
    h = Harness(tmp_path, monkeypatch, env=env, mode="SHADOW",
                factory=lambda mode: ShadowBroker(holder["r"], "NEXT_QUOTE", h.clock))
    holder["r"] = h.broker
    h.clock.set(ts(2026, 11, 23, 9, 30))
    h.add(mode="SHADOW")
    h.tick(); h.clock.advance(60)
    h.broker.quote_exc = QuoteUnavailable("x")
    h.tick(); h.clock.advance(60)
    h.broker.quote_exc = None
    h.broker.price = Decimal("44")
    h.tick(); h.clock.advance(60)
    h.tick()
    rep = service.daily_report(h.store, "2026-11-23", "SHADOW", h.st, now=h.clock())
    assert rep["triggers"] == 1 and rep["orders"]["filled"] == 1
    assert rep["api"]["by_category"].get("quote") == 1
    f = rep["fills_vs_stop"][0]
    assert f["fill_price_basis"] == "limit_price" and f["vs_stop_pct"] == pytest.approx(-2.71, abs=0.01)
    assert service.daily_report(h.store, "2026-11-26", "SHADOW", h.st)["trading_day"] is False


def test_session_summary_alert_uses_daily_report(h):
    h.add()
    h.tick()
    h.clock.set(ts(2026, 11, 23, 16, 10)); h.tick()
    text = [a["text"] for a in h.store.recent_alerts() if a["dedup_key"] == "session:2026-11-23:LIVE:summary"][0]
    assert "일일 보고" in text and "정상 감시" in text


# ── API: 인증, 긴급 중지, 보고서, 실주문 클라이언트 미호출 ─────
@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import scheduler as sched
    from trading import kiwoom_orders
    from trading.auto_stop import runner
    from web import app as app_module

    def forbidden(*a, **k):
        raise AssertionError("실주문 클라이언트 호출")
    monkeypatch.setattr(kiwoom_orders.KiwoomOrderClient, "sell_us_limit", forbidden)
    monkeypatch.setattr(kiwoom_orders.KiwoomOrderClient, "cancel_us_order", forbidden)
    monkeypatch.setattr(sched, "start_scheduler", lambda: None)
    monkeypatch.setattr(app_module, "start_scheduler", lambda: None, raising=False)
    monkeypatch.setattr(app_module, "stop_scheduler", lambda: None, raising=False)
    monkeypatch.setattr(app_module, "SITES_API_KEY", "k" * 32)
    monkeypatch.setenv("SCANNER_ORDER_JOURNAL_PATH", str(tmp_path / "j.sqlite"))
    monkeypatch.setenv("AUTO_STOP_US_MODE", "SHADOW")
    monkeypatch.setattr(runner, "live_reader", lambda allow_orders=False: FakeBroker(lambda: ts(2026, 11, 23, 10, 0)))
    c = TestClient(app_module.app)
    c.headers["Authorization"] = "Bearer " + "k" * 32
    return c


def test_api_requires_auth_for_every_auto_stop_route(client):
    from fastapi.testclient import TestClient
    from web import app as app_module
    anon = TestClient(app_module.app)
    for method, path in [("get", "/api/auto-stop/health"), ("get", "/api/auto-stop/positions"),
                         ("post", "/api/auto-stop/kill-switch"), ("get", "/api/auto-stop/report"),
                         ("get", "/api/auto-stop/holdings"), ("post", "/api/auto-stop/orders/1/resolve")]:
        assert getattr(anon, method)(path).status_code == 401, path


def test_api_kill_switch_roundtrip(client):
    r = client.post("/api/auto-stop/kill-switch", json={"enabled": True, "confirmation": "nope", "reason": "x"})
    assert r.status_code == 409
    r = client.post("/api/auto-stop/kill-switch", json={"enabled": True, "confirmation": "STOP", "reason": "점검"})
    assert r.status_code == 200 and r.json()["kill_switch"]["on"] is True
    assert client.get("/api/auto-stop/health").json()["kill_switch"]["reason"] == "점검"
    r = client.post("/api/auto-stop/kill-switch", json={"enabled": False, "confirmation": "RESUME"})
    assert r.status_code == 200 and client.get("/api/auto-stop/kill-switch").json()["kill_switch"] is None


def test_api_report_holdings_and_health_contract_view(client):
    assert client.get("/api/auto-stop/report?day=2026-11-26&mode=SHADOW").json()["trading_day"] is False
    assert client.get("/api/auto-stop/report?day=bad").status_code == 422
    holdings = client.get("/api/auto-stop/holdings").json()
    assert holdings == [{"holding_id": "kiwoom:account1:US:T", "ticker": "T", "qty": 10, "sellable": 10,
                         "avg_cost": "60"}]
    hs = client.get("/api/auto-stop/health").json()
    assert hs["shadow_fill_model"] == "NEXT_QUOTE" and set(hs["missing_live_contracts"]) == set(LIVE_REQUIRED)
    assert hs["live_qty_cap"] == 1


# ── SHADOW 전용 재주문 스위치 ──────────────────────────────────
SHADOW_ENV = {"KIWOOM_WEB_ENABLED": "true", "AUTO_STOP_US_MODE": "SHADOW"}


def _stress_harness(tmp_path, monkeypatch, **over):
    holder = {}
    over = {"shadow_fill_model": "STRESS", **over}
    h = Harness(tmp_path, monkeypatch, env=SHADOW_ENV, mode="SHADOW",
                factory=lambda mode: ShadowBroker(holder["r"], h.st.shadow_fill_model, h.clock), **over)
    holder["r"] = h.broker = NoOrderReader(h.clock)
    return h


def _run_stress(h, pid):
    trigger(h)
    h.clock.advance(61); h.tick()                 # 절반 가상 체결
    h.clock.advance(250); h.tick()                # 300초 경과 → 가상 취소 판단
    h.clock.advance(100); h.tick()                # 취소 지연 90초 경과
    h.clock.advance(60); h.tick()


def test_shadow_replace_on_live_replace_off(tmp_path, monkeypatch):
    h = _stress_harness(tmp_path, monkeypatch, replace_enabled=False, shadow_replace_enabled=True)
    pid = h.add(mode="SHADOW")
    _run_stress(h, pid)
    kinds = [o["kind"] for o in h.orders()]
    assert kinds == ["ORIGINAL", "REPLACE"] and h.pos(pid)["state"] == "SOLD"
    assert all(o["order_number"].startswith("V") for o in h.orders())
    # 같은 조합에서 LIVE 는 자동 취소하지 않는다
    (tmp_path / "live").mkdir()
    live = Harness(tmp_path / "live", monkeypatch, replace_enabled=False, shadow_replace_enabled=True)
    live.add()
    trigger(live)
    live.clock.advance(400)
    live.tick(2)
    assert len(live.broker.sells) == 1 and live.broker.cancels == []


def test_shadow_replace_off_live_replace_on(tmp_path, monkeypatch):
    h = _stress_harness(tmp_path, monkeypatch, replace_enabled=True, shadow_replace_enabled=False)
    pid = h.add(mode="SHADOW")
    _run_stress(h, pid)
    orders = h.orders()
    assert len(orders) == 1 and orders[0]["state"] == "PARTIAL_FILLED" and orders[0]["cancel_sent_at"] is None
    assert h.pos(pid)["state"] == "EXITING"
    # 같은 조합에서 LIVE 는 자동 취소·재주문한다
    (tmp_path / "live").mkdir()
    live = Harness(tmp_path / "live", monkeypatch, replace_enabled=True, shadow_replace_enabled=False)
    live.add()
    trigger(live)
    live.clock.advance(400)
    live.tick()
    assert len(live.broker.cancels) == 1


@pytest.mark.parametrize("live_flag", ["true", "false"])
@pytest.mark.parametrize("shadow_flag", ["true", "false"])
@pytest.mark.parametrize("model", ["IMMEDIATE", "NEXT_QUOTE", "STRESS"])
def test_shadow_never_creates_or_calls_real_order_client(tmp_path, monkeypatch, live_flag, shadow_flag, model):
    """실거래 스위치가 모두 켜져 있어도 SHADOW 는 주문 객체를 만들지 않고 실주문·실취소를 호출하지 않는다."""
    from trading import kiwoom_orders
    from trading.auto_stop import runner

    def forbidden(*a, **k):
        raise AssertionError("SHADOW 에서 실주문 클라이언트 호출")
    monkeypatch.setattr(kiwoom_orders.KiwoomOrderClient, "__init__", forbidden)
    monkeypatch.setattr(kiwoom_orders.KiwoomOrderClient, "sell_us_limit", forbidden)
    monkeypatch.setattr(kiwoom_orders.KiwoomOrderClient, "cancel_us_order", forbidden)
    env = {**SHADOW_ENV, "KIWOOM_TRADING_ENABLED": "true", "ACCOUNT1_ORDER_ENABLED": "true",
           "AUTO_STOP_US_REPLACE_ENABLED": live_flag, "AUTO_STOP_US_SHADOW_REPLACE_ENABLED": shadow_flag,
           "AUTO_STOP_US_SHADOW_FILL_MODEL": model}
    requested = []
    holder = {}

    def fake_reader(*, allow_orders=False):
        requested.append(allow_orders)
        return holder["r"]
    monkeypatch.setattr(runner, "live_reader", fake_reader)
    h = Harness(tmp_path, monkeypatch, env=env, mode="SHADOW", shadow_fill_model=model,
                replace_enabled=live_flag == "true", shadow_replace_enabled=shadow_flag == "true",
                factory=runner.broker_factory)
    holder["r"] = h.broker = NoOrderReader(h.clock)
    monkeypatch.setattr(runner, "ShadowBroker", lambda reader, m: ShadowBroker(reader, m, h.clock))
    pid = h.add(mode="SHADOW")
    _run_stress(h, pid)
    assert requested and not any(requested)                # SHADOW 는 allow_orders=False 로만 만든다
    assert h.orders() and all(o["mode"] == "SHADOW" and o["order_number"].startswith("V") for o in h.orders())
    assert h.store.orders(mode="LIVE") == []


def test_shadow_orders_keep_creation_model_and_report_separates_models(tmp_path, monkeypatch):
    h = _stress_harness(tmp_path, monkeypatch, shadow_fill_model="NEXT_QUOTE")
    h.broker.holding["U"] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
    t = h.add(mode="SHADOW")
    trigger(h)                                         # NEXT_QUOTE 주문 (같은 시세로는 체결하지 않음)
    h.set(shadow_fill_model="STRESS")
    h.engine = h.new_engine("w1")
    h.clock.advance(61); h.tick()
    first = h.orders()[0]
    assert first["state"] == "FILLED" and first["filled_qty"] == 10   # STRESS 절반 체결이 아니라 NEXT_QUOTE 로 판정
    u = h.add(mode="SHADOW", ticker="U")
    trigger(h)
    h.clock.advance(61); h.tick()
    assert h.store.order(h.orders()[-1]["id"])["state"] == "PARTIAL_FILLED"
    rep = service.daily_report(h.store, "2026-11-23", "SHADOW", settings=h.st, now=h.clock())
    sh = rep["shadow"]
    assert set(sh["by_model"]) == {"NEXT_QUOTE", "STRESS"} and sh["mixed_models"] is True
    assert sh["by_model"]["NEXT_QUOTE"]["filled"] == 1 and sh["by_model"]["STRESS"]["partial_or_open"] == 1
    assert [c["to"]["fill_model"] for c in sh["config_changes"]] == ["NEXT_QUOTE", "STRESS"]
    assert sh["current"] == {"fill_model": "STRESS", "shadow_replace_enabled": True}
    text = service.report_text(rep)
    assert "[NEXT_QUOTE]" in text and "[STRESS]" in text and "모델 혼재" in text
    assert any(k.startswith("shadow_config:") for k in h.alert_keys())
    assert h.pos(t)["state"] == "SOLD" and h.pos(u)["state"] == "EXITING"


def test_stress_events_never_move_to_live(tmp_path, monkeypatch):
    h = _stress_harness(tmp_path, monkeypatch, shadow_replace_enabled=True)
    h.broker.holding["U"] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
    busy = h.add(mode="SHADOW")
    trigger(h)                                         # STRESS 이벤트 진행 중
    idle = h.add(mode="SHADOW", ticker="U")
    for k, v in {"KIWOOM_TRADING_ENABLED": "true", "ACCOUNT1_ORDER_ENABLED": "true",
                 "AUTO_STOP_US_MODE": "LIVE"}.items():
        monkeypatch.setenv(k, v)
    h.set(mode="LIVE")
    with pytest.raises(service.Refused, match="진행 중"):
        service.promote(h.store, h.broker, busy, confirmation_ticker="T", now=h.clock(), settings=h.st)
    h.broker.price = Decimal("50.00")
    live = service.promote(h.store, h.broker, idle, confirmation_ticker="U", now=h.clock(), settings=h.st)
    assert live["mode"] == "LIVE" and h.store.orders(mode="LIVE") == []
    assert not [e for e in h.store.events() if e["position_id"] == live["id"]]
    assert h.store.events(mode="LIVE") == []
