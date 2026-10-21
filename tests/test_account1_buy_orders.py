"""account1 스캐너 후보 매수 저널·전송 테스트. 가짜 브로커만 사용한다."""
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from trading.account1_buy_orders import (
    Account1BuyBlocked,
    Account1BuyOrders,
    Account1LiveBroker,
)
from trading.kiwoom_orders import OrderRejected, OrderUnknown
from trading.kiwoom_readonly import KiwoomConfig, KiwoomError


class FakeBroker:
    def __init__(self, *, price="100.00", max_qty=50, buy_error=None, as_of=1000.0):
        self.price, self.max_qty, self.buy_error, self.as_of = Decimal(price), max_qty, buy_error, as_of
        self.buys, self.capacity_calls, self.lookups = [], [], []
        self.match = None

    def quote(self, ticker):
        return {"exchange": "ND", "price": self.price, "as_of": self.as_of}

    def buy_capacity(self, ticker, exchange, price):
        self.capacity_calls.append((ticker, exchange, str(price)))
        if isinstance(self.max_qty, Exception):
            raise self.max_qty
        return {"max_quantity_with_reserve": self.max_qty, "broker_orderable_cash": "9999.00"}

    def buy(self, ticker, exchange, quantity, price):
        self.buys.append((ticker, exchange, quantity, str(price)))
        if self.buy_error:
            raise self.buy_error
        return {"order_number": "123456789"}

    def lookup(self, intent):
        self.lookups.append(intent)
        return self.match


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def candidate(**overrides):
    values = dict(id=7, market="US", ticker="NVDA", name="NVIDIA", stop_loss=92.0,
                  strict_filter_passed=True)
    values.update(overrides)
    return values


@pytest.fixture
def setup(tmp_path):
    broker, clock = FakeBroker(), Clock()
    service = Account1BuyOrders(tmp_path / "journal.sqlite3", lambda: broker, clock=clock)
    return service, broker, clock


def test_preview_records_prepared_order_without_sending(setup):
    service, broker, _ = setup
    row = service.prepare(candidate(), 3, 101.5)

    assert row["state"] == "PREPARED"
    assert (row["ticker"], row["exchange"], row["quantity"], row["price"]) == ("NVDA", "ND", 3, "101.50")
    assert row["details"]["risk"] == {"stop_loss": "92.00", "risk_per_share": "9.50", "total_risk": "28.50"}
    assert broker.buys == []


@pytest.mark.parametrize("overrides,message", [
    ({"market": "KR", "ticker": "005930"}, "미국주식만"),
    ({"strict_filter_passed": False}, "Strict"),
    ({"stop_loss": 101.5}, "손절가"),
])
def test_preview_blocks_ineligible_candidates(setup, overrides, message):
    service, broker, _ = setup
    with pytest.raises(Account1BuyBlocked, match=message):
        service.prepare(candidate(**overrides), 1, 101.5)
    assert broker.buys == []


def test_preview_blocks_fat_finger_price_and_sub_cent_price(setup):
    service, _, _ = setup
    with pytest.raises(Account1BuyBlocked, match="10%"):
        service.prepare(candidate(), 1, 110.01)
    with pytest.raises(Account1BuyBlocked, match="둘째 자리"):
        service.prepare(candidate(), 1, 100.001)


def test_preview_blocks_quantity_above_broker_capacity(setup):
    service, broker, _ = setup
    broker.max_qty = 2
    with pytest.raises(Account1BuyBlocked, match="2주"):
        service.prepare(candidate(), 3, 100)


def test_execute_sends_once_after_fresh_capacity_check(setup):
    service, broker, _ = setup
    row = service.prepare(candidate(), 3, 100)
    broker.capacity_calls.clear()

    result = service.execute(row["id"], "nvda")

    assert result["state"] == "SUBMITTED" and result["order_number"] == "123456789"
    assert broker.capacity_calls == [("NVDA", "ND", "100.00")]
    assert broker.buys == [("NVDA", "ND", 3, "100.00")]
    with pytest.raises(Account1BuyBlocked, match="이미 사용"):
        service.execute(row["id"], "NVDA")
    assert len(broker.buys) == 1


def test_execute_requires_matching_ticker_and_unexpired_preview(setup):
    service, broker, clock = setup
    row = service.prepare(candidate(), 1, 100)
    with pytest.raises(Account1BuyBlocked, match="종목코드"):
        service.execute(row["id"], "AAPL")
    clock.now += 121
    with pytest.raises(Account1BuyBlocked, match="만료"):
        service.execute(row["id"], "NVDA")
    assert broker.buys == []


def test_capacity_drop_before_send_blocks_without_sending(setup):
    service, broker, _ = setup
    row = service.prepare(candidate(), 3, 100)
    broker.max_qty = 1
    with pytest.raises(Account1BuyBlocked, match="1주"):
        service.execute(row["id"], "NVDA")
    assert broker.buys == [] and service.status(row["id"])["state"] == "BLOCKED"


def test_capacity_error_before_send_blocks_without_sending(setup):
    service, broker, _ = setup
    row = service.prepare(candidate(), 1, 100)
    broker.max_qty = KiwoomError("HTTP 오류")
    with pytest.raises(Account1BuyBlocked):
        service.execute(row["id"], "NVDA")
    assert broker.buys == [] and service.status(row["id"])["state"] == "BLOCKED"


def test_rejection_is_recorded(setup):
    service, broker, _ = setup
    broker.buy_error = OrderRejected("미국주식 주문 거절: 장 종료")
    row = service.prepare(candidate(), 1, 100)
    result = service.execute(row["id"], "NVDA")
    assert result["state"] == "REJECTED" and "장 종료" in result["details"]["error"]


def test_unknown_response_locks_ticker_until_manual_resolution(setup):
    service, broker, clock = setup
    broker.buy_error = OrderUnknown("접수 여부 불명")
    first = service.prepare(candidate(), 1, 100)
    assert service.execute(first["id"], "NVDA")["state"] == "UNKNOWN"

    with pytest.raises(Account1BuyBlocked, match="UNKNOWN"):
        service.prepare(candidate(), 1, 100)
    assert len(broker.buys) == 1

    resolved = service.resolve(first["id"], "NVDA", "키움 앱에서 접수 확인")
    assert resolved["state"] == "RESOLVED_MANUAL"
    assert resolved["details"]["resolved_from"] == "UNKNOWN"
    assert service.prepare(candidate(), 1, 100)["state"] == "PREPARED"


def test_two_previews_cannot_both_be_in_flight(setup):
    service, broker, _ = setup
    first = service.prepare(candidate(), 1, 100)
    second = service.prepare(candidate(), 1, 100)
    broker.buy_error = OrderUnknown("접수 여부 불명")
    service.execute(first["id"], "NVDA")

    with pytest.raises(Account1BuyBlocked, match="확인되지 않은 주문"):
        service.execute(second["id"], "NVDA")
    assert len(broker.buys) == 1


def test_recent_sending_order_cannot_be_resolved(setup):
    service, _, clock = setup
    row = service.prepare(candidate(), 1, 100)
    with service.db() as db:
        db.execute("UPDATE account1_buy_orders SET state='SENDING', updated_at=? WHERE id=?",
                   (clock.now, row["id"]))
    with pytest.raises(Account1BuyBlocked, match="전송 중"):
        service.resolve(row["id"], "NVDA")
    clock.now += 121
    assert service.resolve(row["id"], "NVDA")["state"] == "RESOLVED_MANUAL"


def test_refresh_records_broker_fill_state(setup):
    service, broker, _ = setup
    row = service.execute(service.prepare(candidate(), 3, 100)["id"], "NVDA")
    broker.match = {"state": "PARTIAL", "filled_quantity": 1, "remaining_quantity": 2}

    result = service.refresh(row["id"])

    assert result["broker_state"] == "PARTIAL"
    assert result["details"]["filled_quantity"] == 1
    assert broker.lookups[0]["order_number"] == "123456789"
    assert broker.lookups[0]["price"] == "100.00"


def test_live_broker_requires_real_profile():
    with pytest.raises(KiwoomError):
        Account1LiveBroker(KiwoomConfig("key", "secret", "mock"), token="t")


class QuoteClient:
    def __init__(self, responses):
        self.responses = responses

    def get_overseas_orderbook(self, token, *, exchange, ticker):
        value = self.responses.get(exchange)
        if isinstance(value, Exception) or value is None:
            raise value or KiwoomError("no data")
        return {"quote": value}


def live_broker(responses):
    return Account1LiveBroker(KiwoomConfig("key", "secret", "real"),
                              client=QuoteClient(responses), token="t",
                              evidence=object(), orders=object())


def test_live_broker_resolves_single_matching_exchange():
    broker = live_broker({
        "ND": {"stk_cd": "NVDA", "stex_tp": "ND", "cur_prc": "+182.454", "dt": "20261020", "bid_tm": "15:59"},
        "NY": KiwoomError("not listed"),
        "NA": {"stk_cd": "NVDA", "stex_tp": "NA", "cur_prc": "0"},
    })
    quote = broker.quote("NVDA")
    assert quote["exchange"] == "ND" and quote["price"] == Decimal("182.45")
    assert quote["as_of"] is not None


def test_live_broker_refuses_ambiguous_exchange():
    same = {"stk_cd": "ABC", "cur_prc": "10"}
    broker = live_broker({"ND": {**same, "stex_tp": "ND"}, "NY": {**same, "stex_tp": "NY"}})
    with pytest.raises(Account1BuyBlocked, match="2건"):
        broker.quote("ABC")


# ── API ─────────────────────────────────────────────────────────

from web import scanner_order_api as api  # noqa: E402


class FakeDb:
    def __init__(self, row):
        self.row = row

    def query(self, model):
        return self

    def filter(self, *args):
        return self

    def first(self):
        return self.row


def scan_row(**overrides):
    values = dict(id=7, market="US", ticker="NVDA", name="NVIDIA", stop_loss=92.0,
                  strict_filter_passed=True)
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def api_env(tmp_path, monkeypatch):
    broker = FakeBroker()
    monkeypatch.setenv("KIWOOM_WEB_ENABLED", "true")
    monkeypatch.setenv("ACCOUNT1_ORDER_JOURNAL_PATH", str(tmp_path / "journal.sqlite3"))
    monkeypatch.setenv("KIWOOM_TRADING_ENABLED", "true")
    monkeypatch.setenv("ACCOUNT1_ORDER_ENABLED", "false")
    monkeypatch.setattr(api, "_broker", lambda: broker)
    return broker


def test_api_preview_reads_candidate_from_db_and_reports_disabled(api_env):
    body = api.BuyPreviewRequest(result_id=7, quantity=2, limit_price=100)
    result = api.preview_buy(body, FakeDb(scan_row()))
    assert result["state"] == "PREPARED" and result["account_profile"] == "account1"
    assert result["execution_enabled"] is False
    assert "ACCOUNT1_ORDER_ENABLED" in result["execution_block_reason"]


def test_api_execute_is_blocked_unless_both_switches_are_on(api_env, monkeypatch):
    preview = api.preview_buy(api.BuyPreviewRequest(result_id=7, quantity=1, limit_price=100),
                              FakeDb(scan_row()))
    with pytest.raises(HTTPException) as exc:
        api.execute_buy(preview["id"], api.BuyConfirmRequest(confirmation_ticker="NVDA"))
    assert exc.value.status_code == 503 and api_env.buys == []

    monkeypatch.setenv("ACCOUNT1_ORDER_ENABLED", "true")
    monkeypatch.setenv("KIWOOM_TRADING_ENABLED", "false")
    with pytest.raises(HTTPException):
        api.execute_buy(preview["id"], api.BuyConfirmRequest(confirmation_ticker="NVDA"))
    assert api_env.buys == []

    monkeypatch.setenv("KIWOOM_TRADING_ENABLED", "true")
    result = api.execute_buy(preview["id"], api.BuyConfirmRequest(confirmation_ticker="NVDA"))
    assert result["state"] == "SUBMITTED" and len(api_env.buys) == 1


def test_api_rejects_kr_candidate_and_hides_broker_errors(api_env, monkeypatch):
    with pytest.raises(HTTPException) as exc:
        api.preview_buy(api.BuyPreviewRequest(result_id=7, quantity=1, limit_price=70000),
                        FakeDb(scan_row(market="KR", ticker="005930")))
    assert exc.value.status_code == 409

    def boom():
        raise KiwoomError("secret account 12345678")
    monkeypatch.setattr(api, "_broker", boom)
    with pytest.raises(HTTPException) as exc:
        api.preview_buy(api.BuyPreviewRequest(result_id=7, quantity=1, limit_price=100),
                        FakeDb(scan_row()))
    assert exc.value.status_code == 503 and "12345678" not in exc.value.detail


def test_api_requires_journal_path(api_env, monkeypatch):
    monkeypatch.delenv("ACCOUNT1_ORDER_JOURNAL_PATH")
    with pytest.raises(HTTPException) as exc:
        api.preview_buy(api.BuyPreviewRequest(result_id=7, quantity=1, limit_price=100),
                        FakeDb(scan_row()))
    assert exc.value.status_code == 503
    assert api.capabilities()["execution_enabled"] is False
