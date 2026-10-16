"""보유종목 화면의 매수·매도 주문 미리보기 API.

현재 단계는 주문 내용을 계산·검증해 보여주기만 한다. 증권사 주문 전송,
미리보기 저장, 실행 엔드포인트는 두지 않는다. 실제 전송은 체결·현금 대조
검증이 끝난 뒤 별도 단계에서 연결한다.
"""

from __future__ import annotations

import math
import os
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database.models import Holding, get_db
from trading.asset_allocation_universe import ETF_EXCHANGES
from web.kiwoom_holdings import get_kiwoom_holdings


router = APIRouter(prefix="/api/holding-orders", tags=["holding-orders"])

EXECUTION_BLOCK_REASON = (
    "보유종목 주문 전송은 아직 연결되지 않았습니다. 이 화면은 주문 미리보기만 "
    "제공하며 증권사로 주문을 보내지 않습니다."
)
PRICE_DEVIATION_WARN_PCT = 10.0


class HoldingOrderPreviewRequest(BaseModel):
    holding_id: str = Field(min_length=1, max_length=80)
    side: Literal["BUY", "SELL"]
    quantity: int = Field(ge=1, le=10_000_000)
    limit_price: float = Field(gt=0)


def _kiwoom_enabled() -> bool:
    return os.getenv("KIWOOM_WEB_ENABLED", "false").lower() == "true"


def kr_tick_size(price: float) -> int:
    """KRX 주식 호가단위(2023-01 개편 기준). ETF/ETN은 별도 규칙이다."""
    for upper, tick in ((2_000, 1), (5_000, 5), (20_000, 10), (50_000, 50),
                        (200_000, 100), (500_000, 500)):
        if price < upper:
            return tick
    return 1_000


def _find_holding(holding_id: str, db: Session) -> dict[str, Any]:
    if holding_id.startswith("kiwoom:"):
        if not _kiwoom_enabled():
            raise HTTPException(status_code=404, detail="보유종목을 찾을 수 없습니다.")
        try:
            rows = get_kiwoom_holdings()
        except Exception:
            raise HTTPException(status_code=502, detail="실계좌 보유종목을 조회하지 못했습니다.") from None
        for row in rows:
            if row.get("id") == holding_id:
                return row
        raise HTTPException(status_code=404, detail="보유종목을 찾을 수 없습니다.")

    if not holding_id.isdigit():
        raise HTTPException(status_code=422, detail="보유종목 ID 형식이 올바르지 않습니다.")
    h = db.query(Holding).filter(
        Holding.id == int(holding_id), Holding.is_active == True,  # noqa: E712
    ).first()
    if not h:
        raise HTTPException(status_code=404, detail="보유종목을 찾을 수 없습니다.")
    account = h.account
    return {
        "id": h.id,
        "ticker": h.ticker,
        "name": h.name,
        "market": h.market,
        "quantity": h.quantity,
        "current_price": h.current_price,
        "account_name": account.name if account else "",
        "account_profile": None,
        "source": "manual",
    }


@router.post("/preview")
def preview_holding_order(body: HoldingOrderPreviewRequest, db: Session = Depends(get_db)):
    """주문 내용을 검증해 돌려준다. 저장·전송하지 않으며 실행 경로가 없다."""
    holding = _find_holding(body.holding_id.strip(), db)
    market = str(holding.get("market") or "").upper()
    if market not in ("KR", "US"):
        raise HTTPException(status_code=422, detail="지원하지 않는 시장입니다.")
    if not math.isfinite(body.limit_price):
        raise HTTPException(status_code=422, detail="지정가는 유한한 숫자여야 합니다.")

    price = Decimal(str(body.limit_price))
    warnings: list[str] = []
    if market == "US":
        if price != price.quantize(Decimal("0.01")):
            raise HTTPException(status_code=422, detail="미국주식 지정가는 소수점 둘째 자리까지만 입력할 수 있습니다.")
    else:
        if price != price.to_integral_value():
            raise HTTPException(status_code=422, detail="국내주식 지정가는 원 단위 정수로 입력해야 합니다.")
        tick = kr_tick_size(body.limit_price)
        if int(price) % tick:
            warnings.append(f"주식 호가단위({tick:,}원)에 맞지 않습니다. ETF·ETN이면 호가단위가 다를 수 있습니다.")

    held_quantity = int(float(holding.get("quantity") or 0))
    if body.side == "SELL" and body.quantity > held_quantity:
        raise HTTPException(status_code=422, detail="매도수량이 보유수량을 초과합니다.")
    quantity_after = held_quantity + body.quantity if body.side == "BUY" else held_quantity - body.quantity

    current_price = float(holding.get("current_price") or 0) or None
    if current_price:
        deviation = (body.limit_price - current_price) / current_price * 100
        if abs(deviation) >= PRICE_DEVIATION_WARN_PCT:
            warnings.append(f"지정가가 현재가 대비 {deviation:+.1f}% 떨어져 있습니다.")
    else:
        warnings.append("현재가 정보가 없어 지정가를 비교하지 못했습니다.")

    ticker = str(holding.get("ticker") or "").upper()
    if holding.get("account_profile") == "account2" and ticker in ETF_EXCHANGES:
        warnings.append("자산배분(차액 재분배) 관리 ETF입니다. 리밸런싱 사이클 밖의 주문은 목표 비중을 어긋나게 합니다.")

    return {
        "holding_id": holding["id"],
        "side": body.side,
        "ticker": ticker,
        "name": holding.get("name") or ticker,
        "market": market,
        "currency": "USD" if market == "US" else "KRW",
        "account_name": holding.get("account_name") or "",
        "source": holding.get("source") or "manual",
        "order_type": "LIMIT",
        "quantity": body.quantity,
        "limit_price": body.limit_price,
        "estimated_amount": round(body.quantity * body.limit_price, 2),
        "held_quantity": held_quantity,
        "quantity_after": quantity_after,
        "current_price": current_price,
        "warnings": warnings,
        "execution_enabled": False,
        "execution_block_reason": EXECUTION_BLOCK_REASON,
    }
