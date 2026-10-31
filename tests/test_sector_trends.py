"""Display-only trends: deterministic prices/providers and isolated SQLite."""
from datetime import date, timedelta, datetime, timezone
import json
import numpy as np
import pandas as pd
import pytest

from scanner import sector_trends as st

TODAY = date(2026, 10, 30)


def prices(change=0, periods=21, end=TODAY):
    return pd.DataFrame({'Close': np.linspace(100, 100 + change, periods)},
                        index=pd.bdate_range(end=end, periods=periods))


@pytest.mark.parametrize('value,label', [(5,'STRONG_UP'), (4.999,'UP'), (1,'UP'),
    (.999,'FLAT'), (0,'FLAT'), (-.999,'FLAT'), (-1,'DOWN'), (-4.999,'DOWN'),
    (-5,'STRONG_DOWN'), (float('nan'),None), (float('inf'),None)])
def test_thresholds(value, label):
    assert st.classify_return(value) == label


def test_exact_twenty_sessions_and_sorted_dates():
    frame = prices(6)
    result = st.price_trend(frame.iloc[::-1], today=TODAY)
    assert result == dict(status='OK', trend='STRONG_UP', return_20d_pct=6,
                          as_of=TODAY.isoformat())


@pytest.mark.parametrize('bad', [0, -1, np.nan, np.inf])
def test_bad_price_is_not_filled(bad):
    frame = prices(5)
    frame.iloc[8, 0] = bad
    assert st.price_trend(frame, today=TODAY)['status'] == 'INSUFFICIENT_DATA'


def test_history_stale_missing_duplicate_and_future():
    assert st.price_trend(prices(periods=20), today=TODAY)['trend'] is None
    assert st.price_trend(None, today=TODAY)['status'] == 'UNAVAILABLE'
    assert st.price_trend(prices(end=TODAY-timedelta(days=10)), today=TODAY)['status'] == 'STALE'
    frame = prices(5)
    assert st.price_trend(pd.concat([frame, frame.iloc[-1:]]), today=TODAY)['trend'] is None
    future = pd.DataFrame({'Close':[999]}, index=[pd.Timestamp(TODAY + timedelta(days=3))])
    assert st.price_trend(pd.concat([frame, future]), today=TODAY)['return_20d_pct'] == 5


def test_us_gics_reuses_etf_snapshot_without_yahoo(monkeypatch):
    from scanner import market_analysis as ma
    monkeypatch.setattr(st, '_yahoo_sector', lambda _: pytest.fail('unnecessary lookup'))
    monkeypatch.setattr(ma, 'get_market_stages', lambda: {'US_SECTORS': [
        {'ticker':'XLK', 'recent_trend':st.price_trend(prices(2), today=TODAY)}]})
    result = st.us_sector_snapshot({'ticker':'AAPL', 'sector':'Information Technology'})
    assert (result['name'], result['proxy'], result['trend']) == ('기술','XLK','UP')
    assert result['classification_source'] == 'Wikipedia S&P500 GICS'


def test_us_fallback_and_unmapped_no_guesses(monkeypatch):
    from scanner import market_analysis as ma
    monkeypatch.setattr(st, '_yahoo_sector', lambda _: 'Healthcare')
    monkeypatch.setattr(ma, 'get_market_stages', lambda: {'US_SECTORS': []})
    result = st.us_sector_snapshot({'ticker':'TEST'})
    assert result['name'] == '헬스케어'
    assert result['classification_source'] == 'Yahoo Finance sector'
    assert result['trend'] is None
    monkeypatch.setattr(st, '_yahoo_sector', lambda _: 'unknown')
    assert st.us_sector_snapshot({'ticker':'TEST'})['status'] == 'UNMAPPED'


def test_classification_loaders_and_cache_ttls(monkeypatch):
    import FinanceDataReader as fdr
    import yfinance as yf
    monkeypatch.setattr(st, '_cache', {})
    calls = []
    def listing(key):
        calls.append(key)
        return pd.DataFrame({'Code':['005930'], 'Sector':['전자부품 제조업'], 'Industry':['주요제품']})
    monkeypatch.setattr(fdr, 'StockListing', listing)
    assert st.kr_sector_map() == {'005930':'전자부품 제조업'}
    st.kr_sector_map()
    assert calls == ['KRX-DESC']
    now = datetime.now(timezone.utc)
    assert st._cache[('kr','classification')][0] > now + timedelta(hours=23)
    class Ticker:
        def __init__(self, ticker): pass
        def get_info(self): raise RuntimeError('offline')
    monkeypatch.setattr(yf, 'Ticker', Ticker)
    assert st._yahoo_sector('T') is None
    assert now < st._cache[('yahoo','T')][0] < now + timedelta(hours=2)
    monkeypatch.setattr(st, '_cache', {})
    monkeypatch.setattr(fdr, 'StockListing', lambda _: pd.DataFrame({'Code':['123456'], 'Industry':['product']}))
    assert st.kr_sector_map() == {}


def _kr(monkeypatch, n=4):
    tickers = [{'ticker':str(i)} for i in range(n)]
    monkeypatch.setattr(st, 'kr_sector_map', lambda: {str(i):'반도체 제조업' for i in range(n)})
    return tickers


def test_kr_equal_weight_and_coverage(monkeypatch):
    tickers = _kr(monkeypatch)
    frames = {str(i):prices(change) for i, change in enumerate([2,4,6])}
    result = st.kr_sector_snapshots(tickers, frames, today=TODAY)['0']
    assert result['return_20d_pct'] == 4
    assert result['trend'] == 'UP'
    assert (result['members'],result['total_members']) == (3,4)
    assert result['proxy'] == '스캔 업종 평균'
    # At 3/5 coverage the same prices no longer represent the cohort.
    tickers = _kr(monkeypatch, 5)
    assert st.kr_sector_snapshots(tickers, frames, today=TODAY)['0']['status'] == 'INSUFFICIENT_DATA'


def test_kr_minimum_sample_and_aligned_dates(monkeypatch):
    tickers = _kr(monkeypatch, 3)
    frames = {str(i):prices(6) for i in range(3)}
    frames['2'] = prices(6, end=TODAY-timedelta(days=3))
    result = st.kr_sector_snapshots(tickers, frames, today=TODAY)['0']
    assert result['members'] == 2
    assert result['trend'] is None
    assert st.kr_sector_snapshots(tickers, {str(i):prices(6, end=TODAY-timedelta(days=10))
        for i in range(3)}, today=TODAY)['0']['status'] == 'STALE'
    monkeypatch.setattr(st, 'kr_sector_map', lambda: {})
    assert st.kr_sector_snapshots(tickers, frames, today=TODAY)['0']['status'] == 'UNMAPPED'


def test_new_snapshot_never_changes_gate2(monkeypatch):
    from scanner import scan_engine, market_analysis, strict_filter
    monkeypatch.setattr(market_analysis, 'get_sector_stage', lambda *a: (None, None))
    monkeypatch.setattr(strict_filter, 'apply_strict_filter', lambda s,c: (c['sector_stage'] is None, []))
    signal = {'market':'US','sector_context':dict(name='기술',trend='STRONG_DOWN')}
    assert scan_engine._evaluate_strict_filter(signal, 'BULL', None) == (True, [])
    assert signal['sector_stage'] is None


def test_kr_scan_attaches_context_without_extra_price_fetch(monkeypatch):
    from scanner import scan_engine, kr_stocks, weinstein
    tickers = [{'ticker':str(i), 'name':'Test'} for i in range(3)]
    seen = []
    monkeypatch.setattr(kr_stocks, 'get_all_kr_tickers', lambda **k: tickers)
    def fetch(ticker):
        seen.append(ticker)
        return prices(6)
    monkeypatch.setattr(kr_stocks, 'get_kr_ohlcv', fetch)
    monkeypatch.setattr(st, '_today', lambda market: TODAY)
    monkeypatch.setattr(st, 'kr_sector_map', lambda: {str(i):'반도체' for i in range(3)})
    monkeypatch.setattr(weinstein, 'analyze_stock', lambda df,ticker,name,market,*a,**k:
                        dict(ticker=ticker,market=market,signal_type='BREAKOUT'))
    monkeypatch.setattr(scan_engine, '_process_signal', lambda db,res,*a: True)
    signals, count = scan_engine._scan_kr(None)
    assert count == 3 and seen == ['0','1','2']
    assert all(s['sector_context']['trend']=='STRONG_UP' for s in signals)


def test_us_scan_attaches_display_without_changing_gics(monkeypatch):
    from scanner import scan_engine, us_stocks, weinstein
    info = dict(ticker='T',name='Test')
    monkeypatch.setattr(us_stocks, 'get_all_us_tickers', lambda _: [info])
    monkeypatch.setattr(us_stocks, 'get_us_batch', lambda *a,**k: [(info,prices(2))])
    monkeypatch.setattr(weinstein, 'analyze_stock', lambda *a,**k: dict(ticker='T',market='US'))
    monkeypatch.setattr(st, 'us_sector_snapshot', lambda _: dict(name='기술',trend='UP'))
    monkeypatch.setattr(scan_engine, '_process_signal', lambda db,res,*a: True)
    signals, count = scan_engine._scan_us(None,'nasdaq100')
    assert count == 1 and signals[0]['sector_context']['trend']=='UP'
    assert signals[0]['_gics_sector'] is None


def test_us_universe_cache_refreshes_classification_daily(monkeypatch):
    from scanner import us_stocks
    monkeypatch.setattr(us_stocks, '_cache', {})
    monkeypatch.setattr(us_stocks, '_cache_expiry', {})
    now = [0]
    monkeypatch.setattr(us_stocks.time, 'monotonic', lambda: now[0])
    calls = []
    def load():
        calls.append(True)
        return [dict(ticker='T',sector='Energy' if len(calls)==1 else 'Utilities')]
    monkeypatch.setattr(us_stocks, 'get_sp500_tickers', load)
    assert us_stocks.get_all_us_tickers('sp500')[0]['sector']=='Energy'
    now[0] = 86399
    assert us_stocks.get_all_us_tickers('sp500')[0]['sector']=='Energy'
    now[0] = 86401
    assert us_stocks.get_all_us_tickers('sp500')[0]['sector']=='Utilities'
    assert len(calls) == 2
