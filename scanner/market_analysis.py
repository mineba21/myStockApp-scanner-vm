"""시장 지수 Stage 분석 (Weinstein 'Forest to Trees')
나스닥/S&P500/KOSPI 가 Stage4(하락장)이면 개별주 돌파 성공률 급감.
"""
import logging
from datetime import datetime, timedelta
from typing import Dict

logger = logging.getLogger(__name__)

_cache: Dict = {}
_cache_time: datetime = None
CACHE_MINUTES = 60

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


def get_market_stages(force: bool = False) -> Dict:
    """미국·한국 주요 지수의 Weinstein Stage를 반환합니다."""
    global _cache, _cache_time
    if (not force and _cache_time
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

    for idx in US_INDICES:
        _analyze_index(idx, "US", get_us_ohlcv, MA_PERIOD, stage_of, _slope, result["US"])
    for idx in KR_INDICES:
        _analyze_index(idx, "KR", get_kr_ohlcv, MA_PERIOD, stage_of, _slope, result["KR"])

    # 섹터 ETF 분석 (실패해도 무시)
    for idx in US_SECTOR_ETFS:
        _analyze_index(idx, "US", get_us_ohlcv, MA_PERIOD, stage_of, _slope, result["US_SECTORS"])
    for idx in KR_SECTOR_ETFS:
        _analyze_index(idx, "KR", get_kr_ohlcv, MA_PERIOD, stage_of, _slope, result["KR_SECTORS"])

    result["US_condition"] = _condition(result["US"])
    result["KR_condition"] = _condition(result["KR"])

    _cache = result
    _cache_time = datetime.now()
    return result


def get_benchmark_close(market: str = "US") -> "pd.Series | None":
    """스캔 엔진에서 RS 계산용 벤치마크 종가 시리즈를 반환합니다."""
    try:
        if market == "US":
            from scanner.us_stocks import get_us_ohlcv
            df = get_us_ohlcv("SPY")
        else:
            from scanner.kr_stocks import get_kr_ohlcv
            df = get_kr_ohlcv("069500")
        return df["Close"] if df is not None else None
    except Exception as e:
        logger.error(f"벤치마크 로드 실패: {e}")
        return None


# ── 내부 헬퍼 ────────────────────────────────────────────────

def _analyze_index(idx, market, fetch_fn, MA_PERIOD, stage_of, _slope, out_list):
    try:
        import pandas as pd
        df = fetch_fn(idx["ticker"])
        if df is None or len(df) < MA_PERIOD:
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
        })
    except Exception as e:
        logger.error(f"지수 분석 실패 {idx['ticker']}: {e}")


def _condition(indices: list) -> str:
    """지수 리스트로 전체 시장 상태 판단"""
    if not indices:
        return "UNKNOWN"
    stages = [i["stage"] for i in indices]
    if all(s == "STAGE4" for s in stages):  return "BEAR"      # 완전 하락장 🔴
    if any(s == "STAGE4" for s in stages):  return "CAUTION"   # 혼조세 주의 🟡
    if all(s == "STAGE2" for s in stages):  return "BULL"      # 완전 상승장 🟢
    if all(s in ("STAGE1", "STAGE2") for s in stages): return "NEUTRAL"  # 회복/횡보 🔵
    return "CAUTION"
