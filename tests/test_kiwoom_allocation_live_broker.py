from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from trading.kiwoom_allocation_live_broker import KiwoomAllocationLiveBroker
from trading.kiwoom_orders import OrderRejected
from trading.kiwoom_readonly import KiwoomConfig, KiwoomError


NOW = datetime(2026, 9, 15, 10, 29, 30,
               tzinfo=ZoneInfo("America/New_York")).timestamp()


class Evidence:
    def __init__(self, *, max_quantity=10, open_orders=0):
        self.max_quantity = max_quantity
        self.open_orders = open_orders
        self.capacity_calls = []

    def cash_check(self, day, include_previous_day=False):
        assert include_previous_day is True
        return {"broker_orderable_cash": "1044.31",
                "open_order_count": self.open_orders}

    def buy_capacity(self, ticker, exchange, price, *, reserve_bps):
        self.capacity_calls.append((ticker, exchange, price, reserve_bps))
        return {"max_quantity_with_reserve": self.max_quantity}

    def lookup(self, intent):
        return {"state": "OPEN", "matched_intent_id": intent["id"]}


class Client:
    def __init__(self, *, quote_time="10:29", sellable="6"):
        self.quote_time = quote_time
        self.sellable = sellable
        self.balance_calls = []

    def get_overseas_account_balance(self, token, exchange="", ticker=""):
        self.balance_calls.append((exchange, ticker))
        return {"holdings": [{"stk_cd": ticker or "QQQ", "qty": "6", "poss_qty": "6",
                              "sell_alowq": self.sellable}]}

    def get_overseas_orderbook(self, token, *, exchange, ticker):
        return {"quote": {"stex_tp": exchange, "stk_cd": ticker,
                          "cur_prc": "+600.1250", "dt": "20260915",
                          "bid_tm": self.quote_time}}


class Orders:
    def __init__(self):
        self.calls = []

    def buy_us_limit(self, token, **kwargs):
        self.calls.append(("BUY", kwargs))
        return {"status": "submitted", "order_number": "123"}

    def sell_us_limit(self, token, **kwargs):
        self.calls.append(("SELL", kwargs))
        return {"status": "submitted", "order_number": "124"}


def broker(*, evidence=None, client=None, orders=None):
    return KiwoomAllocationLiveBroker(
        KiwoomConfig("key", "secret", mode="real"), client=client or Client(),
        token="token", evidence=evidence or Evidence(), orders=orders or Orders(),
        reserve_bps=100, clock=lambda: NOW,
    )


def test_live_adapter_rejects_mock_configuration():
    with pytest.raises(KiwoomError, match="real account2"):
        KiwoomAllocationLiveBroker(KiwoomConfig("key", "secret", mode="mock"))


def test_snapshot_uses_total_holdings_and_broker_net_cash():
    result = broker().snapshot()
    assert result["holdings"] == {"QQQ": 6}
    assert result["orderable_cash"] == "1044.31"
    assert result["cash_includes_reservations"] is True


def test_quotes_require_matching_recent_broker_timestamp():
    result = broker().quotes(["QQQ"])
    assert result["QQQ"]["limit_price"] == "600.13"
    assert result["QQQ"]["as_of"] == NOW - 30

    with pytest.raises(KiwoomError, match="60초"):
        broker(client=Client(quote_time="10:27")).quotes(["QQQ"])


def test_buy_checks_exact_price_capacity_before_single_order_call():
    evidence, orders = Evidence(max_quantity=2), Orders()
    result = broker(evidence=evidence, orders=orders).submit(
        {"ticker": "QQQ", "side": "BUY", "quantity": 2, "price": "600.13"}
    )
    assert result["order_number"] == "123"
    assert evidence.capacity_calls == [("QQQ", "ND", "600.13", 100)]
    assert len(orders.calls) == 1


def test_insufficient_buy_capacity_never_calls_order_api():
    orders = Orders()
    with pytest.raises(OrderRejected, match="주문가능수량"):
        broker(evidence=Evidence(max_quantity=1), orders=orders).submit(
            {"ticker": "QQQ", "side": "BUY", "quantity": 2, "price": "600.13"}
        )
    assert orders.calls == []


def test_sell_checks_fresh_sellable_quantity_before_order():
    orders = Orders()
    with pytest.raises(OrderRejected, match="매도가능수량"):
        broker(client=Client(sellable="1"), orders=orders).submit(
            {"ticker": "QQQ", "side": "SELL", "quantity": 2, "price": "600.13"}
        )
    assert orders.calls == []


class BookClient(Client):
    def __init__(self, book, **kw):
        super().__init__(**kw)
        self.book = book

    def get_overseas_orderbook(self, token, *, exchange, ticker):
        quote = super().get_overseas_orderbook(token, exchange=exchange, ticker=ticker)
        quote["quote"].update(self.book)
        return quote


def test_unset_orderbook_fields_keep_last_price_for_both_sides(monkeypatch):
    monkeypatch.delenv("KIWOOM_US_ORDERBOOK_ASK_FIELD", raising=False)
    monkeypatch.delenv("KIWOOM_US_ORDERBOOK_BID_FIELD", raising=False)
    quote = broker().quotes(["QQQ"])["QQQ"]
    assert quote["limit_price"] == quote["buy_limit"] == quote["sell_limit"] == "600.13"


def test_configured_orderbook_fields_set_marketable_side_limits(monkeypatch):
    monkeypatch.setenv("KIWOOM_US_ORDERBOOK_ASK_FIELD", "ask1")
    monkeypatch.setenv("KIWOOM_US_ORDERBOOK_BID_FIELD", "bid1")
    client = BookClient({"ask1": "+600.1400", "bid1": "-600.1200"})
    quote = broker(client=client).quotes(["QQQ"])["QQQ"]
    assert (quote["limit_price"], quote["buy_limit"], quote["sell_limit"]) == ("600.13", "600.14", "600.12")


@pytest.mark.parametrize("env,book", [
    ({"KIWOOM_US_ORDERBOOK_ASK_FIELD": "ask1"}, {"ask1": "600.14"}),          # only one side
    ({"KIWOOM_US_ORDERBOOK_ASK_FIELD": "ask1", "KIWOOM_US_ORDERBOOK_BID_FIELD": "bid1"},
     {"ask1": "600.14"}),                                                      # field missing
    ({"KIWOOM_US_ORDERBOOK_ASK_FIELD": "ask1", "KIWOOM_US_ORDERBOOK_BID_FIELD": "bid1"},
     {"ask1": "600.10", "bid1": "600.12"}),                                    # crossed
    ({"KIWOOM_US_ORDERBOOK_ASK_FIELD": "ask1", "KIWOOM_US_ORDERBOOK_BID_FIELD": "bid1"},
     {"ask1": "610.00", "bid1": "590.00"}),                                    # spread > 1%
])
def test_bad_orderbook_configuration_fails_closed(monkeypatch, env, book):
    monkeypatch.delenv("KIWOOM_US_ORDERBOOK_ASK_FIELD", raising=False)
    monkeypatch.delenv("KIWOOM_US_ORDERBOOK_BID_FIELD", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(KiwoomError):
        broker(client=BookClient(book)).quotes(["QQQ"])


def test_cancel_targets_the_original_order_number_once():
    calls = []

    class CancelOrders(Orders):
        def cancel_us_order(self, token, **kwargs):
            calls.append(kwargs)
            return {"status": "submitted", "order_number": "777"}

    result = broker(orders=CancelOrders()).cancel(
        {"ticker": "QQQ", "order_number": "0000123", "side": "BUY"})
    assert result["status"] == "submitted"
    assert calls == [{"exchange": "ND", "ticker": "QQQ", "original_order_number": "0000123"}]
    with pytest.raises(OrderRejected):
        broker(orders=CancelOrders()).cancel({"ticker": "QQQ", "order_number": None})
    assert len(calls) == 1
