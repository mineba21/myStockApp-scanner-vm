"""등록·해제·LIVE 전환·증거 기반 UNKNOWN 해제·헬스·API (실계좌 호출 없음)."""
from decimal import Decimal

import pytest

from tests.auto_stop_fakes import Harness, ts
from trading.auto_stop import service
from trading.kiwoom_orders import OrderUnknown

HID = "kiwoom:account1:US:T"


def scan(**over):
    row = {"id": 7, "market": "US", "ticker": "T", "name": "Test", "signal_type": "BREAKOUT",
           "signal_date": "2026-11-20", "scan_time": None, "first_detected_at": None, "price": 52.0,
           "pivot_price": 51.0, "stop_loss": 45.0, "grade": "A", "strict_filter_passed": True}
    row.update(over)
    return row


@pytest.fixture
def h(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


def reg(h, **over):
    args = dict(scan=scan(), holding_id=HID, managed_qty=10, mode="LIVE", confirmation_ticker="t",
                now=h.clock(), settings=h.st)
    args.update(over)
    return service.register(h.store, h.broker, **args)


def test_register_ok_snapshots_signal_and_fixes_stop(h):
    p = reg(h)
    assert p["state"] == "ACTIVE" and p["stop_price"] == "45.00" and p["exchange"] == "ND"
    assert '"signal_type": "BREAKOUT"' in p["signal_snapshot"]
    assert any(k.endswith(":registered") for k in h.alert_keys())


@pytest.mark.parametrize("over,msg", [
    (dict(confirmation_ticker="X"), "확인 티커"),
    (dict(holding_id="kiwoom:account2:US:T"), "account1"),
    (dict(holding_id="kiwoom:account1:KR:T"), "account1"),
    (dict(scan=scan(market="KR")), "시장"),
    (dict(scan=scan(ticker="U")), "티커"),
    (dict(scan=scan(strict_filter_passed=None)), "Strict"),
    (dict(scan=scan(stop_loss=None)), "손절가"),
    (dict(scan=scan(stop_loss=0)), "손절가"),
    (dict(scan=scan(stop_loss=55)), "이미 손절가"),
    (dict(managed_qty=11), "보유 수량"),
    (dict(mode="SHADOW"), "운영 모드"),
    (dict(scan=scan(signal_date="2026-09-01")), "오래된"),
])
def test_register_rejections(h, over, msg):
    with pytest.raises(service.Refused, match=msg):
        reg(h, **over)
    assert h.store.positions() == []


def test_register_stale_with_acknowledgement(h):
    assert reg(h, scan=scan(signal_date="2026-09-01"), acknowledge_stale=True)["state"] == "ACTIVE"


def test_register_duplicate_and_live_switch(h, monkeypatch):
    reg(h)
    with pytest.raises(service.Refused, match="이미"):
        reg(h)
    monkeypatch.setenv("ACCOUNT1_ORDER_ENABLED", "false")
    with pytest.raises(service.Refused, match="LIVE 등록 불가"):
        reg(h, holding_id="kiwoom:account1:US:T")


def test_register_requires_holding_and_caps(h):
    h.broker.holding = {}
    with pytest.raises(service.Refused, match="보유하지"):
        reg(h)
    h.broker.holding = {"T": {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}}
    h.set(max_order_notional_usd=Decimal("100"))
    with pytest.raises(service.Refused, match="상한"):
        reg(h, settings=h.st)


def test_pause_disable_rules(h):
    p = reg(h)
    service.set_paused(h.store, p["id"], True, now=h.clock())
    assert h.pos(p["id"])["auto_paused"] == 1
    service.set_paused(h.store, p["id"], False, now=h.clock())
    h.broker.price = Decimal("44")
    h.tick()                                           # 매도 주문 진행 중
    with pytest.raises(service.Refused, match="미확정"):
        service.disable(h.store, p["id"], now=h.clock())
    h.broker.fill("1000", 0, state="CANCELLED")         # 외부 취소 → review
    h.tick()
    out = service.disable(h.store, p["id"], now=h.clock())
    assert out["state"] == "DISABLED" and h.event(p["id"])["state"] == "DONE"


def test_promote_shadow_to_live_revalidates(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    sid = h.add(mode="SHADOW")
    live = service.promote(h.store, h.broker, sid, confirmation_ticker="T", now=h.clock(), settings=h.st)
    assert live["mode"] == "LIVE" and live["state"] == "ACTIVE" and live["source"] == "PROMOTED"
    assert h.pos(sid)["state"] == "DISABLED"
    assert h.store.orders(mode="LIVE") == []


def test_promote_below_stop_registers_needs_review(h):
    sid = h.add(mode="SHADOW")
    h.broker.price = Decimal("44")
    live = service.promote(h.store, h.broker, sid, confirmation_ticker="T", now=h.clock(), settings=h.st)
    assert live["state"] == "NEEDS_REVIEW"
    h.tick(2)
    assert h.broker.sells == []


def test_promote_refuses_when_broker_qty_short(h):
    sid = h.add(mode="SHADOW")
    h.broker.holding["T"]["qty"] = 3
    with pytest.raises(service.Refused, match="적습니다"):
        service.promote(h.store, h.broker, sid, confirmation_ticker="T", now=h.clock(), settings=h.st)


def _unknown_order(h):
    pid = h.add()
    h.broker.sell_exc = OrderUnknown("lost")
    h.broker.price = Decimal("44")
    h.tick()
    h.broker.sell_exc = None
    return pid, h.orders()[0]


def test_resolve_unknown_requires_matching_broker_evidence(h):
    pid, o = _unknown_order(h)
    with pytest.raises(service.Refused, match="주문번호"):
        service.resolve_unknown(h.store, h.broker, o["id"], order_number=None, confirmation_ticker="T")
    with pytest.raises(service.Refused, match="찾지 못했"):
        service.resolve_unknown(h.store, h.broker, o["id"], order_number="777", confirmation_ticker="T")
    assert h.store.order(o["id"])["state"] == "UNKNOWN"
    h.broker.rows["777"] = {"order_number": "777", "ticker": "T", "side": "SELL", "quantity": 10,
                            "state": "FILLED", "filled_quantity": 10, "remaining_quantity": 0}
    h.broker.holding["T"].update(qty=0, sellable=0)
    with pytest.raises(service.Refused, match="확인 티커"):
        service.resolve_unknown(h.store, h.broker, o["id"], order_number="777", confirmation_ticker="X")
    out = service.resolve_unknown(h.store, h.broker, o["id"], order_number="777", confirmation_ticker="T",
                                  now=h.clock())
    assert out["state"] == "FILLED" and out["order_number"] == "777"
    assert h.event(pid)["state"] == "NEEDS_REVIEW"
    h.tick()
    assert h.pos(pid)["state"] == "SOLD" and len(h.broker.sells) == 0


def test_no_generic_state_reset_api_exists():
    from web import auto_stop_api
    paths = {r.path for r in auto_stop_api.router.routes}
    assert not any("reset" in p or "force" in p for p in paths)
    assert "/api/auto-stop/orders/{oid}/resolve" in paths


def test_health_reports_counts(h):
    reg(h)
    h.broker.price = Decimal("44")
    h.tick()
    hs = service.health(h.store, h.st, now=h.clock())
    assert hs["mode"] == "LIVE" and hs["unresolved_orders"] == 1 and hs["open_managed_qty"]["LIVE"] == 10
    assert hs["heartbeat_age_sec"] == 0 and hs["events"][0]["event_replace_count"] == 0


def test_api_health_without_journal_and_auth(monkeypatch):
    from fastapi.testclient import TestClient
    import scheduler as sched
    monkeypatch.setattr(sched, "start_scheduler", lambda: None)
    monkeypatch.setattr(sched, "stop_scheduler", lambda: None)
    from web import app as app_module
    monkeypatch.setattr(app_module, "start_scheduler", lambda: None, raising=False)
    monkeypatch.setattr(app_module, "stop_scheduler", lambda: None, raising=False)
    monkeypatch.setattr(app_module, "SITES_API_KEY", "test-key")
    monkeypatch.setenv("SCANNER_ORDER_JOURNAL_PATH", "")
    client = TestClient(app_module.app)
    assert client.get("/api/auto-stop/health").status_code in (401, 403)      # 인증 필수
    client.headers["Authorization"] = "Bearer test-key"
    r = client.get("/api/auto-stop/health")
    assert r.status_code == 200 and r.json()["mode"] == "OFF" and r.json()["journal"] is False
    r = client.post("/api/auto-stop/positions/1/pause")
    assert r.status_code == 503
