"""Narrow, brokerage-read-only projection for the portfolio MCP."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException

from web.kiwoom_holdings import (
    ACCOUNT_MARKETS,
    get_kiwoom_account_summaries,
    get_kiwoom_holdings,
)


router = APIRouter(prefix="/api/portfolio-read", tags=["portfolio-read"])
Account = Literal["ALL", "account1", "account2", "account4"]


def _number(value: Any) -> float:
    try:
        result = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _account_projection(row: dict[str, Any]) -> dict[str, Any]:
    profile = str(row.get("account_profile") or "")
    allowed = ACCOUNT_MARKETS.get(profile, frozenset())
    result: dict[str, Any] = {
        "account_profile": profile,
        "account_name": str(row.get("account_name") or profile),
        "updated_at": row.get("updated_at"),
        "read_only": True,
        "markets": [],
    }
    if "KR" in allowed:
        source = row.get("domestic") or {}
        result["markets"].append({
            "market": "KR",
            "currency": "KRW",
            "cash": _number(source.get("cash")),
            "withdrawable_cash": _number(source.get("withdrawable_cash")),
            "orderable_cash": _number(source.get("orderable_cash")),
            "d2_cash": _number(source.get("d2_cash")),
            "purchase_amount": _number(source.get("purchase_amount")),
            "evaluation_amount": _number(source.get("evaluation_amount")),
            "profit_loss": _number(source.get("profit_loss")),
            "profit_loss_pct": _number(source.get("profit_loss_pct")),
            "estimated_assets": _number(source.get("estimated_assets")),
        })
    if "US" in allowed:
        source = row.get("overseas") or {}
        result["markets"].append({
            "market": "US",
            "currency": "USD",
            "cash": _number(source.get("cash")),
            "withdrawable_cash": _number(source.get("withdrawable_cash")),
            "orderable_cash": _number(source.get("orderable_cash")),
            "evaluation_amount": _number(source.get("evaluation_amount")),
            "profit_loss": _number(source.get("profit_loss")),
            "profit_loss_pct": _number(source.get("profit_loss_pct")),
            "exchange_rate": _number(source.get("exchange_rate")),
            "cash_krw": _number(source.get("cash_krw")),
            "evaluation_amount_krw": _number(source.get("evaluation_amount_krw")),
            "profit_loss_krw": _number(source.get("profit_loss_krw")),
            "estimated_assets_krw": _number(source.get("estimated_assets_krw")),
        })
    return result


def _holding_projection(row: dict[str, Any]) -> dict[str, Any]:
    # Only broker rows enter this endpoint. Do not expose database IDs, memos,
    # order data, credentials, account numbers, or mutable risk fields.
    return {
        "account_profile": str(row.get("account_profile") or ""),
        "account_name": str(row.get("account_name") or ""),
        "market": str(row.get("market") or ""),
        "currency": str(row.get("currency") or ""),
        "ticker": str(row.get("ticker") or ""),
        "name": str(row.get("name") or ""),
        "quantity": _number(row.get("quantity")),
        "avg_price": _number(row.get("avg_price")),
        "current_price": _number(row.get("current_price")),
        "evaluation_amount": _number(row.get("eval_amount")),
        "profit_loss": _number(row.get("profit_loss")),
        "profit_loss_pct": _number(row.get("profit_loss_pct")),
        "evaluation_amount_krw": _number(row.get("eval_amount_krw")),
        "profit_loss_krw": _number(row.get("profit_loss_krw")),
        "price_updated_at": row.get("price_updated_at"),
        "read_only": True,
    }


def _matches(account: str, profile: str) -> bool:
    return account == "ALL" or account == profile


@router.get("/overview")
def portfolio_overview():
    try:
        rows = [_account_projection(row) for row in get_kiwoom_account_summaries()]
    except Exception:
        raise HTTPException(502, "실계좌 요약을 조회하지 못했습니다.") from None
    rows = [row for row in rows if row["account_profile"] in ACCOUNT_MARKETS]
    warnings = []
    for row in rows:
        values = row["markets"][0] if row["markets"] else {}
        fields = ("cash", "evaluation_amount", "estimated_assets", "estimated_assets_krw")
        if values and not any(_number(values.get(field)) for field in fields):
            warnings.append(f'{row["account_profile"]}: 모든 잔고 값이 0입니다. 빈 계좌인지 조회 누락인지 확인하세요.')
    return {
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "accounts": rows,
        "warnings": warnings,
        "source": "kiwoom_brokerage_read_only",
        "coverage": "account1/account2 US and account4 KR; currencies are not implicitly combined",
    }


@router.get("/holdings")
def portfolio_holdings(account: Account = "ALL"):
    try:
        source = get_kiwoom_holdings()
    except Exception:
        raise HTTPException(502, "실계좌 보유종목을 조회하지 못했습니다.") from None
    rows = [
        _holding_projection(row)
        for row in source
        if row.get("source") == "kiwoom"
        and row.get("account_profile") in ACCOUNT_MARKETS
        and _matches(account, str(row.get("account_profile")))
    ]
    if len(rows) > 500:
        raise HTTPException(502, "보유종목 수가 안전 제한을 초과했습니다.")
    rows.sort(key=lambda row: (row["account_profile"], row["market"], row["ticker"]))
    return {
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "account": account,
        "items": rows,
        "count": len(rows),
        "source": "kiwoom_brokerage_read_only",
        "coverage": "current broker holdings snapshot; no manual holdings or order history",
    }
