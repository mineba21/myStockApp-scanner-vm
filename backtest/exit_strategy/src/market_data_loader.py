"""Historical OHLCV 확보 (운영 스캐너와 같은 공급자·같은 가격 기준).

- US: yfinance ``auto_adjust=True`` (분할·배당 수정주가) — 운영 ``get_us_batch`` 와 동일 기준.
- KR: FinanceDataReader (분할 수정, 배당 미수정) — 운영 ``get_kr_ohlcv`` 와 동일 기준.
  거래정지일은 Open/High/Low=0, Volume=0 행으로 내려온다. 스캐너 입력은 운영처럼 그대로
  두고, 체결 시뮬레이터가 해당 일을 체결 불가로 처리한다.

조회 실패 종목은 제외 사유와 함께 ``failures`` 에 남긴다 (임의 제외 금지).
결과는 pickle 캐시(pyarrow 미설치 환경)로 저장한다.
"""
from __future__ import annotations

import logging
import pickle
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from .common import isolate_scanner_imports

logger = logging.getLogger(__name__)
COLS = ["Open", "High", "Low", "Close", "Volume"]


def _cache_path(cache_dir: str, market: str) -> Path:
    return Path(cache_dir) / f"ohlcv_{market}.pkl"


def load_cached(cache_dir: str, market: str) -> Optional[dict]:
    path = _cache_path(cache_dir, market)
    if not path.exists():
        return None
    with open(path, "rb") as fh:
        return pickle.load(fh)


def save_cache(cache_dir: str, market: str, payload: dict) -> Path:
    path = _cache_path(cache_dir, market)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
    return path


def _clean(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    if df is None or df.empty:
        return None
    df = df[COLS].dropna().copy()
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df if len(df) else None


# ── US ────────────────────────────────────────────────────────────

def fetch_us(start: str, universe: str, extra: List[str],
             tickers: Optional[List[dict]] = None, batch: int = 80) -> dict:
    import yfinance as yf

    isolate_scanner_imports()
    if tickers is None:
        from scanner.us_stocks import get_all_us_tickers
        tickers = get_all_us_tickers(universe)
    meta = {t["ticker"]: t for t in tickers}
    symbols = list(meta) + [s for s in extra if s not in meta]
    frames: Dict[str, pd.DataFrame] = {}
    failures: Dict[str, str] = {}
    for i in range(0, len(symbols), batch):
        chunk = symbols[i:i + batch]
        for attempt in range(3):
            try:
                raw = yf.download(chunk, start=start, auto_adjust=True, group_by="ticker",
                                  progress=False, threads=True)
                break
            except Exception as exc:  # pragma: no cover - network
                logger.warning("yfinance batch fail %s: %s", i, exc)
                raw = None
                time.sleep(2 + attempt * 3)
        for sym in chunk:
            try:
                df = _clean(raw[sym]) if raw is not None and len(chunk) > 1 else _clean(raw)
            except Exception:
                df = None
            if df is None or len(df) < 50:
                failures[sym] = "no_or_short_history"
            else:
                frames[sym] = df
        logger.info("US %d/%d", min(i + batch, len(symbols)), len(symbols))
    return {"market": "US", "frames": frames, "meta": meta, "failures": failures,
            "fetched_at": pd.Timestamp.utcnow().isoformat(), "price_basis": "yfinance auto_adjust=True"}


# ── KR ────────────────────────────────────────────────────────────

def _fetch_kr_one(code: str, start: str) -> Tuple[str, Optional[pd.DataFrame], Optional[str]]:
    import FinanceDataReader as fdr

    for attempt in range(3):
        try:
            raw = fdr.DataReader(code, start)
            df = _clean(raw)
            if df is None or len(df) < 50:
                return code, None, "no_or_short_history"
            return code, df, None
        except Exception as exc:  # pragma: no cover - network
            err = type(exc).__name__
            time.sleep(1 + attempt)
    return code, None, f"fetch_error:{err}"


def fetch_kr(start: str, universe: str, extra: List[str],
             tickers: Optional[List[dict]] = None, workers: int = 4) -> dict:
    import socket
    from concurrent.futures import ThreadPoolExecutor, as_completed

    socket.setdefaulttimeout(20)      # 응답 없는 요청이 스레드를 영구 점유하지 않도록
    isolate_scanner_imports()
    if tickers is None:
        from scanner.kr_stocks import get_all_kr_tickers
        tickers = get_all_kr_tickers(market_filter=universe)
    meta = {t["ticker"]: t for t in tickers}
    codes = list(meta) + [c for c in extra if c not in meta]
    frames, failures = {}, {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_fetch_kr_one, c, start) for c in codes]
        for n, fut in enumerate(as_completed(futs)):
            code, df, err = fut.result()
            if df is None:
                failures[code] = err
            else:
                frames[code] = df
            if n % 100 == 0:
                logger.info("KR %d/%d (fail %d)", n, len(codes), len(failures))
    return {"market": "KR", "frames": frames, "meta": meta, "failures": failures,
            "fetched_at": pd.Timestamp.utcnow().isoformat(),
            "price_basis": "FinanceDataReader (split-adjusted, dividends not adjusted)"}


def ensure_market_data(cfg: dict, market: str, refresh: bool = False,
                       extra_tickers: Optional[List[str]] = None) -> dict:
    cache_dir = cfg["data"]["cache_dir"]
    if not refresh:
        cached = load_cached(cache_dir, market)
        if cached is not None:
            missing = [t for t in (extra_tickers or []) if t not in cached["frames"]
                       and t not in cached["failures"]]
            if not missing:
                return cached
            logger.info("%s cache missing %d extra tickers → topping up", market, len(missing))
            top = (fetch_us if market == "US" else fetch_kr)(
                cfg["data"]["history_start"], "", [], tickers=[{"ticker": t, "name": t} for t in missing])
            cached["frames"].update(top["frames"])
            cached["failures"].update(top["failures"])
            for t in missing:
                cached["meta"].setdefault(t, {"ticker": t, "name": t, "_extra": True})
            save_cache(cache_dir, market, cached)
            return cached
    d = cfg["data"]
    if market == "US":
        extra = list(dict.fromkeys(d["us_condition_indices"] + [d["us_benchmark"]] + (extra_tickers or [])))
        payload = fetch_us(d["history_start"], d["us_universe"], extra)
    else:
        extra = list(dict.fromkeys(d["kr_condition_indices"] + [d["kr_benchmark"]] + (extra_tickers or [])))
        payload = fetch_kr(d["history_start"], d["kr_universe"], extra)
    save_cache(cache_dir, market, payload)
    return payload


def is_halted(row) -> bool:
    """거래정지/결측 봉: 시가·고가·저가 0 또는 거래량 0."""
    return (row["Open"] <= 0 or row["High"] <= 0 or row["Low"] <= 0 or row["Volume"] <= 0)
