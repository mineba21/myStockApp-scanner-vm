"""VAA · LAA · 듀얼 모멘텀 독립 계산기용 FastAPI 라우터."""

import calendar
import json
import logging
import os
import subprocess
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel
from trading.allocation_rebalance import (
    Rebalance, RebalanceBlocked, LIVE_BLOCK_REASON,
)
from starlette.concurrency import run_in_threadpool

from web.asset_allocation_sizing import build_live_allocation_sizing
from trading.kiwoom_execution_evidence import ExecutionEvidence, EvidenceBroker, order_date
from trading.kiwoom_readonly import KiwoomConfig, KiwoomReadOnlyClient, KiwoomError
from web.kiwoom_holdings import _get_token

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["asset-allocation"])

ASSET_ALLOCATION_DIR = Path(os.getenv(
    "ASSET_ALLOCATION_DIR", "/home/ubuntu/apps/asset-allocation"
))
ASSET_ALLOCATION_CACHE_DIR = Path(os.getenv(
    "ASSET_ALLOCATION_CACHE_DIR", str(ASSET_ALLOCATION_DIR / "cache")
))
ASSET_ALLOCATION_TIMEOUT_SECONDS = int(os.getenv(
    "ASSET_ALLOCATION_TIMEOUT_SECONDS", "120"
))
ALLOCATION_PROFILES = {"easy", "original"}
_allocation_lock = threading.Lock()
_allocation_runtime = {}


def _latest_completed_month_end(today: Optional[date] = None) -> date:
    """월간 신호에는 진행 중인 달이 아닌 직전 완료월을 사용한다."""
    current = today or date.today()
    first_of_month = current.replace(day=1)
    return first_of_month.fromordinal(first_of_month.toordinal() - 1)


def _allocation_params(profile: str, as_of: Optional[str]):
    normalized_profile = profile.strip().lower()
    if normalized_profile not in ALLOCATION_PROFILES:
        raise HTTPException(status_code=422, detail="profile은 easy 또는 original이어야 합니다.")

    if as_of:
        try:
            requested = date.fromisoformat(as_of)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="as_of는 YYYY-MM-DD 형식이어야 합니다.") from exc
    else:
        requested = _latest_completed_month_end()

    if requested.day != calendar.monthrange(requested.year, requested.month)[1]:
        raise HTTPException(status_code=422, detail="as_of는 해당 월의 달력상 월말이어야 합니다.")
    return normalized_profile, requested


def _allocation_key(profile: str, requested: date) -> str:
    return f"{profile}-{requested.isoformat()}"


def _allocation_cache_path(profile: str, requested: date) -> Path:
    return ASSET_ALLOCATION_CACHE_DIR / f"{_allocation_key(profile, requested)}.json"


def _read_allocation_cache(profile: str, requested: date):
    try:
        payload = json.loads(
            _allocation_cache_path(profile, requested).read_text(encoding="utf-8")
        )
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("report"), dict):
        return None
    return payload


def _run_asset_allocation(profile: str, requested: date):
    python_path = ASSET_ALLOCATION_DIR / ".venv" / "bin" / "python"
    script_path = ASSET_ALLOCATION_DIR / "asset_allocation.py"
    if not python_path.is_file() or not script_path.is_file():
        raise RuntimeError("자산배분 계산기가 설치되어 있지 않습니다.")

    command = [
        str(python_path), str(script_path),
        "--profile", profile,
        "--as-of", requested.isoformat(),
        "--json",
    ]
    if requested < date.today().replace(day=1):
        command.append("--confirmed")

    completed = subprocess.run(
        command,
        cwd=str(ASSET_ALLOCATION_DIR),
        capture_output=True,
        text=True,
        timeout=ASSET_ALLOCATION_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else "계산에 실패했습니다."
        raise RuntimeError(message)
    try:
        report = json.loads(completed.stdout)
    except ValueError as exc:
        raise RuntimeError("계산 결과 JSON을 읽지 못했습니다.") from exc
    if not isinstance(report, dict) or "combined_allocations" not in report:
        raise RuntimeError("계산 결과 형식이 올바르지 않습니다.")
    return report


def _refresh_asset_allocation(profile: str, requested: date):
    key = _allocation_key(profile, requested)
    try:
        report = _run_asset_allocation(profile, requested)
        sizing = None
        sizing_error = None
        try:
            sizing = build_live_allocation_sizing(report)
        except Exception as exc:
            logger.warning("키움 현재가 기반 자산배분 수량 계산 실패: %s", type(exc).__name__)
            sizing_error = "키움 현재가로 매수수량을 계산하지 못했습니다."
        updated_at = datetime.utcnow().isoformat() + "Z"
        payload = {
            "updated_at": updated_at, "report": report,
            "sizing": sizing, "sizing_error": sizing_error,
        }
        ASSET_ALLOCATION_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path = _allocation_cache_path(profile, requested)
        temp_path = cache_path.with_suffix(f".{os.getpid()}.tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temp_path, cache_path)
        with _allocation_lock:
            _allocation_runtime[key] = {
                "is_running": False, "error": None, "updated_at": updated_at,
            }
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        logger.exception("자산배분 계산 실패: %s", key)
        with _allocation_lock:
            _allocation_runtime[key] = {
                "is_running": False, "error": str(exc), "updated_at": None,
            }


@router.get("/asset-allocation")
async def get_asset_allocation(profile: str = "easy", as_of: Optional[str] = None):
    profile, requested = _allocation_params(profile, as_of)
    key = _allocation_key(profile, requested)
    cached = _read_allocation_cache(profile, requested)
    with _allocation_lock:
        runtime = dict(_allocation_runtime.get(key, {}))
    is_running = bool(runtime.get("is_running"))
    live_sizing = cached.get("sizing") if cached else None
    live_sizing_error = cached.get("sizing_error") if cached else None
    if is_running:
        live_sizing = None
        live_sizing_error = "계산 갱신 중입니다. 이전 수량은 주문 계획으로 사용하지 않습니다."
    if cached and not is_running:
        try:
            live_sizing = await run_in_threadpool(
                build_live_allocation_sizing, cached["report"]
            )
            live_sizing_error = None
        except Exception as exc:
            logger.warning("탭 진입 키움 가격 갱신 실패: %s", type(exc).__name__)
            live_sizing = None  # never present the cached quantity as a fresh plan
            live_sizing_error = "키움 현재가와 계좌 상태를 새로 불러오지 못했습니다."
    return {
        "status": "running" if is_running else "ready" if cached else "error" if runtime.get("error") else "empty",
        "is_running": is_running,
        "error": runtime.get("error"),
        "updated_at": cached.get("updated_at") if cached else runtime.get("updated_at"),
        "report": cached.get("report") if cached else None,
        "sizing": live_sizing,
        "sizing_error": live_sizing_error,
    }


@router.post("/asset-allocation/refresh", status_code=202)
async def refresh_asset_allocation(
    background_tasks: BackgroundTasks,
    profile: str = "easy",
    as_of: Optional[str] = None,
):
    profile, requested = _allocation_params(profile, as_of)
    key = _allocation_key(profile, requested)
    with _allocation_lock:
        if _allocation_runtime.get(key, {}).get("is_running"):
            return {"status": "already_running", "profile": profile, "as_of": requested.isoformat()}
        _allocation_runtime[key] = {"is_running": True, "error": None, "updated_at": None}
    background_tasks.add_task(_refresh_asset_allocation, profile, requested)
    return {"status": "started", "profile": profile, "as_of": requested.isoformat()}

# Rebalance evidence is never accepted from a browser. The broker adapter must
# implement authenticated full-history reads before these actions can go live.
def _execution_evidence():
    config = KiwoomConfig.from_profile('account2')
    client = KiwoomReadOnlyClient(config)
    return ExecutionEvidence(client, _get_token('account2', config, client))


def _rebalance_service():
    path = os.getenv("ALLOCATION_JOURNAL_PATH")
    if not path:
        raise RebalanceBlocked(LIVE_BLOCK_REASON + " 영속 주문 저널 경로도 설정해야 합니다.")
    return Rebalance(path, EvidenceBroker(_execution_evidence),
                     reserve_bps=int(os.getenv("ALLOCATION_RESERVE_BPS", "100")))


class RebalanceConfirmation(BaseModel):
    confirmation: str
    side: str


@router.get("/asset-allocation/rebalance/capabilities")
def rebalance_capabilities():
    return {"execution_enabled": False, "reason": LIVE_BLOCK_REASON,
            "order_history_implemented": True, "cash_validation_implemented": True,
            "cash_reservation_semantics_verified": False, "buy_capacity_implemented": True}


@router.get("/asset-allocation/rebalance/buy-capacity")
def rebalance_buy_capacity(ticker: str, exchange: str, limit_price: str):
    try:
        return _execution_evidence().buy_capacity(ticker, exchange, limit_price,
            reserve_bps=int(os.getenv('ALLOCATION_RESERVE_BPS', '100')))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="증권사 매수가능수량 검증 실패: 주문을 실행하지 않습니다.") from exc


@router.get("/asset-allocation/rebalance/evidence")
def rebalance_evidence(order_day: str):
    try:
        order_date(order_day)  # Validate before credentials or network access.
    except KiwoomError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        evidence = _execution_evidence()
        return {"orders": evidence.history(order_day), "cash": evidence.cash_check(order_day),
                "execution_enabled": False}
    except Exception as exc:
        # Do not echo broker exceptions, which can contain account data/tokens.
        raise HTTPException(status_code=503, detail="증권사 검증 조회 실패: 주문을 실행하지 않습니다.") from exc


@router.post("/asset-allocation/rebalance/preview")
def preview_rebalance(profile: str = "easy", as_of: Optional[str] = None):
    profile, requested = _allocation_params(profile, as_of)
    cached = _read_allocation_cache(profile, requested)
    if not cached:
        raise HTTPException(status_code=409, detail="월간 배분 보고서가 먼저 필요합니다.")
    scope = [t.strip().upper() for t in os.getenv("ALLOCATION_LIQUIDATION_SCOPE", "").split(",") if t.strip()]
    try:
        return _rebalance_service().create(_allocation_key(profile, requested),
                                          cached["report"]["combined_allocations"], scope)
    except RebalanceBlocked as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/asset-allocation/rebalance/current")
def current_rebalance():
    try:
        return _rebalance_service().active()
    except RebalanceBlocked as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/asset-allocation/rebalance/{cycle_id}")
def rebalance_status(cycle_id: str):
    try:
        return _rebalance_service().status(cycle_id)
    except RebalanceBlocked as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/asset-allocation/rebalance/{cycle_id}/confirm")
def confirm_rebalance(cycle_id: str, body: RebalanceConfirmation):
    if body.confirmation != "account2":
        raise HTTPException(status_code=422, detail="account2 확인 문구가 일치하지 않습니다.")
    if os.getenv("KIWOOM_TRADING_ENABLED", "false").lower() != "true":
        raise HTTPException(status_code=503, detail="실계좌 주문 기능이 비활성화되어 있습니다.")
    try:
        return _rebalance_service().confirm(cycle_id, body.side)
    except RebalanceBlocked as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/asset-allocation/rebalance/{cycle_id}/reconcile")
def reconcile_rebalance(cycle_id: str):
    try:
        return _rebalance_service().reconcile(cycle_id)
    except RebalanceBlocked as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/asset-allocation/rebalance/{cycle_id}/buy-preview")
def preview_rebalance_buys(cycle_id: str):
    try:
        return _rebalance_service().preview_buys(cycle_id)
    except RebalanceBlocked as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/asset-allocation/rebalance/{cycle_id}/sell-preview")
def refresh_rebalance_sell_preview(cycle_id: str):
    try:
        return _rebalance_service().refresh_sell_preview(cycle_id)
    except RebalanceBlocked as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
