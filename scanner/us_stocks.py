"""미국 주식 데이터 - yfinance / FinanceDataReader"""
import io
import pandas as pd
import yfinance as yf
import requests
import logging
import time
from typing import Optional

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
    """S&P500 구성 종목. ``sector`` 는 위키 표의 GICS Sector 원문이다.

    Strict Gate 2 (섹터) 의 유일한 종목→섹터 소스이며, 목록을 받을 때 이미
    같은 표에 실려 오므로 추가 네트워크 호출이 없다. 위키 표 구조가 바뀌어
    컬럼이 사라져도 종목 목록 자체는 계속 나와야 하므로 방어적으로 읽는다
    (그 경우 sector=None → sector_name 이 NULL 로 남는다).
    """
    try:
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        tables = _read_html_wiki(url)
        df = tables[0]
        has_sector = "GICS Sector" in df.columns
        if not has_sector:
            logger.warning("S&P500 표에 'GICS Sector' 컬럼 없음 — 섹터 매핑 생략")
        rows = []
        for _, r in df.iterrows():
            sector = None
            if has_sector:
                value = r["GICS Sector"]
                if pd.notna(value):
                    sector = str(value).strip() or None
            rows.append({"ticker": str(r["Symbol"]).replace(".", "-"),
                         "name": str(r["Security"]), "market_type": "SP500",
                         "sector": sector})
        return rows
    except Exception as e:
        logger.error(f"S&P500 목록 실패: {e}"); return []


NASDAQ100_API = "https://api.nasdaq.com/api/quote/list-type/nasdaq100"


def _strip_security_suffix(name: str) -> str:
    """'Apple Inc. Common Stock' → 'Apple Inc.'"""
    cleaned = str(name).strip()
    for suffix in (" Common Stock", " Ordinary Shares", " Class A", " Class C"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)].strip()
    return cleaned


def get_nasdaq100_tickers() -> list:
    """NASDAQ-100 구성 종목 — Nasdaq 공식 JSON API.

    예전에는 nasdaq.com 의 HTML 표를 ``pd.read_html`` 로 긁었으나 페이지가
    JS 렌더링으로 바뀌면서 Symbol 컬럼을 찾지 못해 **조용히 빈 목록**을
    반환했다(운영 실측: ``universe=nasdaq100`` 스캔이 total_scanned=0 으로
    완료). 같은 데이터를 주는 JSON 엔드포인트로 교체한다.

    응답의 ``sector`` 필드는 전 종목 빈 문자열이라 쓰지 않는다. NASDAQ-100 의
    대부분(실측 87/102)은 S&P500 과 겹치고, ``get_all_us_tickers`` 가 S&P500 을
    먼저 넣으므로 그 종목들은 GICS 섹터를 그대로 물려받는다. 겹치지 않는
    나머지(ASML·ARM·PDD 등 외국 발행사)는 sector 가 None 으로 남는다.
    """
    try:
        resp = requests.get(NASDAQ100_API, headers=_HEADERS, timeout=20)
        resp.raise_for_status()
        rows = (resp.json() or {}).get("data", {}).get("data", {}).get("rows") or []
        if not rows:
            logger.error("NASDAQ100: 응답에 rows 가 없음")
            return []

        results = []
        for row in rows:
            ticker = str(row.get("symbol") or "").strip().replace(".", "-")
            if not ticker:
                continue
            results.append({
                "ticker": ticker,
                "name": _strip_security_suffix(row.get("companyName") or ticker),
                "market_type": "NASDAQ100",
            })
        if not results:
            logger.error("NASDAQ100: 파싱 결과가 비어 있음")
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


def get_us_ohlcv(ticker: str, period: str = "2y") -> Optional[pd.DataFrame]:
    try:
        df = yf.Ticker(ticker).history(period=period, auto_adjust=True)
        if df is None or len(df) < 50: return None
        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
        df.index = pd.to_datetime(df.index).tz_localize(None)
        return df
    except Exception as e:
        logger.debug(f"US {ticker} 실패: {e}"); return None


def fetch_ohlcv(ticker: str, lookback_days: int = 730) -> Optional[pd.DataFrame]:
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
        return get_us_ohlcv(ticker, period=period)
    except Exception as e:
        from scanner.errors import DataFetchError
        logger.debug(f"US fetch_ohlcv {ticker} 실패: {e}")
        raise DataFetchError(f"US fetch failed for {ticker}: {e}") from e


def get_us_batch(tickers: list, progress_callback=None, delay: float = 0.1) -> list:
    results, total, bs = [], len(tickers), 50
    for start in range(0, total, bs):
        batch = tickers[start:start + bs]
        syms  = [t["ticker"] for t in batch]
        try:
            raw = yf.download(syms, period="2y", auto_adjust=True,
                              group_by="ticker", threads=True, progress=False)
            for info in batch:
                sym = info["ticker"]
                try:
                    df = (raw[["Open","High","Low","Close","Volume"]] if len(syms)==1
                          else raw[sym][["Open","High","Low","Close","Volume"]]).dropna()
                    df.index = pd.to_datetime(df.index).tz_localize(None)
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
