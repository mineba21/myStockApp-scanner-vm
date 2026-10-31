"""Display-only sector snapshots; never feeds Strict Gate 2."""
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from threading import RLock

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)
_cache = {}
_lock = RLock()
LOOKBACK = 20
MIN_MEMBERS = 3
MIN_COVERAGE = .70


def _today(market):
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo('Asia/Seoul' if market == 'KR' else 'America/New_York')).date()


def _cached(key, loader):
    # Serialize cold loads so concurrent scans cannot stampede providers.
    with _lock:
        now = datetime.now(timezone.utc)
        cached = _cache.get(key)
        if cached and now < cached[0]:
            return cached[1]
        try:
            value = loader()
        except Exception as exc:
            logger.warning('Sector classification unavailable %s: %s', key, exc)
            value = None
        _cache[key] = (now + timedelta(hours=24 if value else 1), value)
        return value


def classify_return(value):
    if not np.isfinite(value):
        return None
    if value >= 5:
        return 'STRONG_UP'
    if value >= 1:
        return 'UP'
    if value <= -5:
        return 'STRONG_DOWN'
    if value <= -1:
        return 'DOWN'
    return 'FLAT'


def _close(df, today):
    if df is None or 'Close' not in df:
        return pd.Series(dtype=float)
    s = pd.to_numeric(df['Close'], errors='coerce').copy()
    # Date labels are exchange-local sessions, not UTC timestamps.
    s.index = pd.DatetimeIndex(pd.to_datetime(s.index, errors='coerce')).tz_localize(None).normalize()
    s = s.loc[~s.index.isna()]
    s = s.sort_index()
    s = s[~s.index.duplicated(keep=False)]
    s = s.loc[s.index.date <= today]
    return s.where(np.isfinite(s) & (s > 0))


def price_trend(df, market='US', today=None):
    today = today or _today(market)
    empty = dict(trend=None, return_20d_pct=None, as_of=None, status='UNAVAILABLE')
    s = _close(df, today)
    if s.empty:
        return empty
    empty['as_of'] = s.index[-1].date().isoformat()
    if (today - s.index[-1].date()).days > 7:
        return dict(empty, status='STALE')
    window = s.iloc[-(LOOKBACK + 1):]
    if len(window) < LOOKBACK + 1 or window.isna().any():
        return dict(empty, status='INSUFFICIENT_DATA')
    value = (float(window.iloc[-1]) / float(window.iloc[0]) - 1) * 100
    return dict(empty, status='OK', trend=classify_return(value), return_20d_pct=round(value, 4))


def _snapshot(name=None, source=None, proxy=None):
    return dict(version=1, name=name, classification_source=source, proxy=proxy,
                performance_source=None, trend=None, return_20d_pct=None,
                as_of=None, status='UNMAPPED' if not name else 'UNAVAILABLE')


def _yahoo_sector(ticker):
    def load():
        import yfinance as yf
        return yf.Ticker(ticker).get_info().get('sector')
    return _cached(('yahoo', ticker), load)


def us_sector_snapshot(info):
    from scanner.market_analysis import US_SECTOR_ETFS, get_market_stages
    source = 'Wikipedia S&P500 GICS'
    gics = info.get('sector')
    if not gics:
        source = 'Yahoo Finance sector'
        raw = _yahoo_sector(info['ticker'])
        aliases = {'Technology': 'Information Technology', 'Healthcare': 'Health Care',
                   'Financial Services': 'Financials', 'Consumer Cyclical': 'Consumer Discretionary',
                   'Consumer Defensive': 'Consumer Staples', 'Basic Materials': 'Materials'}
        gics = aliases.get(raw, raw)
    entry = next((e for e in US_SECTOR_ETFS if e['gics'] == gics), None)
    if not entry:
        return _snapshot()
    result = _snapshot(entry['name'], source, entry['ticker'])
    result['performance_source'] = 'SPDR 섹터 ETF'
    try:
        row = next((r for r in get_market_stages().get('US_SECTORS', [])
                    if r['ticker'] == entry['ticker']), {})
        result.update(row.get('recent_trend') or {})
    except Exception as exc:
        logger.warning('Sector ETF unavailable: %s', exc)
    return result


def kr_sector_map():
    def load():
        import FinanceDataReader as fdr
        df = fdr.StockListing('KRX-DESC')
        if not {'Code', 'Sector'} <= set(df.columns):
            return None
        return {str(r['Code']).zfill(6): str(r['Sector']).strip()
                for _, r in df.iterrows() if pd.notna(r['Sector']) and str(r['Sector']).strip()}
    return _cached(('kr', 'classification'), load) or {}


def kr_sector_snapshots(tickers, prices, today=None):
    """Equal-weight 20-session returns of the selected scanner universe."""
    today = today or _today('KR')
    mapping = kr_sector_map()
    groups = defaultdict(list)
    for info in tickers:
        name = mapping.get(info['ticker'])
        if name:
            groups[name].append(info['ticker'])
    series = {ticker: _close(df, today) for ticker, df in prices.items()}
    # Shared dates prevent comparing a suspended stock's old window with today's.
    calendar = sorted({d for s in series.values() for d in s.index})[-(LOOKBACK + 1):]
    contexts = {}
    for name, members in groups.items():
        result = _snapshot(name, 'KRX-DESC Sector', '스캔 업종 평균')
        result.update(performance_source='선택 유니버스 동일가중', members=0, total_members=len(members))
        if calendar:
            result['as_of'] = calendar[-1].date().isoformat()
        if calendar and (today - calendar[-1].date()).days > 7:
            result['status'] = 'STALE'
        else:
            returns = []
            for ticker in members:
                s = series.get(ticker, pd.Series(dtype=float)).reindex(calendar)
                if len(s) == LOOKBACK + 1 and s.notna().all():
                    returns.append((float(s.iloc[-1]) / float(s.iloc[0]) - 1) * 100)
            result['members'] = len(returns)
            result['status'] = 'INSUFFICIENT_DATA'
            if len(returns) >= MIN_MEMBERS and len(returns) / len(members) >= MIN_COVERAGE:
                value = float(np.mean(returns))
                result.update(status='OK', trend=classify_return(value), return_20d_pct=round(value, 4))
        contexts[name] = result
    return {info['ticker']: dict(contexts[mapping[info['ticker']]])
            if info['ticker'] in mapping else _snapshot() for info in tickers}
