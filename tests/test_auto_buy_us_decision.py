from decimal import Decimal as D

import pytest

from trading.auto_buy_us.decision import (confirm_stop, exit_limit, open_check, plan_entries, stop_check)
from trading.auto_buy_us.settings import Settings
from trading.auto_buy_us.sizing import SleeveState

S = Settings(mode="SHADOW", mode_block=None, journal_path="", kill=False)


def sig(i, ticker, grade="A", bull=True, **kw):
    d = dict(id=i, ticker=ticker, signal_key=f"k{i}", price=50.0, stop_loss=46.0, sector_name="Tech",
             strict=1, first_strict_pass=1, grade=grade, signal_quality="STRONG", is_first_detection=1,
             market_condition="BULL" if bull else "NEUTRAL", ext_pct=1.0, signal_type="BREAKOUT",
             entry_warnings=None, stop_pct=8.0)
    d.update(kw)
    return d


def state(cash=100000.0, **kw):
    base = dict(cash=cash, equity=cash, positions_value=0.0, reserved_cash=0.0, entry_heat_usd=0.0,
                current_heat_usd=0.0, sector_values={})
    base.update(kw)
    return SleeveState(**base)


EX = {t: "NAS" for t in ("AAA", "BBB", "CCC")}
three = lambda: [sig(1, "AAA", "S"), sig(2, "BBB", "A"), sig(3, "CCC", "B")]


def verdicts(plans):
    return {p.ticker: p.verdict for p in plans}


def test_b00n_takes_top2_by_grade():
    v = verdicts(plan_entries(three(), "B00N", state(), set(), EX, "2026-11-28"))
    assert v == {"AAA": "PLANNED", "BBB": "PLANNED", "CCC": "RANK_CUT"}


def test_b00_has_no_topn():
    v = verdicts(plan_entries(three(), "B00", state(), set(), EX, "2026-11-28"))
    assert set(v.values()) == {"PLANNED"}


def test_b01_filters_before_rank():
    sigs = [sig(1, "AAA", "S", bull=False), sig(2, "BBB", "A"), sig(3, "CCC", "B")]
    v = verdicts(plan_entries(sigs, "B01", state(), set(), EX, "d"))
    assert v == {"AAA": "INELIGIBLE", "BBB": "PLANNED", "CCC": "PLANNED"}


def test_held_and_exchange_unresolved():
    ex = dict(EX, BBB=None)
    v = verdicts(plan_entries(three(), "B00", state(), {"AAA"}, ex, "d"))
    assert v == {"AAA": "HELD", "BBB": "EXCHANGE_UNRESOLVED", "CCC": "PLANNED"}


def test_state_updated_and_heat_limit():
    st = state()
    plans = plan_entries([sig(1, "AAA", "S")], "B00", st, set(), EX, "d")
    p = plans[0]
    assert p.verdict == "PLANNED" and p.reasons == [] and p.qty > 0
    assert st.reserved_cash == p.reserved_cash and st.entry_heat_usd == p.planned_risk == p.entry_heat
    assert st.sector_values["Tech"] == p.reserved_cash
    assert p.decision_price == D("50.0") and p.limit_price == D("50.50")
    # 열 한도 소진 직전: 두 번째는 SIZE_ZERO
    st2 = state(entry_heat_usd=100000 * 0.04 - 1)
    plans = plan_entries([sig(1, "AAA", "S"), sig(2, "BBB", "A")], "B00", st2, set(), EX, "d")
    assert plans[1].verdict == "SIZE_ZERO" and plans[1].qty == 0 and plans[1].reasons


def test_rank_cut_uses_rank_not_planned_count():
    sigs = [sig(1, "AAA", "S", sector_name=None), sig(2, "BBB", "A"), sig(3, "CCC", "B")]
    v = verdicts(plan_entries(sigs, "B00N", state(), set(), EX, "d"))
    assert v == {"AAA": "SIZE_ZERO", "BBB": "PLANNED", "CCC": "RANK_CUT"}


def test_one_plan_row_per_signal_and_invalid_price_safe():
    sigs = three() + [sig(4, "DDD", price=float("nan"), stop_loss=None), sig(5, "EEE", strict=None)]
    plans = plan_entries(sigs, "B00N", state(), {"BBB"}, EX, "d")
    assert len(plans) == len(sigs) == len({p.signal_id for p in plans})
    bad = next(p for p in plans if p.ticker == "DDD")
    assert bad.verdict == "INELIGIBLE" and bad.decision_price == D("0") and bad.stop_price == D("0")
    assert bad.limit_price is None and bad.qty == 0 and bad.planned_risk == 0.0


def test_open_check():
    assert open_check(D("9"), D("9")).reason == "OPEN_BELOW_STOP"
    assert open_check(D("9.80"), D("10")).reason == "OPEN_R_TOO_SMALL"
    assert open_check(D("9"), D("10")).ok


def test_stop_check_and_confirm():
    assert stop_check(D("50"), D("51"), D("55"), S).action == "NONE"
    assert stop_check(D("50"), D("30"), D("55"), S).action == "CONFIRM"
    assert stop_check(D("50"), D("49"), D("50"), S).action == "TRIGGER"
    assert stop_check(D("50"), D("49"), None, S).action == "TRIGGER"
    assert confirm_stop(D("30"), D("45"), D("50"), S).action == "REVIEW"
    assert confirm_stop(D("30"), D("45"), D("50"), S).reason == "QUOTE_JUMP_UNCONFIRMED"
    assert confirm_stop(D("30"), D("30.1"), D("50"), S).action == "TRIGGER"
    assert confirm_stop(D("49"), D("49.5"), D("48"), S).action == "NONE"


def test_exit_limit():
    assert exit_limit(D("100.00"), S) == D("99.50")
    with pytest.raises(ValueError):
        exit_limit(D("0.99"), S)
    with pytest.raises(ValueError):
        exit_limit(D("1.00"), S)  # 0.99 로 내려감
