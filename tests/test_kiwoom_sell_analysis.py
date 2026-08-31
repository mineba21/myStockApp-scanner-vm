import pandas as pd

from web import kiwoom_sell_analysis


def _daily():
    index = pd.date_range("2025-01-01", periods=220, freq="B")
    values = [100 + i * 0.1 for i in range(len(index))]
    return pd.DataFrame({
        "Open": values,
        "High": [value + 1 for value in values],
        "Low": [value - 1 for value in values],
        "Close": values,
        "Volume": [1_000_000] * len(index),
    }, index=index)


def test_only_account1_and_account4_receive_read_only_sell_analysis(
    monkeypatch, tmp_path
):
    from scanner import kr_stocks, market_analysis, us_stocks, weinstein

    monkeypatch.setenv("KIWOOM_SELL_CACHE_FILE", str(tmp_path / "sell.json"))
    kiwoom_sell_analysis.clear_kiwoom_sell_cache()
    calls = []
    monkeypatch.setattr(market_analysis, "get_benchmark_close", lambda market: _daily()["Close"])
    monkeypatch.setattr(us_stocks, "get_us_ohlcv", lambda ticker: _daily())
    monkeypatch.setattr(kr_stocks, "get_kr_ohlcv", lambda ticker: _daily())
    monkeypatch.setattr(weinstein, "to_weekly_ohlcv", lambda frame: frame)

    def fake_signal(frame, ticker, *args, **kwargs):
        calls.append(ticker)
        if ticker == "AAPL":
            return {"severity": "HIGH", "sell_reason": "주봉 30-SMA 하향 이탈"}
        return None

    monkeypatch.setattr(weinstein, "check_sell_signal", fake_signal)
    rows = [
        {"account_profile": "account1", "market": "US", "ticker": "AAPL", "name": "Apple", "avg_price": 100, "sell_status": "BROKER_LIVE"},
        {"account_profile": "account2", "market": "US", "ticker": "SPY", "name": "SPY", "avg_price": 100, "sell_status": "BROKER_LIVE"},
        {"account_profile": "account4", "market": "KR", "ticker": "005930", "name": "삼성전자", "avg_price": 70000, "sell_status": "BROKER_LIVE"},
    ]

    result = kiwoom_sell_analysis.apply_kiwoom_sell_analysis(
        rows, force=True, background=False
    )

    assert calls == ["AAPL", "005930"] or calls == ["005930", "AAPL"]
    assert result[0]["sell_status"] == "SELL_REQUIRED"
    assert result[0]["sell_reason"] == "주봉 30-SMA 하향 이탈"
    assert result[0]["sell_analysis"] == "WEINSTEIN_READ_ONLY"
    assert result[1]["sell_status"] == "BROKER_LIVE"
    assert result[2]["sell_status"] == "HOLD"
    assert result[2]["sell_reason"] == "Weinstein 매도 신호 없음"


def test_missing_price_data_becomes_check_failed(monkeypatch, tmp_path):
    from scanner import market_analysis, us_stocks

    monkeypatch.setenv("KIWOOM_SELL_CACHE_FILE", str(tmp_path / "sell.json"))
    kiwoom_sell_analysis.clear_kiwoom_sell_cache()
    monkeypatch.setattr(market_analysis, "get_benchmark_close", lambda market: None)
    monkeypatch.setattr(us_stocks, "get_us_ohlcv", lambda ticker: None)

    result = kiwoom_sell_analysis.apply_kiwoom_sell_analysis(
        [{"account_profile": "account1", "market": "US", "ticker": "AAPL"}],
        force=True,
        background=False,
    )

    assert result[0]["sell_status"] == "CHECK_FAILED"
    assert "가격 데이터" in result[0]["sell_reason"]
