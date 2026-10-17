"""가드 설정. 기본값은 보수적인 예시값이며, 실제 값은 TOML 파일로 정한다."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields
from datetime import date
from pathlib import Path

from .models import OrderType


@dataclass(frozen=True)
class GuardConfig:
    # --- 범위 ---
    enabled_markets: tuple[str, ...] = ("US",)
    allowed_order_types: tuple[str, ...] = ("LIMIT",)
    symbol_allowlist: tuple[str, ...] = ()   # 비어 있으면 전체 허용
    symbol_blocklist: tuple[str, ...] = ()
    allow_short: bool = False

    # --- 에이전트 자금 (계좌 전체가 아니라 에이전트에 떼어준 금액) ---
    capital_cap: float = 5000.0

    # --- 포지션 사이징 (R 기준) ---
    risk_per_trade_pct: float = 0.01       # 1R = 에이전트 자금의 1%
    max_position_notional: float = 1000.0  # 종목당 최대 금액
    max_position_pct: float = 0.20         # 종목당 에이전트 자금 대비 최대 비중
    require_stop_for_buy: bool = True
    max_stop_distance_pct: float = 0.10    # 진입가 대비 손절 거리 상한
    allow_resize: bool = True              # 한도 초과 시 줄여서 허용할지

    # --- 일일/보유 한도 (매수에만 적용) ---
    max_open_positions: int = 5
    max_orders_per_day: int = 5
    max_new_positions_per_day: int = 2
    max_buy_notional_per_day: float = 2000.0

    # --- 주문 품질 ---
    max_price_deviation_pct: float = 0.03  # 지정가와 기준가 차이 상한 (팻핑거 방지)
    duplicate_window_minutes: int = 60

    # --- 시간 ---
    market_hours_only: bool = True
    market_timezone: str = "America/New_York"
    market_holidays: tuple[date, ...] = ()

    # --- 실행 ---
    require_human_approval: bool = True
    decision_ttl_minutes: int = 15
    live_trading: bool = False  # True여도 환경변수 RISK_GUARD_LIVE=1 이 있어야 실주문

    # --- 파일 경로 ---
    kill_switch_path: str = "./state/KILL"
    ledger_path: str = "./state/ledger.json"
    audit_log_path: str = "./state/audit.jsonl"

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        errors: list[str] = []
        for name in ("risk_per_trade_pct", "max_position_pct", "max_stop_distance_pct",
                     "max_price_deviation_pct"):
            v = getattr(self, name)
            if not 0 < v <= 1:
                errors.append(f"{name}={v} 는 0 초과 1 이하여야 함")
        for name in ("capital_cap", "max_position_notional", "max_buy_notional_per_day"):
            if getattr(self, name) <= 0:
                errors.append(f"{name} 는 0보다 커야 함")
        for name in ("max_open_positions", "max_orders_per_day", "max_new_positions_per_day",
                     "decision_ttl_minutes"):
            if getattr(self, name) < 1:
                errors.append(f"{name} 는 1 이상이어야 함")
        if self.duplicate_window_minutes < 0:
            errors.append("duplicate_window_minutes 는 0 이상이어야 함")
        if not self.enabled_markets:
            errors.append("enabled_markets 가 비어 있음")
        valid_types = {t.value for t in OrderType}
        bad = [t for t in self.allowed_order_types if t not in valid_types]
        if bad or not self.allowed_order_types:
            errors.append(f"allowed_order_types 값 오류: {bad or '비어 있음'}")
        if errors:
            raise ValueError("GuardConfig 오류:\n- " + "\n- ".join(errors))

    @classmethod
    def from_toml(cls, path: str | Path) -> "GuardConfig":
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        data = raw.get("guard", raw)
        known = {f.name: f for f in fields(cls)}
        unknown = sorted(set(data) - set(known))
        if unknown:  # 오타로 한도가 조용히 기본값이 되는 것을 막는다
            raise ValueError(f"알 수 없는 설정 키: {unknown}")
        kwargs = {}
        for key, value in data.items():
            if key == "market_holidays":
                value = tuple(v if isinstance(v, date) else date.fromisoformat(str(v)) for v in value)
            elif key in ("enabled_markets", "allowed_order_types", "symbol_allowlist", "symbol_blocklist"):
                value = tuple(str(v).upper() for v in value)
            kwargs[key] = value
        return cls(**kwargs)
