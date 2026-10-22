"""스캐너 후보 지정가 매수 API: 미국 → 자유투자(account1), 국내 → ISA(account4).

설계와 운영 설정은 docs/scanner_buy_orders.md.
후보 정보(종목·시장·손절가·Strict 결과)는 브라우저 값이 아니라 저장된 스캔 결과에서 읽는다.
"""

from __future__ import annotations

import logging
import math
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database.models import ScanResult, get_db
from trading.scanner_buy_orders import (
    MARKETS,
    Account1LiveBroker,
    Account4LiveBroker,
    ScannerBuyBlocked,
    ScannerBuyOrders,
)
from trading.kiwoom_readonly import KiwoomConfig, KiwoomReadOnlyClient
from web.kiwoom_holdings import _get_token


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/scanner-orders", tags=["scanner-orders"])

ENABLE_FLAG_BY_MARKET = {"US": "ACCOUNT1_ORDER_ENABLED", "KR": "ACCOUNT4_ORDER_ENABLED"}
BROKER_BY_MARKET = {"US": Account1LiveBroker, "KR": Account4LiveBroker}


class BuyPreviewRequest(BaseModel):
    result_id: int = Field(ge=1)
    quantity: int = Field(ge=1, le=1_000_000)
    limit_price: float = Field(gt=0)


class BuyConfirmRequest(BaseModel):
    confirmation_ticker: str = Field(min_length=1, max_length=12)


class BuyResolveRequest(BuyConfirmRequest):
    note: str = Field(default="", max_length=200)


def _flag(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() == "true"


def _journal_path() -> str:
    return os.getenv("SCANNER_ORDER_JOURNAL_PATH", "").strip()


def disabled_reason(market: str) -> str | None:
    spec = MARKETS[market]
    if not (_flag("KIWOOM_WEB_ENABLED") and _flag("KIWOOM_TRADING_ENABLED")
            and _flag(ENABLE_FLAG_BY_MARKET[market]) and _journal_path()):
        return (f"{spec['account_name']}({spec['account_profile']}) 주문 전송이 꺼져 있습니다. "
                f"KIWOOM_TRADING_ENABLED와 {ENABLE_FLAG_BY_MARKET[market]}가 모두 true이고 "
                "SCANNER_ORDER_JOURNAL_PATH가 설정되어야 합니다.")
    return None


def _reserve_bps() -> int:
    return int(os.getenv("SCANNER_ORDER_RESERVE_BPS", "100"))


def _broker(market: str):
    profile = MARKETS[market]["account_profile"]
    config = KiwoomConfig.from_profile(profile)
    client = KiwoomReadOnlyClient(config)
    return BROKER_BY_MARKET[market](config, client=client,
                                    token=_get_token(profile, config, client),
                                    reserve_bps=_reserve_bps())


def _service() -> ScannerBuyOrders:
    if not _flag("KIWOOM_WEB_ENABLED"):
        raise HTTPException(status_code=503, detail="키움 실계좌 연결(KIWOOM_WEB_ENABLED)이 꺼져 있습니다.")
    path = _journal_path()
    if not path:
        raise HTTPException(status_code=503, detail="주문 저널 경로(SCANNER_ORDER_JOURNAL_PATH)가 설정되지 않았습니다.")
    return ScannerBuyOrders(path, _broker)


def _view(row: dict) -> dict:
    reason = disabled_reason(row["market"])
    return {**row, "account_name": MARKETS[row["market"]]["account_name"],
            "execution_enabled": reason is None, "execution_block_reason": reason}


def _run(action):
    try:
        return action()
    except ScannerBuyBlocked as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        # 증권사 예외에는 계좌·토큰 정보가 섞일 수 있어 그대로 돌려주지 않는다.
        logger.warning("스캐너 매수 API 실패: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="키움 조회에 실패했습니다. 주문을 보내지 않았습니다.") from exc


def _market(value: str) -> str:
    market = value.strip().upper()
    if market not in MARKETS:
        raise HTTPException(status_code=422, detail="market은 US 또는 KR이어야 합니다.")
    return market


@router.get("/capabilities")
def capabilities(market: str):
    market = _market(market)
    spec, reason = MARKETS[market], disabled_reason(market)
    return {"market": market, "account_profile": spec["account_profile"],
            "account_name": spec["account_name"], "currency": spec["currency"],
            "order_type": "LIMIT", "execution_enabled": reason is None,
            "reason": reason or f"{spec['account_name']}({spec['account_profile']}) 실계좌 · 사용자 확인 후 1건씩 전송"}


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
    service = _service()
    row = _run(lambda: service.status(order_id))
    reason = disabled_reason(row["market"])
    if reason:
        raise HTTPException(status_code=503, detail=reason)
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
def recent_buys(market: str = "", ticker: str = "", limit: int = 20):
    market = _market(market) if market else None
    service = _service()
    rows = _run(lambda: service.recent(market, ticker or None, max(1, min(limit, 100))))
    return [_view(row) for row in rows]
