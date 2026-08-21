"""
Phase 3 — 차트 데이터 API (`GET /api/chart/ohlcv`) 테스트.

- Pydantic/Query 검증 → 422
- fetch_ohlcv → None / [] → candles=[] + 200
- 정상 일봉 → JSON 스키마/길이/MA 채움 검증
- timeframe=weekly → to_weekly_ohlcv 적용 + ma_period=30
- 외부 페치 예외 → 503
- KR/US 라우팅 → 옳은 어댑터 호출
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, timedelta
import pandas as pd
import pytest

from fastapi.testclient import TestClient


# ── 공통 fixture ─────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client():
    """앱 import 전에 init_db / scheduler 부작용 차단."""
    import database.models as dbm
    import scheduler as sched
    dbm.init_db = lambda: None
    sched.start_scheduler = lambda: None
    sched.stop_scheduler = lambda: None
    sched.get_next_run_times = lambda: {}

    import web.app as webapp
    webapp.SITES_API_KEY = "test-key"
    return TestClient(webapp.app, headers={"Authorization": "Bearer test-key"})


def _make_daily(n=300, start_price=100.0, step=0.3, start=date(2023, 1, 2)):
    dates  = [start + timedelta(days=i) for i in range(n)]
    prices = [start_price + i * step for i in range(n)]
    return pd.DataFrame({
        "Open":   [p * 0.998 for p in prices],
        "High":   [p * 1.005 for p in prices],
        "Low":    [p * 0.995 for p in prices],
        "Close":  prices,
        "Volume": [1_000_000.0 + i * 100 for i in range(n)],
    }, index=pd.DatetimeIndex(dates))


# ══════════════════════════════════════════════════════════════════
# 입력 검증 (422)
# ══════════════════════════════════════════════════════════════════

class TestChartValidation:
    def test_invalid_market(self, client):
        r = client.get("/api/chart/ohlcv?market=JP&ticker=AAPL&timeframe=daily&range=1y")
        assert r.status_code == 422

    def test_invalid_timeframe(self, client):
        r = client.get("/api/chart/ohlcv?market=US&ticker=AAPL&timeframe=hourly&range=1y")
        assert r.status_code == 422

    def test_invalid_range(self, client):
        r = client.get("/api/chart/ohlcv?market=US&ticker=AAPL&timeframe=daily&range=10y")
        assert r.status_code == 422

    def test_invalid_ticker_special_chars(self, client):
        r = client.get("/api/chart/ohlcv?market=US&ticker=AA%24PL&timeframe=daily&range=1y")
        assert r.status_code == 422

    def test_missing_ticker(self, client):
        r = client.get("/api/chart/ohlcv?market=US&timeframe=daily&range=1y")
        assert r.status_code == 422

    def test_market_case_insensitive(self, client, monkeypatch):
        from scanner import us_stocks
        monkeypatch.setattr(us_stocks, "fetch_ohlcv", lambda t, lookback_days=730: None)
        r = client.get("/api/chart/ohlcv?market=us&ticker=AAPL&timeframe=daily&range=1y")
        assert r.status_code == 200
        assert r.json()["market"] == "US"


# ══════════════════════════════════════════════════════════════════
# 정상 응답
# ══════════════════════════════════════════════════════════════════

class TestChartDailyHappy:
    def test_returns_candles_and_ma(self, client, monkeypatch):
        from scanner import us_stocks
        df = _make_daily(n=600)  # MA150 채움을 위해 1y + buffer 확보
        monkeypatch.setattr(us_stocks, "fetch_ohlcv", lambda t, lookback_days=730: df)

        r = client.get("/api/chart/ohlcv?market=US&ticker=AAPL&timeframe=daily&range=1y")
        assert r.status_code == 200
        body = r.json()
        assert body["market"] == "US"
        assert body["ticker"] == "AAPL"
        assert body["timeframe"] == "daily"
        assert body["range"] == "1y"
        assert body["ma_period"] == 150
        assert len(body["candles"]) > 0

        c0 = body["candles"][0]
        for k in ("t", "o", "h", "l", "c", "v", "ma"):
            assert k in c0
        # 첫 캔들 t 는 YYYY-MM-DD 포맷
        assert len(c0["t"]) == 10 and c0["t"][4] == "-"
        # MA150은 충분한 buffer 페치 후라 첫 캔들에서도 채워져 있어야 함
        assert all(c["ma"] is not None for c in body["candles"][:5])

    def test_range_trims_visible_window(self, client, monkeypatch):
        from scanner import us_stocks
        df = _make_daily(n=800)
        monkeypatch.setattr(us_stocks, "fetch_ohlcv", lambda t, lookback_days=730: df)

        r6m = client.get("/api/chart/ohlcv?market=US&ticker=AAPL&timeframe=daily&range=6m")
        r1y = client.get("/api/chart/ohlcv?market=US&ticker=AAPL&timeframe=daily&range=1y")
        assert r6m.status_code == 200 and r1y.status_code == 200
        c6 = r6m.json()["candles"]
        c1 = r1y.json()["candles"]
        # 6m < 1y < total 페치 길이
        assert 0 < len(c6) <= 200
        assert len(c6) < len(c1)


class TestChartWeekly:
    def test_weekly_uses_resample_and_ma30(self, client, monkeypatch):
        from scanner import us_stocks, weinstein
        df = _make_daily(n=600)
        monkeypatch.setattr(us_stocks, "fetch_ohlcv", lambda t, lookback_days=730: df)

        called = {}
        original = weinstein.to_weekly_ohlcv
        def spy(daily_df):
            called["weekly_called"] = True
            return original(daily_df)
        monkeypatch.setattr(weinstein, "to_weekly_ohlcv", spy)

        r = client.get("/api/chart/ohlcv?market=US&ticker=AAPL&timeframe=weekly&range=2y")
        assert r.status_code == 200
        body = r.json()
        assert called.get("weekly_called") is True
        assert body["timeframe"] == "weekly"
        assert body["ma_period"] == 30
        # 주봉이라 candles 수 ≈ daily / 5
        assert 0 < len(body["candles"]) < len(df) // 4
        # MA 채움 (buffer 추가 후 30주 SMA 가능)
        assert any(c["ma"] is not None for c in body["candles"])


# ══════════════════════════════════════════════════════════════════
# Empty / Error 경로
# ══════════════════════════════════════════════════════════════════

class TestChartEmpty:
    def test_fetch_returns_none(self, client, monkeypatch):
        from scanner import us_stocks
        monkeypatch.setattr(us_stocks, "fetch_ohlcv", lambda t, lookback_days=730: None)
        r = client.get("/api/chart/ohlcv?market=US&ticker=AAPL&timeframe=daily&range=1y")
        assert r.status_code == 200
        body = r.json()
        assert body["candles"] == []
        assert body["ma_period"] == 150

    def test_fetch_returns_empty_df(self, client, monkeypatch):
        from scanner import us_stocks
        monkeypatch.setattr(us_stocks, "fetch_ohlcv",
                            lambda t, lookback_days=730: pd.DataFrame())
        r = client.get("/api/chart/ohlcv?market=US&ticker=AAPL&timeframe=daily&range=1y")
        assert r.status_code == 200
        assert r.json()["candles"] == []


class TestChartFetchError:
    def test_data_fetch_error_returns_503(self, client, monkeypatch):
        """외부 어댑터의 명시적 DataFetchError → 503 (downstream 일시적 장애)."""
        from scanner import us_stocks
        from scanner.errors import DataFetchError

        def boom(t, lookback_days=730):
            raise DataFetchError("yfinance 다운")

        monkeypatch.setattr(us_stocks, "fetch_ohlcv", boom)
        r = client.get("/api/chart/ohlcv?market=US&ticker=AAPL&timeframe=daily&range=1y")
        assert r.status_code == 503
        body = r.json()
        assert "detail" in body

    def test_unexpected_exception_returns_500(self, client, monkeypatch):
        """그 외 예외(서버 내부 버그) → 500. 503 과 명확히 구분."""
        from scanner import us_stocks

        def boom(t, lookback_days=730):
            raise RuntimeError("내부 버그")

        monkeypatch.setattr(us_stocks, "fetch_ohlcv", boom)
        r = client.get("/api/chart/ohlcv?market=US&ticker=AAPL&timeframe=daily&range=1y")
        assert r.status_code == 500
        body = r.json()
        assert "detail" in body

    def test_data_fetch_error_for_kr_market(self, client, monkeypatch):
        from scanner import kr_stocks
        from scanner.errors import DataFetchError

        def boom(t, lookback_days=730):
            raise DataFetchError("FDR 다운")

        monkeypatch.setattr(kr_stocks, "fetch_ohlcv", boom)
        r = client.get("/api/chart/ohlcv?market=KR&ticker=005930&timeframe=daily&range=1y")
        assert r.status_code == 503


# ══════════════════════════════════════════════════════════════════
# KR/US 라우팅
# ══════════════════════════════════════════════════════════════════

class TestChartRouting:
    def test_kr_routes_to_kr_fetch(self, client, monkeypatch):
        from scanner import kr_stocks, us_stocks

        kr_called = {}
        us_called = {}

        def kr_fake(t, lookback_days=730):
            kr_called["ticker"] = t
            kr_called["lookback_days"] = lookback_days
            return _make_daily(n=400)

        def us_fake(t, lookback_days=730):
            us_called["ticker"] = t
            return _make_daily(n=400)

        monkeypatch.setattr(kr_stocks, "fetch_ohlcv", kr_fake)
        monkeypatch.setattr(us_stocks, "fetch_ohlcv", us_fake)

        r = client.get("/api/chart/ohlcv?market=KR&ticker=005930&timeframe=daily&range=1y")
        assert r.status_code == 200
        assert kr_called["ticker"] == "005930"
        # buffer 250일이 더해져 365 + 250 = 615
        assert kr_called["lookback_days"] == 365 + 250
        assert us_called == {}

    def test_us_routes_to_us_fetch(self, client, monkeypatch):
        from scanner import kr_stocks, us_stocks

        kr_called = {}
        us_called = {}

        def kr_fake(t, lookback_days=730):
            kr_called["hit"] = True
            return _make_daily(n=400)

        def us_fake(t, lookback_days=730):
            us_called["hit"] = True
            return _make_daily(n=600)

        monkeypatch.setattr(kr_stocks, "fetch_ohlcv", kr_fake)
        monkeypatch.setattr(us_stocks, "fetch_ohlcv", us_fake)

        r = client.get("/api/chart/ohlcv?market=US&ticker=AAPL&timeframe=daily&range=1y")
        assert r.status_code == 200
        assert us_called.get("hit") is True
        assert kr_called == {}


# ══════════════════════════════════════════════════════════════════
# weekly 리샘플링 helper (이미 존재) 결정성 검증 — 안전망
# ══════════════════════════════════════════════════════════════════

class TestWeeklyResample:
    def test_to_weekly_ohlcv_produces_w_fri_index(self):
        from scanner.weinstein import to_weekly_ohlcv
        daily = _make_daily(n=60)
        weekly = to_weekly_ohlcv(daily)
        assert len(weekly) > 0
        # 결과 인덱스는 모두 Friday (weekday() == 4)
        for ts in weekly.index:
            assert pd.Timestamp(ts).weekday() == 4
        # 컬럼 보존
        for col in ("Open", "High", "Low", "Close", "Volume"):
            assert col in weekly.columns

    def test_to_weekly_ohlcv_short_input_returns_empty(self):
        from scanner.weinstein import to_weekly_ohlcv
        short = _make_daily(n=3)
        result = to_weekly_ohlcv(short)
        assert isinstance(result, pd.DataFrame)
        assert len(result) == 0
