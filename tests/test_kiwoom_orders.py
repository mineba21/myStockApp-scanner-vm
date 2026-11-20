from types import SimpleNamespace

import pytest

from trading.kiwoom_orders import KiwoomOrderClient, OrderUnknown
from trading.kiwoom_readonly import KiwoomConfig


class Session:
    def __init__(self, order_number="123456789"):
        self.calls = []
        self.order_number = order_number

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"return_code": 0, "ord_no": self.order_number},
        )


def test_order_client_sends_us_limit_sell_shape():
    session = Session()
    client = KiwoomOrderClient(KiwoomConfig("key", "secret", "mock"), session)

    result = client.sell_us_limit(
        "token", exchange="ND", ticker="SPY", quantity=2, price=650.25
    )

    assert result["order_number"] == "123456789"
    _, request = session.calls[0]
    assert request["headers"]["api-id"] == "ust20001"
    assert request["json"] == {
        "stex_tp": "ND", "stk_cd": "SPY", "ord_qty": "2",
        "ord_uv": "650.25", "trde_tp": "00",
    }


def test_order_client_keeps_two_decimal_places_for_us_price():
    session = Session()
    client = KiwoomOrderClient(KiwoomConfig("key", "secret", "mock"), session)

    client.sell_us_limit(
        "token", exchange="NY", ticker="BIL", quantity=1, price=91.4
    )

    assert session.calls[0][1]["json"]["ord_uv"] == "91.40"


def test_order_client_sends_us_limit_buy_shape():
    session = Session()
    client = KiwoomOrderClient(KiwoomConfig("key", "secret", "mock"), session)

    client.buy_us_limit(
        "token", exchange="ND", ticker="QQQ", quantity=3, price=512.4
    )

    request = session.calls[0][1]
    assert request["headers"]["api-id"] == "ust20000"
    assert request["json"] == {
        "stex_tp": "ND", "stk_cd": "QQQ", "ord_qty": "3",
        "ord_uv": "512.40", "trde_tp": "00",
    }


def test_order_client_sends_us_cancel_shape():
    session = Session()
    client = KiwoomOrderClient(KiwoomConfig("key", "secret", "mock"), session)

    result = client.cancel_us_order(
        "token", exchange="ND", ticker="QQQ", original_order_number="123456789"
    )

    request = session.calls[0][1]
    assert request["headers"]["api-id"] == "ust20003"
    assert request["json"] == {
        "orig_ord_no": "123456789", "stex_tp": "ND", "stk_cd": "QQQ"
    }
    assert result["original_order_number"] == "123456789"


def test_cancel_missing_order_number_is_unknown():
    session = Session()
    session.post = lambda *args, **kwargs: SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"return_code": 0, "ord_no": ""},
    )
    client = KiwoomOrderClient(KiwoomConfig("key", "secret", "mock"), session)

    with pytest.raises(OrderUnknown):
        client.cancel_us_order(
            "token", exchange="ND", ticker="QQQ", original_order_number="123456789"
        )


def test_order_client_sends_domestic_buy_and_sell_shapes():
    session = Session("0000123")
    client = KiwoomOrderClient(KiwoomConfig("key", "secret", "mock"), session)

    client.buy_kr_limit("token", exchange="KRX", ticker="005930", quantity=1, price=70000)
    client.sell_kr_limit("token", exchange="KRX", ticker="005930", quantity=2, price=71000)

    assert session.calls[0][0].endswith("/api/dostk/ordr")
    assert session.calls[0][1]["headers"]["api-id"] == "kt10000"
    assert session.calls[0][1]["json"] == {
        "dmst_stex_tp": "KRX", "stk_cd": "005930", "ord_qty": "1",
        "ord_uv": "70000", "trde_tp": "0", "cond_uv": "",
    }
    assert session.calls[1][1]["headers"]["api-id"] == "kt10001"
    assert session.calls[1][1]["json"]["ord_qty"] == "2"


def test_domestic_buy_supports_explicit_after_hours_single_price():
    session = Session("0000123")
    client = KiwoomOrderClient(KiwoomConfig("key", "secret", "mock"), session)

    client.buy_kr_limit("token", exchange="KRX", ticker="000660", quantity=1,
                        price=1686000, trade_type="62")

    assert session.calls[0][1]["json"]["trde_tp"] == "62"


def test_order_client_sends_domestic_cancel_shape():
    session = Session("0000124")
    client = KiwoomOrderClient(KiwoomConfig("key", "secret", "mock"), session)

    result = client.cancel_kr_order(
        "token", exchange="KRX", ticker="005930",
        original_order_number="0000123", quantity=1,
    )

    request = session.calls[0][1]
    assert request["headers"]["api-id"] == "kt10003"
    assert request["json"] == {
        "dmst_stex_tp": "KRX", "orig_ord_no": "0000123",
        "stk_cd": "005930", "cncl_qty": "1",
    }
    assert result["original_order_number"] == "0000123"


def test_domestic_buy_accepts_krx_alphanumeric_code():
    session = Session("0000123")
    client = KiwoomOrderClient(KiwoomConfig("key", "secret", "mock"), session)
    client.buy_kr_limit("token", exchange="KRX", ticker="0000D0", quantity=1, price=70000)
    assert session.calls[0][1]["json"]["stk_cd"] == "0000D0"


@pytest.mark.parametrize("ticker,price", [("5930", 70000), ("005930", 70000.0), ("005930", 0)])
def test_domestic_limit_validates_ticker_and_integer_price(ticker, price):
    client = KiwoomOrderClient(KiwoomConfig("key", "secret", "mock"), Session())
    with pytest.raises(Exception):
        client.buy_kr_limit("token", exchange="KRX", ticker=ticker, quantity=1, price=price)
