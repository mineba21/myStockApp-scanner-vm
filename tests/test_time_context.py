"""Phase 2 — market-session time correctness and no-look-ahead guards."""
from datetime import date

import exchange_calendars as xcals
import pandas as pd
import pytest

from scanner.time_context import (
    ScanContext,
    completed_week_label,
    last_started_session,
    normalize_close_series,
    normalize_ohlcv,
)
from scanner.weinstein import (
    _build_indicators,
    _daily_vol_ratio,
    _scan_offsets,
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


def test_naive_as_of_is_rejected_instead_of_assumed_utc():
    with pytest.raises(ValueError, match="explicit timezone"):
        ScanContext.create("KR", "2026-08-25 15:00:00")


def test_latest_started_session_distinguishes_open_and_preopen_markets():
    assert last_started_session("KR", "2026-08-25T05:00:00Z") == date(2026, 8, 25)
    # 22:00 KST is 09:00 ET during DST, before the NYSE 09:30 open.
    assert last_started_session("US", "2026-08-25T13:00:00Z") == date(2026, 8, 24)


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


def test_stale_daily_data_is_not_labelled_final():
    context = ScanContext.create("US", "2026-08-25T20:16:00Z")
    idx = _session_index("US", "2026-08-10", "2026-08-21")
    stale = normalize_ohlcv(_ohlcv(idx), context)

    assert stale.attrs["bar_status"] == "STALE"
    assert stale.attrs["data_status"] == "STALE"
    assert stale.attrs["last_bar_date"] == "2026-08-21"
    assert stale.attrs["staleness_sessions"] == 2


def test_stale_daily_data_cannot_emit_a_current_buy_signal(monkeypatch):
    from scanner import weinstein as module

    context = ScanContext.create("US", "2026-08-25T20:16:00Z")
    idx = _session_index("US", "2025-06-02", "2026-08-21")
    frame = _ohlcv(idx, close=[80.0 + i * 0.1 for i in range(len(idx))])

    def should_not_run(*args, **kwargs):
        raise AssertionError("stale data reached a signal detector")

    monkeypatch.setattr(module, "detect_stage2_breakout", should_not_run)
    assert module.analyze_stock(
        frame, "TEST", "Test", "US", scan_context=context
    ) is None


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


def test_latest_candidate_offsets_are_unique_and_flag_is_consistent():
    assert list(_scan_offsets(20, include_latest=True, latest_only=False)) == list(range(7))
    assert list(_scan_offsets(20, include_latest=False, latest_only=False)) == list(range(1, 7))
    assert list(_scan_offsets(20, include_latest=True, latest_only=True)) == [0]
    assert list(_scan_offsets(20, include_latest=False, latest_only=True)) == [1]


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


def test_us_fetch_applies_minimum_length_after_normalization(monkeypatch):
    from scanner import us_stocks

    context = ScanContext.create("US", "2025-08-25T20:16:00Z")
    # Raw provider frame is long enough, but only a small prefix is <= as_of.
    idx = _session_index("US", "2025-08-01", "2025-11-15")
    source = _ohlcv(idx)

    class FakeTicker:
        def history(self, **kwargs):
            return source

    monkeypatch.setattr(us_stocks.yf, "Ticker", lambda ticker: FakeTicker())
    assert us_stocks.get_us_ohlcv("TEST", scan_context=context) is None


def test_point_in_time_selection_preserves_type_priority_and_reuses_work(monkeypatch):
    from scanner import weinstein as module

    context = ScanContext.create("US", "2026-08-21T20:16:00Z")
    idx = _session_index("US", "2025-07-01", "2026-08-21")
    frame = _ohlcv(idx, close=[80.0 + i * 0.1 for i in range(len(idx))])
    older_breakout_date = idx[-3].date()
    newest_rebound_date = idx[-1].date()
    calls = {"prepare": 0, "weekly": 0}

    real_prepare = module._prepare_daily_indicators
    real_weekly = module.compute_weekly_indicators

    def counted_prepare(df):
        calls["prepare"] += 1
        return real_prepare(df)

    def counted_weekly(df):
        calls["weekly"] += 1
        return real_weekly(df)

    def breakout(snapshot, weekly_ind, daily_ind, **kwargs):
        if snapshot.index[-1].date() == older_breakout_date:
            return {"signal_type": "BREAKOUT",
                    "signal_date": str(older_breakout_date)}
        return None

    def rebound(snapshot, weekly_ind, daily_ind, **kwargs):
        if snapshot.index[-1].date() == newest_rebound_date:
            return {"signal_type": "REBOUND",
                    "signal_date": str(newest_rebound_date)}
        return None

    monkeypatch.setattr(module, "_prepare_daily_indicators", counted_prepare)
    monkeypatch.setattr(module, "compute_weekly_indicators", counted_weekly)
    monkeypatch.setattr(module, "detect_stage2_breakout", breakout)
    monkeypatch.setattr(module, "detect_continuation_breakout", lambda *a, **k: None)
    monkeypatch.setattr(module, "detect_rebound_entry", rebound)

    signal, *_ = module._detect_signal_point_in_time(frame, context)

    assert signal["signal_type"] == "BREAKOUT"
    assert signal["signal_date"] == str(older_breakout_date)
    assert calls["prepare"] == 1
    # 금요일 기준 최근 7세션은 최대 3개의 완료 주 라벨에 걸칠 수 있다.
    assert calls["weekly"] <= 3


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


def test_sell_check_reuses_normalized_daily_and_supplied_weekly(monkeypatch):
    from scanner import time_context, weinstein as module

    context = ScanContext.create("US", "2026-08-25T20:16:00Z")
    idx = _session_index("US", "2025-01-02", "2026-08-25")
    normalized = normalize_ohlcv(
        _ohlcv(idx, close=[80.0 + i * 0.1 for i in range(len(idx))]),
        context,
    )
    weekly = to_weekly_ohlcv(normalized, context)

    def should_not_run(*args, **kwargs):
        raise AssertionError("already normalized input was recomputed")

    monkeypatch.setattr(time_context, "normalize_ohlcv", should_not_run)
    monkeypatch.setattr(module, "to_weekly_ohlcv", should_not_run)

    module.check_sell_signal(
        normalized, "TEST", "Test", "US",
        weekly_df=weekly, scan_context=context, current_price=100.0,
    )
