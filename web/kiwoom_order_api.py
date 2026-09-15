"""퀀트투자(account2) 미국 ETF 지정가 매도용 2단계 API."""

from __future__ import annotations

import secrets
import threading
import time
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from trading.allocation_rebalance import LIVE_BLOCK_REASON
from trading.asset_allocation_universe import ETF_EXCHANGES
from trading.kiwoom_readonly import KiwoomError, KiwoomReadOnlyClient, load_profile_configs
from web.kiwoom_holdings import (
    _get_token,
    get_kiwoom_account_summaries,
    get_kiwoom_holdings,
)


router = APIRouter(prefix="/api/kiwoom/orders", tags=["kiwoom-orders"])
_lock = threading.Lock()
_previews: dict[str, dict[str, Any]] = {}
PREVIEW_TTL_SECONDS = 300
US_EXCHANGE_BY_TICKER = ETF_EXCHANGES


class SellPreviewRequest(BaseModel):
    ticker: str = Field(min_length=1, max_length=12)
    quantity: int = Field(ge=1)
    limit_price: float = Field(gt=0)


class SellExecuteRequest(BaseModel):
    preview_id: str = Field(min_length=20, max_length=200)
    confirmation_ticker: str = Field(min_length=1, max_length=12)


class BuyPreviewRequest(BaseModel):
    ticker: str = Field(min_length=1, max_length=12)
    quantity: int = Field(ge=1)
    limit_price: float = Field(gt=0)


class BuyExecuteRequest(BaseModel):
    preview_id: str = Field(min_length=20, max_length=200)
    confirmation_ticker: str = Field(min_length=1, max_length=12)


def _execution_enabled() -> bool:
    # A flag cannot supply missing reconciliation evidence. Legacy execution
    # has no durable cycle identity and must not bypass the new workflow.
    return False


def _exchange_code(value: str) -> str:
    normalized = value.strip().upper()
    if "NASDAQ" in normalized or normalized == "ND":
        return "ND"
    if "NYSE" in normalized or normalized == "NY":
        return "NY"
    if "AMEX" in normalized or normalized == "NA":
        return "NA"
    raise HTTPException(status_code=422, detail="거래소를 확인할 수 없습니다.")


def _find_holding(ticker: str) -> dict[str, Any]:
    wanted = ticker.strip().upper()
    for holding in get_kiwoom_holdings(force=True):
        if (
            holding.get("account_profile") == "account2"
            and holding.get("market") == "US"
            and str(holding.get("ticker", "")).upper() == wanted
        ):
            return holding
    raise HTTPException(status_code=404, detail="퀀트투자 계좌 보유종목이 아닙니다.")


def _holding_exchange(holding: dict[str, Any]) -> str:
    try:
        return _exchange_code(str(holding.get("exchange") or ""))
    except HTTPException:
        ticker = str(holding.get("ticker") or "").upper()
        exchange = US_EXCHANGE_BY_TICKER.get(ticker)
        if exchange:
            return exchange
        raise


def _allocation_exchange(ticker: str) -> str:
    normalized = ticker.strip().upper()
    exchange = US_EXCHANGE_BY_TICKER.get(normalized)
    if not exchange:
        raise HTTPException(status_code=422, detail="자산배분 대상 ETF가 아닙니다.")
    return exchange


def _account2_orderable_cash() -> float:
    summary = next(
        (row for row in get_kiwoom_account_summaries(force=True)
         if row.get("account_profile") == "account2"),
        None,
    )
    if not summary:
        raise HTTPException(status_code=502, detail="퀀트투자 계좌의 주문가능 현금을 확인하지 못했습니다.")
    return max(0.0, float((summary.get("overseas") or {}).get("orderable_cash") or 0))


@router.get("/sell/quote")
async def quote_sell(ticker: str):
    """매도 모달을 열 때 account2 보유종목의 키움 현재가를 다시 조회한다."""
    holding = _find_holding(ticker)
    exchange = _holding_exchange(holding)
    try:
        config = load_profile_configs()["account2"]
        client = KiwoomReadOnlyClient(config)
        token = _get_token("account2", config, client)
        quote = client.get_overseas_quote(
            token, exchange=exchange, ticker=str(holding["ticker"]).upper()
        )["quote"]
        current_price = abs(float(str(quote.get("cur_prc") or "0").replace(",", "")))
        if current_price <= 0:
            raise ValueError("empty price")
    except (KeyError, KiwoomError, TypeError, ValueError):
        raise HTTPException(status_code=502, detail="키움 현재가를 확인하지 못했습니다.")
    return {
        "ticker": str(holding["ticker"]).upper(),
        "current_price": current_price,
        "exchange": exchange,
        "source": "KIWOOM_USA20100",
        "read_only": True,
    }


@router.get("/buy/quote")
async def quote_buy(ticker: str):
    """자산배분 ETF 매수 모달을 열 때 키움 현재가를 다시 조회한다."""
    ticker = ticker.strip().upper()
    exchange = _allocation_exchange(ticker)
    try:
        config = load_profile_configs()["account2"]
        client = KiwoomReadOnlyClient(config)
        token = _get_token("account2", config, client)
        quote = client.get_overseas_quote(token, exchange=exchange, ticker=ticker)["quote"]
        current_price = abs(float(str(quote.get("cur_prc") or "0").replace(",", "")))
        if current_price <= 0:
            raise ValueError("empty price")
    except (KeyError, KiwoomError, TypeError, ValueError):
        raise HTTPException(status_code=502, detail="키움 현재가를 확인하지 못했습니다.")
    return {
        "ticker": ticker, "current_price": current_price, "exchange": exchange,
        "source": "KIWOOM_USA20100", "read_only": True,
    }


@router.post("/buy/preview")
async def preview_buy(body: BuyPreviewRequest):
    ticker = body.ticker.strip().upper()
    exchange = _allocation_exchange(ticker)
    price = Decimal(str(body.limit_price))
    if price != price.quantize(Decimal("0.01")):
        raise HTTPException(status_code=422, detail="미국주식 지정가는 소수점 둘째 자리까지만 입력할 수 있습니다.")
    estimated_cost = body.quantity * body.limit_price
    cash = _account2_orderable_cash()
    if estimated_cost > cash + 1e-9:
        raise HTTPException(status_code=422, detail="예상 매수금액이 주문가능 현금을 초과합니다.")
    preview_id = secrets.token_urlsafe(32)
    preview = {
        "side": "BUY", "ticker": ticker, "quantity": body.quantity,
        "limit_price": body.limit_price, "estimated_cost": estimated_cost,
        "orderable_cash": cash, "exchange": exchange,
        "expires_at": time.time() + PREVIEW_TTL_SECONDS,
    }
    with _lock:
        _previews[preview_id] = preview
    return {
        **preview, "preview_id": preview_id,
        "expires_in_seconds": PREVIEW_TTL_SECONDS,
        "execution_block_reason": LIVE_BLOCK_REASON,
        "execution_enabled": _execution_enabled(), "order_type": "LIMIT",
    }


@router.post("/buy/execute")
async def execute_buy(body: BuyExecuteRequest):
    raise HTTPException(status_code=503, detail=LIVE_BLOCK_REASON)


@router.post("/sell/preview")
async def preview_sell(body: SellPreviewRequest):
    price = Decimal(str(body.limit_price))
    if price != price.quantize(Decimal("0.01")):
        raise HTTPException(status_code=422, detail="미국주식 지정가는 소수점 둘째 자리까지만 입력할 수 있습니다.")
    holding = _find_holding(body.ticker)
    held_quantity = int(float(holding.get("quantity") or 0))
    if body.quantity > held_quantity:
        raise HTTPException(status_code=422, detail="매도수량이 보유수량을 초과합니다.")
    preview_id = secrets.token_urlsafe(32)
    preview = {
        "side": "SELL",
        "ticker": str(holding["ticker"]).upper(),
        "name": holding.get("name") or holding["ticker"],
        "quantity": body.quantity,
        "held_quantity": held_quantity,
        "limit_price": body.limit_price,
        "exchange": _holding_exchange(holding),
        "expires_at": time.time() + PREVIEW_TTL_SECONDS,
    }
    with _lock:
        _previews[preview_id] = preview
    return {
        **preview,
        "preview_id": preview_id,
        "expires_in_seconds": PREVIEW_TTL_SECONDS,
        "execution_block_reason": LIVE_BLOCK_REASON,
        "execution_enabled": _execution_enabled(),
        "order_type": "LIMIT",
    }


@router.post("/sell/execute")
async def execute_sell(body: SellExecuteRequest):
    raise HTTPException(status_code=503, detail=LIVE_BLOCK_REASON)
