"""스캐너 후보 → 자유투자(account1) 미국주식 지정가 매수 API.

설계와 운영 설정은 docs/account1_scanner_buy.md. 국내(account4) 주문은 연결하지 않는다.
후보 정보(종목·손절가·Strict 결과)는 브라우저 값이 아니라 저장된 스캔 결과에서 읽는다.
"""

from __future__ import annotations

import logging
import math
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database.models import ScanResult, get_db
from trading.account1_buy_orders import (
    Account1BuyBlocked,
    Account1BuyOrders,
    Account1LiveBroker,
)
from trading.kiwoom_readonly import KiwoomConfig, KiwoomReadOnlyClient
from web.kiwoom_holdings import _get_token


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/scanner-orders", tags=["scanner-orders"])

DISABLED_REASON = (
    "자유투자(account1) 주문 전송이 꺼져 있습니다. KIWOOM_TRADING_ENABLED와 "
    "ACCOUNT1_ORDER_ENABLED가 모두 true여야 합니다."
)


class BuyPreviewRequest(BaseModel):
    result_id: int = Field(ge=1)
    quantity: int = Field(ge=1, le=100_000)
    limit_price: float = Field(gt=0)


class BuyConfirmRequest(BaseModel):
    confirmation_ticker: str = Field(min_length=1, max_length=12)


class BuyResolveRequest(BuyConfirmRequest):
    note: str = Field(default="", max_length=200)


def _flag(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() == "true"


def execution_enabled() -> bool:
    return (_flag("KIWOOM_WEB_ENABLED") and _flag("KIWOOM_TRADING_ENABLED")
            and _flag("ACCOUNT1_ORDER_ENABLED")
            and bool(os.getenv("ACCOUNT1_ORDER_JOURNAL_PATH", "").strip()))


def _reserve_bps() -> int:
    return int(os.getenv("ACCOUNT1_ORDER_RESERVE_BPS", "100"))


def _broker():
    config = KiwoomConfig.from_profile("account1")
    client = KiwoomReadOnlyClient(config)
    return Account1LiveBroker(config, client=client,
                              token=_get_token("account1", config, client),
                              reserve_bps=_reserve_bps())


def _service() -> Account1BuyOrders:
    if not _flag("KIWOOM_WEB_ENABLED"):
        raise HTTPException(status_code=503, detail="키움 실계좌 연결(KIWOOM_WEB_ENABLED)이 꺼져 있습니다.")
    path = os.getenv("ACCOUNT1_ORDER_JOURNAL_PATH", "").strip()
    if not path:
        raise HTTPException(status_code=503, detail="account1 주문 저널 경로(ACCOUNT1_ORDER_JOURNAL_PATH)가 설정되지 않았습니다.")
    return Account1BuyOrders(path, _broker)


def _view(row: dict) -> dict:
    return {**row, "account_profile": "account1", "account_name": "자유투자",
            "execution_enabled": execution_enabled(),
            "execution_block_reason": None if execution_enabled() else DISABLED_REASON}


def _run(action):
    try:
        return action()
    except Account1BuyBlocked as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        # 증권사 예외에는 계좌·토큰 정보가 섞일 수 있어 그대로 돌려주지 않는다.
        logger.warning("account1 주문 API 실패: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="키움 조회에 실패했습니다. 주문을 보내지 않았습니다.") from exc


@router.get("/capabilities")
def capabilities():
    enabled = execution_enabled()
    return {"account_profile": "account1", "markets": ["US"], "order_type": "LIMIT",
            "execution_enabled": enabled,
            "reason": "자유투자(account1) 실계좌 · 사용자 확인 후 1건씩 전송" if enabled else DISABLED_REASON}


@router.post("/buy/preview")
def preview_buy(body: BuyPreviewRequest, db: Session = Depends(get_db)):
    if not math.isfinite(body.limit_price):
        raise HTTPException(status_code=422, detail="지정가는 유한한 숫자여야 합니다.")
    row = db.query(ScanResult).filter(ScanResult.id == body.result_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="스캔 결과를 찾을 수 없습니다.")
    candidate = {"id": row.id, "market": row.market, "ticker": row.ticker, "name": row.name,
                 "stop_loss": row.stop_loss, "strict_filter_passed": row.strict_filter_passed}
    service = _service()
    return _view(_run(lambda: service.prepare(candidate, body.quantity, body.limit_price)))


@router.post("/buy/{order_id}/execute")
def execute_buy(order_id: str, body: BuyConfirmRequest):
    if not execution_enabled():
        raise HTTPException(status_code=503, detail=DISABLED_REASON)
    service = _service()
    return _view(_run(lambda: service.execute(order_id, body.confirmation_ticker)))


@router.post("/buy/{order_id}/refresh")
def refresh_buy(order_id: str):
    service = _service()
    return _view(_run(lambda: service.refresh(order_id)))


@router.post("/buy/{order_id}/resolve")
def resolve_buy(order_id: str, body: BuyResolveRequest):
    service = _service()
    return _view(_run(lambda: service.resolve(order_id, body.confirmation_ticker, body.note)))


@router.get("/buy")
def recent_buys(ticker: str = "", limit: int = 20):
    service = _service()
    return [_view(row) for row in _run(lambda: service.recent(ticker or None, max(1, min(limit, 100))))]
