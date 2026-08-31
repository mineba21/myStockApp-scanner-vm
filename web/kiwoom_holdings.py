"""키움 실계좌 잔고를 WEB 보유현황 행으로 변환하는 읽기 전용 어댑터."""

from __future__ import annotations

import hashlib
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from trading.kiwoom_readonly import (
    KiwoomConfig,
    KiwoomError,
    KiwoomReadOnlyClient,
    load_profile_configs,
)


_LOCK = threading.Lock()
_BALANCE_CACHE: dict[str, Any] = {"expires_at": 0.0, "rows": []}
_TOKEN_CACHE: dict[str, tuple[float, str]] = {}


def _number(value: Any, *, absolute: bool = False) -> float:
    if value is None:
        return 0.0
    text = str(value).strip().replace(",", "")
    if not text:
        return 0.0
    try:
        number = float(text)
    except (TypeError, ValueError):
        return 0.0
    return abs(number) if absolute else number


def _ticker(value: Any) -> str:
    ticker = str(value or "").strip().upper().split("_", 1)[0]
    if len(ticker) == 7 and ticker.startswith("A") and ticker[1:].isdigit():
        ticker = ticker[1:]
    return ticker


def _account_name(profile: str) -> str:
    if profile == "account2":
        return "키움 account2 · 자산배분 전용"
    return f"키움 {profile}"


def map_kiwoom_holding(
    profile: str,
    raw: dict[str, Any],
    *,
    updated_at: str | None = None,
) -> dict[str, Any]:
    """``kt00018`` 종목 행을 기존 WEB 보유현황 스키마로 맞춘다."""
    ticker = _ticker(raw.get("stk_cd"))
    quantity = _number(raw.get("rmnd_qty"), absolute=True)
    avg_price = _number(raw.get("pur_pric"), absolute=True)
    current_price = _number(raw.get("cur_prc"), absolute=True)
    eval_amount = _number(raw.get("evlt_amt"), absolute=True)
    profit_loss = _number(raw.get("evltv_prft"))
    profit_loss_pct = _number(raw.get("prft_rt"))
    timestamp = updated_at or datetime.now(timezone.utc).isoformat()
    return {
        "id": f"kiwoom:{profile}:KR:{ticker}",
        "account_id": None,
        "account_name": _account_name(profile),
        "account_profile": profile,
        "currency": "KRW",
        "ticker": ticker,
        "name": str(raw.get("stk_nm") or ticker).strip(),
        "market": "KR",
        "quantity": quantity,
        "avg_price": avg_price,
        "current_price": current_price,
        "entry_price": None,
        "initial_stop_loss": None,
        "current_stop_loss": None,
        "initial_r": None,
        "unrealized_r": None,
        "eval_amount": eval_amount or round(current_price * quantity, 2),
        "profit_loss": profit_loss,
        "profit_loss_pct": profit_loss_pct,
        "price_updated_at": timestamp,
        "last_buy_date": None,
        "sell_status": "BROKER_LIVE",
        "sell_severity": None,
        "sell_reason": None,
        "sell_checked_at": None,
        "last_alert_severity": None,
        "last_alert_reason": None,
        "last_alert_at": None,
        "memo": "키움 REST API 실계좌 조회",
        "source": "kiwoom",
        "read_only": True,
    }


def map_kiwoom_overseas_holding(
    profile: str,
    raw: dict[str, Any],
    *,
    updated_at: str | None = None,
) -> dict[str, Any]:
    """``ust21070`` 미국주식 원장잔고 행을 WEB 보유현황 스키마로 맞춘다."""
    ticker = str(raw.get("stk_cd") or "").strip().upper()
    quantity = _number(raw.get("poss_qty"), absolute=True)
    avg_price = _number(raw.get("frgn_stk_book_uv"), absolute=True)
    current_price = _number(raw.get("now_pric"), absolute=True)
    eval_amount = _number(raw.get("evlt_amt"), absolute=True)
    timestamp = updated_at or datetime.now(timezone.utc).isoformat()
    return {
        "id": f"kiwoom:{profile}:US:{ticker}",
        "account_id": None,
        "account_name": _account_name(profile),
        "account_profile": profile,
        "currency": str(raw.get("crnc_code") or "USD").strip().upper(),
        "ticker": ticker,
        "name": str(raw.get("frgn_stk_nm") or ticker).strip(),
        "market": "US",
        "exchange": str(raw.get("stex_nm") or "").strip(),
        "exchange_rate": _number(raw.get("exch_rate"), absolute=True),
        "quantity": quantity,
        "avg_price": avg_price,
        "current_price": current_price,
        "entry_price": None,
        "initial_stop_loss": None,
        "current_stop_loss": None,
        "initial_r": None,
        "unrealized_r": None,
        "eval_amount": eval_amount or round(current_price * quantity, 2),
        "profit_loss": _number(raw.get("pl_amt")),
        "profit_loss_pct": _number(raw.get("pl_rt")),
        "eval_amount_krw": _number(raw.get("evlt_amt_krw"), absolute=True),
        "profit_loss_krw": _number(raw.get("pl_amt_krw")),
        "price_updated_at": timestamp,
        "last_buy_date": None,
        "sell_status": "BROKER_LIVE",
        "sell_severity": None,
        "sell_reason": None,
        "sell_checked_at": None,
        "last_alert_severity": None,
        "last_alert_reason": None,
        "last_alert_at": None,
        "memo": "키움 REST API 미국주식 실계좌 조회",
        "source": "kiwoom",
        "read_only": True,
    }


def _token_key(profile: str, config: KiwoomConfig) -> str:
    fingerprint = hashlib.sha256(config.app_key.encode("utf-8")).hexdigest()[:16]
    return f"{profile}:{config.mode}:{fingerprint}"


def _get_token(profile: str, config: KiwoomConfig, client: KiwoomReadOnlyClient) -> str:
    key = _token_key(profile, config)
    cached = _TOKEN_CACHE.get(key)
    now = time.monotonic()
    if cached and cached[0] > now:
        return cached[1]
    token_data = client.issue_token()
    token = str(token_data["token"])
    # 공식 유효기간은 24시간. 서버 시각 파싱에 의존하지 않고 23시간만 재사용한다.
    _TOKEN_CACHE[key] = (now + 23 * 60 * 60, token)
    return token


def get_kiwoom_holdings(
    *,
    force: bool = False,
    client_factory: Callable[[KiwoomConfig], KiwoomReadOnlyClient] = KiwoomReadOnlyClient,
) -> list[dict[str, Any]]:
    """5계좌 잔고를 합쳐 반환한다. 금융 데이터는 프로세스 메모리에만 캐시한다."""
    try:
        cache_seconds = max(0, int(os.getenv("KIWOOM_BALANCE_CACHE_SECONDS", "60")))
    except ValueError:
        cache_seconds = 60
    now = time.monotonic()
    if not force and _BALANCE_CACHE["expires_at"] > now:
        return list(_BALANCE_CACHE["rows"])

    with _LOCK:
        now = time.monotonic()
        if not force and _BALANCE_CACHE["expires_at"] > now:
            return list(_BALANCE_CACHE["rows"])
        rows: list[dict[str, Any]] = []
        updated_at = datetime.now(timezone.utc).isoformat()
        for profile, config in load_profile_configs().items():
            client = client_factory(config)
            token = _get_token(profile, config, client)
            balance = client.get_account_balance(token)
            try:
                overseas_balance = client.get_overseas_account_balance(token)
            except KiwoomError as exc:
                # 국내전용 계좌는 미국주식 API가 508540을 반환한다. 이 경우만
                # 해외 잔고를 비운 채 국내 잔고 조회 결과는 계속 노출한다.
                if "508540" not in str(exc):
                    raise
                overseas_balance = {"holdings": []}
            rows.extend(
                map_kiwoom_holding(profile, item, updated_at=updated_at)
                for item in balance["holdings"]
                if _number(item.get("rmnd_qty"), absolute=True) > 0
            )
            rows.extend(
                map_kiwoom_overseas_holding(profile, item, updated_at=updated_at)
                for item in overseas_balance["holdings"]
                if _number(item.get("poss_qty"), absolute=True) > 0
            )
        rows.sort(
            key=lambda item: (
                item["account_profile"],
                item["market"],
                item["ticker"],
            )
        )
        _BALANCE_CACHE.update(expires_at=now + cache_seconds, rows=rows)
        return list(rows)


def clear_kiwoom_cache() -> None:
    """테스트 및 강제 새로고침용 캐시 초기화."""
    with _LOCK:
        _BALANCE_CACHE.update(expires_at=0.0, rows=[])
        _TOKEN_CACHE.clear()
