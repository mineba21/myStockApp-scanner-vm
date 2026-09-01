import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from trading.kiwoom_orders import KiwoomOrderClient
from trading.kiwoom_readonly import KiwoomConfig
from web import kiwoom_order_api


class Session:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"return_code": 0, "ord_no": "123456789"},
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


def test_preview_is_account2_only_and_cannot_exceed_holdings(monkeypatch):
    monkeypatch.setattr(kiwoom_order_api, "get_kiwoom_holdings", lambda force: [{
        "account_profile": "account2", "market": "US", "ticker": "SPY",
        "name": "SPY", "quantity": 3, "exchange": "NASDAQ",
    }])
    monkeypatch.delenv("KIWOOM_TRADING_ENABLED", raising=False)

    preview = asyncio.run(kiwoom_order_api.preview_sell(
        kiwoom_order_api.SellPreviewRequest(ticker="spy", quantity=2, limit_price=650)
    ))
    assert preview["execution_enabled"] is False
    assert preview["exchange"] == "ND"

    with pytest.raises(HTTPException) as exc:
        asyncio.run(kiwoom_order_api.preview_sell(
            kiwoom_order_api.SellPreviewRequest(ticker="SPY", quantity=4, limit_price=650)
        ))
    assert exc.value.status_code == 422


def test_execute_is_blocked_without_explicit_server_flag():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(kiwoom_order_api.execute_sell(
            kiwoom_order_api.SellExecuteRequest(
                preview_id="x" * 24, confirmation_ticker="SPY"
            )
        ))
    assert exc.value.status_code == 503


def test_sell_quote_uses_account2_kiwoom_current_price(monkeypatch):
    monkeypatch.setattr(
        kiwoom_order_api,
        "_find_holding",
        lambda ticker: {"ticker": "QQQ", "exchange": "NASDAQ", "quantity": 3},
    )
    monkeypatch.setattr(
        kiwoom_order_api, "load_profile_configs", lambda: {"account2": object()}
    )

    class Client:
        def __init__(self, config):
            pass

        def get_overseas_quote(self, token, *, exchange, ticker):
            assert exchange == "ND"
            assert ticker == "QQQ"
            return {"quote": {"cur_prc": "512.3400"}}

    monkeypatch.setattr(kiwoom_order_api, "KiwoomReadOnlyClient", Client)
    monkeypatch.setattr(kiwoom_order_api, "_get_token", lambda *args: "token")

    result = asyncio.run(kiwoom_order_api.quote_sell("QQQ"))

    assert result["current_price"] == 512.34
    assert result["source"] == "KIWOOM_USA20100"
    assert result["read_only"] is True
