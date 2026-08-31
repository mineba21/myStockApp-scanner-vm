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
_BALANCE_CACHE: dict[str, Any] = {"expires_at": 0.0, "rows": [], "accounts": []}
_TOKEN_CACHE: dict[str, tuple[float, str]] = {}

ACCOUNT_NAMES = {
    "account1": "퀀트투자",
    "account2": "자유투자",
    "account4": "ISA",
}


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
    label = ACCOUNT_NAMES.get(profile)
    return f"{label} · {profile}" if label else f"키움 {profile}"


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


def _currency_item(report: dict[str, Any], currency: str = "USD") -> dict[str, Any]:
    return next(
        (
            item
            for item in report.get("items", [])
            if str(item.get("crnc_code") or "").strip().upper() == currency
        ),
        {},
    )


def _empty_report() -> dict[str, Any]:
    return {"summary": {}, "items": [], "holdings": []}


def _safe_overseas_call(call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return call()
    except KiwoomError as exc:
        # 국내전용 계좌가 미국주식 조회 TR을 호출하면 508540을 반환한다.
        message = str(exc)
        if "508540" in message or "조회내역이 없습니다" in message or "실패 (20)" in message:
            return _empty_report()
        raise


def _optional_report(client: Any, method: str, token: str) -> dict[str, Any]:
    call = getattr(client, method, None)
    return call(token) if callable(call) else _empty_report()


def _account_summary(
    profile: str,
    domestic_balance: dict[str, Any],
    domestic_deposit: dict[str, Any],
    overseas_deposit: dict[str, Any],
    overseas_currency: dict[str, Any],
    overseas_valuation: dict[str, Any],
    *,
    updated_at: str,
) -> dict[str, Any]:
    domestic = domestic_balance.get("summary", {})
    cash = domestic_deposit.get("summary", {})
    usd_cash = _currency_item(overseas_deposit)
    usd_assets = _currency_item(overseas_currency)
    usd_profit = _currency_item(overseas_valuation)
    return {
        "account_profile": profile,
        "account_name": _account_name(profile),
        "display_name": ACCOUNT_NAMES.get(profile, profile),
        "updated_at": updated_at,
        "read_only": True,
        "domestic": {
            "currency": "KRW",
            "cash": _number(cash.get("entr")),
            "withdrawable_cash": _number(cash.get("pymn_alow_amt")),
            "orderable_cash": _number(cash.get("ord_alow_amt")),
            "d2_cash": _number(cash.get("d2_entra")),
            "purchase_amount": _number(domestic.get("tot_pur_amt"), absolute=True),
            "evaluation_amount": _number(domestic.get("tot_evlt_amt"), absolute=True),
            "profit_loss": _number(domestic.get("tot_evlt_pl")),
            "profit_loss_pct": _number(domestic.get("tot_prft_rt")),
            "estimated_assets": _number(domestic.get("prsm_dpst_aset_amt"), absolute=True),
        },
        "overseas": {
            "currency": "USD",
            "cash": _number(usd_cash.get("fc_entra")),
            "withdrawable_cash": _number(usd_cash.get("fc_pymn_alowa")),
            "orderable_cash": _number(usd_cash.get("fc_ord_alowa")),
            "evaluation_amount": _number(usd_assets.get("evlt_amt"), absolute=True),
            "profit_loss": _number(usd_profit.get("pl_amt")),
            "profit_loss_pct": _number(usd_profit.get("pl_rt")),
            "exchange_rate": _number(usd_assets.get("crnc_rt"), absolute=True),
            "cash_krw": _number(usd_assets.get("chg_entr")),
            "evaluation_amount_krw": _number(usd_assets.get("chg_evlt_amt"), absolute=True),
            "profit_loss_krw": _number(usd_profit.get("chg_profit_amt")),
            "estimated_assets_krw": _number(
                overseas_currency.get("summary", {}).get("aset_evlt_amt"),
                absolute=True,
            ),
        },
    }


def _load_kiwoom_portfolio(
    *,
    force: bool = False,
    client_factory: Callable[[KiwoomConfig], KiwoomReadOnlyClient] = KiwoomReadOnlyClient,
) -> dict[str, list[dict[str, Any]]]:
    """계좌별 주식·현금·평가 요약을 조회하고 메모리에만 캐시한다."""
    try:
        cache_seconds = max(0, int(os.getenv("KIWOOM_BALANCE_CACHE_SECONDS", "60")))
    except ValueError:
        cache_seconds = 60
    now = time.monotonic()
    if not force and _BALANCE_CACHE["expires_at"] > now:
        return {
            "rows": list(_BALANCE_CACHE["rows"]),
            "accounts": list(_BALANCE_CACHE["accounts"]),
        }

    with _LOCK:
        now = time.monotonic()
        if not force and _BALANCE_CACHE["expires_at"] > now:
            return {
                "rows": list(_BALANCE_CACHE["rows"]),
                "accounts": list(_BALANCE_CACHE["accounts"]),
            }
        rows: list[dict[str, Any]] = []
        accounts: list[dict[str, Any]] = []
        updated_at = datetime.now(timezone.utc).isoformat()
        for profile, config in load_profile_configs().items():
            client = client_factory(config)
            token = _get_token(profile, config, client)
            balance = client.get_account_balance(token)
            domestic_deposit = _optional_report(client, "get_domestic_deposit", token)
            overseas_balance = _safe_overseas_call(
                lambda: client.get_overseas_account_balance(token)
            )
            overseas_deposit = _safe_overseas_call(
                lambda: _optional_report(client, "get_overseas_deposit", token)
            )
            overseas_currency = _safe_overseas_call(
                lambda: _optional_report(client, "get_overseas_currency_valuation", token)
            )
            overseas_valuation = _safe_overseas_call(
                lambda: _optional_report(client, "get_overseas_valuation", token)
            )
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
            accounts.append(
                _account_summary(
                    profile,
                    balance,
                    domestic_deposit,
                    overseas_deposit,
                    overseas_currency,
                    overseas_valuation,
                    updated_at=updated_at,
                )
            )
        rows.sort(
            key=lambda item: (
                item["account_profile"],
                item["market"],
                item["ticker"],
            )
        )
        _BALANCE_CACHE.update(
            expires_at=now + cache_seconds,
            rows=rows,
            accounts=accounts,
        )
        return {"rows": list(rows), "accounts": list(accounts)}


def get_kiwoom_holdings(
    *,
    force: bool = False,
    client_factory: Callable[[KiwoomConfig], KiwoomReadOnlyClient] = KiwoomReadOnlyClient,
) -> list[dict[str, Any]]:
    """기존 WEB 호환용 실계좌 종목 행을 반환한다."""
    return _load_kiwoom_portfolio(force=force, client_factory=client_factory)["rows"]


def get_kiwoom_account_summaries(
    *,
    force: bool = False,
    client_factory: Callable[[KiwoomConfig], KiwoomReadOnlyClient] = KiwoomReadOnlyClient,
) -> list[dict[str, Any]]:
    """계좌별 원화·달러 현금과 국내·해외 평가손익을 반환한다."""
    return _load_kiwoom_portfolio(force=force, client_factory=client_factory)["accounts"]


def clear_kiwoom_cache() -> None:
    """테스트 및 강제 새로고침용 캐시 초기화."""
    with _LOCK:
        _BALANCE_CACHE.update(expires_at=0.0, rows=[], accounts=[])
        _TOKEN_CACHE.clear()
