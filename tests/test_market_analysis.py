"""Market analysis cache contracts for point-in-time scans."""
import pandas as pd

from scanner.time_context import ScanContext


def _index_frame():
    idx = pd.bdate_range("2025-01-02", periods=220)
    values = [100.0 + i * 0.1 for i in range(len(idx))]
    frame = pd.DataFrame({
        "Open": values,
        "High": [value + 1 for value in values],
        "Low": [value - 1 for value in values],
        "Close": values,
        "Volume": [1_000_000] * len(values),
    }, index=idx)
    frame.attrs["data_status"] = "FINAL"
    return frame


def test_context_cache_is_keyed_per_market_session(monkeypatch):
    from scanner import kr_stocks, market_analysis, us_stocks

    market_analysis._context_cache.clear()
    calls = {"KR": 0, "US": 0}

    def fetch_kr(ticker, **kwargs):
        calls["KR"] += 1
        return _index_frame()

    def fetch_us(ticker, **kwargs):
        calls["US"] += 1
        return _index_frame()

    monkeypatch.setattr(kr_stocks, "get_kr_ohlcv", fetch_kr)
    monkeypatch.setattr(us_stocks, "get_us_ohlcv", fetch_us)

    contexts = {
        "KR": ScanContext.create("KR", "2026-08-25T06:46:00Z"),
        "US": ScanContext.create("US", "2026-08-25T20:16:00Z"),
    }
    market_analysis.get_market_stages(scan_contexts=contexts)
    first_calls = dict(calls)
    market_analysis.get_market_stages(scan_contexts=contexts)
    assert calls == first_calls

    next_kr_session = {
        **contexts,
        "KR": ScanContext.create("KR", "2026-08-26T06:46:00Z"),
    }
    market_analysis.get_market_stages(scan_contexts=next_kr_session)

    assert calls["US"] == first_calls["US"]
    assert calls["KR"] == first_calls["KR"] + 4  # 1 index + 3 sectors
