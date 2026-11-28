from decimal import Decimal

from trading.auto_buy_us.sizing import (UNKNOWN_SECTOR, limit_price, sector_of, size_order, sleeve_state,
                                        unit_cost, unit_proceeds)

D = Decimal


def st(cash):
    return sleeve_state(cash, [], [])


def test_limit_price_floor():
    assert limit_price(D("10.07")) == D("10.17")  # 10.1707 -> 10.17


def test_limit_price_exact_cent():
    assert limit_price(D("20.00")) == D("20.20")


def test_sector_of():
    assert sector_of(None) == UNKNOWN_SECTOR
    assert sector_of(" none ") == UNKNOWN_SECTOR
    assert sector_of("Tech") == "Tech"


def test_unit_cost_proceeds():
    assert abs(unit_cost(D("100")) - 100 * 1.001 * 1.0025) < 1e-9
    assert abs(unit_proceeds(100.0) - 100 * 0.999 * (1 - 0.0025 - 0.0000278)) < 1e-9


def test_sector_unknown_blocks():
    assert size_order(st(15000), D("50"), D("46"), None)[1] == "SECTOR_UNKNOWN"


def test_risk_pct_limits_qty():
    q, r, info = size_order(st(15000), D("50"), D("46"), "Tech")
    assert r is None and q > 0
    assert info["planned_risk"] <= 15000 * 0.0075 + 1e-6
    assert info["limit_price"] == D("50.50")
    assert info["reserved_cash"] == q * unit_cost(D("50.50"))


def test_heat_cap_blocks():
    s = st(15000)
    s.entry_heat_usd = 0.04 * s.equity
    q, r, _ = size_order(s, D("50"), D("46"), "Tech")
    assert q == 0 and "HEAT" in r


def test_r_band():
    assert size_order(st(15000), D("50"), D("49"), "Tech")[1] == "R_BAND"


def test_sector_cap():
    s = st(15000)
    s.sector_values["Tech"] = 0.35 * s.equity
    assert size_order(s, D("50"), D("46"), "Tech")[1] == "SECTOR_CAP"


def test_unpriced_position_current_heat_none():
    pos = [{"ticker": "AAA", "sector": "Tech", "qty": 10, "entry_price": 50.0, "initial_stop": 46.0,
            "last_price": None},
           {"ticker": "BBB", "sector": "Tech", "qty": 10, "entry_price": 20.0, "initial_stop": 18.0,
            "last_price": 25.0}]
    s = sleeve_state(1000, pos, [])
    assert s.current_heat_usd is None and s.unpriced == ["AAA"]
    assert s.entry_heat_usd == 40 + 20
    assert s.equity == 1000 + 10 * 50 + 10 * 25  # R4: last 없으면 entry
    assert s.sector_values["Tech"] == 750


def test_current_heat_priced():
    pos = [{"ticker": "BBB", "sector": "Tech", "qty": 10, "entry_price": 20.0, "initial_stop": 18.0,
            "last_price": 25.0}]
    s = sleeve_state(0, pos, [{"ticker": "C", "sector": "X", "reserved_cash": 100, "planned_risk": 7}])
    assert s.current_heat_usd == 70 + 7 and s.entry_heat_usd == 20 + 7 and s.unpriced == []
    assert s.reserved_cash == 100 and s.equity == 250  # reserved 는 equity 에 더하지 않는다


def test_reserved_cash_reduces_available():
    s = sleeve_state(1000, [], [{"ticker": "C", "sector": "X", "reserved_cash": 980, "planned_risk": 5}])
    assert size_order(s, D("50"), D("46"), "Tech")[1] == "INSUFFICIENT_CASH"
