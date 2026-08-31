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
        today = date.today()
        requested = date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])

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
        updated_at = datetime.utcnow().isoformat() + "Z"
        payload = {"updated_at": updated_at, "report": report}
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
    return {
        "status": "running" if is_running else "ready" if cached else "error" if runtime.get("error") else "empty",
        "is_running": is_running,
        "error": runtime.get("error"),
        "updated_at": cached.get("updated_at") if cached else runtime.get("updated_at"),
        "report": cached.get("report") if cached else None,
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
