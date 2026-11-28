"""자동매수 RT1 SHADOW 설정. env 는 모드·저널·킬스위치·추가 휴장일만 받고 나머지는 상수."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

STRATEGY_VERSION = "RT1"
FILL_MODEL = "CUR_V1"
OPEN_CHECK_BASIS = "FIRST_QUOTE"
ACCOUNT_PROFILE = "account1"

STRATEGIES = {
    "B00": {"pool": "B00", "topn": None},
    "B00N": {"pool": "B00", "topn": 2},
    "B01": {"pool": "B01", "topn": 2},
}
RISK = {"RISK_PCT": 0.75, "MAX_TOTAL_HEAT_PCT": 4.0, "MAX_POSITION_PCT": 15.0,
        "MIN_R_PCT": 3.0, "MAX_R_PCT": 15.0}
SECTOR_CAP_PCT = 35.0
SLEEVE_PCT = Decimal("0.30")
LIMIT_PCT = Decimal("0.01")
COSTS = {"buy_commission": 0.0025, "sell_commission": 0.0025, "sell_tax": 0.0000278, "slippage_bp": 10}

MODES = ("OFF", "SHADOW")


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def _dates(name: str) -> tuple[frozenset, str | None]:
    out, bad = set(), []
    for part in os.getenv(name, "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(date.fromisoformat(part).isoformat())
        except ValueError:
            bad.append(part)
    reason = f"{name}에 잘못된 날짜가 있습니다: {', '.join(bad)}" if bad else None
    return frozenset(out), reason


def _same_path(a: str, b: str) -> bool:
    return Path(a).expanduser().resolve() == Path(b).expanduser().resolve()


@dataclass(frozen=True)
class Settings:
    mode: str
    mode_block: str | None
    journal_path: str
    kill: bool
    tick_sec: int = 30
    quote_interval_sec: int = 60
    quote_max_age_sec: int = 120
    signal_cutoff_et: str = "22:00"
    open_check_wait_min: int = 30
    gap_max_sec: int = 300
    slip_bps: int = 50
    replace_after_sec: int = 300
    max_replaces_per_session: int = 3
    max_carry_over_sessions: int = 1
    session_start_offset_min: int = 0
    session_cutoff_min: int = 5
    quote_jump_pct: int = 25
    quote_confirm_tolerance_pct: int = 2
    calls_per_sec: float = 1.0
    per_tr_per_sec: float = 1.0
    max_calls_per_tick: int = 20
    max_watch: int = 40
    cooldown_429_sec: int = 60
    max_consecutive_errors: int = 5
    lease_sec: int = 90
    extra_closed_dates: frozenset = frozenset()
    extra_early_close_dates: frozenset = frozenset()

    @classmethod
    def from_env(cls) -> "Settings":
        raw = os.getenv("AUTO_BUY_US_RT_MODE", "OFF").strip().upper() or "OFF"
        journal = os.getenv("AUTO_BUY_US_RT_JOURNAL_PATH", "").strip()
        s01 = os.getenv("SCANNER_ORDER_JOURNAL_PATH", "").strip()
        closed, r1 = _dates("AUTO_BUY_US_RT_EXTRA_CLOSED_DATES")
        early, r2 = _dates("AUTO_BUY_US_RT_EXTRA_EARLY_CLOSE_DATES")

        mode, block = raw, None
        if raw not in MODES:
            mode = "OFF"
            block = (f"AUTO_BUY_US_RT_MODE={raw} 는 허용되지 않습니다 (LIVE 포함). OFF 로 처리합니다."
                     if raw == "LIVE" else f"알 수 없는 AUTO_BUY_US_RT_MODE={raw}. OFF 로 처리합니다.")
        elif journal and s01 and _same_path(journal, s01):
            mode, block = "OFF", "RT 저널 경로가 S01 주문 저널(SCANNER_ORDER_JOURNAL_PATH)과 같아 OFF 로 처리합니다."
        elif r1 or r2:
            mode, block = "OFF", r1 or r2
        return cls(mode=mode, mode_block=block, journal_path=journal,
                   kill=_flag("AUTO_BUY_US_RT_KILL"),
                   extra_closed_dates=closed, extra_early_close_dates=early)
