"""한국 주식 데이터 (KOSPI + KOSDAQ) - pykrx + FinanceDataReader"""
import pandas as pd
import time
import logging
from typing import Optional
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# ── 필터 설정 ─────────────────────────────────────────────────────
EXCLUDE_KR_KEYWORDS = [
    "스팩", "SPAC", "리츠", "ETF", "ETN", "선물", "인버스", "레버리지",
]
MIN_MARKET_CAP = 100_000_000_000   # 1,000억 원
MIN_PRICE      = 1_000             # 1,000 원


def _get_kr_cap_price() -> "pd.DataFrame | None":
    """pykrx로 전종목 시가총액+종가 조회. 실패 시 None 반환 (필터 생략)."""
    try:
        from pykrx import stock as px
        today = datetime.now().strftime("%Y%m%d")
        df = px.get_market_cap_by_ticker(today)
        # 컬럼: 시가총액, 거래량, 거래대금, 상장주식수  (index = 종목코드)
        # 종가를 별도로 가져오기
        ohlcv = px.get_market_ohlcv_by_ticker(today)
        if ohlcv is not None and "종가" in ohlcv.columns:
            df["종가"] = ohlcv["종가"]
        return df
    except Exception as e:
        logger.warning(f"pykrx 시가총액 조회 실패 → 필터 생략: {e}")
        return None


def get_all_kr_tickers(market_filter: str = "kospi+kosdaq") -> list:
    """KOSPI + KOSDAQ 전종목 (키워드·시가총액·가격 필터 적용).

    market_filter:
      "kospi+kosdaq" — KOSPI + KOSDAQ 전체 (기본값)
      "kospi"        — KOSPI 만
      "kosdaq"       — KOSDAQ 만
    """
    # HTTP 301 changes POST to GET and drops the finder parameters.
    import requests
    from scanner.errors import DataFetchError
    tickers = None
    for attempt in range(3):
        try:
            resp = requests.post(
                "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd",
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://data.krx.co.kr/"},
                data={"bld": "dbms/comm/finder/finder_stkisu", "mktsel": "ALL", "searchText": ""},
                timeout=(5, 20), allow_redirects=False,
            )
            logger.info("KRX finder attempt=%s status=%s bytes=%s", attempt + 1,
                        resp.status_code, len(resp.content))
            if resp.status_code != 200:
                raise ValueError("unexpected KRX status")
            payload = resp.json()
            items = payload.get("block1") if isinstance(payload, dict) else None
            if not isinstance(items, list) or not items:
                raise ValueError("missing KRX inventory")
            mkt_map = {"STK": "KOSPI", "KSQ": "KOSDAQ"}
            result = []
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError("invalid KRX row")
                if item.get("marketCode") not in mkt_map:
                    continue
                code, name = item.get("short_code"), item.get("codeName")
                if not isinstance(code, str) or len(code) != 6 or not code.isdigit() or not isinstance(name, str) or not name.strip():
                    raise ValueError("invalid KRX ticker")
                result.append(dict(ticker=code, name=name, market_type=mkt_map[item["marketCode"]]))
            if not result or len({t['ticker'] for t in result}) != len(result):
                raise ValueError("empty or duplicate KRX inventory")
            tickers = result
            break
        except (requests.RequestException, ValueError) as exc:
            logger.warning("KRX finder failed attempt=%s error=%s", attempt + 1, type(exc).__name__)
            if attempt < 2:
                time.sleep(attempt + 1)
    if tickers is None:
        raise DataFetchError("KRX ticker inventory unavailable after 3 attempts")
    logger.info("한국 전종목 (필터 전): %s개", len(tickers))

    # ② 시장 유형 필터 (KOSPI / KOSDAQ 선택)
    key = market_filter.lower().strip()
    if key == "kospi":
        tickers = [t for t in tickers if t["market_type"] == "KOSPI"]
    elif key == "kosdaq":
        tickers = [t for t in tickers if t["market_type"] == "KOSDAQ"]
    # else: kospi+kosdaq → 전체 유지

    # ③ 키워드 필터 (스팩·ETF·ETN·인버스 등 제외)
    before_kw = len(tickers)
    tickers = [
        t for t in tickers
        if not any(kw in t["name"] for kw in EXCLUDE_KR_KEYWORDS)
    ]
    logger.info(f"키워드 필터: {before_kw} → {len(tickers)}개")

    # ③ 시가총액·가격 필터 (pykrx 실패 시 생략)
    cap_df = _get_kr_cap_price()
    if cap_df is not None:
        before_cap = len(tickers)
        filtered = []
        for t in tickers:
            code = t["ticker"]
            if code not in cap_df.index:
                filtered.append(t)   # 데이터 없으면 포함 (안전 처리)
                continue
            row = cap_df.loc[code]
            cap   = float(row.get("시가총액", 0) or 0)
            price = float(row.get("종가",     0) or 0)
            if cap >= MIN_MARKET_CAP and price >= MIN_PRICE:
                filtered.append(t)
        tickers = filtered
        logger.info(
            f"시가총액·가격 필터: {before_cap} → {len(tickers)}개 "
            f"(시총≥{MIN_MARKET_CAP // 100_000_000}억, 가격≥{MIN_PRICE}원)"
        )

    return tickers


def get_kr_ohlcv(ticker: str, period_years: int = 2) -> Optional[pd.DataFrame]:
    end   = datetime.now()
    start = end - timedelta(days=period_years * 365)

    # FinanceDataReader 먼저 시도
    try:
        import FinanceDataReader as fdr
        from scanner.sector_trends import attach_raw_close
        raw = fdr.DataReader(ticker, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        if raw is not None and len(raw) > 50:
            df = raw[["Open", "High", "Low", "Close", "Volume"]].dropna()
            df.index = pd.to_datetime(df.index)
            # 섹터 추세(표시용)는 결측 행이 지워지지 않은 원본 종가로 구간을 판정한다.
            return attach_raw_close(df, raw["Close"])
    except Exception:
        pass

    # pykrx fallback
    try:
        from pykrx import stock as px
        from scanner.sector_trends import attach_raw_close
        raw = px.get_market_ohlcv(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), ticker)
        if raw is not None and len(raw) > 50:
            raw = raw.rename(columns={"시가": "Open", "고가": "High", "저가": "Low",
                                      "종가": "Close", "거래량": "Volume"})
            df = raw[["Open", "High", "Low", "Close", "Volume"]].dropna()
            df.index = pd.to_datetime(df.index)
            return attach_raw_close(df, raw["Close"])
    except Exception as e:
        logger.debug(f"KR {ticker} 조회 실패: {e}")

    return None


def fetch_ohlcv(ticker: str, lookback_days: int = 730) -> Optional[pd.DataFrame]:
    """Phase 2 통합 어댑터 — KR 한정. lookback_days를 period_years로 환산해
    기존 get_kr_ohlcv()를 호출한다.

    실패 정책 (Phase 4):
      - lookback_days ≤ 0 → None (정상 빈 결과)
      - get_kr_ohlcv 가 None / 빈 DF 반환 → None (legitimately empty)
      - 외부 어댑터 예외 → `DataFetchError` raise (호출자가 명시적으로 처리)
    """
    if lookback_days <= 0:
        return None
    period_years = max(1, (lookback_days + 364) // 365)
    try:
        return get_kr_ohlcv(ticker, period_years=period_years)
    except Exception as e:
        from scanner.errors import DataFetchError
        logger.debug(f"KR fetch_ohlcv {ticker} 실패: {e}")
        raise DataFetchError(f"KR fetch failed for {ticker}: {e}") from e


def get_kr_batch(tickers: list, progress_callback=None, delay: float = 0.05) -> list:
    results = []
    for i, info in enumerate(tickers):
        df = get_kr_ohlcv(info["ticker"])
        results.append((info, df))
        if progress_callback:
            progress_callback(i + 1, len(tickers), f"KR [{i+1}/{len(tickers)}] {info['name']}")
        time.sleep(delay)
    return results
