"""스캐너 후보 매수(account1 미국, account4 국내) 저널·전송 테스트. 가짜 브로커만 사용한다."""
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from trading.scanner_buy_orders import (
    Account1LiveBroker,
    Account4LiveBroker,
    ScannerBuyBlocked,
    ScannerBuyOrders,
    kr_tick_size,
    parse_limit_price,
)
from trading.kiwoom_orders import OrderRejected, OrderUnknown
from trading.kiwoom_readonly import KiwoomConfig, KiwoomError


class FakeBroker:
    def __init__(self, *, price="100.00", max_qty=50, buy_error=None, as_of=1000.0,
                 exchange="ND", upper=None):
        self.price, self.max_qty, self.buy_error = Decimal(price), max_qty, buy_error
        self.as_of, self.exchange, self.upper = as_of, exchange, upper
        self.buys, self.capacity_calls, self.lookups = [], [], []
        self.match = None

    def quote(self, ticker):
        quote = {"exchange": self.exchange, "price": self.price}
        if self.exchange == "KRX":
            quote.update(upper_limit=self.upper, lower_limit=None)
        else:
            quote["as_of"] = self.as_of
        return quote

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


def kr_candidate(**overrides):
    values = dict(id=8, market="KR", ticker="005930", name="삼성전자", stop_loss=65_000.0,
                  strict_filter_passed=True)
    values.update(overrides)
    return values


@pytest.fixture
def setup(tmp_path):
    brokers = {"US": FakeBroker(), "KR": FakeBroker(price="70000", exchange="KRX", upper=Decimal("91000"))}
    clock = Clock()
    service = ScannerBuyOrders(tmp_path / "journal.sqlite3", lambda market: brokers[market], clock=clock)
    return service, brokers["US"], clock, brokers["KR"]


def test_preview_records_prepared_order_without_sending(setup):
    service, broker, _, _ = setup
    row = service.prepare(candidate(), 3, 101.5)

    assert row["state"] == "PREPARED"
    assert (row["account_profile"], row["market"], row["currency"]) == ("account1", "US", "USD")
    assert (row["ticker"], row["exchange"], row["quantity"], row["price"]) == ("NVDA", "ND", 3, "101.50")
    assert row["details"]["risk"] == {"stop_loss": "92.00", "risk_per_share": "9.50", "total_risk": "28.50"}
    assert broker.buys == []


@pytest.mark.parametrize("overrides,message", [
    ({"market": "JP"}, "지원하지 않는 시장"),
    ({"strict_filter_passed": False}, "Strict"),
    ({"stop_loss": 101.5}, "손절가"),
    ({"ticker": "BAD TICKER"}, "종목코드"),
])
def test_preview_blocks_ineligible_candidates(setup, overrides, message):
    service, broker, _, _ = setup
    with pytest.raises(ScannerBuyBlocked, match=message):
        service.prepare(candidate(**overrides), 1, 101.5)
    assert broker.buys == []


def test_preview_blocks_fat_finger_price_and_sub_cent_price(setup):
    service, _, _, _ = setup
    with pytest.raises(ScannerBuyBlocked, match="10%"):
        service.prepare(candidate(), 1, 110.01)
    with pytest.raises(ScannerBuyBlocked, match="둘째 자리"):
        service.prepare(candidate(), 1, 100.001)


def test_preview_blocks_quantity_above_broker_capacity(setup):
    service, broker, _, _ = setup
    broker.max_qty = 2
    with pytest.raises(ScannerBuyBlocked, match="2주"):
        service.prepare(candidate(), 3, 100)


def test_execute_sends_once_after_fresh_capacity_check(setup):
    service, broker, _, _ = setup
    row = service.prepare(candidate(), 3, 100)
    broker.capacity_calls.clear()

    result = service.execute(row["id"], "nvda")

    assert result["state"] == "SUBMITTED" and result["order_number"] == "123456789"
    assert broker.capacity_calls == [("NVDA", "ND", "100.00")]
    assert broker.buys == [("NVDA", "ND", 3, "100.00")]
    with pytest.raises(ScannerBuyBlocked, match="이미 사용"):
        service.execute(row["id"], "NVDA")
    assert len(broker.buys) == 1


def test_execute_requires_matching_ticker_and_unexpired_preview(setup):
    service, broker, clock, _ = setup
    row = service.prepare(candidate(), 1, 100)
    with pytest.raises(ScannerBuyBlocked, match="종목코드"):
        service.execute(row["id"], "AAPL")
    clock.now += 121
    with pytest.raises(ScannerBuyBlocked, match="만료"):
        service.execute(row["id"], "NVDA")
    assert broker.buys == []


def test_capacity_drop_before_send_blocks_without_sending(setup):
    service, broker, _, _ = setup
    row = service.prepare(candidate(), 3, 100)
    broker.max_qty = 1
    with pytest.raises(ScannerBuyBlocked, match="1주"):
        service.execute(row["id"], "NVDA")
    assert broker.buys == [] and service.status(row["id"])["state"] == "BLOCKED"


def test_capacity_error_before_send_blocks_without_sending(setup):
    service, broker, _, _ = setup
    row = service.prepare(candidate(), 1, 100)
    broker.max_qty = KiwoomError("HTTP 오류")
    with pytest.raises(ScannerBuyBlocked):
        service.execute(row["id"], "NVDA")
    assert broker.buys == [] and service.status(row["id"])["state"] == "BLOCKED"


def test_rejection_is_recorded(setup):
    service, broker, _, _ = setup
    broker.buy_error = OrderRejected("미국주식 주문 거절: 장 종료")
    row = service.prepare(candidate(), 1, 100)
    result = service.execute(row["id"], "NVDA")
    assert result["state"] == "REJECTED" and "장 종료" in result["details"]["error"]


def test_unknown_response_locks_ticker_until_manual_resolution(setup):
    service, broker, _, _ = setup
    broker.buy_error = OrderUnknown("접수 여부 불명")
    first = service.prepare(candidate(), 1, 100)
    assert service.execute(first["id"], "NVDA")["state"] == "UNKNOWN"

    with pytest.raises(ScannerBuyBlocked, match="UNKNOWN"):
        service.prepare(candidate(), 1, 100)
    assert len(broker.buys) == 1

    resolved = service.resolve(first["id"], "NVDA", "키움 앱에서 접수 확인")
    assert resolved["state"] == "RESOLVED_MANUAL"
    assert resolved["details"]["resolved_from"] == "UNKNOWN"
    assert service.prepare(candidate(), 1, 100)["state"] == "PREPARED"


def test_two_previews_cannot_both_be_in_flight(setup):
    service, broker, _, _ = setup
    first = service.prepare(candidate(), 1, 100)
    second = service.prepare(candidate(), 1, 100)
    broker.buy_error = OrderUnknown("접수 여부 불명")
    service.execute(first["id"], "NVDA")

    with pytest.raises(ScannerBuyBlocked, match="확인되지 않은 주문"):
        service.execute(second["id"], "NVDA")
    assert len(broker.buys) == 1


def test_recent_sending_order_cannot_be_resolved(setup):
    service, _, clock, _ = setup
    row = service.prepare(candidate(), 1, 100)
    with service.db() as db:
        db.execute("UPDATE scanner_buy_orders SET state='SENDING', updated_at=? WHERE id=?",
                   (clock.now, row["id"]))
    with pytest.raises(ScannerBuyBlocked, match="전송 중"):
        service.resolve(row["id"], "NVDA")
    clock.now += 121
    assert service.resolve(row["id"], "NVDA")["state"] == "RESOLVED_MANUAL"


def test_refresh_records_broker_fill_state(setup):
    service, broker, _, _ = setup
    row = service.execute(service.prepare(candidate(), 3, 100)["id"], "NVDA")
    broker.match = {"state": "PARTIAL", "filled_quantity": 1, "remaining_quantity": 2}

    result = service.refresh(row["id"])

    assert result["broker_state"] == "PARTIAL"
    assert result["details"]["filled_quantity"] == 1
    assert broker.lookups[0]["order_number"] == "123456789"
    assert broker.lookups[0]["price"] == "100.00"


# ── 국내(account4) ─────────────────────────────────────────────

def test_kr_tick_size_and_price_parsing():
    assert [kr_tick_size(p) for p in (1_999, 4_995, 19_990, 49_950, 199_900, 499_500, 500_000)] \
        == [1, 5, 10, 50, 100, 500, 1_000]
    assert parse_limit_price("KR", 70_000) == Decimal("70000")
    with pytest.raises(ScannerBuyBlocked, match="호가단위"):
        parse_limit_price("KR", 70_050)
    with pytest.raises(ScannerBuyBlocked, match="정수"):
        parse_limit_price("KR", 70_000.5)


def test_kr_preview_uses_account4_and_won_risk(setup):
    service, us, _, kr = setup
    row = service.prepare(kr_candidate(), 10, 70_000)

    assert (row["account_profile"], row["market"], row["currency"], row["exchange"]) == \
        ("account4", "KR", "KRW", "KRX")
    assert row["price"] == "70000"
    assert row["details"]["risk"] == {"stop_loss": "65000", "risk_per_share": "5000", "total_risk": "50000"}
    assert not any("시세 시각" in w for w in row["details"]["warnings"])
    assert us.buys == [] and kr.buys == []


def test_kr_preview_blocks_above_upper_limit_and_fat_finger(setup):
    service, _, _, kr = setup
    kr.price, kr.upper = Decimal("85000"), Decimal("91000")
    with pytest.raises(ScannerBuyBlocked, match="상한가"):
        service.prepare(kr_candidate(), 1, 92_000)
    kr.price = Decimal("70000")
    with pytest.raises(ScannerBuyBlocked, match="10%"):
        service.prepare(kr_candidate(), 1, 78_000)


def test_kr_execute_sends_to_kr_broker_only(setup):
    service, us, _, kr = setup
    row = service.prepare(kr_candidate(), 2, 70_000)
    result = service.execute(row["id"], "005930")
    assert result["state"] == "SUBMITTED"
    assert kr.buys == [("005930", "KRX", 2, "70000")] and us.buys == []


def test_in_flight_lock_is_per_account(setup):
    service, us, _, kr = setup
    us.buy_error = OrderUnknown("x")
    service.execute(service.prepare(candidate(), 1, 100)["id"], "NVDA")
    # 다른 계좌의 다른 종목은 영향을 받지 않는다.
    assert service.prepare(kr_candidate(), 1, 70_000)["state"] == "PREPARED"


class DomesticClient:
    session = None

    def __init__(self, quote):
        self.quote = quote

    def get_domestic_quote(self, token, *, ticker):
        return {"quote": self.quote}


class DomesticEvidence:
    def __init__(self, cash="1000000", history=()):
        self.cash, self.rows = Decimal(cash), list(history)

    def orderable_cash(self):
        return self.cash

    def history(self):
        return self.rows


class DomesticOrders:
    def __init__(self):
        self.calls = []

    def buy_kr_limit(self, token, **kwargs):
        self.calls.append(kwargs)
        return {"order_number": "0001234"}


def kr_live(quote=None, **kwargs):
    return Account4LiveBroker(KiwoomConfig("key", "secret", "real"),
                              client=DomesticClient(quote or {}), token="t", **kwargs)


def test_account4_broker_requires_real_profile():
    with pytest.raises(KiwoomError):
        Account4LiveBroker(KiwoomConfig("key", "secret", "mock"), token="t")


def test_account4_quote_parses_signed_prices_and_checks_code():
    broker = kr_live({"stk_cd": "A005930", "cur_prc": "-70100", "upl_pric": "+91100", "lst_pric": "-49100"},
                     evidence=DomesticEvidence())
    assert broker.quote("005930") == {"exchange": "KRX", "price": Decimal("70100"),
                                      "upper_limit": Decimal("91100"), "lower_limit": Decimal("49100")}
    with pytest.raises(ScannerBuyBlocked, match="일치"):
        kr_live({"stk_cd": "000660", "cur_prc": "100"}, evidence=DomesticEvidence()).quote("005930")
    with pytest.raises(ScannerBuyBlocked, match="현재가"):
        kr_live({"stk_cd": "005930", "cur_prc": ""}, evidence=DomesticEvidence()).quote("005930")


def test_account4_capacity_uses_orderable_cash_with_reserve():
    broker = kr_live(evidence=DomesticEvidence(cash="1000000"), reserve_bps=100)
    # 1,000,000 × 0.99 = 990,000 → 70,000원 14주
    assert broker.buy_capacity("005930", "KRX", Decimal("70000"))["max_quantity_with_reserve"] == 14


def test_account4_buy_uses_krx_regular_limit():
    orders = DomesticOrders()
    broker = kr_live(evidence=DomesticEvidence(), orders=orders)
    assert broker.buy("005930", "KRX", 3, Decimal("70000"))["order_number"] == "0001234"
    assert orders.calls == [{"exchange": "KRX", "ticker": "005930", "quantity": 3, "price": 70000}]


@pytest.mark.parametrize("filled,remaining,state", [(0, 3, "OPEN"), (1, 2, "PARTIAL"), (3, 0, "FILLED"), (1, 0, "UNKNOWN")])
def test_account4_lookup_matches_order_identity(filled, remaining, state):
    row = {"order_number": "0001234", "ticker": "005930", "side": "BUY", "quantity": 3,
           "filled_quantity": filled, "remaining_quantity": remaining, "price": "70000"}
    broker = kr_live(evidence=DomesticEvidence(history=[row]))
    intent = {"order_number": "1234", "ticker": "005930", "quantity": 3, "price": "70000"}
    assert broker.lookup(intent)["state"] == state
    assert broker.lookup({**intent, "price": "71000"}) is None


# ── 미국(account1) 어댑터 ───────────────────────────────────────

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
    with pytest.raises(ScannerBuyBlocked, match="2건"):
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


def kr_scan_row():
    return scan_row(id=8, market="KR", ticker="005930", name="삼성전자", stop_loss=65_000.0)


@pytest.fixture
def api_env(tmp_path, monkeypatch):
    brokers = {"US": FakeBroker(), "KR": FakeBroker(price="70000", exchange="KRX")}
    monkeypatch.setenv("KIWOOM_WEB_ENABLED", "true")
    monkeypatch.setenv("SCANNER_ORDER_JOURNAL_PATH", str(tmp_path / "journal.sqlite3"))
    monkeypatch.setenv("KIWOOM_TRADING_ENABLED", "true")
    monkeypatch.setenv("ACCOUNT1_ORDER_ENABLED", "false")
    monkeypatch.setenv("ACCOUNT4_ORDER_ENABLED", "false")
    monkeypatch.setattr(api, "_broker", lambda market: brokers[market])
    return brokers


def preview(row, quantity=1, price=100):
    return api.preview_buy(api.BuyPreviewRequest(result_id=row.id, quantity=quantity, limit_price=price),
                           FakeDb(row))


def execute(order_id, ticker):
    return api.execute_buy(order_id, api.BuyConfirmRequest(confirmation_ticker=ticker))


def test_api_preview_reads_candidate_from_db_and_reports_disabled(api_env):
    result = preview(scan_row(), 2)
    assert result["state"] == "PREPARED" and result["account_profile"] == "account1"
    assert result["execution_enabled"] is False
    assert "ACCOUNT1_ORDER_ENABLED" in result["execution_block_reason"]


def test_api_execute_is_blocked_unless_both_switches_are_on(api_env, monkeypatch):
    order = preview(scan_row())
    with pytest.raises(HTTPException) as exc:
        execute(order["id"], "NVDA")
    assert exc.value.status_code == 503 and api_env["US"].buys == []

    monkeypatch.setenv("ACCOUNT1_ORDER_ENABLED", "true")
    monkeypatch.setenv("KIWOOM_TRADING_ENABLED", "false")
    with pytest.raises(HTTPException):
        execute(order["id"], "NVDA")
    assert api_env["US"].buys == []

    monkeypatch.setenv("KIWOOM_TRADING_ENABLED", "true")
    assert execute(order["id"], "NVDA")["state"] == "SUBMITTED" and len(api_env["US"].buys) == 1


def test_api_market_switches_are_independent(api_env, monkeypatch):
    monkeypatch.setenv("ACCOUNT1_ORDER_ENABLED", "true")
    order = preview(kr_scan_row(), 1, 70_000)
    assert order["account_profile"] == "account4" and order["execution_enabled"] is False
    with pytest.raises(HTTPException) as exc:
        execute(order["id"], "005930")
    assert exc.value.status_code == 503 and "ACCOUNT4_ORDER_ENABLED" in exc.value.detail
    assert api_env["KR"].buys == []

    monkeypatch.setenv("ACCOUNT4_ORDER_ENABLED", "true")
    assert execute(order["id"], "005930")["state"] == "SUBMITTED"
    assert api.capabilities("kr")["account_name"] == "ISA"


def test_api_hides_broker_errors(api_env, monkeypatch):
    def boom(market):
        raise KiwoomError("secret account 12345678")
    monkeypatch.setattr(api, "_broker", boom)
    with pytest.raises(HTTPException) as exc:
        preview(scan_row())
    assert exc.value.status_code == 503 and "12345678" not in exc.value.detail


def test_api_requires_journal_path(api_env, monkeypatch):
    monkeypatch.delenv("SCANNER_ORDER_JOURNAL_PATH")
    with pytest.raises(HTTPException) as exc:
        preview(scan_row())
    assert exc.value.status_code == 503
    assert api.capabilities("US")["execution_enabled"] is False
    with pytest.raises(HTTPException):
        api.capabilities("JP")
