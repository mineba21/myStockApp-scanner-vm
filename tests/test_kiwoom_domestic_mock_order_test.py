import pytest

from trading.kiwoom_domestic_mock_order_test import DomesticMockOrderTest, DomesticMockTestBlocked
from trading.kiwoom_orders import OrderUnknown
from trading.kiwoom_readonly import MOCK_BASE_URL


class Broker:
    mode = "mock"
    base_url = MOCK_BASE_URL

    def __init__(self):
        self.cash, self.open_count, self.available = "100000", 0, 3
        self.submit_calls = self.cancel_calls = 0
        self.submit_error = self.cancel_error = None

    def snapshot(self):
        return {"account": "account4", "currency": "KRW", "broker_orderable_cash": self.cash,
                "cash_source": "fake", "open_order_count": self.open_count}

    def sellable_quantity(self, ticker):
        return self.available

    def submit(self, test):
        self.submit_calls += 1
        if self.submit_error:
            raise self.submit_error
        return {"order_number": "0000123"}

    def cancel(self, test):
        self.cancel_calls += 1
        if self.cancel_error:
            raise self.cancel_error
        return {"order_number": "0000124"}

    def lookup_open(self, test):
        return {"state": "OPEN", "remaining_quantity": 1}

    def lookup_cancel(self, test):
        return {"state": "CANCELLED", "remaining_quantity": 0}


def prepared(tmp_path, side="BUY", broker=None):
    broker = broker or Broker()
    workflow = DomesticMockOrderTest(tmp_path / "kr.sqlite3", broker, account_profile="account4")
    record = workflow.prepare("key-" + side, side=side, ticker="005930", price=70000)
    return broker, workflow, record


def test_refuses_real_server(tmp_path):
    broker = Broker()
    broker.mode, broker.base_url = "real", "https://api.kiwoom.com"
    with pytest.raises(DomesticMockTestBlocked):
        DomesticMockOrderTest(tmp_path / "kr.sqlite3", broker, account_profile="account4")


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_buy_and_sell_complete_open_cancel_flow(tmp_path, side):
    broker, workflow, record = prepared(tmp_path, side)
    submitted = workflow.submit(record["id"], f"{side}:005930:1@70000")
    assert submitted["state"] == "ORDER_SUBMITTED"
    assert workflow.verify_open(record["id"])["state"] == "OPEN_VERIFIED"
    assert workflow.submit_cancel(record["id"], "CANCEL:0000123")["state"] == "CANCEL_SUBMITTED"
    assert workflow.verify_cancel(record["id"])["state"] == "COMPLETE"


def test_duplicate_click_and_restart_do_not_resend(tmp_path):
    broker, workflow, record = prepared(tmp_path)
    workflow.submit(record["id"], "BUY:005930:1@70000")
    restarted = DomesticMockOrderTest(tmp_path / "kr.sqlite3", broker, account_profile="account4")
    assert restarted.submit(record["id"], "BUY:005930:1@70000")["state"] == "ORDER_SUBMITTED"
    assert broker.submit_calls == 1


def test_timeout_stays_unknown_across_restart(tmp_path):
    broker, workflow, record = prepared(tmp_path)
    broker.submit_error = OrderUnknown("timeout")
    assert workflow.submit(record["id"], "BUY:005930:1@70000")["state"] == "ORDER_UNKNOWN"
    restarted = DomesticMockOrderTest(tmp_path / "kr.sqlite3", broker, account_profile="account4")
    assert restarted.submit(record["id"], "BUY:005930:1@70000")["state"] == "ORDER_UNKNOWN"
    assert broker.submit_calls == 1


def test_cancel_timeout_is_reconciled_without_resend(tmp_path):
    broker, workflow, record = prepared(tmp_path)
    workflow.submit(record["id"], "BUY:005930:1@70000")
    workflow.verify_open(record["id"])
    broker.cancel_error = OrderUnknown("timeout")
    assert workflow.submit_cancel(record["id"], "CANCEL:0000123")["state"] == "CANCEL_UNKNOWN"
    assert workflow.submit_cancel(record["id"], "CANCEL:0000123")["state"] == "CANCEL_UNKNOWN"
    assert workflow.verify_cancel(record["id"])["state"] == "COMPLETE"
    assert broker.cancel_calls == 1


def test_prepare_blocks_existing_open_and_sell_without_holding(tmp_path):
    broker = Broker()
    broker.open_count = 1
    with pytest.raises(DomesticMockTestBlocked):
        DomesticMockOrderTest(tmp_path / "one.sqlite3", broker, account_profile="account4").prepare(
            "one", side="BUY", ticker="005930", price=70000)
    broker.open_count, broker.available = 0, 0
    with pytest.raises(DomesticMockTestBlocked):
        DomesticMockOrderTest(tmp_path / "two.sqlite3", broker, account_profile="account4").prepare(
            "two", side="SELL", ticker="005930", price=70000)
