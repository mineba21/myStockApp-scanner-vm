"""미국 유니버스 조립 — NASDAQ-100 목록과 섹터 승계.

네트워크를 타지 않는다. HTTP 응답은 전부 합성한다.
"""
import io
from types import SimpleNamespace

import pandas as pd
import pytest

from scanner import us_stocks


@pytest.fixture(autouse=True)
def _clear_universe_cache():
    """``get_all_us_tickers`` 의 모듈 레벨 캐시가 테스트 간에 새지 않게."""
    us_stocks._cache.clear()
    yield
    us_stocks._cache.clear()


def _ndx_payload(rows):
    return SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"data": {"data": {"rows": rows}}},
    )


class TestNasdaq100:
    """예전 HTML 스크레이퍼는 페이지가 JS 렌더링으로 바뀌자 조용히 빈 목록을
    반환했다(운영에서 total_scanned=0 스캔으로 드러남). JSON API 로 교체."""

    def test_parses_symbol_and_cleans_company_name(self, monkeypatch):
        monkeypatch.setattr(us_stocks.requests, "get", lambda *a, **k: _ndx_payload([
            {"symbol": "AAPL", "companyName": "Apple Inc. Common Stock"},
            {"symbol": "BRK.B", "companyName": "Berkshire Class A"},
        ]))
        rows = us_stocks.get_nasdaq100_tickers()
        assert [(r["ticker"], r["name"]) for r in rows] == [
            ("AAPL", "Apple Inc."),
            ("BRK-B", "Berkshire"),      # 점은 야후 표기로, 접미사는 제거
        ]
        assert all(r["market_type"] == "NASDAQ100" for r in rows)

    def test_blank_symbols_are_skipped(self, monkeypatch):
        monkeypatch.setattr(us_stocks.requests, "get", lambda *a, **k: _ndx_payload([
            {"symbol": "  ", "companyName": "Ghost"},
            {"symbol": "NVDA", "companyName": "NVIDIA Corporation Common Stock"},
        ]))
        assert [r["ticker"] for r in us_stocks.get_nasdaq100_tickers()] == ["NVDA"]

    def test_empty_rows_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr(us_stocks.requests, "get", lambda *a, **k: _ndx_payload([]))
        assert us_stocks.get_nasdaq100_tickers() == []

    def test_network_failure_is_contained(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("connection reset")
        monkeypatch.setattr(us_stocks.requests, "get", boom)
        assert us_stocks.get_nasdaq100_tickers() == []


class TestUniverseSectorInheritance:
    """NASDAQ-100 응답에는 쓸 만한 sector 가 없다(실측: 102종목 전부 빈 값).
    S&P500 을 먼저 넣어 겹치는 종목이 GICS 섹터를 물려받게 하는 것이 핵심."""

    def _wiki(self):
        df = pd.DataFrame(
            [["AAPL", "Apple Inc.", "Information Technology"],
             ["XOM", "Exxon Mobil", "Energy"]],
            columns=["Symbol", "Security", "GICS Sector"])
        return pd.read_html(io.StringIO(df.to_html(index=False)))

    def test_overlapping_ticker_keeps_sp500_sector(self, monkeypatch):
        monkeypatch.setattr(us_stocks, "_read_html_wiki", lambda url: self._wiki())
        monkeypatch.setattr(us_stocks.requests, "get", lambda *a, **k: _ndx_payload([
            {"symbol": "AAPL", "companyName": "Apple Inc. Common Stock"},
            {"symbol": "ASML", "companyName": "ASML Holding N.V."},
        ]))
        rows = {r["ticker"]: r for r in us_stocks.get_all_us_tickers("sp500+nasdaq100")}

        # AAPL 은 양쪽에 있지만 S&P500 이 먼저 들어가 섹터가 살아남는다
        assert rows["AAPL"]["sector"] == "Information Technology"
        assert rows["AAPL"]["market_type"] == "SP500"
        # NDX 전용 종목은 섹터 없음 — Gate 2 를 켜기 전에 알아야 할 공백
        assert rows["ASML"].get("sector") is None
        assert len(rows) == 3      # AAPL, XOM, ASML
