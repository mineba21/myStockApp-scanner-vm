"""자동매수 설정 (환경변수, 호출 시점에 다시 읽는다).

- ``AUTO_BUY_US_MODE``: OFF(기본) / SHADOW. LIVE 는 이번 범위 밖이라 **거부**하고 OFF 로 동작한다
  (``live_requested=True`` 로 health 에 표시). LIVE 주문 경로 자체가 코드에 없다.
- ``AUTO_BUY_SHADOW_JOURNAL_PATH``: SHADOW 전용 SQLite 절대경로. 기본값 없음(미설정이면 SHADOW 도 동작하지 않음).
  운영 주문 저널(SCANNER_ORDER_JOURNAL_PATH)·ETF 저널(ALLOCATION_JOURNAL_PATH)과 같은 파일이면 거부한다.
- 전략·위험 설정은 코드에 동결한다 (``STRATEGY_VERSION``). 바꾸려면 버전을 올려 새 가상 계좌로 시작한다.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

MODES = ("OFF", "SHADOW")
STRATEGY_VERSION = "SV1"
FILL_MODEL = "DAILY_T1_100_v1"

# 비용 (정책 확정 2026-11-25): 가상 체결·손익은 P7 백테스트와 **같은 비교용 비용**으로 계산한다
# (backtest/entry_strategy/configs/entry_backtest.yaml costs 와 동일. 대조 테스트가 일치를 확인한다).
COST_BASIS = "P7_COMPARISON_v1"
COSTS = {"buy_commission": 0.0025, "sell_commission": 0.0025, "sell_tax": 0.0000278, "slippage_bp": 10}
# 실제 운영 추정 거래비용 — 비교용 비용과 구분해 기록만 한다. 시뮬레이션 손익에 쓰지 않는다.
# 증권사 실제 수수료율·환전 비용·규제 수수료를 확인하지 않은 값이며 실제 체결 비용으로 보지 않는다.
OPS_COST_ESTIMATE = {
    "buy_commission": 0.0025, "sell_commission": 0.0025, "sell_tax": 0.0, "slippage_bp": 10,
    "fx_cost": None,
    "status": "UNVERIFIED",
    "note": "운영 추정치(미검증). 실제 수수료·환전·SEC fee 미확인. 비교용 손익에 쓰지 않음",
}
LIMIT_PCT = 0.010          # 판단 종가 +1.0% 지정가, 다음 정규장 1일
MIN_R_PCT = 3.0
MAX_R_PCT = 15.0

# Current Heat 관찰 기준 (A10: 검증된 최적 한도가 아닌 운영 관찰용, 매수 차단에 쓰지 않는다)
CURRENT_HEAT_WARN = 0.10
CURRENT_HEAT_DANGER = 0.15
# 손절가 없는 수동 보유의 보수적 위험 추정 (A6: 표시용 미확정 위험. 0 으로 표시하지 않는다)
MANUAL_ASSUMED_RISK_PCT = 15.0
# 계좌 전체 위험 관찰 기준 (LIVE 전 확정 필요. SHADOW 에서는 표시만)
ACCOUNT_TOTAL_RISK_WATCH = 0.10

B02_SEEDS = tuple(range(1, 21))     # 고정 seed 20개 (S4)

# 거래일 입력 확정 (P1-1·P1-3). 시각은 해당 거래일의 미국 동부시간.
REGULAR_SCAN_TRIGGER = "scheduler"   # 정규 Scanner 실행 (ScanLog.triggered_by). 이 스캔 1회로 당일 후보를 확정한다
SIGNAL_CUTOFF_ET = "22:00"           # 이때까지 정규 스캔이 완전 수집되지 않으면 그날은 NO_DECISION_SCAN (신규 매수 없음)
BARS_CUTOFF_ET = "23:20"             # 이때까지 필요한 일봉이 모두 모이지 않으면 DATA_INCOMPLETE 로 확정 (결측을 정상으로 보지 않음)
BAR_MIN_DELAY_MIN = 60               # 정규장 마감 후 이 시간이 지나기 전 일봉은 잠정치로 보고 쓰지 않는다
RECOVERY_WINDOW_DAYS = 20            # 결측 일봉을 다시 받아 보는 기간 (거래일). 이후는 미복구로 남긴다
RECOVERY_MAX_DAYS_PER_RUN = 10       # job 1회에 재조회하는 결측 거래일 수 상한
MAX_SCAN_HOURS = 6                   # 실행권이 비어 있고 이보다 오래된 RUNNING 스캔은 중단(ABANDONED)으로 본다
LONG_MISSING_DAYS = 5                # 연속 결측이 이 이상이면 '거래정지·상장폐지 의심'으로 표시 (판정 아님)

# 운영 정책 (2026-11-25 사용자 확정. 바꾸려면 버전을 올린다)
POLICY = {
    "unresolved_position": "가격 미확정 포지션은 신규 매수 차단을 유지한다. 가격·위험 확인 없이 자동 청산하거나 차단을 해제하지 않는다",
    "substitute_decision_scan": False,      # 정규 스캔 실패·불완전일에 대체 결정 스캔을 허용하지 않는다 (NO_DECISION_SCAN)
    "signal_cutoff_et": SIGNAL_CUTOFF_ET,
    "bars_cutoff_et": BARS_CUTOFF_ET,
    "early_close": "신호·데이터 마감은 조기폐장 마감 시각에 맞춰 당긴다 (마감 + 6h / + 7h20m)",
    "recovery_window_days": RECOVERY_WINDOW_DAYS,
    "after_recovery_window": "결측 위험·불완전 상태를 유지하고 수동 검토 대상(MANUAL_REVIEW)으로 표시한다",
    "account_reconcile_tolerance": "max($5, 0.5%) — 초기 검증 기준",
    "cost_basis": COST_BASIS,
}

# S5 채택 검토 기준 (평가 시작 전 고정, 표시·검토용. 충족해도 LIVE 를 승인하지 않는다)
S5_CRITERIA = {
    "milestones": {"safety_days": 5, "integrity_days": 20, "interim_days": 60, "min_b01_closed_trades": 40},
    "performance": {
        "return_vs_b00n": "B01 누적수익률 >= 같은 프로필 B00-N",
        "pf": "PF > 1.0 이고 >= B00-N",
        "avg_r": "평균 R (미청산 평가 포함) > 0 이고 >= B00-N",
        "mdd_worsening_max_pp": 2.0,
        "b02_percentile_rank_review": 75.0,
        "include_open_pnl": True,
        "safety_violations_max": 0,
    },
    "effects": {"filter": "B01 vs B00-N", "ranking": "B01 vs B02 분포"},
    "verdict_hold": "NOT_RECOMMENDED 유지, B01 = NEED_MORE_DATA. 40건 확보만으로 LIVE 승인 없음",
}

PROFILES = {
    # P7 비교형: 기존 P7 계산 기준 그대로 (독립 $50k 가상 계좌)
    "P7C": {
        "label": "P7 비교형", "kind": "standalone", "initial_cash": 50000.0,
        "RISK_PCT": 0.75, "MAX_TOTAL_HEAT_PCT": 4.0, "MAX_POSITION_PCT": 15.0,
        "MIN_R_PCT": MIN_R_PCT, "MAX_R_PCT": MAX_R_PCT, "min_cash_pct": 0.20,
        "sleeve_pct": None, "sector_cap_pct": None,
    },
    # 초기 운영 검증형: 실제 account1 순자산의 30% 를 자동매수 전용 예산으로. 위험·비중은 전용 예산 기준
    "OPS": {
        "label": "운영 검증형", "kind": "sleeve", "initial_cash": None,
        "RISK_PCT": 0.75, "MAX_TOTAL_HEAT_PCT": 4.0, "MAX_POSITION_PCT": 15.0,
        "MIN_R_PCT": MIN_R_PCT, "MAX_R_PCT": MAX_R_PCT, "min_cash_pct": 0.20,   # 계좌 전체 현금 하한
        "sleeve_pct": 0.30, "sector_cap_pct": 35.0,
    },
}

STRATEGIES = {
    "B00": {"label": "Strict 전체 + Grade", "pool": "B00", "rank": "GRADE", "topn": None},
    "B00N": {"label": "B00 + 하루 2종목", "pool": "B00", "rank": "GRADE", "topn": 2},
    "B01": {"label": "P7 B01 필터 + Grade + 하루 2", "pool": "B01", "rank": "GRADE", "topn": 2},
    "B02": {"label": "B01 후보 무작위 Top-2", "pool": "B01", "rank": "RANDOM", "topn": 2},
}


def frozen_config() -> dict:
    return {"version": STRATEGY_VERSION, "fill_model": FILL_MODEL, "costs": COSTS, "cost_basis": COST_BASIS,
            "ops_cost_estimate": OPS_COST_ESTIMATE, "policy": POLICY, "limit_pct": LIMIT_PCT,
            "profiles": PROFILES, "strategies": STRATEGIES, "b02_seeds": list(B02_SEEDS),
            "inputs": {"regular_trigger": REGULAR_SCAN_TRIGGER, "signal_cutoff_et": SIGNAL_CUTOFF_ET,
                       "bars_cutoff_et": BARS_CUTOFF_ET, "bar_min_delay_min": BAR_MIN_DELAY_MIN,
                       "recovery_window_days": RECOVERY_WINDOW_DAYS, "max_scan_hours": MAX_SCAN_HOURS},
            "heat": {"warn": CURRENT_HEAT_WARN, "danger": CURRENT_HEAT_DANGER,
                     "manual_assumed_risk_pct": MANUAL_ASSUMED_RISK_PCT, "account_total_watch": ACCOUNT_TOTAL_RISK_WATCH},
            "s5": S5_CRITERIA,
            "b01": "first strict pass == first detection, market BULL, BREAKOUT/RE_BREAKOUT pivot ext >= 0, "
                   "no chase warning, stop <= 15% (REBOUND: no pivot conditions)"}


def config_hash() -> str:
    raw = json.dumps(frozen_config(), sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def account_ids() -> list[tuple[str, str, str, Optional[int]]]:
    """(account_id, profile, strategy, seed) 목록. 버전이 계좌 ID 에 들어가 이전 버전 계좌를 덮어쓰지 않는다."""
    out = []
    for prof in PROFILES:
        for strat in ("B00", "B00N", "B01"):
            out.append((f"{STRATEGY_VERSION}:{prof}:{strat}", prof, strat, None))
        for seed in B02_SEEDS:
            out.append((f"{STRATEGY_VERSION}:{prof}:B02:s{seed:02d}", prof, "B02", seed))
    return out


def _same_file(a: str, b: str) -> bool:
    if not a or not b:
        return False
    try:
        return Path(a).expanduser().resolve() == Path(b).expanduser().resolve()
    except OSError:
        return False


@dataclass(frozen=True)
class AutoBuySettings:
    mode: str = "OFF"
    requested_mode: str = "OFF"
    live_requested: bool = False
    journal_path: str = ""
    config_error: Optional[str] = None
    job_minutes: int = 20
    start_day: Optional[str] = None
    extra_closed_dates: frozenset = field(default_factory=frozenset)
    extra_early_close_dates: frozenset = field(default_factory=frozenset)

    @property
    def shadow_enabled(self) -> bool:
        return self.mode == "SHADOW" and not self.config_error

    @classmethod
    def from_env(cls) -> "AutoBuySettings":
        raw = os.getenv("AUTO_BUY_US_MODE", "OFF").strip().upper()
        live = raw == "LIVE"
        mode = raw if raw in MODES else "OFF"
        path = os.getenv("AUTO_BUY_SHADOW_JOURNAL_PATH", "").strip()
        err = None
        if live:
            err = "AUTO_BUY_US_MODE=LIVE 는 이번 구현 범위 밖이라 거부합니다 (OFF 로 동작, 실주문 경로 없음)."
        elif raw not in MODES:
            err = f"알 수 없는 AUTO_BUY_US_MODE={raw!r} — OFF 로 동작합니다."
        if mode == "SHADOW":
            if not path:
                err = "AUTO_BUY_SHADOW_JOURNAL_PATH 가 설정되지 않아 SHADOW 를 실행하지 않습니다."
            elif not os.path.isabs(path):
                err = "AUTO_BUY_SHADOW_JOURNAL_PATH 는 절대경로여야 합니다."
            else:
                for other in ("SCANNER_ORDER_JOURNAL_PATH", "ALLOCATION_JOURNAL_PATH"):
                    if _same_file(path, os.getenv(other, "").strip()):
                        err = f"AUTO_BUY_SHADOW_JOURNAL_PATH 가 {other} 와 같은 파일입니다. 운영 저널과 분리해야 합니다."
        start = os.getenv("AUTO_BUY_SHADOW_START_DAY", "").strip() or None
        try:
            minutes = min(max(int(os.getenv("AUTO_BUY_SHADOW_JOB_MINUTES", "20")), 5), 60)
        except ValueError:
            minutes = 20
        dates = lambda n: frozenset(p.strip() for p in os.getenv(n, "").split(",") if p.strip())  # noqa: E731
        return cls(mode=mode, requested_mode=raw, live_requested=live, journal_path=path, config_error=err,
                   job_minutes=minutes, start_day=start,
                   extra_closed_dates=dates("AUTO_BUY_US_EXTRA_CLOSED_DATES"),
                   extra_early_close_dates=dates("AUTO_BUY_US_EXTRA_EARLY_CLOSE_DATES"))
