"""account1 미국 S01 자동 손절 API (docs/auto_stop_sell.md).

/api/* 는 web/app.py 의 SITES_API_KEY 인증을 따른다. 모든 엔드포인트는 동기 함수라 FastAPI 스레드풀에서 실행되며,
이 API 는 주문을 전송하지 않는다 (전송은 스케줄러 tick 만). 일반 상태 초기화 API 는 의도적으로 없다.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database.models import ScanResult, get_db
from trading.auto_stop import service
from trading.auto_stop import settings as S
from trading.auto_stop.brokers import ShadowBroker

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auto-stop", tags=["auto-stop"])


class RegisterRequest(BaseModel):
    holding_id: str = Field(min_length=1, max_length=64)
    scan_result_id: int = Field(ge=1)
    managed_qty: int = Field(ge=1, le=1_000_000)
    mode: str = Field(pattern="^(SHADOW|LIVE)$")
    confirmation_ticker: str = Field(min_length=1, max_length=12)
    acknowledge_stale: bool = False


class TickerConfirm(BaseModel):
    confirmation_ticker: str = Field(min_length=1, max_length=12)


class KillSwitchRequest(BaseModel):
    enabled: bool
    confirmation: str = Field(min_length=1, max_length=10)
    reason: str = Field(default="", max_length=200)


class ResolveRequest(TickerConfirm):
    order_number: str = Field(default="", max_length=20)


def _store():
    from trading.auto_stop.runner import store_or_none
    store = store_or_none()
    if store is None:
        raise HTTPException(status_code=503, detail="주문 저널 경로(SCANNER_ORDER_JOURNAL_PATH)가 설정되지 않았습니다.")
    return store


def _reader():
    from trading.auto_stop.runner import live_reader
    return live_reader(allow_orders=False)


def _run(action):
    try:
        return action()
    except service.Refused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("자동 손절 API 실패: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="처리하지 못했습니다. 상태는 바뀌지 않았을 수 있으니 새로고침 후 확인하세요.") from exc


@router.get("/positions")
def list_positions(response: Response):
    response.headers["Cache-Control"] = "no-store"
    store = _store()
    rows = store.positions()
    for r in rows:
        r["events"] = [e for e in store.events() if e["position_id"] == r["id"]]
        r["orders"] = [o for e in r["events"] for o in store.orders(event_id=e["id"])]
    return {"mode": S.Settings.from_env().mode, "positions": rows}


@router.post("/positions")
def register_position(body: RegisterRequest, db: Session = Depends(get_db)):
    row = db.query(ScanResult).filter(ScanResult.id == body.scan_result_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="스캔 결과를 찾을 수 없습니다.")
    scan = {c: getattr(row, c, None) for c in ("id", "market", "ticker", "name", "signal_type", "signal_date",
                                                "scan_time", "first_detected_at", "price", "pivot_price",
                                                "stop_loss", "grade", "strict_filter_passed")}
    store = _store()
    return _run(lambda: service.register(store, _reader(), scan=scan, holding_id=body.holding_id,
                                         managed_qty=body.managed_qty, mode=body.mode,
                                         confirmation_ticker=body.confirmation_ticker,
                                         acknowledge_stale=body.acknowledge_stale))


@router.post("/positions/{pid}/pause")
def pause_position(pid: int):
    store = _store()
    return _run(lambda: service.set_paused(store, pid, True))


@router.post("/positions/{pid}/resume")
def resume_position(pid: int):
    store = _store()
    return _run(lambda: service.set_paused(store, pid, False))


@router.post("/positions/{pid}/disable")
def disable_position(pid: int):
    store = _store()
    return _run(lambda: service.disable(store, pid))


@router.post("/positions/{pid}/promote")
def promote_position(pid: int, body: TickerConfirm):
    store = _store()
    return _run(lambda: service.promote(store, _reader(), pid, confirmation_ticker=body.confirmation_ticker))


@router.post("/orders/{oid}/resolve")
def resolve_order(oid: int, body: ResolveRequest):
    store = _store()

    def act():
        order = store.order(oid)
        if not order:
            raise service.Refused("주문 기록이 없습니다.")
        reader = _reader()
        broker = reader if order["mode"] == "LIVE" else ShadowBroker(reader, S.Settings.from_env().shadow_fill_model)
        return service.resolve_unknown(store, broker, oid, order_number=body.order_number or None,
                                       confirmation_ticker=body.confirmation_ticker)
    return _run(act)


def _actor(request: Request) -> str:
    # 인증은 app 미들웨어의 SITES_API_KEY Bearer 로 끝난 상태다. 감사 기록에는 접속 출처만 남긴다.
    host = request.client.host if request.client else "unknown"
    return f"web:{host}"


@router.get("/kill-switch")
def get_kill_switch(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return {"kill_switch": _store().kill_switch()}


@router.post("/kill-switch")
def post_kill_switch(body: KillSwitchRequest, request: Request):
    store = _store()
    return _run(lambda: {"kill_switch": service.set_kill_switch(
        store, body.enabled, confirmation=body.confirmation, reason=body.reason, actor=_actor(request))})


@router.get("/report")
def daily_report(response: Response, day: str = Query(default="", pattern=r"^(\d{4}-\d{2}-\d{2})?$"),
                 mode: str = Query(default="", pattern="^(SHADOW|LIVE)?$")):
    response.headers["Cache-Control"] = "no-store"
    import time as _time
    from trading.auto_stop.us_calendar import ny_date
    settings = S.Settings.from_env()
    store = _store()
    mode = mode or (settings.mode if settings.mode != "OFF" else "SHADOW")
    day = day or ny_date(_time.time()).isoformat()
    return _run(lambda: service.daily_report(store, day, mode, settings))


@router.get("/signals")
def list_signals(ticker: str = Query(min_length=1, max_length=12), db: Session = Depends(get_db)):
    """등록용 신호 후보: 저장된 미국 스캔 결과 중 Strict 통과·손절가 있는 행 (DB 조회만)."""
    rows = (db.query(ScanResult)
            .filter(ScanResult.market == "US", ScanResult.ticker == ticker.strip().upper(),
                    ScanResult.strict_filter_passed.is_(True), ScanResult.stop_loss.isnot(None))
            .order_by(ScanResult.scan_time.desc()).limit(10).all())
    return [{"id": r.id, "ticker": r.ticker, "name": r.name, "signal_type": r.signal_type,
             "signal_date": r.signal_date, "stop_loss": r.stop_loss, "price": r.price, "grade": r.grade,
             "scan_time": r.scan_time.isoformat() if r.scan_time else None} for r in rows]


@router.get("/holdings")
def list_holdings(response: Response):
    """account1 미국 보유종목 (실계좌 조회 전용, 주문 없음)."""
    response.headers["Cache-Control"] = "no-store"

    def load():
        holdings = _reader().holdings()
        return [{"holding_id": f"kiwoom:account1:US:{t}", "ticker": t, "qty": h["qty"],
                 "sellable": h["sellable"], "avg_cost": str(h["avg_cost"]) if h.get("avg_cost") else None}
                for t, h in sorted(holdings.items())]
    return _run(load)


@router.get("/health")
def auto_stop_health(response: Response):
    response.headers["Cache-Control"] = "no-store"
    settings = S.Settings.from_env()
    if not settings.journal_path:
        return {"mode": settings.mode, "journal": False,
                "live_order_block_reason": S.live_order_block_reason(settings)}
    store = _store()
    return {"journal": True, **service.health(store, settings),
            "recent_alerts": store.recent_alerts(10)}
