from web.asset_allocation_sizing import calculate_allocation_sizing


def test_sizing_uses_cash_and_evaluation_without_exceeding_cash():
    report = {"combined_allocations": {"SPY": 0.5, "QQQ": 0.5}}
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
    assert items["SPY"]["required_buy_quantity"] == 2
    assert items["SPY"]["buy_quantity"] == 2
    assert items["QQQ"]["target_quantity"] == 5
    assert items["QQQ"]["required_buy_quantity"] == 5
    assert items["QQQ"]["buy_quantity"] == 5
    assert result["remaining_cash"] == 50
    assert result["required_cost"] == 450
    assert result["recommended_cost"] == 450
    assert result["liquidate_before_rebalance"] is True


def test_sizing_marks_missing_price_without_recommending_purchase():
    result = calculate_allocation_sizing(
        {"combined_allocations": {"NEW": 1.0}}, [],
        {"overseas": {"orderable_cash": 100, "evaluation_amount": 0}},
        lambda ticker: None,
    )

    assert result["items"][0]["price"] is None
    assert result["items"][0]["target_quantity"] is None
    assert result["items"][0]["buy_quantity"] == 0
