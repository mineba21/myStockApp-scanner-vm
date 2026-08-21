"""Phase 4 — `kr_stocks.fetch_ohlcv` / `us_stocks.fetch_ohlcv` 의 예외 정책.

DataFetchError 도입 후 어댑터 contract:
  - 외부 어댑터(`get_kr_ohlcv`/`get_us_ohlcv`) 가 raise → `DataFetchError`
  - 외부 어댑터가 None 반환 → None (legitimately empty)
  - lookback_days ≤ 0 → None (정상 빈 결과)
  - 외부 어댑터가 정상 DF 반환 → DF 그대로
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, timedelta
import pandas as pd
import pytest


def _make_daily(n=120, start_price=100.0):
    dates  = [date(2023, 1, 1) + timedelta(days=i) for i in range(n)]
    prices = [start_price + i * 0.3 for i in range(n)]
    return pd.DataFrame({
        "Open":   [p * 0.998 for p in prices],
        "High":   [p * 1.005 for p in prices],
        "Low":    [p * 0.995 for p in prices],
        "Close":  prices,
        "Volume": [500_000.0] * n,
    }, index=pd.DatetimeIndex(dates))


# ══════════════════════════════════════════════════════════════════
# KR fetch_ohlcv
# ══════════════════════════════════════════════════════════════════

class TestKRFetchOhlcvErrorPolicy:
    def test_raises_datafetcherror_when_underlying_raises(self, monkeypatch):
        from scanner import kr_stocks
        from scanner.errors import DataFetchError

        def boom(ticker, period_years=2):
            raise RuntimeError("FDR network down")

        monkeypatch.setattr(kr_stocks, "get_kr_ohlcv", boom)
        with pytest.raises(DataFetchError) as excinfo:
            kr_stocks.fetch_ohlcv("005930", lookback_days=365)
        # __cause__ 로 원본 예외 보존 — 디버깅 시 추적 가능
        assert isinstance(excinfo.value.__cause__, RuntimeError)
        assert "005930" in str(excinfo.value)

    def test_returns_none_when_underlying_returns_none(self, monkeypatch):
        from scanner import kr_stocks

        monkeypatch.setattr(kr_stocks, "get_kr_ohlcv",
                            lambda ticker, period_years=2: None)
        # legitimately empty (e.g. 미상장/비활성 티커) → None, 예외 아님
        assert kr_stocks.fetch_ohlcv("999999", lookback_days=365) is None

    def test_returns_df_on_success(self, monkeypatch):
        from scanner import kr_stocks

        df = _make_daily(n=200)
        monkeypatch.setattr(kr_stocks, "get_kr_ohlcv",
                            lambda ticker, period_years=2: df)
        result = kr_stocks.fetch_ohlcv("005930", lookback_days=365)
        assert result is not None
        assert len(result) == 200

    def test_zero_lookback_returns_none_without_calling_underlying(self, monkeypatch):
        from scanner import kr_stocks

        called = {"n": 0}
        def fake(*a, **kw):
            called["n"] += 1
            return _make_daily()
        monkeypatch.setattr(kr_stocks, "get_kr_ohlcv", fake)

        assert kr_stocks.fetch_ohlcv("005930", lookback_days=0)   is None
        assert kr_stocks.fetch_ohlcv("005930", lookback_days=-5)  is None
        assert called["n"] == 0  # ≤0 분기에서 underlying 호출 자체 안 함


# ══════════════════════════════════════════════════════════════════
# US fetch_ohlcv (대칭 정책 검증)
# ══════════════════════════════════════════════════════════════════

class TestUSFetchOhlcvErrorPolicy:
    def test_raises_datafetcherror_when_underlying_raises(self, monkeypatch):
        from scanner import us_stocks
        from scanner.errors import DataFetchError

        def boom(ticker, period="2y"):
            raise RuntimeError("yfinance gone")

        monkeypatch.setattr(us_stocks, "get_us_ohlcv", boom)
        with pytest.raises(DataFetchError) as excinfo:
            us_stocks.fetch_ohlcv("AAPL", lookback_days=365)
        assert isinstance(excinfo.value.__cause__, RuntimeError)
        assert "AAPL" in str(excinfo.value)

    def test_returns_none_when_underlying_returns_none(self, monkeypatch):
        from scanner import us_stocks

        monkeypatch.setattr(us_stocks, "get_us_ohlcv",
                            lambda ticker, period="2y": None)
        assert us_stocks.fetch_ohlcv("ZZZZ", lookback_days=365) is None
