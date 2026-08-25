"""미국 주식 데이터 - yfinance / FinanceDataReader"""
import io
import pandas as pd
import yfinance as yf
import requests
import logging
import time
from typing import Optional
from datetime import timedelta

logger = logging.getLogger(__name__)
_cache: dict = {}   # universe_key → list

# SP500/NASDAQ100 중복 등록되는 대표 심볼 제외 (BRK-B=BRK-A 등)
EXCLUDE_US: set = {"GOOGL"}   # GOOG 와 중복; 필요 시 추가

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def _read_html_wiki(url: str) -> list:
    """Wikipedia 403 우회: requests로 HTML 받아서 pd.read_html에 전달"""
    resp = requests.get(url, headers=_HEADERS, timeout=20)
    resp.raise_for_status()
    return pd.read_html(io.StringIO(resp.text))


def get_sp500_tickers() -> list:
    try:
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        tables = _read_html_wiki(url)
        df = tables[0]
        return [{"ticker": str(r["Symbol"]).replace(".", "-"),
                 "name": str(r["Security"]), "market_type": "SP500"}
                for _, r in df.iterrows()]
    except Exception as e:
        logger.error(f"S&P500 목록 실패: {e}"); return []


def get_nasdaq100_tickers() -> list:
    """NASDAQ-100 구성 종목 — Nasdaq 공식 페이지"""
    try:
        url = "https://www.nasdaq.com/solutions/global-indexes/nasdaq-100/companies"

        resp = requests.get(url, headers=_HEADERS, timeout=20)
        resp.raise_for_status()

        tables = pd.read_html(io.StringIO(resp.text))

        if not tables:
            return []

        df = tables[0].copy()

        # Nasdaq 페이지는 첫 번째 행이 실제 header로 들어오는 경우가 있음
        if len(df) > 0:
            first_row = [str(v).strip() for v in df.iloc[0].tolist()]

            if "Symbol" in first_row:
                df.columns = first_row
                df = df.iloc[1:].reset_index(drop=True)

        # 혹시 정상 header로 파싱된 경우도 대응
        symbol_col = next(
            (c for c in df.columns if str(c).strip().lower() == "symbol"),
            None
        )
        name_col = next(
            (
                c for c in df.columns
                if "company" in str(c).strip().lower()
                or "name" in str(c).strip().lower()
            ),
            None
        )

        if symbol_col is None:
            logger.error("NASDAQ100: Symbol 컬럼을 찾지 못함")
            return []

        results = []

        for _, row in df.iterrows():
            ticker = str(row[symbol_col]).strip()

            if not ticker or ticker.lower() == "nan":
                continue

            ticker = ticker.replace(".", "-")

            name = (
                str(row[name_col]).strip()
                if name_col is not None
                else ticker
            )

            results.append({
                "ticker": ticker,
                "name": name,
                "market_type": "NASDAQ100",
            })

        return results

    except Exception as e:
        logger.error(f"NASDAQ100 목록 실패: {e}")
        return []


def get_nyse_tickers() -> list:
    """NYSE 전체 상장 종목 (FinanceDataReader)"""
    try:
        import FinanceDataReader as fdr
        df = fdr.StockListing('NYSE')
        return [{"ticker": str(r["Symbol"]).replace(".", "-"),
                 "name": str(r["Name"]), "market_type": "NYSE"}
                for _, r in df.iterrows()
                if str(r["Symbol"]).isalpha() and len(str(r["Symbol"])) <= 5]
    except Exception as e:
        logger.error(f"NYSE 목록 실패: {e}"); return []


def get_nasdaq_tickers() -> list:
    """NASDAQ 전체 상장 종목 (FinanceDataReader)"""
    try:
        import FinanceDataReader as fdr
        df = fdr.StockListing('NASDAQ')
        return [{"ticker": str(r["Symbol"]).replace(".", "-"),
                 "name": str(r["Name"]), "market_type": "NASDAQ"}
                for _, r in df.iterrows()
                if str(r["Symbol"]).isalpha() and len(str(r["Symbol"])) <= 5]
    except Exception as e:
        logger.error(f"NASDAQ 목록 실패: {e}"); return []


def get_all_us_tickers(universe: str = "sp500+nasdaq100") -> list:
    global _cache
    key = universe.lower().strip()
    if key in _cache:
        return _cache[key]

    seen, tickers = set(), []

    def _add(items):
        for t in items:
            if t["ticker"] not in seen and t["ticker"] not in EXCLUDE_US:
                tickers.append(t)
                seen.add(t["ticker"])

    use_all = key in ("all", "")
    parts = {x.strip() for x in key.split("+") if x.strip()}
    # S&P500
    if use_all or "sp500" in parts:
        _add(get_sp500_tickers())

    # NASDAQ100
    if use_all or "nasdaq100" in parts:
        _add(get_nasdaq100_tickers())

    # NYSE 전체 (sp500/nasdaq100과 중복 제거됨)
    if use_all or "nyse" in parts:
        _add(get_nyse_tickers())

    # NASDAQ 전체 (sp500/nasdaq100과 중복 제거됨)
    if use_all or "nasdaq" in parts:
        _add(get_nasdaq_tickers())

    logger.info(f"US 유니버스 [{universe}]: {len(tickers)}개")
    _cache[key] = tickers
    return tickers


def get_us_ohlcv(ticker: str, period: str = "2y",
                 scan_context=None) -> Optional[pd.DataFrame]:
    try:
        history_kwargs = {"period": period, "auto_adjust": True}
        if scan_context is not None:
            years = int(period[:-1]) if period.endswith("y") and period[:-1].isdigit() else 2
            local_as_of = scan_context.as_of.astimezone(scan_context.timezone)
            end = local_as_of.date() + timedelta(days=1)  # yfinance end is exclusive
            start = end - timedelta(days=years * 365 + 7)
            history_kwargs = {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "auto_adjust": True,
            }
        df = yf.Ticker(ticker).history(**history_kwargs)
        if df is None or len(df) < 50: return None
        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
        df.index = pd.to_datetime(df.index).tz_localize(None)
        if scan_context is not None:
            from scanner.time_context import normalize_ohlcv
            df = normalize_ohlcv(df, scan_context)
        return df
    except Exception as e:
        logger.debug(f"US {ticker} 실패: {e}"); return None


def fetch_ohlcv(ticker: str, lookback_days: int = 730,
                scan_context=None) -> Optional[pd.DataFrame]:
    """Phase 2 통합 어댑터 — US 한정. lookback_days를 yfinance period 문자열로
    환산해 기존 get_us_ohlcv()를 호출한다.

    실패 정책 (Phase 4):
      - lookback_days ≤ 0 → None (정상 빈 결과)
      - get_us_ohlcv 가 None / 빈 DF 반환 → None (legitimately empty)
      - 외부 어댑터 예외 → `DataFetchError` raise (호출자가 명시적으로 처리)
    """
    if lookback_days <= 0:
        return None
    years = max(1, (lookback_days + 364) // 365)
    period = f"{years}y"
    try:
        if scan_context is None:
            return get_us_ohlcv(ticker, period=period)
        return get_us_ohlcv(ticker, period=period, scan_context=scan_context)
    except Exception as e:
        from scanner.errors import DataFetchError
        logger.debug(f"US fetch_ohlcv {ticker} 실패: {e}")
        raise DataFetchError(f"US fetch failed for {ticker}: {e}") from e


def get_us_batch(tickers: list, progress_callback=None, delay: float = 0.1,
                 scan_context=None) -> list:
    results, total, bs = [], len(tickers), 50
    for start in range(0, total, bs):
        batch = tickers[start:start + bs]
        syms  = [t["ticker"] for t in batch]
        try:
            download_kwargs = {
                "period": "2y", "auto_adjust": True,
                "group_by": "ticker", "threads": True, "progress": False,
            }
            if scan_context is not None:
                local_as_of = scan_context.as_of.astimezone(scan_context.timezone)
                end = local_as_of.date() + timedelta(days=1)
                start_date = end - timedelta(days=2 * 365 + 7)
                download_kwargs.pop("period")
                download_kwargs.update(
                    start=start_date.isoformat(), end=end.isoformat()
                )
            raw = yf.download(syms, **download_kwargs)
            for info in batch:
                sym = info["ticker"]
                try:
                    df = (raw[["Open","High","Low","Close","Volume"]] if len(syms)==1
                          else raw[sym][["Open","High","Low","Close","Volume"]]).dropna()
                    df.index = pd.to_datetime(df.index).tz_localize(None)
                    if scan_context is not None:
                        from scanner.time_context import normalize_ohlcv
                        df = normalize_ohlcv(df, scan_context)
                    results.append((info, df if len(df) >= 50 else None))
                except Exception:
                    results.append((info, None))
        except Exception as e:
            logger.error(f"US 배치 실패: {e}")
            for info in batch: results.append((info, None))

        if progress_callback:
            progress_callback(min(start + bs, total), total,
                              f"US [{min(start+bs,total)}/{total}]")
        time.sleep(delay)
    return results
