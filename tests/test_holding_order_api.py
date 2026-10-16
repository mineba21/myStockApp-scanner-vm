"""보유종목 매수·매도 주문 미리보기 API 테스트.

미리보기는 검증·계산만 하고 증권사 호출·저장·실행 경로가 없어야 한다.
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from web import holding_order_api as api
from web.holding_order_api import HoldingOrderPreviewRequest, kr_tick_size, preview_holding_order


class FakeQuery:
    def __init__(self, row):
        self.row = row

    def filter(self, *args):
        return self

    def first(self):
        return self.row


class FakeDb:
    def __init__(self, row=None):
        self.row = row

    def query(self, model):
        return FakeQuery(self.row)


def manual_holding(**overrides):
    values = dict(id=7, ticker="005930", name="삼성전자", market="KR", quantity=10.0,
                  current_price=70_000.0, account=SimpleNamespace(name="수동계좌"))
    values.update(overrides)
    return SimpleNamespace(**values)


def kiwoom_row(**overrides):
    row = {"id": "kiwoom:account1:US:AAPL", "ticker": "AAPL", "name": "Apple", "market": "US",
           "quantity": 5.0, "current_price": 200.0, "account_name": "자유투자",
           "account_profile": "account1", "source": "kiwoom"}
    row.update(overrides)
    return row


def preview(db=None, **body):
    request = dict(holding_id="7", side="BUY", quantity=1, limit_price=70_000)
    request.update(body)
    return preview_holding_order(HoldingOrderPreviewRequest(**request), db or FakeDb(manual_holding()))


def test_manual_kr_buy_preview_is_never_executable():
    result = preview(quantity=3, limit_price=70_000)

    assert result["side"] == "BUY"
    assert result["estimated_amount"] == 210_000
    assert result["held_quantity"] == 10
    assert result["quantity_after"] == 13
    assert result["currency"] == "KRW"
    assert result["execution_enabled"] is False
    assert result["execution_block_reason"] == api.EXECUTION_BLOCK_REASON
    assert result["warnings"] == []


def test_sell_cannot_exceed_held_quantity():
    with pytest.raises(HTTPException) as exc:
        preview(side="SELL", quantity=11)
    assert exc.value.status_code == 422

    assert preview(side="SELL", quantity=10)["quantity_after"] == 0


def test_kr_price_must_be_whole_won_and_warns_off_tick():
    with pytest.raises(HTTPException) as exc:
        preview(limit_price=70_000.5)
    assert exc.value.status_code == 422

    warnings = preview(limit_price=70_010)["warnings"]
    assert any("호가단위(100원)" in w for w in warnings)


def test_kr_tick_size_table():
    assert kr_tick_size(1_999) == 1
    assert kr_tick_size(4_995) == 5
    assert kr_tick_size(19_990) == 10
    assert kr_tick_size(49_950) == 50
    assert kr_tick_size(199_900) == 100
    assert kr_tick_size(499_500) == 500
    assert kr_tick_size(500_000) == 1_000


def test_large_price_deviation_warns():
    warnings = preview(limit_price=80_000)["warnings"]
    assert any("현재가 대비 +14.3%" in w for w in warnings)


def test_missing_manual_holding_is_404():
    with pytest.raises(HTTPException) as exc:
        preview(db=FakeDb(None))
    assert exc.value.status_code == 404


def test_kiwoom_holding_requires_web_flag(monkeypatch):
    monkeypatch.setenv("KIWOOM_WEB_ENABLED", "false")
    monkeypatch.setattr(api, "get_kiwoom_holdings", lambda: pytest.fail("must not query broker"))
    with pytest.raises(HTTPException) as exc:
        preview(holding_id="kiwoom:account1:US:AAPL", limit_price=200)
    assert exc.value.status_code == 404


def test_kiwoom_us_preview_uses_cached_holdings(monkeypatch):
    monkeypatch.setenv("KIWOOM_WEB_ENABLED", "true")
    monkeypatch.setattr(api, "get_kiwoom_holdings", lambda: [kiwoom_row()])

    result = preview(holding_id="kiwoom:account1:US:AAPL", side="SELL", quantity=2, limit_price=201.25)

    assert result["currency"] == "USD"
    assert result["estimated_amount"] == 402.5
    assert result["quantity_after"] == 3
    assert result["execution_enabled"] is False

    with pytest.raises(HTTPException) as exc:
        preview(holding_id="kiwoom:account1:US:AAPL", limit_price=200.123)
    assert exc.value.status_code == 422


def test_account2_managed_etf_warns_about_rebalance(monkeypatch):
    monkeypatch.setenv("KIWOOM_WEB_ENABLED", "true")
    row = kiwoom_row(id="kiwoom:account2:US:BIL", ticker="BIL", account_profile="account2",
                     current_price=91.5)
    monkeypatch.setattr(api, "get_kiwoom_holdings", lambda: [row])

    warnings = preview(holding_id=row["id"], side="SELL", limit_price=91.5)["warnings"]
    assert any("차액 재분배" in w for w in warnings)


def test_router_exposes_preview_only():
    paths = {(route.path, tuple(sorted(route.methods))) for route in api.router.routes}
    assert paths == {("/api/holding-orders/preview", ("POST",))}
