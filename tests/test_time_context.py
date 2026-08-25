"""Phase 2 — market-session time correctness and no-look-ahead guards."""
from datetime import date

import exchange_calendars as xcals
import pandas as pd

from scanner.time_context import (
    ScanContext,
    completed_week_label,
    normalize_close_series,
    normalize_ohlcv,
)
from scanner.weinstein import (
    _build_indicators,
    _daily_vol_ratio,
    classify_stage,
    compute_weekly_indicators,
    detect_base_pivot,
    to_weekly_ohlcv,
)


def _ohlcv(index, close=100.0, volume=100.0):
    index = pd.DatetimeIndex(index)
    size = len(index)
    closes = ([float(close)] * size if not hasattr(close, "__len__")
              else list(close))
    volumes = ([float(volume)] * size if not hasattr(volume, "__len__")
               else list(volume))
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [value * 1.01 for value in closes],
            "Low": [value * 0.99 for value in closes],
            "Close": closes,
            "Volume": volumes,
        },
        index=index,
    )


def _session_index(market, start, end):
    calendar_name = "XKRX" if market == "KR" else "XNYS"
    sessions = xcals.get_calendar(calendar_name).sessions_in_range(start, end)
    return pd.DatetimeIndex(sessions).tz_localize(None)


def test_close_delay_uses_last_final_session_for_both_markets():
    assert ScanContext.create("KR", "2026-08-25T06:40:00Z").session_date == date(2026, 8, 24)
    assert ScanContext.create("KR", "2026-08-25T06:46:00Z").session_date == date(2026, 8, 25)
    assert ScanContext.create("US", "2026-08-25T20:10:00Z").session_date == date(2026, 8, 24)
    assert ScanContext.create("US", "2026-08-25T20:16:00Z").session_date == date(2026, 8, 25)


def test_us_holiday_early_close_and_dst_are_calendar_driven():
    # 7/3 is the observed Independence Day holiday in 2026.
    holiday = ScanContext.create("US", "2026-07-03T22:00:00Z")
    assert holiday.session_date == date(2026, 7, 2)
    assert completed_week_label(holiday) == date(2026, 7, 3)

    # 11/27 closes at 13:00 ET (18:00 UTC), then waits the configured 15 min.
    assert ScanContext.create("US", "2026-11-27T18:10:00Z").session_date == date(2026, 11, 25)
    assert ScanContext.create("US", "2026-11-27T18:16:00Z").session_date == date(2026, 11, 27)

    # Winter regular close is 21:00 UTC, proving UTC hour is not hard-coded.
    assert ScanContext.create("US", "2026-12-07T21:10:00Z").session_date == date(2026, 12, 4)
    assert ScanContext.create("US", "2026-12-07T21:16:00Z").session_date == date(2026, 12, 7)


def test_daily_normalization_drops_future_weekend_and_holiday_rows():
    context = ScanContext.create("US", "2026-07-06T20:16:00Z")
    raw = _ohlcv(pd.date_range("2026-07-01", "2026-07-07", freq="D"))
    final = normalize_ohlcv(raw, context)
    assert list(final.index.date) == [
        date(2026, 7, 1), date(2026, 7, 2), date(2026, 7, 6)
    ]
    assert final.attrs["bar_status"] == "FINAL"

    benchmark = normalize_close_series(raw["Close"], context)
    assert list(benchmark.index.date) == list(final.index.date)


def test_partial_week_is_excluded_but_holiday_short_week_is_final():
    midweek = ScanContext.create("US", "2026-08-25T20:16:00Z")
    idx = _session_index("US", "2026-08-17", "2026-08-25")
    weekly = to_weekly_ohlcv(_ohlcv(idx), midweek)
    assert list(weekly.index.date) == [date(2026, 8, 21)]

    holiday_week = ScanContext.create("US", "2026-07-02T20:16:00Z")
    idx = _session_index("US", "2026-06-29", "2026-07-02")
    weekly = to_weekly_ohlcv(_ohlcv(idx), holiday_week)
    assert list(weekly.index.date) == [date(2026, 7, 3)]


def test_evaluation_bar_is_excluded_from_volume_baselines_and_pivot():
    daily_idx = pd.bdate_range("2025-01-02", periods=170)
    daily = _ohlcv(daily_idx, volume=[100.0] * 169 + [1000.0])
    daily_ind = _build_indicators(daily)
    assert daily_ind["cur_va"] == 100.0
    assert _daily_vol_ratio(daily, len(daily) - 1) == 10.0

    weekly_idx = pd.date_range("2025-01-03", periods=31, freq="W-FRI")
    weekly = _ohlcv(weekly_idx, volume=[100.0] * 30 + [1000.0])
    weekly_ind = compute_weekly_indicators(weekly)
    assert weekly_ind["cur_volavg_w"] == 100.0
    assert weekly_ind["weekly_volume_ratio"] == 10.0

    base = _ohlcv(pd.bdate_range("2026-01-02", periods=31), close=100.0)
    base.iloc[-1, base.columns.get_loc("High")] = 150.0
    pivot = detect_base_pivot(base, lookback_weeks=6, min_weeks=5)
    assert pivot is not None
    assert pivot["pivot_price"] < 150.0


def test_missing_weekly_history_is_not_silently_stage1():
    daily = {
        "cur_p": 100.0,
        "cur_m150": 90.0,
        "slope150": 0.1,
    }
    assert classify_stage(None, daily) == "INSUFFICIENT_DATA"


def test_us_replay_fetch_uses_as_of_history_window(monkeypatch):
    from scanner import us_stocks

    context = ScanContext.create("US", "2025-08-25T20:16:00Z")
    idx = _session_index("US", "2024-07-01", "2025-08-25")
    source = _ohlcv(idx)
    captured = {}

    class FakeTicker:
        def history(self, **kwargs):
            captured.update(kwargs)
            return source

    monkeypatch.setattr(us_stocks.yf, "Ticker", lambda ticker: FakeTicker())
    result = us_stocks.get_us_ohlcv("TEST", scan_context=context)

    assert "period" not in captured
    assert captured["end"] == "2025-08-26"
    assert result.index[-1].date() == context.session_date


def test_analyze_stock_is_invariant_to_rows_after_as_of(monkeypatch):
    from scanner import weinstein as module

    context = ScanContext.create("US", "2026-08-21T20:16:00Z")
    idx = _session_index("US", "2025-07-01", "2026-08-28")
    prices = [80.0 + position * 0.1 for position in range(len(idx))]
    frame = _ohlcv(idx, close=prices, volume=100.0)
    cutoff_frame = frame.loc[:pd.Timestamp(context.session_date)]

    def signal_on_evaluation_bar(snapshot, weekly_ind, daily_ind, **kwargs):
        return {
            "signal_type": "BREAKOUT",
            "signal_date": str(snapshot.index[-1].date()),
            "vol_ratio": 3.0,
            "pivot_price": float(snapshot["Close"].iloc[-2]),
            "support_level": float(daily_ind["cur_m50"]),
            "base_quality": "STRONG",
            "base_quality_v4": "TIGHT",
            "base_weeks": 6.0,
            "base_low": float(snapshot["Low"].iloc[-10:].min()),
            "warning_flags": [],
        }

    monkeypatch.setattr(module, "detect_stage2_breakout", signal_on_evaluation_bar)
    monkeypatch.setattr(module, "detect_continuation_breakout", lambda *a, **k: None)
    monkeypatch.setattr(module, "detect_rebound_entry", lambda *a, **k: None)

    at_cutoff = module.analyze_stock(
        cutoff_frame, "TEST", "Test", "US", scan_context=context
    )
    with_future = module.analyze_stock(
        frame, "TEST", "Test", "US", scan_context=context
    )

    keys = [
        "signal_type", "signal_date", "price", "ma150", "ma50",
        "weekly_stage", "volume_avg", "strict_price", "session_date",
    ]
    assert at_cutoff is not None
    assert {key: at_cutoff[key] for key in keys} == {
        key: with_future[key] for key in keys
    }
    assert with_future["bar_status"] == "FINAL"
