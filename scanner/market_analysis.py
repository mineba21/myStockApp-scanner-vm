"""시장 지수 Stage 분석 (Weinstein 'Forest to Trees')
나스닥/S&P500/KOSPI 가 Stage4(하락장)이면 개별주 돌파 성공률 급감.
"""
import logging
from datetime import datetime, timedelta
from typing import Dict

logger = logging.getLogger(__name__)

_cache: Dict = {}
_cache_time: datetime = None
_context_cache: Dict = {}
CACHE_MINUTES = 60
CONTEXT_CACHE_MAX_ENTRIES = 64

US_INDICES = [
    {"ticker": "SPY",  "name": "S&P500"},
    {"ticker": "QQQ",  "name": "NASDAQ100"},
]
KR_INDICES = [
    {"ticker": "069500", "name": "KOSPI200"},  # KODEX 200
]

# ── 섹터 ETF (Forest-to-Trees 보조) ─────────────────────────────
US_SECTOR_ETFS = [
    {"ticker": "XLK", "name": "기술"},
    {"ticker": "XLF", "name": "금융"},
    {"ticker": "XLV", "name": "헬스케어"},
    {"ticker": "XLE", "name": "에너지"},
    {"ticker": "XLI", "name": "산업재"},
    {"ticker": "XLY", "name": "경기소비재"},
]
KR_SECTOR_ETFS = [
    {"ticker": "091160", "name": "반도체"},    # KODEX 반도체
    {"ticker": "305720", "name": "2차전지"},   # KODEX 2차전지산업
    {"ticker": "244580", "name": "바이오"},    # KODEX 바이오
]


def get_market_stages(force: bool = False, scan_contexts=None) -> Dict:
    """미국·한국 주요 지수의 Weinstein Stage를 반환합니다."""
    global _cache, _cache_time
    if (scan_contexts is None and not force and _cache_time
            and (datetime.now() - _cache_time) < timedelta(minutes=CACHE_MINUTES)):
        return _cache

    from scanner.us_stocks import get_us_ohlcv
    from scanner.kr_stocks import get_kr_ohlcv
    from scanner.weinstein import stage_of, _slope
    from config import MA_PERIOD

    result: Dict = {
        "US": [], "KR": [],
        "US_SECTORS": [], "KR_SECTORS": [],
        "updated_at": datetime.now().isoformat(),
    }

    market_specs = {
        "US": (US_INDICES, US_SECTOR_ETFS, get_us_ohlcv),
        "KR": (KR_INDICES, KR_SECTOR_ETFS, get_kr_ohlcv),
    }
    for market, (indices, sectors, fetch_fn) in market_specs.items():
        context = (scan_contexts or {}).get(market)
        cache_key = None
        cached = None
        if context is not None:
            cache_key = (
                market,
                context.session_date.isoformat(),
                context.strategy_version,
            )
            if not force:
                cached = _context_cache.get(cache_key)
                if (cached is not None
                        and (datetime.now() - cached["cached_at"])
                        >= timedelta(minutes=CACHE_MINUTES)):
                    _context_cache.pop(cache_key, None)
                    cached = None

        if cached is not None:
            # 새 list 로 복사해 condition 계산/호출자 변경이 캐시를 오염시키지 않게 한다.
            result[market] = [dict(item) for item in cached["indices"]]
            result[f"{market}_SECTORS"] = [
                dict(item) for item in cached["sectors"]
            ]
            continue

        market_rows = []
        sector_rows = []
        for idx in indices:
            _analyze_index(
                idx, market, fetch_fn, MA_PERIOD, stage_of, _slope,
                market_rows, context,
            )
        for idx in sectors:
            _analyze_index(
                idx, market, fetch_fn, MA_PERIOD, stage_of, _slope,
                sector_rows, context,
            )
        result[market] = market_rows
        result[f"{market}_SECTORS"] = sector_rows

        if cache_key is not None:
            _context_cache.pop(cache_key, None)
            _context_cache[cache_key] = {
                "indices": [dict(item) for item in market_rows],
                "sectors": [dict(item) for item in sector_rows],
                "cached_at": datetime.now(),
            }
            while len(_context_cache) > CONTEXT_CACHE_MAX_ENTRIES:
                oldest = next(iter(_context_cache))
                _context_cache.pop(oldest)

    result["US_condition"] = _condition(result["US"])
    result["KR_condition"] = _condition(result["KR"])
    result["data_quality"] = {
        market: _data_quality(
            result[market] + result[f"{market}_SECTORS"]
        )
        for market in ("US", "KR")
    }

    if scan_contexts is None:
        _cache = result
        _cache_time = datetime.now()
    return result


def get_benchmark_close(market: str = "US", scan_context=None) -> "pd.Series | None":
    """스캔 엔진에서 RS 계산용 벤치마크 종가 시리즈를 반환합니다."""
    try:
        if market == "US":
            from scanner.us_stocks import get_us_ohlcv
            df = (get_us_ohlcv("SPY") if scan_context is None
                  else get_us_ohlcv("SPY", scan_context=scan_context))
        else:
            from scanner.kr_stocks import get_kr_ohlcv
            df = (get_kr_ohlcv("069500") if scan_context is None
                  else get_kr_ohlcv("069500", scan_context=scan_context))
        if df is None:
            return None
        if scan_context is not None and df.attrs.get("data_status") != "FINAL":
            logger.warning(
                "벤치마크 최신 확정 봉 누락: market=%s last=%s stale=%s",
                market,
                df.attrs.get("last_bar_date"),
                df.attrs.get("staleness_sessions"),
            )
        close = df["Close"].copy()
        close.attrs.update(df.attrs)
        return close
    except Exception as e:
        logger.error(f"벤치마크 로드 실패: {e}")
        return None


# ── 내부 헬퍼 ────────────────────────────────────────────────

def _analyze_index(idx, market, fetch_fn, MA_PERIOD, stage_of, _slope, out_list,
                   scan_context=None):
    try:
        import pandas as pd
        df = (fetch_fn(idx["ticker"]) if scan_context is None
              else fetch_fn(idx["ticker"], scan_context=scan_context))
        if df is None or len(df) < MA_PERIOD:
            if scan_context is not None:
                out_list.append(_unavailable_row(
                    idx, market, "INSUFFICIENT_DATA", df
                ))
            return
        if scan_context is not None and df.attrs.get("data_status") != "FINAL":
            logger.warning(
                "지수/섹터 최신 확정 봉 누락: market=%s ticker=%s last=%s stale=%s",
                market, idx["ticker"], df.attrs.get("last_bar_date"),
                df.attrs.get("staleness_sessions"),
            )
            out_list.append(_unavailable_row(idx, market, "STALE", df))
            return
        close = df["Close"]
        ma    = close.rolling(MA_PERIOD, min_periods=MA_PERIOD // 2).mean()
        cur_p  = float(close.iloc[-1])
        cur_ma = float(ma.iloc[-1])
        slope  = _slope(ma)
        stage  = stage_of(cur_p, cur_ma, slope)
        pct    = (cur_p - cur_ma) / cur_ma * 100

        # 52주 고저 대비 위치
        high52 = float(close.iloc[-252:].max()) if len(close) >= 252 else float(close.max())
        low52  = float(close.iloc[-252:].min()) if len(close) >= 252 else float(close.min())
        pos52  = round((cur_p - low52) / (high52 - low52) * 100, 1) if high52 != low52 else 50.0

        out_list.append({
            "ticker": idx["ticker"],
            "name":   idx["name"],
            "market": market,
            "stage":  stage,
            "price":  round(cur_p, 2),
            "ma150":  round(cur_ma, 2),
            "pct_vs_ma": round(pct, 2),
            "pos52w":  pos52,       # 52주 고저 사이 위치 (%)
            "slope":  round(slope, 4),
            "data_status": df.attrs.get("data_status", "FINAL"),
            "last_bar_date": df.attrs.get("last_bar_date"),
            "staleness_sessions": df.attrs.get("staleness_sessions", 0),
        })
    except Exception as e:
        logger.error(f"지수 분석 실패 {idx['ticker']}: {e}")
        if scan_context is not None:
            out_list.append(_unavailable_row(
                idx, market, "INSUFFICIENT_DATA", None,
                error=str(e),
            ))


def _unavailable_row(idx, market, status, df=None, error=None) -> Dict:
    row = {
        "ticker": idx["ticker"],
        "name": idx["name"],
        "market": market,
        "stage": "INSUFFICIENT_DATA",
        "data_status": status,
        "last_bar_date": (
            df.attrs.get("last_bar_date") if df is not None else None
        ),
        "staleness_sessions": (
            df.attrs.get("staleness_sessions") if df is not None else None
        ),
    }
    if error:
        row["error"] = error
    return row


def _data_quality(rows: list) -> Dict:
    unavailable = [
        row for row in rows if row.get("data_status", "FINAL") != "FINAL"
    ]
    return {
        "status": "FINAL" if not unavailable else "INSUFFICIENT_DATA",
        "unavailable_count": len(unavailable),
        "stale_count": sum(
            row.get("data_status") == "STALE" for row in unavailable
        ),
        "unavailable_tickers": [row["ticker"] for row in unavailable],
    }


def _condition(indices: list) -> str:
    """지수 리스트로 전체 시장 상태 판단"""
    if not indices:
        return "UNKNOWN"
    stages = [i.get("stage") for i in indices]
    if any(stage not in {"STAGE1", "STAGE2", "STAGE3", "STAGE4"}
           for stage in stages):
        return "UNKNOWN"
    if all(s == "STAGE4" for s in stages):  return "BEAR"      # 완전 하락장 🔴
    if any(s == "STAGE4" for s in stages):  return "CAUTION"   # 혼조세 주의 🟡
    if all(s == "STAGE2" for s in stages):  return "BULL"      # 완전 상승장 🟢
    if all(s in ("STAGE1", "STAGE2") for s in stages): return "NEUTRAL"  # 회복/횡보 🔵
    return "CAUTION"
