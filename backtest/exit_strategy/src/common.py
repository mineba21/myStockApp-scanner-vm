"""공통 경로·설정 로더.

운영 스캐너 모듈(config.py)은 import 시 .env 를 읽는다. 이 백테스트는 DB 를 열지
않지만, 실수로 운영 DB 에 바인딩되지 않도록 DATABASE_URL 을 임시 SQLite 로 먼저
고정한 뒤에만 스캐너 모듈을 import 한다 (load_dotenv 는 기존 환경변수를 덮지 않는다).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import yaml

PKG_DIR = Path(__file__).resolve().parents[1]          # backtest/exit_strategy
REPO_ROOT = PKG_DIR.parents[1]                          # repository root
OUTPUT_DIR = PKG_DIR / "output"
DOCS_DIR = PKG_DIR / "docs"
DEFAULT_CONFIG = PKG_DIR / "configs" / "exit_backtest.yaml"

# 운영 설정에서 화면에 내보내도 되는 스캐너 임계값만 고른다 (자격 증명·URL 제외).
_SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "URL", "WEBHOOK",
                   "ACCOUNT", "APP", "SLACK", "OAUTH", "PATH", "DSN", "HOST", "USER")


def load_config(path: str | os.PathLike | None = None) -> dict:
    with open(path or DEFAULT_CONFIG, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    for key in ("cache_dir",):
        cfg["data"][key] = str(REPO_ROOT / cfg["data"][key])
    cfg["snapshot"]["mcp_dump"] = str(REPO_ROOT / cfg["snapshot"]["mcp_dump"])
    return cfg


def isolate_scanner_imports(overrides: dict | None = None) -> None:
    """스캐너 모듈 import 전 호출. 운영 DB·알림·주문을 차단한다."""
    if overrides is None:
        try:
            overrides = load_config().get("scanner_env_overrides") or {}
        except Exception:
            overrides = {}
    for k, v in overrides.items():
        os.environ[k] = str(v)
    scratch = Path(tempfile.gettempdir()) / "exit_backtest_isolated.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{scratch}"
    for flag in ("KIWOOM_TRADING_ENABLED", "ACCOUNT1_ORDER_ENABLED",
                 "ACCOUNT4_ORDER_ENABLED", "AGENT_RUNTIME_ENABLED"):
        os.environ[flag] = "false"
    os.environ["ALLOCATION_EXECUTION_MODE"] = "disabled"
    os.environ["SLACK_WEBHOOK_URL"] = ""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))


def scanner_threshold_snapshot() -> dict:
    """replay 에 실제로 쓰인 스캐너 임계값(비밀값 제외)을 기록용으로 반환."""
    isolate_scanner_imports()
    import config  # noqa: WPS433 (격리 후 import)

    out = {}
    for name in dir(config):
        if not name.isupper() or any(m in name for m in _SECRET_MARKERS):
            continue
        value = getattr(config, name)
        # 문자열은 알려진 스캐너 모드 키만 남긴다 (임의 문자열에 비밀값이 섞일 수 있음).
        if isinstance(value, (int, float, bool)) or name in ("BASE_MODE", "US_UNIVERSE"):
            out[name] = value
    return out
