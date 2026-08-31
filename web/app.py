"""
FastAPI 웹 애플리케이션
"""

import json
import logging
import math
import re
import secrets
import threading
from datetime import datetime, timedelta
from typing import Optional, List
from fastapi import FastAPI, Request, Depends, HTTPException, BackgroundTasks, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlalchemy.orm import Session
from pydantic import BaseModel
import pytz
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.models import (init_db, get_db, ScanResult, ScanLog,
                              Account, AccountEquity, Transaction, Holding, WatchList)
from scanner.scan_engine import run_scan, scan_status
from scheduler import start_scheduler, stop_scheduler, get_next_run_times
from notifications.telegram import test_telegram
from config import (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
                    MAX_PIVOT_EXT_PCT, ALERT_MAX_CUR_STOP_PCT)
from web.asset_allocation_api import router as asset_allocation_router
from web.kiwoom_holdings import get_kiwoom_account_summaries, get_kiwoom_holdings
from web.kiwoom_sell_analysis import apply_kiwoom_sell_analysis
from web.kiwoom_sizing import apply_live_position_sizing

logger = logging.getLogger(__name__)
KST = pytz.timezone("Asia/Seoul")
SITES_API_KEY = os.getenv("SITES_API_KEY", "").strip()
KIWOOM_WEB_ENABLED = os.getenv("KIWOOM_WEB_ENABLED", "false").lower() == "true"

app = FastAPI(title="Weinstein Stock Scanner", version="1.0.0")
app.include_router(asset_allocation_router)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

static_dir = os.path.join(BASE_DIR, "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.middleware("http")
async def require_api_auth(request: Request, call_next):
    """Protect every API endpoint except the minimal health check."""
    path = request.url.path
    if not path.startswith("/api/") or path == "/api/health":
        return await call_next(request)

    if not SITES_API_KEY:
        logger.error("SITES_API_KEY is not configured; rejecting API request")
        return JSONResponse(
            status_code=503,
            content={"detail": "API authentication is not configured"},
        )

    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token or not secrets.compare_digest(token, SITES_API_KEY):
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid or missing API credentials"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    return await call_next(request)


@app.get("/api/health")
async def health_check():
    return {"status": "ok"}


@app.on_event("startup")
async def startup_event():
    init_db()
    start_scheduler()
    logger.info("앱 시작 완료")


@app.on_event("shutdown")
async def shutdown_event():
    stop_scheduler()


# ═══════════════════════════════════════════════════════════════
#  페이지
# ═══════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    try:
        # Starlette 1.x: request is the first argument.
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"request": request},
        )
    except TypeError:
        # Starlette bundled with older supported FastAPI releases.
        return templates.TemplateResponse("index.html", {"request": request})


# ═══════════════════════════════════════════════════════════════
#  스캔 API
# ═══════════════════════════════════════════════════════════════

@app.post("/api/scan/start")
async def start_scan(background_tasks: BackgroundTasks, market: str = "ALL", universe: str = "sp500+nasdaq100"):
    if scan_status["is_running"]:
        return JSONResponse({"status": "already_running", "message": "스캔이 이미 진행 중입니다."})

    def _run():
        run_scan(market=market, universe=universe, triggered_by="manual")

    background_tasks.add_task(_run)
    return {"status": "started", "market": market, "message": f"{market} 스캔을 시작했습니다."}


@app.get("/api/scan/status")
async def get_scan_status():
    now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    return {**scan_status, "now_kst": now_kst, "next_schedules": get_next_run_times()}


def _parse_filter_reasons(raw: Optional[str]) -> List[str]:
    """`filter_reasons` 컬럼은 JSON 문자열(plan D5). 파싱 실패 시 빈 리스트."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []


@app.get("/api/results")
async def get_results(market: str = "ALL", signal_type: str = "ALL",
                      days: int = 7, limit: int = 200,
                      include_rejected: bool = False,
                      db: Session = Depends(get_db)):
    """스캔 결과 조회.

    기본값(`include_rejected=False`)은 strict-pass(True) 또는 strict 평가 이전의
    legacy 행(NULL)만 반환한다. `STRICT_PERSIST_REJECTED=True`로 저장된 거부
    신호(strict_filter_passed=False)는 일반 매수 후보로 노출되지 않는다.
    QA·백테스팅에서 거부 신호까지 함께 보려면 `include_rejected=true` opt-in.
    """
    since_str = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    q = db.query(ScanResult).filter(ScanResult.signal_date >= since_str)
    if market != "ALL":
        q = q.filter(ScanResult.market == market)
    if signal_type != "ALL":
        q = q.filter(ScanResult.signal_type == signal_type)
    if not include_rejected:
        # NULL = legacy(strict 도입 이전 또는 strict OFF), True = strict-pass.
        # False(거부)만 제외.
        q = q.filter(or_(
            ScanResult.strict_filter_passed.is_(None),
            ScanResult.strict_filter_passed.is_(True),
        ))
    rows = q.order_by(ScanResult.signal_date.desc(), ScanResult.scan_time.desc()).limit(limit).all()
    payload = [{"id": r.id, "scan_time": r.scan_time.isoformat(),
             "market": r.market, "ticker": r.ticker, "name": r.name,
             "signal_type": r.signal_type, "stage": r.stage,
             "price": r.price, "ma150": r.ma150,
             "volume_ratio": r.volume_ratio, "signal_date": r.signal_date,
             # Strict Weinstein 메타데이터 (Phase 4 P2 + UI Phase)
             "strict_filter_passed": r.strict_filter_passed,
             "filter_reasons": _parse_filter_reasons(r.filter_reasons),
             "sector_name": r.sector_name,
             "sector_stage": r.sector_stage,
             # 부가 메타데이터 (UI 카드 보강용 — 후방 호환 유지)
             "grade": r.grade,
             "signal_quality": r.signal_quality,
             "rs_value": r.rs_value,
             "rs_trend": r.rs_trend,
             "pivot_price": r.pivot_price,
             "stop_loss": r.stop_loss,
             # Step 5 sizing snapshot (nullable for legacy/equity-missing rows)
             "suggested_qty": r.suggested_qty,
             "r_per_share": r.r_per_share,
             "risk_amount": r.risk_amount,
             "position_pct": r.position_pct,
             "sizing_constrained_by": r.sizing_constrained_by,
             "equity_snapshot": r.equity_snapshot}
            for r in rows]
    if KIWOOM_WEB_ENABLED:
        try:
            payload = apply_live_position_sizing(
                payload,
                get_kiwoom_account_summaries(),
                get_kiwoom_holdings(),
            )
        except Exception as exc:
            logger.warning("키움 실계좌 사이징 계산 실패: %s", type(exc).__name__)
    return payload


@app.delete("/api/results/{result_id}")
async def delete_result(result_id: int, db: Session = Depends(get_db)):
    r = db.query(ScanResult).filter(ScanResult.id == result_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="결과를 찾을 수 없습니다.")
    db.delete(r)
    db.commit()
    return {"status": "deleted"}


@app.delete("/api/results")
async def delete_results_bulk(
    market: str = "ALL",
    signal_type: str = "ALL",
    days: int = 0,
    include_rejected: bool = False,
    db: Session = Depends(get_db),
):
    """현재 필터 조건에 맞는 스캔 결과 일괄 삭제.
    days=0 이면 날짜 필터 없이 전체 삭제.
    `include_rejected=False`(기본): strict 거부 행(QA용)은 보존.
    """
    q = db.query(ScanResult)
    if market != "ALL":
        q = q.filter(ScanResult.market == market)
    if signal_type != "ALL":
        q = q.filter(ScanResult.signal_type == signal_type)
    if days > 0:
        since_str = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
        q = q.filter(ScanResult.signal_date >= since_str)
    if not include_rejected:
        q = q.filter(or_(
            ScanResult.strict_filter_passed.is_(None),
            ScanResult.strict_filter_passed.is_(True),
        ))
    count = q.count()
    q.delete(synchronize_session=False)
    db.commit()
    return {"status": "deleted", "count": count}


# ═══════════════════════════════════════════════════════════════
#  차트 API  (Phase 3 — 일봉/주봉 OHLCV + MA)
# ═══════════════════════════════════════════════════════════════

CHART_RANGE_DAYS = {"6m": 183, "1y": 365, "2y": 730, "5y": 1825}
CHART_TICKER_RE = re.compile(r"^[A-Za-z0-9.\-]{1,15}$")


def _get_chart_overlay_row(db: Session, raw_id: Optional[str],
                           market: str, ticker: str):
    """유효한 양의 정수 id이며 요청 종목과 같은 ScanResult만 반환.

    잘못된 값/없는 행/다른 종목 id는 모두 overlay 없음으로 취급한다. 차트
    OHLCV 자체는 언제나 계속 반환해야 하므로 validation exception을 내지 않는다.
    """
    if raw_id is None:
        return None
    try:
        result_id = int(raw_id)
    except (TypeError, ValueError):
        return None
    if result_id <= 0 or str(result_id) != str(raw_id).strip():
        return None
    return db.query(ScanResult).filter(
        ScanResult.id == result_id,
        ScanResult.market == market,
        ScanResult.ticker == ticker,
    ).first()


def _close_on_date(daily, date_text: Optional[str]) -> Optional[float]:
    """원본 OHLCV에서 signal-date 종가를 읽는다(구간/피벗 재계산 아님)."""
    if daily is None or len(daily) == 0 or not date_text:
        return None
    try:
        import pandas as pd
        target = str(date_text)[:10]
        for idx, value in daily["Close"].items():
            if pd.Timestamp(idx).strftime("%Y-%m-%d") == target:
                return float(value)
    except Exception:
        return None
    return None


def _pct_distance(price: Optional[float], reference: Optional[float]) -> Optional[float]:
    if price is None or reference is None or reference <= 0:
        return None
    return round((float(price) - float(reference)) / float(reference) * 100, 2)


def _stop_pct(price: Optional[float], stop: Optional[float]) -> Optional[float]:
    if price is None or stop is None or price <= 0:
        return None
    return round((float(price) - float(stop)) / float(price) * 100, 2)


def _build_chart_overlay(row, daily) -> dict:
    """저장된 판정 스냅샷 + 원본 종가로 overlay를 구성한다.

    base/tight/pivot/stop은 DB 값만 사용한다. API에서 계산하는 것은 사용자
    요청의 at_signal/at_current 파생 퍼센트와 warnings뿐이다.
    """
    current_price = None
    if daily is not None and len(daily) > 0:
        try:
            current_price = float(daily["Close"].iloc[-1])
        except Exception:
            current_price = None
    if current_price is None and row.price is not None:
        current_price = float(row.price)

    signal_price = _close_on_date(daily, row.signal_date)
    if signal_price is None and row.price is not None:
        # 외부 데이터가 비어도 차트 API 전체를 막지 않는 안전 폴백. 신규 행의
        # 정상 경로에서는 signal_date 봉의 Close가 사용된다.
        signal_price = float(row.price)

    pivot = float(row.pivot_price) if row.pivot_price is not None else None
    stop = float(row.stop_loss) if row.stop_loss is not None else None
    signal_stop_pct = _stop_pct(signal_price, stop)
    current_stop_pct = _stop_pct(current_price, stop)
    signal_ext_pct = _pct_distance(signal_price, pivot)
    current_ext_pct = _pct_distance(current_price, pivot)

    base = None
    if any(getattr(row, key, None) is not None for key in (
        "base_start_date", "base_end_date", "base_high", "base_low", "base_width_pct"
    )):
        base = {
            "from": row.base_start_date,
            "to": row.base_end_date,
            "high": row.base_high,
            "low": row.base_low,
            "width_pct": row.base_width_pct,
        }

    tight = None
    if any(getattr(row, key, None) is not None for key in (
        "tight_start_date", "tight_high", "tight_low", "tight_width_pct"
    )):
        tight = {
            "from": row.tight_start_date,
            "to": row.base_end_date,
            "high": row.tight_high,
            "low": row.tight_low,
            "width_pct": row.tight_width_pct,
        }

    warnings = []
    if current_ext_pct is not None and current_ext_pct > MAX_PIVOT_EXT_PCT:
        warnings.append(f"추격 구간 (피벗 대비 +{current_ext_pct:.1f}%)")
    if current_price is not None and pivot is not None and current_price < pivot:
        warnings.append("돌파 후 피벗 하향 회귀")
    if current_stop_pct is not None and current_stop_pct > ALERT_MAX_CUR_STOP_PCT:
        warnings.append(f"현재가 기준 손절폭 {current_stop_pct:.1f}%")

    return {
        "signal_type": row.signal_type,
        "signal_date": row.signal_date,
        "base_mode": row.base_mode,
        "base": base,
        "tight": tight,
        "contraction_ratio": row.contraction_ratio,
        "pivot_price": pivot,
        "stop_loss": stop,
        "at_signal": {
            "price": signal_price,
            "stop_pct": signal_stop_pct,
            "ext_vs_pivot_pct": signal_ext_pct,
            "volume_ratio": (float(row.volume_ratio)
                             if row.volume_ratio is not None else None),
        },
        "at_current": {
            "price": current_price,
            "stop_pct": current_stop_pct,
            "ext_vs_pivot_pct": current_ext_pct,
        },
        # Step 4 후속 — 경고 판정에 실제로 쓰인 임계값을 함께 실어 보내
        # 프론트가 하드코딩된 5/12 대신 이 값을 표시/재사용할 수 있게 한다.
        "thresholds": {
            "max_pivot_ext_pct": MAX_PIVOT_EXT_PCT,
            "max_cur_stop_pct": ALERT_MAX_CUR_STOP_PCT,
        },
        "warnings": warnings,
    }


@app.get("/api/chart/ohlcv")
async def get_chart_ohlcv(
    market: str = Query(..., description="KR 또는 US"),
    ticker: str = Query(..., min_length=1, max_length=15),
    timeframe: str = Query("daily", description="daily 또는 weekly"),
    range: str = Query("1y", description="6m / 1y / 2y / 5y"),
    scan_result_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """스캔 결과 행에서 일봉/주봉 차트를 그릴 수 있는 OHLCV + MA JSON.

    응답 스키마:
      {
        "market": "KR"|"US", "ticker": "...", "timeframe": "daily"|"weekly",
        "range": "6m"|"1y"|"2y"|"5y",
        "ma_period": 150 | 30,
        "candles": [{"t","o","h","l","c","v","ma"}, ...]
      }

    on-demand 페치 — 스캔 시 차트 데이터를 사전 적재하지 않는다.
    """
    market = market.upper()
    timeframe = timeframe.lower()
    range_key = range.lower()

    if market not in ("KR", "US"):
        raise HTTPException(status_code=422, detail="market 은 KR 또는 US")
    if timeframe not in ("daily", "weekly"):
        raise HTTPException(status_code=422, detail="timeframe 은 daily 또는 weekly")
    if range_key not in CHART_RANGE_DAYS:
        raise HTTPException(status_code=422, detail=f"range 는 {list(CHART_RANGE_DAYS)}")
    if not CHART_TICKER_RE.match(ticker):
        raise HTTPException(status_code=422, detail="ticker 형식 (영숫자/점/하이픈, 1~15)")

    overlay_row = _get_chart_overlay_row(db, scan_result_id, market, ticker)

    # MA를 표시 범위 시작 지점에서도 채우기 위해 buffer 추가 페치
    requested_days = CHART_RANGE_DAYS[range_key]
    ma_period = 150 if timeframe == "daily" else 30
    buffer_days = 250 if timeframe == "daily" else 225  # weekly 30주 ≈ 210일
    fetch_days = requested_days + buffer_days

    # Phase 2 fetch_ohlcv 어댑터 사용 (KR/US 라우팅)
    if market == "KR":
        from scanner.kr_stocks import fetch_ohlcv
    else:
        from scanner.us_stocks import fetch_ohlcv

    from scanner.errors import DataFetchError
    try:
        daily = fetch_ohlcv(ticker, lookback_days=fetch_days)
    except DataFetchError as e:
        # 외부 어댑터의 명시적 fetch 실패 → 503 (downstream 일시적 장애)
        logger.warning(f"[chart] {market} {ticker} 외부 데이터 실패: {e}")
        return JSONResponse(
            {"detail": "외부 데이터 페치 실패", "market": market, "ticker": ticker},
            status_code=503,
        )
    except Exception as e:
        # 그 외 예외 → 500 (서버 내부 버그)
        logger.exception(f"[chart] {market} {ticker} 처리 중 내부 오류")
        return JSONResponse(
            {"detail": "내부 처리 오류", "market": market, "ticker": ticker},
            status_code=500,
        )

    empty_response = {
        "market": market, "ticker": ticker,
        "timeframe": timeframe, "range": range_key,
        "ma_period": ma_period, "candles": [],
    }
    if overlay_row is not None:
        empty_response["overlay"] = _build_chart_overlay(overlay_row, daily)
    if daily is None or len(daily) == 0:
        return empty_response

    if timeframe == "weekly":
        from scanner.weinstein import to_weekly_ohlcv
        df = to_weekly_ohlcv(daily)
    else:
        df = daily

    if df is None or len(df) == 0:
        return empty_response

    import pandas as pd
    df = df.copy()
    df["ma"] = df["Close"].rolling(ma_period, min_periods=ma_period // 2).mean()

    # 요청 범위로 trim — 마지막 인덱스 기준 requested_days 이내
    last_ts = df.index.max()
    cutoff = last_ts - pd.Timedelta(days=requested_days)
    visible = df[df.index >= cutoff]
    if len(visible) == 0:
        visible = df  # 짧은 시리즈는 통째로 반환

    candles = []
    for idx, row in visible.iterrows():
        ma_val = row["ma"]
        candles.append({
            "t": pd.Timestamp(idx).strftime("%Y-%m-%d"),
            "o": float(row["Open"]),
            "h": float(row["High"]),
            "l": float(row["Low"]),
            "c": float(row["Close"]),
            "v": float(row["Volume"]),
            "ma": (float(ma_val) if pd.notna(ma_val) else None),
        })

    response = {
        "market": market, "ticker": ticker,
        "timeframe": timeframe, "range": range_key,
        "ma_period": ma_period, "candles": candles,
    }
    if overlay_row is not None:
        # 항상 원본 daily를 사용하므로 timeframe/range 변경과 무관하게 동일한
        # DB 판정 날짜 및 signal/current 비교를 반환한다.
        response["overlay"] = _build_chart_overlay(overlay_row, daily)
    return response


@app.get("/api/scan/logs")
async def get_scan_logs(limit: int = 20, db: Session = Depends(get_db)):
    logs = db.query(ScanLog).order_by(ScanLog.started_at.desc()).limit(limit).all()
    return [{"id": l.id,
             "started_at": l.started_at.isoformat() if l.started_at else None,
             "finished_at": l.finished_at.isoformat() if l.finished_at else None,
             "market": l.market, "total_scanned": l.total_scanned,
             "signals_found": l.signals_found, "status": l.status,
             "triggered_by": l.triggered_by, "error_msg": l.error_msg}
            for l in logs]


# ═══════════════════════════════════════════════════════════════
#  계좌 API
# ═══════════════════════════════════════════════════════════════

EQUITY_CURRENCY = {"KR": "KRW", "US": "USD"}


class EquityCreate(BaseModel):
    market: str
    total_equity: float
    cash_balance: float
    note: Optional[str] = None

    model_config = {"extra": "forbid"}


def _equity_to_dict(row: Optional[AccountEquity]):
    if row is None:
        return None
    age = datetime.utcnow() - row.recorded_at
    return {
        "id": row.id,
        "market": row.market,
        "currency": row.currency,
        "total_equity": row.total_equity,
        "cash_balance": row.cash_balance,
        "note": row.note,
        "recorded_at": row.recorded_at.isoformat(),
        "age_days": age.days,
        "is_stale": age > timedelta(days=30),
    }


@app.get("/api/equity")
async def get_equity(db: Session = Depends(get_db)):
    """KR/US 각각의 최신 append-only 자산 스냅샷을 반환한다."""
    latest = {}
    for market in ("KR", "US"):
        row = db.query(AccountEquity).filter(
            AccountEquity.market == market,
        ).order_by(
            AccountEquity.recorded_at.desc(),
            AccountEquity.id.desc(),
        ).first()
        latest[market] = _equity_to_dict(row)
    return latest


@app.post("/api/equity")
async def create_equity(body: EquityCreate, db: Session = Depends(get_db)):
    """시장 통화가 고정된 새 자산 행을 append한다 (기존 행 갱신 금지)."""
    market = body.market.strip().upper()
    if market not in EQUITY_CURRENCY:
        raise HTTPException(status_code=422, detail="시장은 KR 또는 US여야 합니다.")
    if (not math.isfinite(body.total_equity) or body.total_equity <= 0):
        raise HTTPException(status_code=422, detail="총 자산은 0보다 커야 합니다.")
    if (not math.isfinite(body.cash_balance) or body.cash_balance < 0):
        raise HTTPException(status_code=422, detail="매수 가능 현금은 0 이상이어야 합니다.")
    if body.cash_balance > body.total_equity:
        raise HTTPException(status_code=422, detail="매수 가능 현금은 총 자산을 초과할 수 없습니다.")
    note = body.note.strip() if body.note else None
    if note and len(note) > 200:
        raise HTTPException(status_code=422, detail="메모는 200자 이하여야 합니다.")

    row = AccountEquity(
        market=market,
        currency=EQUITY_CURRENCY[market],
        total_equity=body.total_equity,
        cash_balance=body.cash_balance,
        note=note,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _equity_to_dict(row)

ACCOUNT_TYPE_CURRENCY = {
    "KR_STOCK":  "KRW",
    "US_STOCK":  "USD",
    "KR_PENSION": "KRW",
    "KR_IRP":    "KRW",
    "KR_ISA":    "KRW",
    "OTHER":     "KRW",
}

ACCOUNT_TYPE_LABEL = {
    "KR_STOCK":  "국내주식",
    "US_STOCK":  "해외주식",
    "KR_PENSION": "연금저축",
    "KR_IRP":    "IRP",
    "KR_ISA":    "ISA",
    "OTHER":     "기타",
}


class AccountCreate(BaseModel):
    name: str
    account_type: str = "KR_STOCK"
    broker: str = ""
    memo: str = ""


@app.get("/api/accounts")
async def list_accounts(db: Session = Depends(get_db)):
    accounts = db.query(Account).filter(Account.is_active == True).all()
    result = []
    for a in accounts:
        currency = ACCOUNT_TYPE_CURRENCY.get(a.account_type, a.currency or "KRW")
        cash = _calc_cash(a.id, db)
        stock_eval = sum(
            (h.current_price or h.avg_price) * h.quantity
            for h in db.query(Holding).filter(
                Holding.account_id == a.id, Holding.is_active == True, Holding.quantity > 0
            ).all()
        )
        result.append({
            "id": a.id,
            "name": a.name,
            "account_type": a.account_type or "KR_STOCK",
            "account_type_label": ACCOUNT_TYPE_LABEL.get(a.account_type, "기타"),
            "currency": currency,
            "broker": a.broker or "",
            "memo": a.memo,
            "cash": round(cash, 2),
            "stock_eval": round(stock_eval, 2),
            "total": round(cash + stock_eval, 2),
            "created_at": a.created_at.isoformat() if a.created_at else None,
        })
    return result


@app.post("/api/accounts")
async def create_account(body: AccountCreate, db: Session = Depends(get_db)):
    currency = ACCOUNT_TYPE_CURRENCY.get(body.account_type, "KRW")
    a = Account(name=body.name, account_type=body.account_type,
                currency=currency, broker=body.broker, memo=body.memo)
    db.add(a)
    db.commit()
    db.refresh(a)
    return {"status": "created", "id": a.id}


@app.delete("/api/accounts/{account_id}")
async def delete_account(account_id: int, db: Session = Depends(get_db)):
    a = db.query(Account).filter(Account.id == account_id).first()
    if not a:
        raise HTTPException(status_code=404, detail="계좌를 찾을 수 없습니다.")
    a.is_active = False
    db.commit()
    return {"status": "deleted"}


# ═══════════════════════════════════════════════════════════════
#  거래 API  (매수/매도/입금/출금)
# ═══════════════════════════════════════════════════════════════

class TxCreate(BaseModel):
    account_id: int
    tx_type: str          # BUY / SELL / DEPOSIT / WITHDRAW
    trade_date: str       # YYYY-MM-DD
    ticker: Optional[str] = None
    name: Optional[str] = None
    market: Optional[str] = None
    quantity: Optional[float] = None
    price: Optional[float] = None
    amount: float = 0
    fee: float = 0
    tax: float = 0
    memo: str = ""


@app.get("/api/transactions")
async def list_transactions(account_id: Optional[int] = None,
                            tx_type: str = "ALL", ticker: Optional[str] = None,
                            limit: int = 200,
                            db: Session = Depends(get_db)):
    q = db.query(Transaction)
    if account_id:
        q = q.filter(Transaction.account_id == account_id)
    if tx_type != "ALL":
        q = q.filter(Transaction.tx_type == tx_type)
    if ticker:
        q = q.filter(Transaction.ticker == ticker.strip().upper())
    rows = q.order_by(Transaction.trade_date.desc(), Transaction.id.desc()).limit(limit).all()
    return [_tx_to_dict(t) for t in rows]


@app.post("/api/transactions")
async def create_transaction(body: TxCreate, db: Session = Depends(get_db)):
    acct = db.query(Account).filter(
        Account.id == body.account_id,
        Account.is_active == True,
    ).first()
    if not acct:
        raise HTTPException(status_code=404, detail="계좌를 찾을 수 없습니다.")

    tx_type = body.tx_type.strip().upper()
    if tx_type not in {"BUY", "SELL", "DEPOSIT", "WITHDRAW"}:
        raise HTTPException(status_code=422, detail="지원하지 않는 거래 유형입니다.")

    try:
        trade_date = datetime.strptime(body.trade_date, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="매수일은 YYYY-MM-DD 형식이어야 합니다.")
    if trade_date > datetime.now(KST).date():
        raise HTTPException(status_code=422, detail="미래 날짜는 입력할 수 없습니다.")

    data = body.model_dump()
    data["tx_type"] = tx_type
    if tx_type in {"BUY", "SELL"}:
        market = (body.market or "").strip().upper()
        ticker = (body.ticker or "").strip().upper()
        name = (body.name or ticker).strip()
        if market not in {"KR", "US"}:
            raise HTTPException(status_code=422, detail="시장은 KR 또는 US여야 합니다.")
        if not ticker:
            raise HTTPException(status_code=422, detail="종목코드가 필요합니다.")
        if body.quantity is None or body.quantity <= 0:
            raise HTTPException(status_code=422, detail="수량은 0보다 커야 합니다.")
        if body.price is None or body.price <= 0:
            raise HTTPException(status_code=422, detail="단가는 0보다 커야 합니다.")
        if market == "KR" and not float(body.quantity).is_integer():
            raise HTTPException(status_code=422, detail="한국 주식 수량은 정수여야 합니다.")

        currency = ACCOUNT_TYPE_CURRENCY.get(acct.account_type, acct.currency or "KRW")
        expected_currency = "USD" if market == "US" else "KRW"
        if currency != expected_currency:
            raise HTTPException(status_code=422, detail=f"{expected_currency} 계좌를 선택해 주세요.")

        data.update(
            ticker=ticker,
            name=name,
            market=market,
            amount=round(body.price * body.quantity, 4),
        )
    elif body.amount <= 0:
        raise HTTPException(status_code=422, detail="금액은 0보다 커야 합니다.")

    tx = Transaction(**data)
    db.add(tx)

    # 매수 → 보유 주식 업데이트 (평단가 재계산)
    if tx_type == "BUY":
        _apply_buy(db, body.account_id, data["ticker"], data["name"],
                   data["market"], body.quantity, body.price)

    # 매도 → 보유 수량 차감
    elif tx_type == "SELL":
        _apply_sell(db, body.account_id, data["ticker"], body.quantity)

    db.commit()
    db.refresh(tx)
    return {"status": "created", "id": tx.id}


@app.delete("/api/transactions/{tx_id}")
async def delete_transaction(tx_id: int, db: Session = Depends(get_db)):
    tx = db.query(Transaction).filter(Transaction.id == tx_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="거래 내역을 찾을 수 없습니다.")
    account_id, ticker, tx_type = tx.account_id, tx.ticker, tx.tx_type
    db.delete(tx)
    db.flush()
    # 매수/매도 삭제 시 보유주식 재계산
    if tx_type in ("BUY", "SELL") and ticker:
        _recalc_holding(db, account_id, ticker)
    db.commit()
    return {"status": "deleted"}


# ═══════════════════════════════════════════════════════════════
#  보유 주식 API
# ═══════════════════════════════════════════════════════════════

@app.get("/api/holdings")
async def list_holdings(account_id: Optional[int] = None, db: Session = Depends(get_db)):
    q = db.query(Holding).filter(Holding.is_active == True, Holding.quantity > 0)
    if account_id:
        q = q.filter(Holding.account_id == account_id)
    holdings = q.order_by(Holding.market, Holding.ticker).all()
    rows = [_holding_to_dict(h, db) for h in holdings]
    if KIWOOM_WEB_ENABLED and account_id is None:
        try:
            rows.extend(apply_kiwoom_sell_analysis(get_kiwoom_holdings()))
        except Exception as exc:
            # 키움 장애가 기존 수동 보유현황까지 막지 않게 한다. 비밀값은 로깅하지 않는다.
            logger.warning("키움 실계좌 보유현황 조회 실패: %s", type(exc).__name__)
    return rows


@app.get("/api/portfolio-summary")
async def portfolio_summary():
    """키움 실계좌별 현금·평가손익 요약. 주문 및 환전 기능은 포함하지 않는다."""
    if not KIWOOM_WEB_ENABLED:
        return []
    try:
        return get_kiwoom_account_summaries()
    except Exception:
        logger.exception("키움 계좌 요약 조회 실패")
        raise HTTPException(status_code=502, detail="키움 계좌 요약을 불러오지 못했습니다.")


class HoldingRiskUpdate(BaseModel):
    entry_price: Optional[float] = None
    initial_stop_loss: Optional[float] = None
    current_stop_loss: Optional[float] = None


@app.get("/api/holdings/{holding_id}")
async def get_holding(holding_id: int, db: Session = Depends(get_db)):
    h = db.query(Holding).filter(Holding.id == holding_id).first()
    if not h:
        raise HTTPException(status_code=404, detail="보유주식을 찾을 수 없습니다.")
    return _holding_to_dict(h, db)


@app.patch("/api/holdings/{holding_id}")
async def update_holding_risk(holding_id: int, body: HoldingRiskUpdate,
                              db: Session = Depends(get_db)):
    """사용자가 정한 진입가/손절가만 저장한다. avg_price 기반 자동 추정은 하지 않는다."""
    h = db.query(Holding).filter(Holding.id == holding_id).first()
    if not h:
        raise HTTPException(status_code=404, detail="보유주식을 찾을 수 없습니다.")

    fields_set = (body.model_fields_set if hasattr(body, "model_fields_set")
                  else body.__fields_set__)
    values = body.model_dump()
    for field in fields_set:
        value = values[field]
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise HTTPException(status_code=400, detail=f"{field}는 0보다 큰 유한수여야 합니다.")

    entry = values["entry_price"] if "entry_price" in fields_set else h.entry_price
    initial_stop = (values["initial_stop_loss"]
                    if "initial_stop_loss" in fields_set else h.initial_stop_loss)
    current_stop = (values["current_stop_loss"]
                    if "current_stop_loss" in fields_set else h.current_stop_loss)
    if (initial_stop is not None or current_stop is not None) and entry is None:
        raise HTTPException(status_code=400, detail="손절가 저장 전에 진입가를 입력해주세요.")
    for label, stop in (("최초 손절가", initial_stop), ("현재 손절가", current_stop)):
        if stop is not None and stop >= entry:
            raise HTTPException(status_code=400, detail=f"{label}는 진입가보다 낮아야 합니다.")

    for field in fields_set:
        setattr(h, field, values[field])
    h.initial_r = (entry - initial_stop
                   if entry is not None and initial_stop is not None else None)
    if "current_stop_loss" in fields_set:
        h.last_alert_severity = None
        h.last_alert_reason = None
        h.last_alert_at = None
    db.commit()
    db.refresh(h)
    return _holding_to_dict(h, db)


@app.delete("/api/holdings/{holding_id}")
async def delete_holding(holding_id: int, db: Session = Depends(get_db)):
    h = db.query(Holding).filter(Holding.id == holding_id).first()
    if not h:
        raise HTTPException(status_code=404, detail="보유주식을 찾을 수 없습니다.")
    db.delete(h)
    db.commit()
    return {"status": "deleted"}


@app.post("/api/holdings/recalc-all")
async def recalc_all_holdings(db: Session = Depends(get_db)):
    """모든 계좌·종목의 보유수량·평단가를 거래내역 기준으로 재계산"""
    from sqlalchemy import text
    pairs = db.execute(text(
        "SELECT DISTINCT account_id, ticker FROM transactions WHERE tx_type IN ('BUY','SELL') AND ticker IS NOT NULL"
    )).fetchall()
    for account_id, ticker in pairs:
        _recalc_holding(db, account_id, ticker)
    db.commit()
    return {"status": "ok", "recalculated": len(pairs)}


@app.post("/api/holdings/refresh-prices")
async def refresh_prices(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """보유 주식 현재가 일괄 업데이트"""
    def _refresh():
        _update_holding_prices()
        if KIWOOM_WEB_ENABLED:
            try:
                live_rows = get_kiwoom_holdings(force=True)
                apply_kiwoom_sell_analysis(live_rows, force=True)
            except Exception as exc:
                logger.warning("키움 실계좌 새로고침 실패: %s", type(exc).__name__)
    background_tasks.add_task(_refresh)
    return {"status": "started", "message": "현재가 업데이트를 시작했습니다."}


# ═══════════════════════════════════════════════════════════════
#  감시 목록 (Weinstein 매도 시그널용)
# ═══════════════════════════════════════════════════════════════

class WatchCreate(BaseModel):
    ticker: str
    name: str
    market: str
    buy_price: Optional[float] = None
    stop_loss: Optional[float] = None
    target_price: Optional[float] = None
    memo: str = ""


@app.get("/api/watchlist")
async def get_watchlist(db: Session = Depends(get_db)):
    items = db.query(WatchList).filter(WatchList.is_active == True).all()
    return [{"id": w.id, "ticker": w.ticker, "name": w.name, "market": w.market,
             "buy_price": w.buy_price, "stop_loss": w.stop_loss,
             "target_price": w.target_price, "memo": w.memo,
             "created_at": w.created_at.isoformat() if w.created_at else None}
            for w in items]


@app.post("/api/watchlist")
async def add_watchlist(body: WatchCreate, db: Session = Depends(get_db)):
    existing = db.query(WatchList).filter(WatchList.ticker == body.ticker).first()
    if existing:
        for k, v in body.dict().items():
            setattr(existing, k, v)
        existing.is_active = True
        db.commit()
        return {"status": "updated", "id": existing.id}
    item = WatchList(**body.dict())
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"status": "created", "id": item.id}


@app.delete("/api/watchlist/{ticker}")
async def remove_watchlist(ticker: str, db: Session = Depends(get_db)):
    item = db.query(WatchList).filter(WatchList.ticker == ticker).first()
    if not item:
        raise HTTPException(status_code=404, detail="종목을 찾을 수 없습니다.")
    item.is_active = False
    db.commit()
    return {"status": "removed"}


# ═══════════════════════════════════════════════════════════════
#  텔레그램 / 설정
# ═══════════════════════════════════════════════════════════════

@app.get("/api/telegram/test")
async def telegram_test():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return {"status": "error", "message": ".env 파일에서 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID를 설정하세요."}
    ok = test_telegram()
    return {"status": "ok" if ok else "error",
            "message": "테스트 메시지를 발송했습니다." if ok else "발송 실패 - 토큰/Chat ID를 확인하세요."}


@app.get("/api/market/status")
async def get_market_status(force: bool = False):
    """미국·한국 지수 Stage 분석 (Forest to Trees)"""
    try:
        from scanner.market_analysis import get_market_stages
        return get_market_stages(force=force)
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/exchange-rate")
async def get_exchange_rate():
    """USD/KRW 환율 (yfinance USDKRW=X)"""
    try:
        import yfinance as yf
        ticker = yf.Ticker("USDKRW=X")
        hist = ticker.history(period="5d")
        if hist is not None and len(hist) > 0:
            rate = float(hist["Close"].iloc[-1])
            return {"rate": round(rate, 2), "base": "USD", "quote": "KRW"}
    except Exception:
        pass
    return {"rate": 1380.0, "base": "USD", "quote": "KRW"}  # fallback


@app.get("/api/settings")
async def get_settings():
    from config import (
        KR_SCHEDULE_TIMES, MA_PERIOD, SCAN_LOOKBACK_DAYS,
        US_SCHEDULE_TIMES, US_UNIVERSE, VOLUME_SURGE_RATIO,
    )
    return {
        "telegram_configured": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
        "scan_lookback_days": SCAN_LOOKBACK_DAYS,
        "ma_period": MA_PERIOD,
        "volume_surge_ratio": VOLUME_SURGE_RATIO,
        "schedule_times": {
            "KR": KR_SCHEDULE_TIMES,
            "US": US_SCHEDULE_TIMES,
            "US_timezone": "America/New_York",
        },
        "us_universe": US_UNIVERSE,
    }


# ═══════════════════════════════════════════════════════════════
#  내부 헬퍼
# ═══════════════════════════════════════════════════════════════

def _calc_cash(account_id: int, db: Session) -> float:
    txs = db.query(Transaction).filter(Transaction.account_id == account_id).all()
    bal = 0.0
    for t in txs:
        if t.tx_type == "DEPOSIT":
            bal += t.amount
        elif t.tx_type == "WITHDRAW":
            bal -= t.amount
        elif t.tx_type == "BUY":
            bal -= t.amount + (t.fee or 0)
        elif t.tx_type == "SELL":
            bal += t.amount - (t.fee or 0) - (t.tax or 0)
    return round(bal, 2)


def _apply_buy(db: Session, account_id: int, ticker: str, name: str,
               market: str, quantity: float, price: float):
    """매수 시 보유 주식 평단가 재계산 (이동평균 방식)"""
    h = db.query(Holding).filter(
        Holding.account_id == account_id,
        Holding.ticker == ticker,
        Holding.is_active == True
    ).first()
    if h:
        total_qty = h.quantity + quantity
        h.avg_price = round((h.avg_price * h.quantity + price * quantity) / total_qty, 4)
        h.quantity = total_qty
        h.name = name or h.name
        h.market = market
        h.current_price = price
        h.price_updated_at = datetime.utcnow()
        h.sell_status = "PENDING"
        h.sell_severity = None
        h.sell_reason = None
        h.sell_checked_at = None
    else:
        h = Holding(account_id=account_id, ticker=ticker, name=name,
                    market=market, quantity=quantity, avg_price=price,
                    current_price=price, price_updated_at=datetime.utcnow(),
                    entry_price=price,
                    sell_status="PENDING")
        db.add(h)


def _recalc_holding(db: Session, account_id: int, ticker: str):
    """남은 BUY/SELL 거래 기반으로 보유 수량·평단가 재계산"""
    txs = db.query(Transaction).filter(
        Transaction.account_id == account_id,
        Transaction.ticker == ticker,
        Transaction.tx_type.in_(["BUY", "SELL"])
    ).order_by(Transaction.trade_date, Transaction.id).all()

    qty, avg_price = 0.0, 0.0
    for t in txs:
        if t.tx_type == "BUY" and t.quantity and t.price:
            next_qty = qty + t.quantity
            avg_price = ((avg_price * qty) + (t.price * t.quantity)) / next_qty
            qty = next_qty
        elif t.tx_type == "SELL" and t.quantity:
            qty = max(0, qty - t.quantity)
            if qty == 0:
                avg_price = 0.0

    source = next((t for t in reversed(txs) if t.tx_type == "BUY"), None)

    h = db.query(Holding).filter(
        Holding.account_id == account_id, Holding.ticker == ticker
    ).first()
    if qty > 0:
        if h:
            h.quantity = qty
            h.avg_price = round(avg_price, 4)
            if source:
                h.name = source.name or source.ticker or h.name
                h.market = source.market or h.market
            h.is_active = True
            h.sell_status = "PENDING"
            h.sell_severity = None
            h.sell_reason = None
            h.sell_checked_at = None
        else:
            h = Holding(account_id=account_id, ticker=ticker,
                        name=(source.name or ticker) if source else ticker,
                        market=(source.market or "KR") if source else "KR",
                        quantity=qty, avg_price=round(avg_price, 4),
                        sell_status="PENDING", is_active=True)
            db.add(h)
    else:
        if h:
            h.quantity = 0
            h.is_active = False


def _apply_sell(db: Session, account_id: int, ticker: str, quantity: float):
    """매도 시 보유 수량 차감"""
    h = db.query(Holding).filter(
        Holding.account_id == account_id,
        Holding.ticker == ticker,
        Holding.is_active == True
    ).first()
    if h:
        h.quantity = max(0, h.quantity - quantity)
        if h.quantity == 0:
            h.is_active = False


def _update_holding_prices():
    """보유 주식 현재가 일괄 업데이트"""
    from scanner.kr_stocks import get_kr_ohlcv
    from scanner.us_stocks import get_us_ohlcv
    db = SessionLocal() if False else next(get_db())
    try:
        from database.models import SessionLocal as SL
        db = SL()
        holdings = db.query(Holding).filter(
            Holding.is_active == True, Holding.quantity > 0
        ).all()
        for h in holdings:
            try:
                df = get_kr_ohlcv(h.ticker) if h.market == "KR" else get_us_ohlcv(h.ticker)
                if df is not None and len(df) > 0:
                    h.current_price = float(df["Close"].iloc[-1])
                    h.price_updated_at = datetime.utcnow()
            except Exception:
                pass
        db.commit()
    finally:
        db.close()


def _tx_to_dict(t: Transaction) -> dict:
    return {
        "id": t.id, "account_id": t.account_id,
        "tx_type": t.tx_type, "trade_date": t.trade_date,
        "ticker": t.ticker, "name": t.name, "market": t.market,
        "quantity": t.quantity, "price": t.price, "amount": t.amount,
        "fee": t.fee, "tax": t.tax, "memo": t.memo,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


def _holding_to_dict(h: Holding, db: Session) -> dict:
    cp = h.current_price or h.avg_price
    eval_amt = round(cp * h.quantity, 2) if cp else 0
    pl = round((cp - h.avg_price) * h.quantity, 2) if h.current_price else 0
    pl_pct = round((cp - h.avg_price) / h.avg_price * 100, 2) if h.current_price and h.avg_price else 0
    last_buy = db.query(Transaction.trade_date).filter(
        Transaction.account_id == h.account_id,
        Transaction.ticker == h.ticker,
        Transaction.tx_type == "BUY",
    ).order_by(Transaction.trade_date.desc(), Transaction.id.desc()).first()
    account = h.account
    unrealized_r = None
    if (h.current_price is not None and h.entry_price is not None
            and h.initial_r is not None and h.initial_r > 0):
        unrealized_r = round((h.current_price - h.entry_price) / h.initial_r, 2)
    return {
        "id": h.id, "account_id": h.account_id,
        "account_name": account.name if account else "",
        "currency": (ACCOUNT_TYPE_CURRENCY.get(account.account_type, account.currency)
                     if account else ("USD" if h.market == "US" else "KRW")),
        "ticker": h.ticker, "name": h.name, "market": h.market,
        "quantity": h.quantity, "avg_price": h.avg_price,
        "current_price": h.current_price,
        "entry_price": h.entry_price,
        "initial_stop_loss": h.initial_stop_loss,
        "current_stop_loss": h.current_stop_loss,
        "initial_r": h.initial_r,
        "unrealized_r": unrealized_r,
        "eval_amount": eval_amt,
        "profit_loss": pl,
        "profit_loss_pct": pl_pct,
        "price_updated_at": h.price_updated_at.isoformat() if h.price_updated_at else None,
        "last_buy_date": last_buy[0] if last_buy else None,
        "sell_status": h.sell_status or "PENDING",
        "sell_severity": h.sell_severity,
        "sell_reason": h.sell_reason,
        "sell_checked_at": h.sell_checked_at.isoformat() if h.sell_checked_at else None,
        "last_alert_severity": h.last_alert_severity,
        "last_alert_reason": h.last_alert_reason,
        "last_alert_at": h.last_alert_at.isoformat() if h.last_alert_at else None,
        "memo": h.memo,
        "source": "manual",
        "read_only": False,
    }
