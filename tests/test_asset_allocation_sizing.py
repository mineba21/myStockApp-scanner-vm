from web.asset_allocation_sizing import calculate_allocation_sizing


def test_sizing_uses_cash_and_evaluation_without_exceeding_cash():
    report = {"combined_allocations": {"SPY": 0.5, "QQQ": 0.5}, "liquidation_scope": ["SPY", "QQQ"]}
    holdings = [{
        "account_profile": "account2", "market": "US", "ticker": "SPY",
        "quantity": 2, "current_price": 100, "eval_amount": 200,
    }]
    summary = {"overseas": {"orderable_cash": 300, "evaluation_amount": 200}}

    result = calculate_allocation_sizing(
        report, holdings, summary, lambda ticker: {"SPY": 100, "QQQ": 50}[ticker]
    )

    assert result["total_assets"] == 500
    assert result["recommended_cost"] <= result["total_assets"]
    items = {item["ticker"]: item for item in result["items"]}
    assert items["SPY"]["target_quantity"] == 2
    assert items["SPY"]["required_buy_quantity"] == 0
    assert items["SPY"]["buy_quantity"] == 0
    assert items["SPY"]["additional_buy_quantity"] == 0
    assert items["SPY"]["additional_sell_quantity"] == 0
    assert items["QQQ"]["target_quantity"] == 5
    assert items["QQQ"]["required_buy_quantity"] == 5
    assert items["QQQ"]["buy_quantity"] == 5
    assert items["QQQ"]["additional_buy_quantity"] == 5
    assert result["remaining_cash"] == 50
    assert result["required_cost"] == 250
    assert result["recommended_cost"] == 250
    assert result["liquidate_before_rebalance"] is False


def test_sizing_includes_non_target_holdings_as_sell_adjustment():
    result = calculate_allocation_sizing(
        {"combined_allocations": {"SPY": 1.0}, "liquidation_scope": ["SPY", "OLD"]},
        [{
            "account_profile": "account2", "market": "US", "ticker": "OLD",
            "quantity": 4, "current_price": 25, "eval_amount": 100,
        }],
        {"overseas": {"orderable_cash": 0, "evaluation_amount": 100}},
        lambda ticker: 100 if ticker == "SPY" else 25,
    )

    items = {item["ticker"]: item for item in result["items"]}
    assert items["OLD"]["target_quantity"] == 0
    assert items["OLD"]["additional_sell_quantity"] == 4
    assert items["OLD"]["adjustment_quantity"] == -4


def test_sizing_marks_missing_price_without_recommending_purchase():
    result = calculate_allocation_sizing(
        {"combined_allocations": {"NEW": 1.0}}, [],
        {"overseas": {"orderable_cash": 100, "evaluation_amount": 0}},
        lambda ticker: None,
    )

    assert result["items"][0]["price"] is None
    assert result["items"][0]["target_quantity"] is None
    assert result["items"][0]["buy_quantity"] == 0


def test_other_strategy_is_excluded_from_liquidation_and_estimated_assets():
    result = calculate_allocation_sizing(
        {"combined_allocations": {"SPY": 1}, "liquidation_scope": ["SPY"]},
        [{"account_profile": "account2", "market": "US", "ticker": "OTHER",
          "quantity": 99, "current_price": 100, "eval_amount": 9900}],
        {"overseas": {"orderable_cash": 100, "evaluation_amount": 9900}},
        lambda ticker: 100)
    assert result["planning_assets"] == 100
    assert [r["ticker"] for r in result["items"]] == ["SPY"]
    assert result["estimate_only"] is True
    assert result["execution_enabled"] is False


def test_missing_scope_prevents_normal_plan():
    result = calculate_allocation_sizing(
        {"combined_allocations": {"SPY": 1}}, [],
        {"overseas": {"orderable_cash": 100}}, lambda ticker: 100)
    assert result["plan_valid"] is False
    assert result["items"][0]["buy_quantity"] == 0
