import pytest

from trading.auto_buy_us.strategy import evaluate, ordered

GOOD = {"strict": 1, "first_strict_pass": 1, "price": 10.0, "stop_loss": 9.0, "is_first_detection": 1,
        "market_condition": "BULL", "signal_type": "BREAKOUT", "ext_pct": 1.0, "entry_warnings": "[]",
        "stop_pct": 10.0, "grade": "A", "signal_quality": "STRONG", "ticker": "AAA",
        "signal_key": "US:AAA:BREAKOUT:2026-11-20"}


@pytest.mark.parametrize("patch,pool,reason", [
    ({"strict": None}, "B00", "STRICT_UNASSESSED"), ({"strict": 0}, "B00", "NOT_STRICT"),
    ({"first_strict_pass": None}, "B00", "FIRST_PASS_UNKNOWN"), ({"first_strict_pass": 0}, "B00", "ALREADY_PASSED"),
    ({"price": None}, "B00", "INVALID_PRICE"), ({"price": 0}, "B00", "INVALID_PRICE"),
    ({"stop_loss": None}, "B00", "INVALID_STOP"), ({"stop_loss": 0}, "B00", "INVALID_STOP"),
    ({"stop_loss": 12.0}, "B00", "INVALID_STOP"), ({"stop_loss": 10.0}, "B00", "INVALID_STOP"),
    ({"price": 0.9, "stop_loss": 0.8}, "B00", "PRICE_BELOW_1"),
    ({"is_first_detection": 0}, "B01", "LATE_STRICT_PASS"), ({"is_first_detection": None}, "B01", "LATE_STRICT_PASS"),
    ({"market_condition": "CAUTION"}, "B01", "MARKET_NOT_BULL"),
    ({"ext_pct": -0.5}, "B01", "BELOW_PIVOT"), ({"signal_type": "RE_BREAKOUT", "ext_pct": -0.5}, "B01", "BELOW_PIVOT"),
    ({"entry_warnings": '["추격 주의"]'}, "B01", "CHASE_WARNING"),
    ({"entry_warnings": ["추격 주의"]}, "B01", "CHASE_WARNING"),
    ({"stop_pct": 15.01}, "B01", "STOP_GT_15"), ({"stop_pct": None}, "B01", "STOP_GT_15")])
def test_reason(patch, pool, reason):
    assert reason in evaluate({**GOOD, **patch})[pool][1]


def test_good_signal_eligible_both():
    assert evaluate(GOOD) == {"B00": (True, []), "B01": (True, [])}


def test_b01_reasons_do_not_leak_into_b00():
    res = evaluate({**GOOD, "market_condition": "CAUTION"})
    assert res["B00"] == (True, []) and res["B01"][0] is False


def test_none_warnings_ok():
    assert evaluate({**GOOD, "entry_warnings": None})["B01"] == (True, [])


def test_rebound_skips_pivot_rule():
    assert evaluate({**GOOD, "signal_type": "REBOUND", "ext_pct": -3.0})["B01"] == (True, [])


def test_ordered_by_grade_quality_ticker():
    rows = [{**GOOD, "grade": "B", "signal_quality": "STRONG", "ticker": "AAA", "signal_key": "k1"},
            {**GOOD, "grade": "S", "signal_quality": "WEAK", "ticker": "ZZZ", "signal_key": "k2"},
            {**GOOD, "grade": "S", "signal_quality": "STRONG", "ticker": "MMM", "signal_key": "k3"}]
    assert [r["ticker"] for r in ordered(rows)] == ["MMM", "ZZZ", "AAA"]


def test_ordered_unknown_last_and_tiebreak():
    rows = [{**GOOD, "grade": None, "ticker": "AAA", "signal_key": "k1"},
            {**GOOD, "ticker": "BBB", "signal_key": "k3"}, {**GOOD, "ticker": "BBB", "signal_key": "k2"}]
    assert [r["signal_key"] for r in ordered(rows)] == ["k2", "k3", "k1"]
