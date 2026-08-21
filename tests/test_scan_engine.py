"""
Phase 2 — scanner orchestration 통합/회귀 테스트.

monkeypatch로 외부 의존(DB, KR/US 페치, telegram, market_analysis)을 차단하고,
- _check_watchlist 가 weekly_df / benchmark_close 옵션을 채워 check_sell_signal
  을 호출하는지,
- kr_stocks.fetch_ohlcv / us_stocks.fetch_ohlcv 어댑터가 올바른 underlying
  fetcher 로 라우팅되는지,
- 외부 페치 실패가 graceful 하게 None 으로 전파되는지
를 결정적으로 검증한다.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
from datetime import date, timedelta
import pandas as pd
import pytest


def _make_daily(n=260, start_price=100.0, step=0.5):
    """Phase 2 단위 테스트용 합성 일봉 OHLCV."""
    dates  = [date(2022, 1, 1) + timedelta(days=i) for i in range(n)]
    prices = [start_price + i * step for i in range(n)]
    return pd.DataFrame({
        "Open":   [p * 0.998 for p in prices],
        "High":   [p * 1.005 for p in prices],
        "Low":    [p * 0.995 for p in prices],
        "Close":  prices,
        "Volume": [500_000.0] * n,
    }, index=pd.DatetimeIndex(dates))


# ══════════════════════════════════════════════════════════════════
# fetch_ohlcv 어댑터 라우팅
# ══════════════════════════════════════════════════════════════════

class TestFetchOhlcvKR:
    def test_kr_fetch_routes_to_get_kr_ohlcv(self, monkeypatch):
        from scanner import kr_stocks

        captured = {}

        def fake_get_kr_ohlcv(ticker, period_years=2):
            captured["ticker"] = ticker
            captured["period_years"] = period_years
            return _make_daily(n=120)

        monkeypatch.setattr(kr_stocks, "get_kr_ohlcv", fake_get_kr_ohlcv)

        result = kr_stocks.fetch_ohlcv("005930", lookback_days=730)

        assert captured["ticker"] == "005930"
        assert captured["period_years"] == 2
        assert result is not None and len(result) == 120

    def test_kr_fetch_raises_datafetcherror_on_external_failure(self, monkeypatch):
        from scanner import kr_stocks
        from scanner.errors import DataFetchError

        def boom(ticker, period_years=2):
            raise RuntimeError("FDR 다운")

        monkeypatch.setattr(kr_stocks, "get_kr_ohlcv", boom)

        # Phase 4: 외부 장애는 명시적으로 raise — 호출자가 None 과 구분 가능
        with pytest.raises(DataFetchError, match="KR fetch failed for 005930"):
            kr_stocks.fetch_ohlcv("005930", lookback_days=365)

    def test_kr_fetch_zero_lookback_returns_none(self):
        from scanner import kr_stocks
        assert kr_stocks.fetch_ohlcv("005930", lookback_days=0) is None
        assert kr_stocks.fetch_ohlcv("005930", lookback_days=-10) is None

    def test_kr_fetch_rounds_up_to_year(self, monkeypatch):
        from scanner import kr_stocks
        seen = {}

        def fake(ticker, period_years=2):
            seen["years"] = period_years
            return _make_daily(n=70)

        monkeypatch.setattr(kr_stocks, "get_kr_ohlcv", fake)
        kr_stocks.fetch_ohlcv("005930", lookback_days=400)  # 1.1년 → 2년
        assert seen["years"] == 2

        kr_stocks.fetch_ohlcv("005930", lookback_days=200)  # 0.5년 → 1년
        assert seen["years"] == 1


class TestFetchOhlcvUS:
    def test_us_fetch_routes_to_get_us_ohlcv(self, monkeypatch):
        from scanner import us_stocks

        captured = {}

        def fake_get_us_ohlcv(ticker, period="2y"):
            captured["ticker"] = ticker
            captured["period"] = period
            return _make_daily(n=120)

        monkeypatch.setattr(us_stocks, "get_us_ohlcv", fake_get_us_ohlcv)

        result = us_stocks.fetch_ohlcv("AAPL", lookback_days=730)

        assert captured["ticker"] == "AAPL"
        assert captured["period"] == "2y"
        assert result is not None and len(result) == 120

    def test_us_fetch_raises_datafetcherror_on_external_failure(self, monkeypatch):
        from scanner import us_stocks
        from scanner.errors import DataFetchError

        def boom(ticker, period="2y"):
            raise RuntimeError("yfinance 다운")

        monkeypatch.setattr(us_stocks, "get_us_ohlcv", boom)

        with pytest.raises(DataFetchError, match="US fetch failed for AAPL"):
            us_stocks.fetch_ohlcv("AAPL", lookback_days=365)

    def test_us_fetch_period_string_rounds_up(self, monkeypatch):
        from scanner import us_stocks
        seen = {}

        def fake(ticker, period="2y"):
            seen["period"] = period
            return _make_daily(n=70)

        monkeypatch.setattr(us_stocks, "get_us_ohlcv", fake)
        us_stocks.fetch_ohlcv("AAPL", lookback_days=365)
        assert seen["period"] == "1y"

        us_stocks.fetch_ohlcv("AAPL", lookback_days=1825)
        assert seen["period"] == "5y"


# ══════════════════════════════════════════════════════════════════
# _check_watchlist — weekly_df / benchmark_close 결선 확인
# ══════════════════════════════════════════════════════════════════

class _WatchItem:
    """WatchList 행 흉내 (DB 안 쓰고 attr 만)."""
    def __init__(self, ticker, name, market, buy_price=None, stop_loss=None):
        self.ticker = ticker
        self.name = name
        self.market = market
        self.buy_price = buy_price
        self.stop_loss = stop_loss
        self.is_active = True


class _FakeQuery:
    def __init__(self, items): self._items = items
    def filter(self, *a, **kw): return self
    def all(self): return self._items


class _FakeDB:
    def __init__(self, items): self._items = items
    def query(self, *a, **kw): return _FakeQuery(self._items)


class TestCheckWatchlistKwargs:
    def test_passes_weekly_and_benchmark_to_check_sell(self, monkeypatch):
        from scanner import scan_engine
        from scanner import kr_stocks, us_stocks
        import scanner.weinstein as weinstein

        kr_daily = _make_daily(n=260, start_price=200.0, step=0.5)
        us_daily = _make_daily(n=260, start_price=100.0, step=0.3)
        kr_bench = pd.Series(
            [50.0 + i * 0.1 for i in range(260)],
            index=kr_daily.index,
        )
        us_bench = pd.Series(
            [400.0 + i * 0.05 for i in range(260)],
            index=us_daily.index,
        )

        monkeypatch.setattr(kr_stocks, "get_kr_ohlcv", lambda t, *a, **kw: kr_daily)
        monkeypatch.setattr(us_stocks, "get_us_ohlcv", lambda t, *a, **kw: us_daily)

        captured = []

        def fake_check_sell_signal(df, ticker, name, market,
                                   buy_price=None, stop_loss=None,
                                   weekly_df=None, benchmark_close=None):
            captured.append({
                "ticker": ticker,
                "market": market,
                "weekly_df_is_df": isinstance(weekly_df, pd.DataFrame) and len(weekly_df) > 0,
                "benchmark_is_series": isinstance(benchmark_close, pd.Series),
            })
            return None  # 시그널 없음 → 결과 없음

        monkeypatch.setattr(weinstein, "check_sell_signal", fake_check_sell_signal)

        items = [
            _WatchItem("005930", "삼성전자", "KR"),
            _WatchItem("AAPL",   "Apple",   "US"),
        ]
        db = _FakeDB(items)
        sells = scan_engine._check_watchlist(db, kr_bench=kr_bench, us_bench=us_bench)

        assert sells == []
        assert len(captured) == 2
        kr_call = next(c for c in captured if c["market"] == "KR")
        us_call = next(c for c in captured if c["market"] == "US")
        assert kr_call["weekly_df_is_df"] is True
        assert kr_call["benchmark_is_series"] is True
        assert us_call["weekly_df_is_df"] is True
        assert us_call["benchmark_is_series"] is True

    def test_skips_when_daily_df_is_none(self, monkeypatch):
        from scanner import scan_engine
        from scanner import kr_stocks, us_stocks
        import scanner.weinstein as weinstein

        monkeypatch.setattr(kr_stocks, "get_kr_ohlcv", lambda t, *a, **kw: None)
        monkeypatch.setattr(us_stocks, "get_us_ohlcv", lambda t, *a, **kw: None)

        called = []
        monkeypatch.setattr(weinstein, "check_sell_signal",
                            lambda *a, **kw: called.append(kw) or None)

        db = _FakeDB([_WatchItem("X", "X", "KR")])
        sells = scan_engine._check_watchlist(db, kr_bench=None, us_bench=None)

        assert sells == []
        assert called == []  # 일봉 None → check_sell_signal 호출 자체 스킵

    def test_passes_none_weekly_when_resample_empty(self, monkeypatch):
        """일봉이 너무 짧아 to_weekly_ohlcv가 빈 DF를 돌려주는 경로."""
        from scanner import scan_engine
        from scanner import kr_stocks
        import scanner.weinstein as weinstein

        # 4 rows < 5 → to_weekly_ohlcv 가 빈 DataFrame 반환
        idx = pd.DatetimeIndex([date(2024, 1, 1) + timedelta(days=i) for i in range(4)])
        short = pd.DataFrame({
            "Open": [1.0]*4, "High": [1.0]*4, "Low": [1.0]*4,
            "Close": [1.0]*4, "Volume": [1.0]*4,
        }, index=idx)
        monkeypatch.setattr(kr_stocks, "get_kr_ohlcv", lambda t, *a, **kw: short)

        captured = {}
        def fake_check(df, ticker, name, market, buy_price=None, stop_loss=None,
                       weekly_df=None, benchmark_close=None):
            captured["weekly_df"] = weekly_df
            captured["bench"] = benchmark_close
            return None
        monkeypatch.setattr(weinstein, "check_sell_signal", fake_check)

        db = _FakeDB([_WatchItem("005930", "삼성전자", "KR")])
        scan_engine._check_watchlist(db, kr_bench=None, us_bench=None)

        assert captured["weekly_df"] is None  # 빈 DF는 None 으로 폴백
        assert captured["bench"] is None

    def test_external_exception_is_caught(self, monkeypatch):
        from scanner import scan_engine
        from scanner import kr_stocks

        def boom(ticker, *a, **kw):
            raise RuntimeError("network gone")

        monkeypatch.setattr(kr_stocks, "get_kr_ohlcv", boom)

        db = _FakeDB([_WatchItem("005930", "삼성전자", "KR")])
        # 예외를 raise 하면 안 된다 — 내부에서 try/except로 잡고 빈 리스트 반환.
        sells = scan_engine._check_watchlist(db, kr_bench=None, us_bench=None)
        assert sells == []


# ══════════════════════════════════════════════════════════════════
# 시장 필터 회귀 (BEAR + BREAKOUT 차단 정책 유지)
# ══════════════════════════════════════════════════════════════════

class TestMarketFilterRegression:
    def test_bear_blocks_breakout(self, monkeypatch):
        # 디폴트 config가 BLOCK_NEW_BUYS_IN_BEAR=True 라는 가정 검증.
        from scanner.scan_engine import _get_market_filter_decision
        allow, msg = _get_market_filter_decision("BEAR", "BREAKOUT")
        assert allow is False
        assert msg is not None and "BEAR" in msg

    def test_bull_allows_breakout(self):
        from scanner.scan_engine import _get_market_filter_decision
        allow, msg = _get_market_filter_decision("BULL", "BREAKOUT")
        assert allow is True

    def test_none_market_allows_anything(self):
        from scanner.scan_engine import _get_market_filter_decision
        allow, msg = _get_market_filter_decision(None, "BREAKOUT")
        assert allow is True


# ══════════════════════════════════════════════════════════════════
# _save() — Mansfield rs_value 영속화 (Phase 2)
# ══════════════════════════════════════════════════════════════════

class TestSavePersistsMansfieldRS:
    """`_save()` 가 legacy `rs` 가 아닌 Mansfield `rs_value` 를 DB 컬럼에 기록한다."""

    def _fresh_db(self):
        """SQLite in-memory + ScanResult 테이블만 사용하는 임시 세션."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from database.models import Base

        eng = create_engine("sqlite:///:memory:",
                            connect_args={"check_same_thread": False})
        Base.metadata.create_all(eng)
        Session = sessionmaker(bind=eng)
        return Session()

    def _signal(self, **overrides):
        sig = {
            "ticker":        "TEST",
            "name":          "테스트",
            "market":        "US",
            "signal_type":   "BREAKOUT",
            "stage":         "STAGE2",
            "price":         105.0,
            "ma150":         100.0,
            "volume":        4_000_000,
            "volume_avg":    1_000_000,
            "volume_ratio":  4.0,
            "signal_date":   "2024-06-03",
            "pivot_price":   104.0,
            "support_level": 99.0,
            "market_condition": "BULL",
            "signal_quality":   "STRONG",
            "base_quality":     "STRONG",
            # legacy ratio RS 와 Mansfield RS 를 동시에 채워 영속화 경로를 검증
            "rs":          1.2,   # 절대 DB 에 들어가서는 안 됨
            "rs_value":    6.0,   # 이 값이 ScanResult.rs_value 로 기록되어야 함
            "rs_trend":    "RISING",
        }
        sig.update(overrides)
        return sig

    def test_save_writes_mansfield_rs_value_on_insert(self):
        from scanner.scan_engine import _save
        from database.models import ScanResult

        db = self._fresh_db()
        try:
            _save(db, self._signal())
            row = db.query(ScanResult).filter(
                ScanResult.ticker == "TEST",
                ScanResult.signal_date == "2024-06-03",
            ).one()
            assert row.rs_value == 6.0
        finally:
            db.close()

    def test_save_updates_mansfield_rs_value_on_existing(self):
        from scanner.scan_engine import _save
        from database.models import ScanResult

        db = self._fresh_db()
        try:
            _save(db, self._signal())
            # 같은 (ticker, signal_date, signal_type) 로 두 번째 저장 → update 분기
            _save(db, self._signal(rs=9.9, rs_value=12.5, price=108.0))
            rows = db.query(ScanResult).filter(
                ScanResult.ticker == "TEST",
                ScanResult.signal_date == "2024-06-03",
            ).all()
            assert len(rows) == 1
            assert rows[0].rs_value == 12.5
            assert rows[0].price    == 108.0
        finally:
            db.close()

    def test_save_writes_none_when_rs_value_missing(self):
        from scanner.scan_engine import _save
        from database.models import ScanResult

        db = self._fresh_db()
        try:
            sig = self._signal()
            sig.pop("rs_value")  # Mansfield 미산출 (벤치마크 없음 등)
            _save(db, sig)
            row = db.query(ScanResult).filter(ScanResult.ticker == "TEST").one()
            assert row.rs_value is None  # legacy rs 1.2 가 잘못 기록되지 않아야 함
        finally:
            db.close()


# ══════════════════════════════════════════════════════════════════
# Strict Weinstein filter — Phase 1: DB 영속화 스캐폴드
# ══════════════════════════════════════════════════════════════════

class TestSavePersistsStrictFields:
    """`_save()` 가 Phase 1 신설 7개 컬럼 (stop_loss, sector_name, sector_stage,
    rs_trend, rs_zero_crossed, strict_filter_passed, filter_reasons) 을
    INSERT/UPDATE 양쪽에서 정상 기록하는지 검증.

    Phase 1 단계에서는 signal dict 가 해당 키를 비워둔 채(None/[]) 들어와도
    DB 컬럼은 NULL 로 안전하게 들어가야 한다 (no-op 보장).
    """

    def _fresh_db(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from database.models import Base

        eng = create_engine("sqlite:///:memory:",
                            connect_args={"check_same_thread": False})
        Base.metadata.create_all(eng)
        Session = sessionmaker(bind=eng)
        return Session()

    def _signal(self, **overrides):
        sig = {
            "ticker":        "STR",
            "name":          "Strict",
            "market":        "US",
            "signal_type":   "BREAKOUT",
            "stage":         "STAGE2",
            "price":         110.0,
            "ma150":         100.0,
            "volume":        5_000_000,
            "volume_avg":    1_000_000,
            "volume_ratio":  5.0,
            "signal_date":   "2024-07-01",
            "pivot_price":   108.0,
            "support_level": 100.0,
            "market_condition": "BULL",
            "signal_quality":   "STRONG",
            "rs_value":      4.5,
        }
        sig.update(overrides)
        return sig

    def test_save_writes_null_when_strict_fields_absent(self):
        """signal dict 에 strict 키가 없거나 None 이면 DB 7개 컬럼 모두 NULL."""
        from scanner.scan_engine import _save
        from database.models import ScanResult

        db = self._fresh_db()
        try:
            _save(db, self._signal())  # strict 키 미포함
            row = db.query(ScanResult).filter(ScanResult.ticker == "STR").one()
            assert row.stop_loss            is None
            assert row.sector_name          is None
            assert row.sector_stage         is None
            assert row.rs_trend             is None
            assert row.rs_zero_crossed      is None
            assert row.strict_filter_passed is None
            assert row.filter_reasons       is None
        finally:
            db.close()

    def test_save_persists_strict_fields_on_insert(self):
        """signal 이 strict 키를 채워주면 INSERT 경로가 그대로 영속화."""
        from scanner.scan_engine import _save
        from database.models import ScanResult

        db = self._fresh_db()
        try:
            sig = self._signal(
                stop_loss            = 102.5,
                sector_name          = "Technology",
                sector_stage         = "STAGE2",
                rs_trend             = "RISING",
                rs_zero_crossed      = True,
                strict_filter_passed = True,
                filter_reasons       = [],   # pass → JSON 직렬화 결과는 None
            )
            _save(db, sig)
            row = db.query(ScanResult).filter(ScanResult.ticker == "STR").one()
            assert row.stop_loss            == 102.5
            assert row.sector_name          == "Technology"
            assert row.sector_stage         == "STAGE2"
            assert row.rs_trend             == "RISING"
            assert row.rs_zero_crossed      is True
            assert row.strict_filter_passed is True
            # 빈 리스트는 NULL 로 정규화 (의미 없는 "[]" 저장 방지)
            assert row.filter_reasons       is None
        finally:
            db.close()

    def test_save_serializes_filter_reasons_and_updates_existing(self):
        """거부 사유 리스트는 JSON 으로 직렬화되며, 동일 키 재호출 시 UPDATE 분기도 동일하게 채운다."""
        from scanner.scan_engine import _save
        from database.models import ScanResult

        db = self._fresh_db()
        try:
            # 1차: 거부 시그널 (strict_filter_passed=False, 사유 2개)
            _save(db, self._signal(
                strict_filter_passed = False,
                filter_reasons       = ["rs_below_zero", "below_weekly_30ma"],
                rs_trend             = "FALLING",
            ))
            row = db.query(ScanResult).filter(ScanResult.ticker == "STR").one()
            assert row.strict_filter_passed is False
            assert row.rs_trend             == "FALLING"
            # JSON 문자열이 정상 round-trip
            decoded = json.loads(row.filter_reasons)
            assert decoded == ["rs_below_zero", "below_weekly_30ma"]

            # 2차: 같은 (ticker, signal_date, signal_type) 재호출 → UPDATE 분기 검증
            _save(db, self._signal(
                strict_filter_passed = True,
                filter_reasons       = [],
                rs_trend             = "RISING",
                stop_loss            = 105.0,
            ))
            rows = db.query(ScanResult).filter(ScanResult.ticker == "STR").all()
            assert len(rows) == 1, "UPSERT 의도 — 한 행으로 합쳐져야 함"
            assert rows[0].strict_filter_passed is True
            assert rows[0].rs_trend             == "RISING"
            assert rows[0].stop_loss            == 105.0
            assert rows[0].filter_reasons       is None
        finally:
            db.close()

    def test_migrate_adds_missing_strict_columns_to_legacy_db(self):
        """기존 DB(strict 컬럼 누락)에 _migrate() 적용 시 7개 컬럼이 모두 추가되어야 한다."""
        from sqlalchemy import create_engine, text, inspect
        # 임시 파일 SQLite (in-memory 는 다중 연결이 별도 DB)
        import tempfile, os as _os
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            url = f"sqlite:///{tmp.name}"
            eng = create_engine(url, connect_args={"check_same_thread": False})

            # 1) strict 컬럼이 없는 legacy 형태로 테이블 생성
            #    (SQLAlchemy 1.4 legacy 모드 — DDL 은 자동 커밋)
            with eng.connect() as conn:
                conn.execute(text("""
                    CREATE TABLE scan_results (
                        id INTEGER PRIMARY KEY,
                        scan_time DATETIME, market VARCHAR(10),
                        ticker VARCHAR(20), name VARCHAR(100),
                        signal_type VARCHAR(20), stage VARCHAR(10),
                        price REAL, ma150 REAL,
                        volume REAL, volume_avg REAL, volume_ratio REAL,
                        signal_date VARCHAR(10), notified BOOLEAN
                    )
                """))

            # 2) 동일 DB URL 로 _migrate() 실행 — engine 을 monkeypatch
            from database import models as _models
            orig_engine = _models.engine
            _models.engine = eng
            try:
                _models._migrate()
            finally:
                _models.engine = orig_engine

            # 3) ALTER 적용 확인
            cols = {c["name"] for c in inspect(eng).get_columns("scan_results")}
            need = {
                "stop_loss", "sector_name", "sector_stage",
                "rs_trend", "rs_zero_crossed",
                "strict_filter_passed", "filter_reasons",
            }
            missing = need - cols
            assert not missing, f"_migrate() 가 추가 못한 컬럼: {missing}"
        finally:
            _os.unlink(tmp.name)


# ══════════════════════════════════════════════════════════════════
# Strict Weinstein filter — Phase 4: scan_engine flow integration
# ══════════════════════════════════════════════════════════════════

def _force_scan_engine_flag(monkeypatch, name: str, value):
    """scan_engine 가 import 시 캡처한 단일 STRICT_* 플래그 강제."""
    import config as _config
    monkeypatch.setattr(_config, name, value, raising=False)
    # _process_signal / _evaluate_strict_filter / _notify 는 매 호출마다
    # config 에서 다시 import 하므로 모듈 재로드는 불필요.


def _force_strict_module_flag(monkeypatch, name: str, value):
    """strict_filter 모듈이 import 시 캡처한 STRICT_* 플래그 강제."""
    from scanner import strict_filter
    monkeypatch.setattr(strict_filter, name, value, raising=False)


class TestStrictFilterFlow:
    """Phase 4 — analyze_stock → apply_strict_filter → _save / _notify 통합 흐름.

    - STRICT_WEINSTEIN_MODE=True 가 기본값. strict-pass 만 _save / notify.
    - STRICT_PERSIST_REJECTED=True 일 때 거부 시그널도 _save 되지만 notify 미포함.
    - STRICT_WEINSTEIN_MODE=False 면 legacy 호환 — 모든 시그널 _save / notify.
    """

    def _fresh_db(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from database.models import Base

        eng = create_engine("sqlite:///:memory:",
                            connect_args={"check_same_thread": False})
        Base.metadata.create_all(eng)
        Session = sessionmaker(bind=eng)
        return Session()

    def _passing_signal(self, **overrides):
        """8 게이트 모두 통과하는 baseline BREAKOUT analyze_stock 결과 dict."""
        sig = {
            # display/persist 공개 필드
            "ticker":           "STR",
            "name":             "Strict",
            "market":           "US",
            "signal_type":      "BREAKOUT",
            "stage":            "STAGE2",
            "weekly_stage":     "STAGE2",
            "price":            110.0,
            "ma150":            100.0,
            "ma50":              98.0,
            "price_vs_ma_pct":  10.0,
            "ma_slope":          0.5,
            "volume":           5_000_000,
            "volume_avg":       1_000_000,
            "volume_ratio":      5.0,
            "weekly_volume_ratio": 2.5,
            "sma30w":            95.0,
            "slope30w":           0.5,
            "signal_date":     "2024-07-01",
            "rs":                1.4,
            "rs_value":          4.5,
            "rs_trend":         "RISING",
            "rs_zero_crossed":  True,
            "pivot_price":     105.0,
            "support_level":   100.0,
            "base_low":         95.0,
            "base_weeks":        8.0,
            "base_quality":   "STRONG",
            "base_quality_v4":"TIGHT",
            "v4_gate":          None,
            "market_condition": "BULL",
            "signal_quality":  "STRONG",
            "rs_passed":        True,
            "warning_flags":     [],
            "stop_loss":         94.0,
            "strict_filter_passed": None,    # _evaluate_strict_filter 가 채움
            "filter_reasons":      [],
            # strict_* signal-date 스냅샷
            "strict_price":             110.0,
            "strict_ma150":             100.0,
            "strict_ma50":               98.0,
            "strict_weekly_stage":   "STAGE2",
            "strict_sma30w":             95.0,
            "strict_slope30w":            0.5,
            "strict_weekly_volume_ratio": 2.5,
        }
        sig.update(overrides)
        return sig

    def _force_strict_baseline(self, monkeypatch, *, mode=True, persist=False):
        """Phase 4 통합 테스트 — 14 STRICT_* 와 strict_filter 모듈 플래그 동시 강제."""
        # config 모듈 (scan_engine 이 매 호출마다 import)
        _force_scan_engine_flag(monkeypatch, "STRICT_WEINSTEIN_MODE",      mode)
        _force_scan_engine_flag(monkeypatch, "STRICT_PERSIST_REJECTED",    persist)
        _force_scan_engine_flag(monkeypatch, "STRICT_NOTIFY_INCLUDE_REASONS", False)
        # strict_filter 모듈 (import 시 캡처 후 매 호출마다 read)
        for name, value in (
            ("STRICT_WEINSTEIN_MODE",                       mode),
            ("STRICT_REQUIRE_MARKET_CONFIRMATION",          True),
            ("STRICT_BLOCK_CAUTION_BREAKOUTS",              True),
            ("STRICT_REQUIRE_SECTOR_STAGE2",                False),
            ("STRICT_REQUIRE_PRICE_ABOVE_WEEKLY_30MA",      True),
            ("STRICT_REQUIRE_PRICE_ABOVE_DAILY_150MA",      True),
            ("STRICT_REQUIRE_BREAKOUT_VOLUME",              True),
            ("STRICT_REQUIRE_RS_POSITIVE",                  True),
            ("STRICT_REQUIRE_RS_RISING",                    True),
            ("STRICT_REQUIRE_RS_ZERO_CROSS_FOR_BREAKOUT",   True),
            ("STRICT_REQUIRE_STOP_LOSS",                    True),
        ):
            _force_strict_module_flag(monkeypatch, name, value)

    def test_strict_pass_saves_and_notifies(self, monkeypatch):
        """STRICT_WEINSTEIN_MODE=True + 모든 게이트 통과 → _save 호출, notify 리스트 포함."""
        from scanner.scan_engine import _process_signal
        from database.models import ScanResult

        self._force_strict_baseline(monkeypatch, mode=True, persist=False)

        db  = self._fresh_db()
        sig = self._passing_signal()
        try:
            notified = _process_signal(db, sig, "US",
                                       market_condition="BULL",
                                       benchmark_close=object())
            # notify 대상으로 인정
            assert notified is True
            # DB 영속화 확인 + strict_filter_passed=True
            row = db.query(ScanResult).filter(ScanResult.ticker == "STR").one()
            assert row.strict_filter_passed is True
            assert row.filter_reasons       is None       # [] → NULL 정규화
            assert sig["strict_filter_passed"] is True
            assert sig["filter_reasons"]      == []
        finally:
            db.close()

    def test_strict_reject_drops_signal_in_strict_mode(self, monkeypatch):
        """STRICT_WEINSTEIN_MODE=True + RS 음수 → _save 미호출, notify 미포함."""
        from scanner.scan_engine import _process_signal
        from database.models import ScanResult

        self._force_strict_baseline(monkeypatch, mode=True, persist=False)

        db  = self._fresh_db()
        sig = self._passing_signal(rs_value=-2.0)   # Gate 6 fail
        try:
            notified = _process_signal(db, sig, "US",
                                       market_condition="BULL",
                                       benchmark_close=object())
            assert notified is False
            # DB 미저장 (STRICT_PERSIST_REJECTED=False)
            assert db.query(ScanResult).count() == 0
            # 거부 메타는 dict 에 남아 있어야 — 디버그 경로
            assert sig["strict_filter_passed"] is False
            assert "rs_below_zero" in sig["filter_reasons"]
        finally:
            db.close()

    def test_persist_rejected_saves_but_does_not_notify(self, monkeypatch):
        """STRICT_PERSIST_REJECTED=True → 거부 시그널도 DB 저장, 그러나 notify 미포함."""
        from scanner.scan_engine import _process_signal
        from database.models import ScanResult

        self._force_strict_baseline(monkeypatch, mode=True, persist=True)

        db  = self._fresh_db()
        sig = self._passing_signal(rs_value=-2.0, rs_trend="FALLING")
        try:
            notified = _process_signal(db, sig, "US",
                                       market_condition="BULL",
                                       benchmark_close=object())
            # notify 미포함 (debug-only persistence)
            assert notified is False
            # DB 에는 거부 시그널이 영속화 — strict_filter_passed=False, reasons JSON
            row = db.query(ScanResult).filter(ScanResult.ticker == "STR").one()
            assert row.strict_filter_passed is False
            decoded = json.loads(row.filter_reasons)
            assert "rs_below_zero" in decoded
            assert "rs_falling"    in decoded
        finally:
            db.close()

    def test_legacy_mode_off_saves_and_notifies_all(self, monkeypatch):
        """STRICT_WEINSTEIN_MODE=False → 모든 시그널 _save / notify (legacy 호환)."""
        from scanner.scan_engine import _process_signal
        from database.models import ScanResult

        self._force_strict_baseline(monkeypatch, mode=False, persist=False)

        db  = self._fresh_db()
        # 거부될 만한 시그널이라도 (rs 음수, stop None) legacy 모드에선 통과
        sig = self._passing_signal(rs_value=-2.0, rs_trend="FALLING",
                                   stop_loss=None)
        try:
            notified = _process_signal(db, sig, "US",
                                       market_condition="BULL",
                                       benchmark_close=object())
            assert notified is True
            # _save 호출됨
            row = db.query(ScanResult).filter(ScanResult.ticker == "STR").one()
            # apply_strict_filter 가 (True, []) 우회 반환 → strict_filter_passed=True
            assert row.strict_filter_passed is True
            assert row.filter_reasons       is None
        finally:
            db.close()

    def test_filter_reasons_serialized_to_json_on_reject_persist(self, monkeypatch):
        """거부 시그널의 filter_reasons 가 JSON 으로 라운드트립되는지 확인.

        plan 의 reason enum 안정성 보증 — DB 에 저장된 문자열을 다시 파싱해
        list[str] 로 복원되어야 BI/대시보드/regression 추적이 가능.
        """
        from scanner.scan_engine import _process_signal
        from database.models import ScanResult

        self._force_strict_baseline(monkeypatch, mode=True, persist=True)

        db  = self._fresh_db()
        # 여러 게이트 동시 실패 — RS 음수 + 거래량 부족 + stop 없음
        sig = self._passing_signal(rs_value=-3.0, volume_ratio=0.5,
                                   stop_loss=None)
        try:
            _process_signal(db, sig, "US",
                            market_condition="BULL",
                            benchmark_close=object())
            row = db.query(ScanResult).filter(ScanResult.ticker == "STR").one()
            assert row.filter_reasons is not None
            decoded = json.loads(row.filter_reasons)
            assert isinstance(decoded, list)
            # 정확한 enum 문자열로 직렬화 — strict_filter 의 reason 상수와 일치
            assert "rs_below_zero"         in decoded
            assert "breakout_daily_volume" in decoded
            assert "stop_loss_missing"     in decoded
        finally:
            db.close()

    def test_bear_market_blocks_in_strict_mode(self, monkeypatch):
        """legacy BEAR fast-path + strict Gate 1 둘 다 BEAR 시그널을 차단.

        BEAR 차단은 두 경로 모두에서 사라지면 안 되는 invariant.
        STRICT_PERSIST_REJECTED=False 인 일반 운영에서는 BEAR fast-path 가
        먼저 발동하므로 strict 평가까지 가지 않고 즉시 drop.
        """
        from scanner.scan_engine import _process_signal
        from database.models import ScanResult

        self._force_strict_baseline(monkeypatch, mode=True, persist=False)

        db  = self._fresh_db()
        sig = self._passing_signal()
        try:
            notified = _process_signal(db, sig, "US",
                                       market_condition="BEAR",
                                       benchmark_close=object())
            assert notified is False
            assert db.query(ScanResult).count() == 0
        finally:
            db.close()

    def test_notify_filter_reasons_opt_in_renders_pass_badge(self, monkeypatch):
        """STRICT_NOTIFY_INCLUDE_REASONS=True 일 때 strict-pass 배지가 알림에 포함."""
        from scanner.scan_engine import _notify

        _force_scan_engine_flag(monkeypatch, "STRICT_NOTIFY_INCLUDE_REASONS", True)
        _force_scan_engine_flag(monkeypatch, "STRICT_WEINSTEIN_MODE",         True)

        captured = {}
        def fake_send(text):
            captured["text"] = text

        sig = self._passing_signal()
        sig["strict_filter_passed"] = True
        sig["filter_reasons"]       = []
        sig["_grade"]               = "A"

        _notify([sig], [], fake_send)
        assert "🛡 strict-pass" in captured.get("text", "")
