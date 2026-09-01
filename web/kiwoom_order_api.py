"""퀀트투자(account2) 미국 ETF 지정가 매도용 2단계 API."""

from __future__ import annotations

import os
import secrets
import threading
import time
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from trading.kiwoom_orders import KiwoomOrderClient
from trading.kiwoom_readonly import KiwoomError, KiwoomReadOnlyClient, load_profile_configs
from web.kiwoom_holdings import _get_token, get_kiwoom_holdings


router = APIRouter(prefix="/api/kiwoom/orders", tags=["kiwoom-orders"])
_lock = threading.Lock()
_previews: dict[str, dict[str, Any]] = {}
PREVIEW_TTL_SECONDS = 300
US_EXCHANGE_BY_TICKER = {
    "AGG": "NY", "BIL": "NY", "EFA": "NY", "GLD": "NY",
    "IEF": "ND", "IEMG": "NY", "LQD": "ND", "QQQ": "ND",
    "SHY": "ND", "SPY": "NY", "VTV": "NY",
}


class SellPreviewRequest(BaseModel):
    ticker: str = Field(min_length=1, max_length=12)
    quantity: int = Field(ge=1)
    limit_price: float = Field(gt=0)


class SellExecuteRequest(BaseModel):
    preview_id: str = Field(min_length=20, max_length=200)
    confirmation_ticker: str = Field(min_length=1, max_length=12)


def _execution_enabled() -> bool:
    return os.getenv("KIWOOM_TRADING_ENABLED", "false").lower() == "true"


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


@router.post("/sell/preview")
async def preview_sell(body: SellPreviewRequest):
    holding = _find_holding(body.ticker)
    held_quantity = int(float(holding.get("quantity") or 0))
    if body.quantity > held_quantity:
        raise HTTPException(status_code=422, detail="매도수량이 보유수량을 초과합니다.")
    preview_id = secrets.token_urlsafe(32)
    preview = {
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
        "execution_enabled": _execution_enabled(),
        "order_type": "LIMIT",
    }


@router.post("/sell/execute")
async def execute_sell(body: SellExecuteRequest):
    if not _execution_enabled():
        raise HTTPException(status_code=503, detail="실계좌 주문 기능이 비활성화되어 있습니다.")
    with _lock:
        preview = _previews.pop(body.preview_id, None)
    if not preview or preview["expires_at"] < time.time():
        raise HTTPException(status_code=410, detail="주문 확인이 만료되었습니다. 다시 확인해 주세요.")
    if body.confirmation_ticker.strip().upper() != preview["ticker"]:
        raise HTTPException(status_code=422, detail="확인용 종목코드가 일치하지 않습니다.")
    holding = _find_holding(preview["ticker"])
    if preview["quantity"] > int(float(holding.get("quantity") or 0)):
        raise HTTPException(status_code=409, detail="보유수량이 변경되어 주문을 중단했습니다.")
    try:
        config = load_profile_configs()["account2"]
        token = str(KiwoomReadOnlyClient(config).issue_token()["token"])
        result = KiwoomOrderClient(config).sell_us_limit(
            token,
            exchange=preview["exchange"],
            ticker=preview["ticker"],
            quantity=preview["quantity"],
            price=preview["limit_price"],
        )
    except KeyError:
        raise HTTPException(status_code=502, detail="키움 매도 주문 설정을 확인하지 못했습니다.")
    except KiwoomError as exc:
        # KiwoomError contains the broker's rejection message, not credentials.
        # Returning it lets the user correct price, quantity, session, or account
        # restrictions instead of seeing an opaque 502 error.
        raise HTTPException(status_code=502, detail=str(exc))
    return {"status": "submitted", **result}
