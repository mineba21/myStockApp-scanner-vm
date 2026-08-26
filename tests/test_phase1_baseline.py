"""Phase 1 safety baseline for the staged Weinstein v1 rollout."""

import importlib
from datetime import date, timedelta

import exchange_calendars as xcals
import numpy as np
import pandas as pd
import pytest


def _make_breakout_df(price_scale: float) -> pd.DataFrame:
    """Build the deterministic legacy breakout used for KR and US baselines."""
    prices = []
    volumes = []

    for i in range(150):
        prices.append(50.0 + (95.0 - 50.0) * i / 149)
        volumes.append(500_000)

    for i in range(80):
        prices.append(100.0 + 2.0 * np.sin(i * np.pi / 5))
        volumes.append(500_000)

    prices[-1] = 104.0
    volumes[-1] = 6_000_000
    prices = [price * price_scale for price in prices]

    dates = [date(2022, 1, 1) + timedelta(days=i) for i in range(len(prices))]
    return pd.DataFrame(
        {
            "Open": [price * 0.998 for price in prices],
            "High": [price * 1.005 for price in prices],
            "Low": [price * 0.995 for price in prices],
            "Close": prices,
            "Volume": volumes,
        },
        index=pd.DatetimeIndex(dates),
    )


@pytest.mark.parametrize(
    ("market", "ticker", "price_scale"),
    (("KR", "000000", 1_000.0), ("US", "TEST", 1.0)),
)
def test_legacy_breakout_result_is_preserved(market, ticker, price_scale):
    from scanner.weinstein import analyze_stock

    df = _make_breakout_df(price_scale)
    result = analyze_stock(df, ticker, "Phase 1 baseline", market)

    assert result is not None
    assert result["market"] == market
    assert result["ticker"] == ticker
    assert result["signal_type"] == "BREAKOUT"
    assert result["signal_date"] == str(df.index[-1].date())
    assert result["strict_filter_passed"] is None
    assert result["filter_reasons"] == []
    assert 0 < result["stop_loss"] < result["strict_price"]


def test_strategy_rollout_defaults_are_legacy_and_off(monkeypatch):
    monkeypatch.delenv("STRATEGY_VERSION", raising=False)
    monkeypatch.delenv("WEINSTEIN_V1_MODE", raising=False)

    import config

    reloaded = importlib.reload(config)
    assert reloaded.STRATEGY_VERSION == "legacy_v4"
    assert reloaded.WEINSTEIN_V1_MODE == "off"


def test_blank_strategy_rollout_mode_is_treated_as_off(monkeypatch):
    monkeypatch.setenv("WEINSTEIN_V1_MODE", "   ")

    import config

    reloaded = importlib.reload(config)
    assert reloaded.WEINSTEIN_V1_MODE == "off"


def test_strategy_version_rejects_unknown_value(monkeypatch):
    monkeypatch.setenv("STRATEGY_VERSION", "weinstein-breakout-v1")

    import config

    with pytest.raises(ValueError, match="legacy_v4, weinstein_breakout_v1"):
        importlib.reload(config)

    monkeypatch.setenv("STRATEGY_VERSION", "legacy_v4")
    importlib.reload(config)


def test_strategy_rollout_rejects_unknown_mode(monkeypatch):
    monkeypatch.setenv("WEINSTEIN_V1_MODE", "unexpected")

    import config

    with pytest.raises(ValueError, match="off, shadow, primary"):
        importlib.reload(config)

    monkeypatch.setenv("WEINSTEIN_V1_MODE", "off")
    importlib.reload(config)


@pytest.mark.parametrize(
    ("market", "ticker", "price_scale", "calendar_name", "as_of"),
    (
        ("KR", "000000", 1_000.0, "XKRX", "2026-08-25T06:46:00Z"),
        ("US", "TEST", 1.0, "XNYS", "2026-08-25T20:16:00Z"),
    ),
)
def test_pit_legacy_fixture_uses_real_exchange_sessions(
        market, ticker, price_scale, calendar_name, as_of):
    from scanner.time_context import ScanContext
    from scanner.weinstein import analyze_stock

    df = _make_breakout_df(price_scale)
    context = ScanContext.create(
        market, as_of, strategy_version="legacy_v4"
    )
    calendar = xcals.get_calendar(calendar_name)
    end = calendar.sessions.get_loc(pd.Timestamp(context.session_date))
    sessions = calendar.sessions[end - len(df) + 1:end + 1]
    df.index = pd.DatetimeIndex(sessions).tz_localize(None)

    result = analyze_stock(
        df, ticker, "PIT Phase 1 baseline", market, scan_context=context
    )

    assert result is not None
    assert result["signal_type"] == "BREAKOUT"
    assert result["strategy_version"] == "legacy_v4"
    assert result["breakout_volume_rule"] == "AND"
    assert result["last_bar_date"] == context.session_date.isoformat()
