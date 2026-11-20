from web.kiwoom_sizing import apply_live_position_sizing


def _summary(profile, name, *, kr_cash=0, kr_equity=0, us_cash=0, us_stock=0):
    return {
        "account_profile": profile,
        "display_name": name,
        "domestic": {
            "cash": kr_cash,
            "orderable_cash": kr_cash,
            "evaluation_amount": max(0, kr_equity - kr_cash),
            "estimated_assets": kr_equity,
        },
        "overseas": {
            "cash": us_cash,
            "orderable_cash": us_cash,
            "evaluation_amount": us_stock,
        },
    }


def test_live_sizing_routes_us_to_account1_and_kr_to_account4(monkeypatch):
    monkeypatch.setattr(
        "web.kiwoom_sizing._sizing_config",
        lambda market: {
            "RISK_PCT": 1,
            "MAX_POSITION_PCT": 20,
            "MAX_TOTAL_HEAT_PCT": 6,
            "MIN_R_PCT": 3,
            "MAX_R_PCT": 15,
        },
    )
    candidates = [
        {"market": "US", "ticker": "AAPL", "price": 100, "stop_loss": 90},
        {"market": "KR", "ticker": "005930", "price": 100_000, "stop_loss": 90_000},
    ]
    summaries = [
        _summary("account1", "자유투자", us_cash=5_000, us_stock=95_000),
        _summary("account2", "퀀트투자", us_cash=1_000_000),
        _summary("account4", "ISA", kr_cash=5_000_000, kr_equity=100_000_000),
    ]

    result = apply_live_position_sizing(candidates, summaries, [])

    assert result[0]["live_sizing"]["account_profile"] == "account1"
    assert result[0]["live_sizing"]["qty"] == 50
    assert result[0]["live_sizing"]["constrained_by"] == "cash"
    assert result[1]["live_sizing"]["account_profile"] == "account4"
    assert result[1]["live_sizing"]["qty"] == 50
    assert result[1]["live_sizing"]["read_only"] is True


def test_live_sizing_keeps_candidate_when_stop_is_missing():
    result = apply_live_position_sizing(
        [{"market": "US", "ticker": "AAPL", "price": 100, "stop_loss": None}],
        [_summary("account1", "자유투자", us_cash=10_000)],
        [],
    )

    assert result[0]["live_sizing"] is None


def _holding(profile, market, ticker, **over):
    row = {"account_profile": profile, "market": market, "ticker": ticker, "quantity": 10,
           "current_price": 100.0, "current_stop_loss": None, "source": "kiwoom"}
    row.update(over)
    return row


def test_live_sizing_hidden_when_existing_holding_has_unknown_stop():
    # Broker holdings carry no stop, so the heat limit cannot be evaluated.
    result = apply_live_position_sizing(
        [{"market": "US", "ticker": "AAPL", "price": 100, "stop_loss": 90}],
        [_summary("account1", "자유투자", us_cash=5_000, us_stock=95_000)],
        [_holding("account1", "US", "NVDA")],
    )
    assert result[0]["live_sizing"] is None
    assert "손절가" in result[0]["live_sizing_blocked_reason"]
    assert "NVDA" not in result[0]["live_sizing_blocked_reason"]  # count only, no names needed
    assert "1건" in result[0]["live_sizing_blocked_reason"]


def test_live_sizing_hidden_when_existing_holding_has_unknown_price():
    result = apply_live_position_sizing(
        [{"market": "US", "ticker": "AAPL", "price": 100, "stop_loss": 90}],
        [_summary("account1", "자유투자", us_cash=5_000, us_stock=95_000)],
        [_holding("account1", "US", "NVDA", current_stop_loss=80.0, current_price=None)],
    )
    assert result[0]["live_sizing"] is None
    assert result[0]["live_sizing_blocked_reason"]


def test_live_sizing_block_is_per_account_and_market():
    result = apply_live_position_sizing(
        [{"market": "US", "ticker": "AAPL", "price": 100, "stop_loss": 90},
         {"market": "KR", "ticker": "005930", "price": 100_000, "stop_loss": 90_000}],
        [_summary("account1", "자유투자", us_cash=5_000, us_stock=95_000),
         _summary("account4", "ISA", kr_cash=5_000_000, kr_equity=100_000_000)],
        [_holding("account1", "US", "NVDA")],  # only the US account is unknown
    )
    assert result[0]["live_sizing"] is None
    assert result[1]["live_sizing"]["qty"] == 50
    assert "live_sizing_blocked_reason" not in result[1]


def test_live_sizing_allows_known_stops_and_ignores_other_accounts():
    result = apply_live_position_sizing(
        [{"market": "US", "ticker": "AAPL", "price": 100, "stop_loss": 90}],
        [_summary("account1", "자유투자", us_cash=5_000, us_stock=95_000)],
        [_holding("account1", "US", "NVDA", current_stop_loss=95.0),
         _holding("account2", "US", "QQQ")],  # account2 ETF has no bearing on account1
    )
    assert result[0]["live_sizing"]["qty"] > 0
