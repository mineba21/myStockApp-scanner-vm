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


def test_live_sizing_routes_us_to_account2_and_kr_to_account4(monkeypatch):
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
        _summary("account1", "퀀트투자", us_cash=1_000_000),
        _summary("account2", "자유투자", us_cash=5_000, us_stock=95_000),
        _summary("account4", "ISA", kr_cash=5_000_000, kr_equity=100_000_000),
    ]

    result = apply_live_position_sizing(candidates, summaries, [])

    assert result[0]["live_sizing"]["account_profile"] == "account2"
    assert result[0]["live_sizing"]["qty"] == 50
    assert result[0]["live_sizing"]["constrained_by"] == "cash"
    assert result[1]["live_sizing"]["account_profile"] == "account4"
    assert result[1]["live_sizing"]["qty"] == 50
    assert result[1]["live_sizing"]["read_only"] is True


def test_live_sizing_keeps_candidate_when_stop_is_missing():
    result = apply_live_position_sizing(
        [{"market": "US", "ticker": "AAPL", "price": 100, "stop_loss": None}],
        [_summary("account2", "자유투자", us_cash=10_000)],
        [],
    )

    assert result[0]["live_sizing"] is None
