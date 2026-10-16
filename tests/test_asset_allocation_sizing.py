from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from web.asset_allocation_sizing import (
    calculate_allocation_sizing, build_live_allocation_sizing, SizingUnavailable,
)


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
    assert result["items"][0]["buy_quantity"] is None


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
    assert result["items"][0]["buy_quantity"] is None


# The display path must never instantiate an order client or relax the execution
# broker. All account/quote reads below use fake responses.
@pytest.fixture()
def reference_feed(monkeypatch):
    import web.asset_allocation_sizing as sizing
    import web.kiwoom_holdings as holdings_api
    import trading.kiwoom_readonly as readonly
    import trading.kiwoom_orders as orders

    now = datetime(2026, 9, 21, 8, 0, tzinfo=ZoneInfo('America/New_York')).timestamp()
    state = {
        'rows': [],
        'accounts': [{'account_profile': 'account2', 'updated_at': '2026-09-21T12:00:00Z',
                      'overseas': {'orderable_cash': 1000}}],
    }
    calls = []
    overrides = {}

    class FakeClient:
        def get_overseas_orderbook(self, token, *, ticker, exchange):
            calls.append(ticker)
            override = overrides.get(ticker, {})
            if isinstance(override, Exception):
                raise override
            return {'quote': dict(stk_cd=ticker, stex_tp=exchange, cur_prc='100',
                                  dt='20260918', bid_tm='16:00') | override}

    def forbidden(*args, **kwargs):
        raise AssertionError('display must not construct an order client')

    monkeypatch.setattr(orders, 'KiwoomOrderClient', forbidden)
    monkeypatch.setattr(holdings_api, '_load_kiwoom_portfolio', lambda **kwargs: state)
    monkeypatch.setattr(holdings_api, '_get_token', lambda *args: 'fake-token')
    monkeypatch.setattr(readonly, 'load_profile_configs', lambda: {'account2': object()})
    monkeypatch.setattr(readonly, 'KiwoomReadOnlyClient', lambda *args: FakeClient())
    monkeypatch.setattr(sizing.time, 'time', lambda: now)
    return state, calls, overrides


def test_weekend_reference_prices_size_only_targets_and_owned_holdings(reference_feed):
    state, calls, _ = reference_feed
    state['rows'] = [
        {'account_profile': 'account2', 'market': 'US', 'ticker': 'QQQ', 'quantity': 2},
        {'account_profile': 'account1', 'market': 'US', 'ticker': 'BIL', 'quantity': 10},
        {'account_profile': 'account2', 'market': 'US', 'ticker': 'OTHER', 'quantity': 10},
    ]
    result = build_live_allocation_sizing({'combined_allocations': {'SPY': 1}})
    assert calls == ['QQQ', 'SPY']
    assert result['plan_valid'] is True
    assert result['estimate_only'] is True
    assert result['execution_enabled'] is False
    assert result['quote_time_verified'] is True
    rows = {r['ticker']: r for r in result['items']}
    assert rows['SPY']['buy_quantity'] == 12
    assert rows['QQQ']['additional_sell_quantity'] == 2
    assert rows['SPY']['price_status'] == 'reference'
    assert rows['SPY']['price_as_of'] == '2026-09-18T20:00:00+00:00'
    assert result['account_as_of'] == '2026-09-21T12:00:00Z'
    assert len(result['warnings']) == 2


def test_recent_quote_stays_reference_only_without_stale_warning(reference_feed):
    _, _, overrides = reference_feed
    overrides['SPY'] = {'dt': '20260921', 'bid_tm': '08:00'}
    result = build_live_allocation_sizing({'combined_allocations': {'SPY': 1}})
    assert result['items'][0]['buy_quantity'] == 10
    assert result['items'][0]['price_status'] == 'recent'
    assert result['warnings'] == []
    assert result['execution_enabled'] is False


@pytest.mark.parametrize('override, message', [
    ({'dt': '20260913'}, '7일'),
    ({'dt': '20260922'}, '미래'),
    ({'bid_tm': None}, '기준시각'),
    ({'cur_prc': 'NaN'}, '가격'),
    ({'cur_prc': 'Infinity'}, '가격'),
    ({'cur_prc': '0'}, '가격'),
    ({'stk_cd': 'WRONG'}, '불일치'),
    ({'stex_tp': 'ND'}, '불일치'),
    (RuntimeError('secret-broker-response'), '호가 조회 실패'),
])
def test_invalid_quote_is_unavailable_not_zero(reference_feed, override, message):
    _, calls, overrides = reference_feed
    overrides['SPY'] = override
    result = build_live_allocation_sizing({'combined_allocations': {'SPY': .5, 'QQQ': .5}})
    assert calls == ['QQQ', 'SPY']
    assert result['plan_valid'] is False
    assert result['planning_assets'] is None
    for row in result['items']:
        assert row['buy_quantity'] is None
        assert row['additional_sell_quantity'] is None
        assert row['target_quantity'] is None
    assert any('SPY' in error and message in error for error in result['validation_errors'])
    assert 'secret-broker-response' not in str(result)
    assert next(r for r in result['items'] if r['ticker'] == 'QQQ')['price'] == 100


def test_account_read_failure_has_safe_stage_error(reference_feed, monkeypatch):
    import web.kiwoom_holdings as holdings_api

    def fail(**kwargs):
        raise RuntimeError('secret-account-response')

    monkeypatch.setattr(holdings_api, '_load_kiwoom_portfolio', fail)
    with pytest.raises(SizingUnavailable, match='계좌 잔고·현금 조회 실패') as exc:
        build_live_allocation_sizing({'combined_allocations': {'SPY': 1}})
    assert 'secret' not in str(exc.value)
