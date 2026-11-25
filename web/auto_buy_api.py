"""AUTO BUY MONITOR API (docs/auto_buy_shadow.md). 조회 전용 GET 만 있다.

주문·취소·설정 변경·상태 초기화 엔드포인트는 없다. 저널은 읽기 전용(mode=ro)으로 열어 파일·테이블을 만들지 않는다.
/api/* 인증은 web/app.py 의 SITES_API_KEY 를 따른다.
"""
from __future__ import annotations

import logging
import math
import re

from fastapi import APIRouter, HTTPException, Query, Response

from trading.auto_buy.shadow import report

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auto-buy", tags=["auto-buy"])


def _clean(x):
    """JSON 에 쓸 수 없는 NaN/inf 를 None 으로."""
    if isinstance(x, float) and not math.isfinite(x):
        return None
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    return x


def _safe(fn, *a):
    try:
        return _clean(fn(*a))
    except Exception as exc:
        logger.warning("자동매수 SHADOW 조회 실패: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="자동매수 SHADOW 기록을 읽지 못했습니다.") from exc


@router.get("/health")
def health(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return _safe(report.health)


@router.get("/summary")
def summary(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return _safe(report.summary)


@router.get("/accounts/{account_id}")
def account(account_id: str, response: Response):
    if not re.fullmatch(r"[A-Za-z0-9:_]{3,40}", account_id):
        raise HTTPException(status_code=400, detail="계좌 ID 형식이 아닙니다.")
    response.headers["Cache-Control"] = "no-store"
    out = _safe(report.account_detail, account_id)
    if out is None:
        raise HTTPException(status_code=404, detail="가상 계좌가 없거나 저널이 준비되지 않았습니다.")
    return out


@router.get("/signals")
def signals(response: Response, day: str = Query(default="", pattern=r"^(\d{4}-\d{2}-\d{2})?$")):
    response.headers["Cache-Control"] = "no-store"
    out = _safe(report.signals, day or None)
    if out is None:
        raise HTTPException(status_code=404, detail="저널이 준비되지 않았습니다.")
    return out
