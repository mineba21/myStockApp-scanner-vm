"""종목별 섹터 분류와 섹터 최근 추세(5단계). 표시 전용이다.

설계와 근거는 docs/sector_trends.md. 매수·매도·알림·Strict Gate 2 판정에는 쓰지 않는다
(Gate 2 는 기존 ``market_analysis.get_sector_stage`` 의 주봉 Stage 를 그대로 쓴다).

- 추세: 최근 N거래일(기본 20) 종가 수익률을 강한 상승/상승/횡보/하락/강한 하락으로 나눈다.
- 미국: 분류는 기존 위키 GICS, 없으면 신호 종목만 Yahoo sector 로 보완한다.
  성과는 해당 SPDR 섹터 ETF(기존 ``get_market_stages`` 60분 캐시)의 수익률이다.
- 국내: 분류는 FinanceDataReader ``KRX-DESC`` 의 KSIC 업종이다. 성과는 이번 스캔
  유니버스에서 같은 업종 종목들의 N거래일 수익률 **중앙값**이다(추가 가격 조회 없음).
- 데이터가 모자라면 횡보로 채우지 않고 INSUFFICIENT/STALE/UNAVAILABLE 로 남긴다.
"""
from __future__ import annotations

import logging
import math
import statistics
from collections import Counter
import threading
import time
from datetime import date, datetime
from typing import Any, Callable, Dict, Iterable, Optional

import pandas as pd

from config import (
    SECTOR_TREND_FLAT_PCT,
    SECTOR_TREND_KR_MIN_COVERAGE,
    SECTOR_TREND_KR_MIN_SAMPLE,
    SECTOR_TREND_LOOKBACK_DAYS,
    SECTOR_TREND_STALE_DAYS,
    SECTOR_TREND_STRONG_PCT,
)

logger = logging.getLogger(__name__)

TREND_LEVELS = ("STRONG_UP", "UP", "FLAT", "DOWN", "STRONG_DOWN")
# 추세를 판정하지 못한 상태. 어느 것도 횡보(FLAT)로 대체하지 않는다.
INSUFFICIENT = "INSUFFICIENT"   # 이력·표본 부족
STALE = "STALE"                 # 마지막 봉이 오래됨
UNAVAILABLE = "UNAVAILABLE"     # 가격·성과 소스를 받지 못함

KR_BASIS = "KR_SCAN_MEDIAN"

# Yahoo(Morningstar) sector → 기존 GICS 원문. 이름이 달라 명시적으로만 대응시킨다.
YAHOO_TO_GICS = {
    "Technology": "Information Technology",
    "Financial Services": "Financials",
    "Healthcare": "Health Care",
    "Energy": "Energy",
    "Industrials": "Industrials",
    "Consumer Cyclical": "Consumer Discretionary",
    "Consumer Defensive": "Consumer Staples",
    "Utilities": "Utilities",
    "Real Estate": "Real Estate",
    "Basic Materials": "Materials",
    "Communication Services": "Communication Services",
}

CLASSIFICATION_TTL_SECONDS = 24 * 3600
FAILURE_TTL_SECONDS = 3600
YAHOO_TTL_SECONDS = 7 * 24 * 3600


# ── 추세 판정 ────────────────────────────────────────────────────

def classify_trend(return_pct: Optional[float]) -> Optional[str]:
    """수익률(%) → 5단계. 반올림 전 값으로 판정하며 결측·비유한 값은 None."""
    if return_pct is None:
        return None
    try:
        value = float(return_pct)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    if value >= SECTOR_TREND_STRONG_PCT:
        return "STRONG_UP"
    if value >= SECTOR_TREND_FLAT_PCT:
        return "UP"
    if value > -SECTOR_TREND_FLAT_PCT:
        return "FLAT"
    if value > -SECTOR_TREND_STRONG_PCT:
        return "DOWN"
    return "STRONG_DOWN"


def _as_date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return pd.Timestamp(value).date()
    except (TypeError, ValueError):
        return None


def trailing_return(close, *, now=None, lookback: Optional[int] = None) -> Dict[str, Any]:
    """종가 시리즈의 최근 ``lookback`` 거래일 수익률.

    날짜를 정리(같은 날짜는 마지막 값, 정렬, 미래 날짜 제외)한 뒤 **먼저 최근 lookback+1개
    거래일 구간을 고정**한다. 그 구간 안에 결측·0 이하·비유한 값이 하나라도 있으면
    INSUFFICIENT 로 둔다. 결측을 지우고 더 오래된 가격으로 시작일을 당기거나 값을 채워 넣지
    않는다(구간이 실제 N거래일이 아니게 되기 때문).

    Returns: ``{"status": "OK"|INSUFFICIENT|STALE|UNAVAILABLE, "return_pct", "asof", "start"}``
    ``start`` 는 구간 시작 거래일(국내 업종 집계가 같은 구간끼리만 묶는 데 쓴다).
    """
    lookback = SECTOR_TREND_LOOKBACK_DAYS if lookback is None else lookback
    missing = {"status": UNAVAILABLE, "return_pct": None, "asof": None, "start": None}
    if close is None:
        return missing
    try:
        series = pd.to_numeric(pd.Series(close), errors="coerce")
        series.index = pd.to_datetime(series.index, errors="coerce")
    except (TypeError, ValueError):
        return missing
    if getattr(series.index, "tz", None) is not None:
        series.index = series.index.tz_localize(None)
    series = series[series.index.notna()]
    series = series[~series.index.duplicated(keep="last")].sort_index()
    today = _as_date(now) or date.today()
    series = series[[d.date() <= today for d in series.index]]
    if len(series) == 0:
        return missing
    window = series.iloc[-(lookback + 1):]
    asof_date = window.index[-1].date()
    result = {"status": INSUFFICIENT, "return_pct": None, "asof": asof_date.isoformat(),
              "start": window.index[0].date().isoformat()}
    if len(window) < lookback + 1:
        return result
    if not all(math.isfinite(v) and v > 0 for v in window):
        return result
    result.update(status="OK", return_pct=(float(window.iloc[-1]) / float(window.iloc[0]) - 1) * 100)
    if (today - asof_date).days > SECTOR_TREND_STALE_DAYS:
        result["status"] = STALE
    return result


# 가격 어댑터(get_us_ohlcv/get_kr_ohlcv)는 매매 판정용으로 결측 행을 dropna 한 표를 돌려준다.
# 행이 지워지면 최근 구간이 과거로 늘어나므로, 어댑터는 dropna 이전 종가의 끝부분을 이 attrs
# 키에 함께 실어 준다. 길이를 짧게 두는 이유: pandas 2.x 는 연산마다 attrs 를 깊은 복사한다.
RAW_CLOSE_ATTR = "raw_close_tail"
RAW_CLOSE_TAIL = 64


def attach_raw_close(cleaned: pd.DataFrame, raw_close: pd.Series) -> pd.DataFrame:
    """어댑터용: dropna 이전 종가의 최근 부분을 ``cleaned.attrs`` 에 싣는다."""
    tail = pd.to_numeric(raw_close, errors="coerce").iloc[-RAW_CLOSE_TAIL:].copy()
    tail.index = pd.to_datetime(tail.index)
    if getattr(tail.index, "tz", None) is not None:
        tail.index = tail.index.tz_localize(None)
    cleaned.attrs[RAW_CLOSE_ATTR] = tail
    return cleaned


def trend_close(df: pd.DataFrame):
    """추세 계산용 종가. 어댑터가 실은 dropna 이전 종가가 있으면 그것을, 없으면 Close 를 쓴다."""
    raw = df.attrs.get(RAW_CLOSE_ATTR) if hasattr(df, "attrs") else None
    return raw if raw is not None else df["Close"]


def align_windows(results: Iterable[Dict[str, Any]], *, min_peers: int = 3) -> str:
    """같은 거래소 달력을 쓰는 시리즈들의 구간을 서로 맞춰 본다(제자리 수정).

    어댑터가 날짜 행 자체를 빠뜨리면 원본 종가로도 결측을 알 수 없다. 가장 흔한
    (시작일, 종료일) 구간과 다른 결과는 실제 N거래일이 아닐 수 있으므로 INSUFFICIENT 로 바꾼다.
    판정 가능한 결과가 ``min_peers`` 개 미만이면 비교하지 않는다. 기준 구간 문자열을 돌려준다.
    """
    results = list(results)
    usable = [r for r in results if r.get("status") in ("OK", STALE) and r.get("start")]
    if len(usable) < min_peers:
        return ""
    reference = Counter((r["start"], r["asof"]) for r in usable).most_common(1)[0][0]
    for r in usable:
        if (r["start"], r["asof"]) != reference:
            r.update(status=INSUFFICIENT, return_pct=None)
    return f"{reference[0]}~{reference[1]}"


def context_from_return(result: Dict[str, Any]) -> Dict[str, Any]:
    """``trailing_return`` 결과 → 저장용 추세 필드."""
    if result.get("status") == "OK":
        trend = classify_trend(result.get("return_pct"))
        return {"sector_trend": trend or UNAVAILABLE,
                "sector_return_20d": result.get("return_pct") if trend else None,
                "sector_trend_asof": result.get("asof")}
    return {"sector_trend": result.get("status") or UNAVAILABLE,
            "sector_return_20d": None,
            "sector_trend_asof": result.get("asof")}


# ── 분류 캐시 ────────────────────────────────────────────────────

class _TtlCache:
    """프로세스 내 TTL 캐시. 실패도 짧게 기억해 공급자를 반복 호출하지 않는다."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._lock = threading.Lock()
        self._items: Dict[Any, tuple] = {}
        self.clock = clock

    def get(self, key):
        with self._lock:
            hit = self._items.get(key)
            if hit and hit[0] > self.clock():
                return True, hit[1]
            return False, None

    def put(self, key, value, ttl):
        with self._lock:
            self._items[key] = (self.clock() + ttl, value)

    def clear(self):
        with self._lock:
            self._items.clear()


_cache = _TtlCache()


def _kr_industry_column(columns: Iterable[str]) -> Optional[str]:
    """KRX-DESC 의 KSIC 업종 컬럼을 고른다.

    FDR 의 GitHub 캐시 CSV(현행 ``StockListing('KRX-DESC')``)는 Sector=KOSDAQ 소속부,
    Industry=업종, Products=주요제품이다. KIND 직접 조회 경로는 Sector=업종,
    Industry=주요제품이다. ``Products`` 컬럼 유무로 구분한다.
    """
    columns = set(columns)
    if "Products" in columns and "Industry" in columns:
        return "Industry"
    if "Sector" in columns:
        return "Sector"
    return None


def parse_kr_industries(df: pd.DataFrame) -> Dict[str, str]:
    """KRX-DESC 표 → ``{6자리 종목코드: 업종}``. 업종이 빈 종목은 넣지 않는다."""
    column = _kr_industry_column(df.columns)
    if column is None or "Code" not in df.columns:
        raise ValueError("KRX-DESC 표에 종목코드/업종 컬럼이 없습니다.")
    result = {}
    for code, industry in zip(df["Code"], df[column]):
        code = str(code).strip().zfill(6)
        if pd.isna(industry):
            continue
        industry = str(industry).strip()
        if industry and len(code) == 6 and code.isdigit():
            result[code] = industry[:50]   # ScanResult.sector_name 길이
    return result


def kr_industry_map(loader: Optional[Callable[[], pd.DataFrame]] = None) -> Optional[Dict[str, str]]:
    """국내 종목코드 → KSIC 업종. 24시간 캐시, 실패는 1시간 기억 후 재시도. 실패 시 None."""
    hit, value = _cache.get("kr_industries")
    if hit:
        return value
    try:
        if loader is None:
            import FinanceDataReader as fdr
            loader = lambda: fdr.StockListing("KRX-DESC")  # noqa: E731
        mapping = parse_kr_industries(loader())
        if not mapping:
            raise ValueError("빈 업종 목록")
        _cache.put("kr_industries", mapping, CLASSIFICATION_TTL_SECONDS)
        logger.info("국내 업종 분류 %d종목", len(mapping))
        return mapping
    except Exception as e:
        logger.warning("국내 업종 분류 조회 실패 → 섹터 미분류 처리: %s", e)
        _cache.put("kr_industries", None, FAILURE_TTL_SECONDS)
        return None


def yahoo_gics_sector(ticker: str, fetcher: Optional[Callable[[str], dict]] = None) -> Optional[str]:
    """GICS 가 없는 미국 신호 종목의 보완 분류. Yahoo sector 를 GICS 원문으로 바꾼다."""
    key = ("yahoo", str(ticker).upper())
    hit, value = _cache.get(key)
    if hit:
        return value
    try:
        if fetcher is None:
            import yfinance as yf
            fetcher = lambda t: yf.Ticker(t).get_info()  # noqa: E731
        info = fetcher(ticker) or {}
        gics = YAHOO_TO_GICS.get(str(info.get("sector") or "").strip())
        _cache.put(key, gics, YAHOO_TTL_SECONDS if gics else FAILURE_TTL_SECONDS)
        return gics
    except Exception as e:
        logger.debug("Yahoo sector 조회 실패 %s: %s", ticker, e)
        _cache.put(key, None, FAILURE_TTL_SECONDS)
        return None


# ── 미국 ────────────────────────────────────────────────────────

def _empty_context() -> Dict[str, Any]:
    return {"sector_source": None, "sector_trend": None, "sector_return_20d": None,
            "sector_trend_asof": None, "sector_trend_basis": None,
            "sector_trend_sample": None, "sector_trend_members": None}


def us_sector_context(ticker: str, gics_sector: Optional[str], sector_rows: Optional[list],
                      *, yahoo_lookup: Optional[Callable[[str], Optional[str]]] = None) -> Dict[str, Any]:
    """미국 신호 하나의 섹터 표시 필드.

    ``sector_rows`` 는 ``get_market_stages()["US_SECTORS"]`` (ETF별 ``return_20d`` 포함).
    반환값의 ``sector_name`` 은 표시용이다. Gate 2 의 ``sector_stage`` 는 건드리지 않는다.
    """
    from scanner.market_analysis import US_SECTOR_ETFS

    by_gics = {e["gics"]: e for e in US_SECTOR_ETFS if e.get("gics")}
    context = _empty_context()
    source = None
    entry = by_gics.get(str(gics_sector or "").strip())
    if entry is not None:
        source = "GICS"
    else:
        fallback = (yahoo_lookup or yahoo_gics_sector)(ticker)
        entry = by_gics.get(fallback or "")
        if entry is not None:
            source = "YAHOO"
    if entry is None:
        context["sector_name"] = None
        return context

    context.update(sector_name=entry["name"], sector_source=source,
                   sector_trend_basis=f"ETF:{entry['ticker']}")
    row = next((r for r in (sector_rows or []) if r.get("ticker") == entry["ticker"]), None)
    if row is None or "return_status" not in row:
        context["sector_trend"] = UNAVAILABLE
        return context
    context.update(context_from_return({"status": row.get("return_status"),
                                        "return_pct": row.get("return_20d"),
                                        "asof": row.get("return_asof")}))
    return context


# ── 국내 ────────────────────────────────────────────────────────

def aggregate_kr_sectors(industries: Dict[str, str], universe: Iterable[str],
                         returns: Dict[str, Dict[str, Any]], *, now=None) -> Dict[str, Dict[str, Any]]:
    """국내 업종별 추세. 같은 업종 유니버스 종목의 N거래일 수익률 중앙값.

    - 분모(members): 이번 유니버스에서 그 업종으로 분류된 종목(가격 실패 포함).
    - 유효 표본: 상태 OK 이고 구간(시작일·종료일)이 유니버스 기준 구간과 같은 종목.
    - 유효 표본이 최소 개수 미만이거나 비율이 최소 커버리지 미만이면 INSUFFICIENT.
    - 유니버스 최신 기준일 자체가 오래됐으면 STALE, 가격이 하나도 없으면 UNAVAILABLE.
    """
    universe = [str(t) for t in universe]
    ok = {t: r for t, r in returns.items() if r.get("status") in ("OK", STALE) and r.get("asof")}
    latest = max((r["asof"] for r in ok.values()), default=None)
    # 같은 종료일 종목들의 가장 흔한 시작일을 기준 구간으로 삼는다. 구간 중간에 행이 빠진
    # 종목(거래정지 등)은 시작일이 달라 표본에서 빠진다.
    starts = Counter(r.get("start") for r in ok.values() if r["asof"] == latest)
    window_start = starts.most_common(1)[0][0] if starts else None
    latest_stale = bool(latest) and (
        ((_as_date(now) or date.today()) - date.fromisoformat(latest)).days > SECTOR_TREND_STALE_DAYS)

    members: Dict[str, list] = {}
    for ticker in universe:
        industry = industries.get(ticker)
        if industry:
            members.setdefault(industry, []).append(ticker)

    out = {}
    for industry, tickers in members.items():
        base = {"sector_source": "KRX", "sector_trend_basis": KR_BASIS,
                "sector_trend_members": len(tickers), "sector_trend_asof": latest}
        if latest is None:
            out[industry] = {**base, "sector_trend": UNAVAILABLE, "sector_return_20d": None,
                             "sector_trend_sample": 0}
            continue
        values = [ok[t]["return_pct"] for t in tickers
                  if t in ok and ok[t]["asof"] == latest and ok[t].get("start") == window_start
                  and ok[t].get("return_pct") is not None and math.isfinite(ok[t]["return_pct"])]
        base["sector_trend_sample"] = len(values)
        if latest_stale:
            out[industry] = {**base, "sector_trend": STALE, "sector_return_20d": None}
        elif (len(values) < SECTOR_TREND_KR_MIN_SAMPLE
              or len(values) / len(tickers) < SECTOR_TREND_KR_MIN_COVERAGE):
            out[industry] = {**base, "sector_trend": INSUFFICIENT, "sector_return_20d": None}
        else:
            median = statistics.median(values)
            out[industry] = {**base, "sector_trend": classify_trend(median), "sector_return_20d": median}
    return out


def kr_sector_name(ticker: str, industries: Optional[Dict[str, str]]) -> Dict[str, Any]:
    """국내 신호 저장 시점의 분류만 담는다. 추세는 스캔 후 ``aggregate_kr_sectors`` 로 채운다."""
    industry = (industries or {}).get(str(ticker))
    if not industry:
        return {"sector_name": None, "sector_source": None}
    return {"sector_name": industry, "sector_source": "KRX"}
