from trading.asset_allocation_universe import (
    ETF_ALLOCATION_SCOPE,
    ETF_EXCHANGES,
    allocation_scope,
)


def test_scope_covers_deployed_easy_original_vaa_laa_and_dual(monkeypatch):
    monkeypatch.delenv("ALLOCATION_LIQUIDATION_SCOPE", raising=False)
    assert ETF_ALLOCATION_SCOPE == (
        "AGG", "BIL", "EEM", "EFA", "GLD", "IEF",
        "IEMG", "LQD", "QQQ", "SHY", "SPY", "VTV",
    )
    assert allocation_scope() == list(ETF_ALLOCATION_SCOPE)
    assert ETF_EXCHANGES["EEM"] == "NY"


def test_explicit_scope_remains_supported(monkeypatch):
    monkeypatch.setenv("ALLOCATION_LIQUIDATION_SCOPE", " SPY, qqq ")
    assert allocation_scope() == ["SPY", "QQQ"]
