"""Current broker holdings, not settled shares, gate post-sale progression."""
import time

import pytest

from trading.allocation_rebalance import Rebalance
from trading.kiwoom_allocation_live_broker import KiwoomAllocationLiveBroker
from trading.kiwoom_readonly import KiwoomConfig, KiwoomError


class Client:
    def __init__(self, row):
        self.row = row

    def get_overseas_account_balance(self, token):
        return {"holdings": [dict(self.row)]}


class Evidence:
    cash = "100"

    def cash_check(self, day, include_previous_day=False):
        return {"broker_orderable_cash": self.cash, "open_order_count": 0}

    def lookup(self, intent):
        return {"matched_intent_id": intent["id"], "state": "FILLED",
                "filled_quantity": intent["quantity"]}


class Orders:
    def __getattr__(self, name):
        raise AssertionError("No order transmission is allowed in these tests")


def live(row):
    return KiwoomAllocationLiveBroker(
        KiwoomConfig("fake", "fake", mode="real"), client=Client(row),
        token="fake", evidence=Evidence(), orders=Orders(),
    )


@pytest.mark.parametrize("settled,current,sellable", [
    (22, 21, 21),  # Today's sell filled but has not settled.
    (21, 22, 22),  # Today's buy filled but has not settled.
    (22, 22, 20),  # Reserved shares reduce the sell cap, not holdings.
    (1, 0, 0),    # A completed full sale must leave no phantom holding.
])
def test_snapshot_uses_current_position(settled, current, sellable):
    broker = live({"stk_cd": "VTV", "qty": str(settled),
                   "poss_qty": str(current), "sell_alowq": str(sellable)})
    assert broker.snapshot()["holdings"] == {"VTV": current}


@pytest.mark.parametrize("value", [None, "", "bad", "NaN", "-1", "1.5", True])
def test_invalid_current_position_never_falls_back_to_settled_or_sellable(value):
    row = {"stk_cd": "VTV", "qty": "22", "sell_alowq": "21"}
    if value is not None:
        row["poss_qty"] = value
    with pytest.raises(KiwoomError):
        live(row).snapshot()


@pytest.mark.parametrize("current,expected_state", [(21, "SELL_CONFIRMED"), (22, "SELL_PENDING")])
def test_filled_sell_checks_current_holdings_before_buy_preview(tmp_path, current, expected_state):
    broker = live({"stk_cd": "VTV", "qty": "22", "poss_qty": "22", "sell_alowq": "22"})
    broker.quotes = lambda tickers: {
        t: {"limit_price": "100.00", "as_of": time.time()} for t in tickers
    }
    service = Rebalance(tmp_path / "isolated.sqlite3", broker)
    cycle = service.create("test", {"VTV": ".95"}, ["VTV"])
    assert cycle["payload"]["planning"]["retained_quantities"] == {"VTV": 21}
    # A synthetic already-submitted order in an isolated test journal only.
    with service.db() as db:
        db.execute("UPDATE allocation_cycles SET state='SELL_PENDING' WHERE id=?", (cycle["id"],))
        db.execute("INSERT INTO allocation_intents "
                   "(id,cycle_id,side,ticker,quantity,price,state,order_number,created_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?)",
                   ("test-intent", cycle["id"], "SELL", "VTV", 1, "100.00", "SUBMITTED", "1", time.time()))
    broker.client.row["poss_qty"] = str(current)
    broker.client.row["sell_alowq"] = str(current)
    broker.evidence.cash = "200"
    result = service.reconcile(cycle["id"])
    assert result["orders"][0]["state"] == "FILLED"
    assert result["state"] == expected_state
    if current == 21:
        assert service.preview_buys(cycle["id"])["state"] == "BUY_PREVIEW"
    else:
        assert any("보유 22주 / 유지 예정 21주" in message for message in result["blockers"])
