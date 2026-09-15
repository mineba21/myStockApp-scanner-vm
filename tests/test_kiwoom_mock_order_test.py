from pathlib import Path

import pytest

from trading.kiwoom_mock_order_test import MockOrderTest, MockOrderTestBlocked
from trading.kiwoom_orders import OrderUnknown
from trading.kiwoom_readonly import MOCK_BASE_URL


class Broker:
    mode = "mock"
    base_url = MOCK_BASE_URL

    def __init__(self):
        self.cash = "1000.00"
        self.open_count = 0
        self.validated = True
        self.blockers = []
        self.buy_calls = 0
        self.cancel_calls = 0
        self.lookup_state = "OPEN"
        self.buy_error = None
        self.cancel_error = None

    def snapshot(self):
        return {"account": "account2", "currency": "USD",
                "broker_orderable_cash": self.cash,
                "cash_source": "fake", "open_order_count": self.open_count,
                "validated_for_execution": self.validated, "blockers": self.blockers}

    def buy(self, test):
        self.buy_calls += 1
        if self.buy_error:
            raise self.buy_error
        return {"order_number": "123456789", "status": "submitted"}

    def cancel(self, test):
        self.cancel_calls += 1
        if self.cancel_error:
            raise self.cancel_error
        return {"order_number": "987654321", "status": "submitted"}

    def lookup(self, test):
        return {"state": self.lookup_state,
                "remaining_quantity": 1 if self.lookup_state == "OPEN" else 0}


def prepared(tmp_path, broker=None):
    broker = broker or Broker()
    workflow = MockOrderTest(tmp_path / "test.sqlite3", broker)
    record = workflow.prepare("2026-09-12-SPY", ticker="SPY", exchange="NY", price="100.00")
    return broker, workflow, record


def test_refuses_real_server(tmp_path):
    broker = Broker()
    broker.mode = "real"
    broker.base_url = "https://api.kiwoom.com"
    with pytest.raises(MockOrderTestBlocked):
        MockOrderTest(tmp_path / "test.sqlite3", broker)


def test_duplicate_prepare_and_buy_click_submit_once(tmp_path):
    broker, workflow, record = prepared(tmp_path)
    duplicate = workflow.prepare("2026-09-12-SPY", ticker="SPY", exchange="NY", price="100.00")
    assert duplicate["id"] == record["id"]

    first = workflow.submit_buy(record["id"], "BUY:SPY:1@100.00")
    second = workflow.submit_buy(record["id"], "BUY:SPY:1@100.00")

    assert first["state"] == second["state"] == "BUY_SUBMITTED"
    assert broker.buy_calls == 1


def test_timeout_is_unknown_and_restart_does_not_resend(tmp_path):
    broker, workflow, record = prepared(tmp_path)
    broker.buy_error = OrderUnknown("timeout")
    assert workflow.submit_buy(record["id"], "BUY:SPY:1@100.00")["state"] == "BUY_UNKNOWN"

    restarted = MockOrderTest(tmp_path / "test.sqlite3", broker)
    assert restarted.submit_buy(record["id"], "BUY:SPY:1@100.00")["state"] == "BUY_UNKNOWN"
    assert broker.buy_calls == 1


def test_open_cancel_and_cash_restoration(tmp_path):
    broker, workflow, record = prepared(tmp_path)
    workflow.submit_buy(record["id"], "BUY:SPY:1@100.00")
    broker.cash = "899.00"
    assert workflow.verify_open(record["id"])["state"] == "OPEN_VERIFIED"

    cancelled = workflow.submit_cancel(record["id"], "CANCEL:123456789")
    assert cancelled["state"] == "CANCEL_SUBMITTED"
    broker.lookup_state = "CANCELLED"
    broker.cash = "1000.00"
    complete = workflow.verify_cancel(record["id"])

    assert complete["state"] == "COMPLETE"
    assert complete["details"]["cash_restored"] is True
    assert broker.cancel_calls == 1


def test_cancel_timeout_can_be_reconciled_without_resend(tmp_path):
    broker, workflow, record = prepared(tmp_path)
    workflow.submit_buy(record["id"], "BUY:SPY:1@100.00")
    workflow.verify_open(record["id"])
    broker.cancel_error = OrderUnknown("timeout")
    assert workflow.submit_cancel(record["id"], "CANCEL:123456789")["state"] == "CANCEL_UNKNOWN"
    assert workflow.submit_cancel(record["id"], "CANCEL:123456789")["state"] == "CANCEL_UNKNOWN"

    broker.lookup_state = "CANCELLED"
    assert workflow.verify_cancel(record["id"])["state"] == "COMPLETE"
    assert broker.cancel_calls == 1


def test_prepare_blocks_existing_open_order_and_insufficient_cash(tmp_path):
    broker = Broker()
    broker.open_count = 1
    with pytest.raises(MockOrderTestBlocked):
        MockOrderTest(tmp_path / "one.sqlite3", broker).prepare(
            "one", ticker="SPY", exchange="NY", price="100.00"
        )
    broker.open_count = 0
    broker.cash = "100.99"
    with pytest.raises(MockOrderTestBlocked):
        MockOrderTest(tmp_path / "two.sqlite3", broker).prepare(
            "two", ticker="SPY", exchange="NY", price="100.00"
        )


def test_prepare_blocks_unvalidated_broker_cash(tmp_path):
    broker = Broker()
    broker.validated = False
    broker.blockers = ["종목별 주문가능수량 API 미지원"]

    with pytest.raises(MockOrderTestBlocked, match="모의 주문을 차단"):
        MockOrderTest(tmp_path / "test.sqlite3", broker).prepare(
            "blocked", ticker="SPY", exchange="NY", price="100.00"
        )
    assert broker.buy_calls == 0


def test_explicit_mock_diagnostic_override_requires_stronger_confirmation(tmp_path):
    broker = Broker()
    broker.validated = False
    broker.blockers = ["종목별 주문가능수량 API 미지원"]
    workflow = MockOrderTest(tmp_path / "test.sqlite3", broker)

    record = workflow.prepare(
        "authorized", ticker="SPY", exchange="NY", price="100.00",
        allow_unvalidated_capacity=True,
    )

    assert record["state"] == "PREPARED"
    assert record["details"]["capacity_unverified"] is True
    assert record["details"]["confirmation"].endswith(":UNVALIDATED_CAPACITY")


def test_prepare_accepts_broker_cash_with_sub_cent_precision(tmp_path):
    broker = Broker()
    broker.cash = "999.999"

    _, _, record = prepared(tmp_path, broker)

    assert record["cash_before"] == "999.999"
