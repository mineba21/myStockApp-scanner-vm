"""키움 자유투자(미국)·ISA(국내) 보유종목의 Weinstein 매도 후보 분석.

주문 API를 호출하지 않는다. 기존 Weinstein 판정기를 읽기 전용 실계좌 행에
적용하고, 결과를 메모리와 로컬 JSON에 캐시한다.
"""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SELL_TARGETS = frozenset({("account2", "US"), ("account4", "KR")})
STATUS_BY_SEVERITY = {
    "HIGH": "SELL_REQUIRED",
    "MEDIUM": "REVIEW",
    "LOW": "CAUTION",
}
DEFAULT_CACHE_FILE = Path.home() / ".cache" / "mystockapp" / "kiwoom_sell_analysis.json"

_LOCK = threading.Lock()
_CACHE: dict[str, dict[str, Any]] = {}
_LOADED = False
_REFRESHING = False


def _cache_file() -> Path:
    configured = os.getenv("KIWOOM_SELL_CACHE_FILE", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_CACHE_FILE


def _cache_seconds() -> int:
    try:
        return max(60, int(os.getenv("KIWOOM_SELL_CACHE_SECONDS", "3600")))
    except ValueError:
        return 3600


def _key(row: dict[str, Any]) -> str:
    return f"{row.get('market')}:{row.get('ticker')}"


def _is_target(row: dict[str, Any]) -> bool:
    return (
        str(row.get("account_profile") or ""),
        str(row.get("market") or "").upper(),
    ) in SELL_TARGETS


def _load_cache() -> None:
    global _LOADED, _CACHE
    if _LOADED:
        return
    try:
        payload = json.loads(_cache_file().read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            _CACHE = {
                str(key): value
                for key, value in payload.items()
                if isinstance(value, dict)
            }
    except (OSError, ValueError, TypeError):
        _CACHE = {}
    _LOADED = True


def _save_cache() -> None:
    path = _cache_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(_CACHE, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)


def _analysis_fields(result: dict[str, Any] | None) -> dict[str, Any]:
    if result is None:
        return {
            "sell_status": "HOLD",
            "sell_severity": None,
            "sell_reason": "Weinstein 매도 신호 없음",
        }
    severity = str(result.get("severity") or "LOW")
    return {
        "sell_status": STATUS_BY_SEVERITY.get(severity, "CAUTION"),
        "sell_severity": severity,
        "sell_reason": str(result.get("sell_reason") or "Weinstein 매도 후보"),
    }


def _analyze_one(
    row: dict[str, Any],
    benchmark_close: Any,
) -> tuple[str, dict[str, Any]]:
    from config import MA_PERIOD
    from scanner.kr_stocks import get_kr_ohlcv
    from scanner.us_stocks import get_us_ohlcv
    from scanner.weinstein import check_sell_signal, to_weekly_ohlcv

    market = str(row.get("market") or "").upper()
    ticker = str(row.get("ticker") or "").upper()
    checked_at = datetime.now(timezone.utc).isoformat()
    try:
        frame = get_kr_ohlcv(ticker) if market == "KR" else get_us_ohlcv(ticker)
        if frame is None or len(frame) < MA_PERIOD + 20:
            raise ValueError("매도 판정에 필요한 시세 데이터가 부족합니다")
        weekly = to_weekly_ohlcv(frame)
        if weekly is not None and len(weekly) == 0:
            weekly = None
        result = check_sell_signal(
            frame,
            ticker,
            str(row.get("name") or ticker),
            market,
            buy_price=float(row.get("avg_price") or 0) or None,
            stop_loss=None,
            weekly_df=weekly,
            benchmark_close=benchmark_close,
            market_condition=None,
        )
        fields = _analysis_fields(result)
    except Exception:
        fields = {
            "sell_status": "CHECK_FAILED",
            "sell_severity": None,
            "sell_reason": "Weinstein 판정용 가격 데이터를 확인하지 못했습니다.",
        }
    fields.update(
        sell_checked_at=checked_at,
        sell_analysis="WEINSTEIN_READ_ONLY",
        checked_epoch=time.time(),
    )
    return _key(row), fields


def refresh_kiwoom_sell_analysis(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """대상 종목을 동기 분석한다. 주문이나 계좌 상태 변경은 수행하지 않는다."""
    from scanner.market_analysis import get_benchmark_close

    targets: dict[str, dict[str, Any]] = {}
    for row in rows:
        if _is_target(row) and row.get("ticker"):
            targets.setdefault(_key(row), dict(row))
    if not targets:
        return {}

    markets = {str(row.get("market")) for row in targets.values()}
    benchmarks = {market: get_benchmark_close(market) for market in markets}
    results: dict[str, dict[str, Any]] = {}
    workers = min(4, len(targets))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="kiwoom-sell") as pool:
        futures = {
            pool.submit(_analyze_one, row, benchmarks.get(str(row.get("market")))): key
            for key, row in targets.items()
        }
        for future in as_completed(futures):
            key, fields = future.result()
            results[key] = fields

    with _LOCK:
        _load_cache()
        _CACHE.update(results)
        try:
            _save_cache()
        except OSError:
            pass
    return results


def _background_refresh(rows: list[dict[str, Any]]) -> None:
    global _REFRESHING
    try:
        refresh_kiwoom_sell_analysis(rows)
    finally:
        with _LOCK:
            _REFRESHING = False


def apply_kiwoom_sell_analysis(
    rows: list[dict[str, Any]],
    *,
    force: bool = False,
    background: bool = True,
) -> list[dict[str, Any]]:
    """자유투자 미국주식·ISA 국내주식에 판정 결과를 합친다."""
    global _REFRESHING
    now = time.time()
    targets = [row for row in rows if _is_target(row)]
    with _LOCK:
        _load_cache()
        stale = force or any(
            now - float(_CACHE.get(_key(row), {}).get("checked_epoch", 0))
            >= _cache_seconds()
            for row in targets
        )
        if stale and background and not _REFRESHING:
            _REFRESHING = True
            threading.Thread(
                target=_background_refresh,
                args=([dict(row) for row in rows],),
                name="kiwoom-sell-refresh",
                daemon=True,
            ).start()

        decorated = []
        for original in rows:
            row = dict(original)
            if _is_target(row):
                cached = _CACHE.get(_key(row))
                if cached:
                    row.update({key: value for key, value in cached.items() if key != "checked_epoch"})
                else:
                    row.update(
                        sell_status="PENDING",
                        sell_severity=None,
                        sell_reason="Weinstein 매도 점검 준비 중",
                        sell_checked_at=None,
                        sell_analysis="WEINSTEIN_READ_ONLY",
                    )
            decorated.append(row)

    if stale and not background:
        refresh_kiwoom_sell_analysis(rows)
        return apply_kiwoom_sell_analysis(rows, background=True)
    return decorated


def clear_kiwoom_sell_cache() -> None:
    global _LOADED, _REFRESHING
    with _LOCK:
        _CACHE.clear()
        _LOADED = True
        _REFRESHING = False
