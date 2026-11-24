"""자동 손절 설정. 모든 값은 환경변수이며 호출 시점에 다시 읽는다 (주문 직전 재검증용)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal

MODES = ("OFF", "SHADOW", "LIVE")
SHADOW_FILL_MODELS = ("IMMEDIATE", "NEXT_QUOTE", "STRESS")
MAX_ORDER_HTTP_TIMEOUT_SEC = 30.0     # 주문·취소 HTTP 응답 대기 상한 (lease 90초보다 충분히 짧게)

# 미검증 API 계약. 운영자가 조회·실주문 검증을 마친 항목만 AUTO_STOP_US_VERIFIED_CONTRACTS 에 적는다.
CONTRACTS = {
    "USA20101_QUOTE_TIME": "usa20101 현재가·dt/bid_tm 이 정규장 실시간 시세로 갱신됨 (조회 검증)",
    "UST21070_SELLABLE": "ust21070 sell_alowq 가 매도가능수량과 일치 (조회 검증)",
    "UST21150_STATUS": "ust21150 주문·체결·잔여·취소 수량과 상태 매핑 (조회 검증)",
    "USA20101_BID": "usa20101 매수1호가 필드명 (조회 검증, KIWOOM_US_ORDERBOOK_*_FIELD 설정)",
    "UST20001_SELL": "account1 실계좌 ust20001 지정가 매도 접수·체결 (1주 실매도 검증)",
    "UST20003_PARTIAL_CANCEL": "ust20003 부분체결 주문 취소 후 잔여 0·최종 체결 확정 (실주문 검증)",
    "POST_CLOSE_EXPIRY": "장 마감 후 미체결 지정가의 소멸 방식 (실주문 관찰)",
}
LIVE_REQUIRED = ("USA20101_QUOTE_TIME", "UST21070_SELLABLE", "UST21150_STATUS")
REPLACE_REQUIRED = ("UST20003_PARTIAL_CANCEL",)
ACCOUNT_PROFILE = "account1"
MARKET = "US"


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() == "true"


def _int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(os.getenv(name, str(default)).strip())
    except ValueError:
        value = default
    return min(max(value, lo), hi)


def _dec(name: str, default: str) -> Decimal:
    try:
        value = Decimal(os.getenv(name, default).strip())
    except Exception:
        value = Decimal(default)
    return value if value.is_finite() and value > 0 else Decimal(default)


def _dates(name: str) -> frozenset[str]:
    raw = os.getenv(name, "")
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


@dataclass(frozen=True)
class Settings:
    mode: str = "OFF"
    replace_enabled: bool = False            # LIVE 전용 (실계좌 자동 취소·재주문)
    shadow_replace_enabled: bool = False     # SHADOW 전용 (가상 취소·재주문). 서로 영향을 주지 않는다
    tick_sec: int = 30
    price_interval_sec: int = 60
    status_interval_sec: int = 30
    replace_after_sec: int = 300
    balance_sync_sec: int = 300
    max_replaces_per_session: int = 3
    max_replaces_per_event: int = 6
    max_carry_over_sessions: int = 1
    slip_bps: int = 50
    session_start_offset_min: int = 0
    session_cutoff_min: int = 5
    quote_max_age_sec: int = 120
    quote_jump_pct: int = 25
    quote_confirm_tolerance_pct: int = 2
    max_consecutive_errors: int = 5
    lease_sec: int = 90
    max_order_qty: int = 1000
    max_order_notional_usd: Decimal = Decimal("20000")
    max_active_positions: int = 20
    stale_signal_days: int = 30
    extra_closed_dates: frozenset = field(default_factory=frozenset)
    extra_early_close_dates: frozenset = field(default_factory=frozenset)
    journal_path: str = ""
    shadow_fill_model: str = "NEXT_QUOTE"
    verified_contracts: frozenset = field(default_factory=frozenset)

    def verified(self, name: str) -> bool:
        return name in self.verified_contracts

    def replace_enabled_for(self, mode: str) -> bool:
        """모드별 재주문 스위치. LIVE 는 REPLACE_ENABLED 만, SHADOW 는 SHADOW_REPLACE_ENABLED 만 본다."""
        if mode == "LIVE":
            return self.replace_enabled
        if mode == "SHADOW":
            return self.shadow_replace_enabled
        return False

    def live_qty_cap(self) -> int:
        """1주 실매도 검증 전에는 LIVE 주문을 1주로 제한한다."""
        return self.max_order_qty if self.verified("UST20001_SELL") else 1

    @classmethod
    def from_env(cls) -> "Settings":
        mode = os.getenv("AUTO_STOP_US_MODE", "OFF").strip().upper()
        model = os.getenv("AUTO_STOP_US_SHADOW_FILL_MODEL", "NEXT_QUOTE").strip().upper()
        contracts = frozenset(p.strip().upper() for p in os.getenv("AUTO_STOP_US_VERIFIED_CONTRACTS", "").split(",")
                              if p.strip().upper() in CONTRACTS)
        return cls(
            shadow_fill_model=model if model in SHADOW_FILL_MODELS else "NEXT_QUOTE",
            verified_contracts=contracts,
            mode=mode if mode in MODES else "OFF",
            replace_enabled=_flag("AUTO_STOP_US_REPLACE_ENABLED"),
            shadow_replace_enabled=_flag("AUTO_STOP_US_SHADOW_REPLACE_ENABLED"),
            tick_sec=_int("AUTO_STOP_US_TICK_SEC", 30, 10, 300),
            price_interval_sec=_int("AUTO_STOP_US_PRICE_INTERVAL_SEC", 60, 10, 900),
            status_interval_sec=_int("AUTO_STOP_US_STATUS_INTERVAL_SEC", 30, 10, 900),
            replace_after_sec=_int("AUTO_STOP_US_REPLACE_AFTER_SEC", 300, 60, 3600),
            balance_sync_sec=_int("AUTO_STOP_US_BALANCE_SYNC_SEC", 300, 30, 3600),
            max_replaces_per_session=_int("AUTO_STOP_US_MAX_REPLACES_PER_SESSION", 3, 0, 10),
            max_replaces_per_event=_int("AUTO_STOP_US_MAX_REPLACES_PER_EVENT", 6, 0, 30),
            max_carry_over_sessions=_int("AUTO_STOP_US_MAX_CARRY_OVER_SESSIONS", 1, 0, 5),
            slip_bps=_int("AUTO_STOP_US_SLIP_BPS", 50, 0, 500),
            session_start_offset_min=_int("AUTO_STOP_US_SESSION_START_OFFSET_MIN", 0, 0, 60),
            session_cutoff_min=_int("AUTO_STOP_US_SESSION_CUTOFF_MIN", 5, 1, 60),
            quote_max_age_sec=_int("AUTO_STOP_US_QUOTE_MAX_AGE_SEC", 120, 30, 600),
            quote_jump_pct=_int("AUTO_STOP_US_QUOTE_JUMP_PCT", 25, 5, 90),
            max_consecutive_errors=_int("AUTO_STOP_US_MAX_CONSECUTIVE_ERRORS", 5, 1, 100),
            lease_sec=_int("AUTO_STOP_US_LEASE_SEC", 90, 30, 600),
            max_order_qty=_int("AUTO_STOP_US_MAX_ORDER_QTY", 1000, 1, 1_000_000),
            max_order_notional_usd=_dec("AUTO_STOP_US_MAX_ORDER_NOTIONAL_USD", "20000"),
            max_active_positions=_int("AUTO_STOP_US_MAX_ACTIVE_POSITIONS", 20, 1, 200),
            stale_signal_days=_int("AUTO_STOP_US_STALE_SIGNAL_DAYS", 30, 1, 365),
            extra_closed_dates=_dates("AUTO_STOP_US_EXTRA_CLOSED_DATES"),
            extra_early_close_dates=_dates("AUTO_STOP_US_EXTRA_EARLY_CLOSE_DATES"),
            journal_path=os.getenv("SCANNER_ORDER_JOURNAL_PATH", "").strip(),
        )


def read_access_block_reason() -> str | None:
    """account1 실계좌 조회(시세·잔고·주문내역)에 필요한 조건."""
    if not _flag("KIWOOM_WEB_ENABLED"):
        return "KIWOOM_WEB_ENABLED가 꺼져 있어 키움 조회를 하지 않습니다."
    if not os.getenv("SCANNER_ORDER_JOURNAL_PATH", "").strip():
        return "SCANNER_ORDER_JOURNAL_PATH가 설정되지 않았습니다."
    return None


def live_order_block_reason(settings: Settings | None = None) -> str | None:
    """LIVE 자동 매도 전송 조건. 주문·취소 요청 직전에 매번 다시 호출한다."""
    settings = settings or Settings.from_env()
    if settings.mode != "LIVE":
        return f"AUTO_STOP_US_MODE={settings.mode} (LIVE 아님)"
    reason = read_access_block_reason()
    if reason:
        return reason
    for name in ("KIWOOM_TRADING_ENABLED", "ACCOUNT1_ORDER_ENABLED"):
        if not _flag(name):
            return f"{name}가 꺼져 있습니다."
    from trading.auto_stop.schema import schema_block_reason      # 순환 import 방지
    reason = schema_block_reason(settings.journal_path or os.getenv("SCANNER_ORDER_JOURNAL_PATH", ""))
    if reason:
        return "저널 스키마 미준비: " + reason
    missing = [c for c in LIVE_REQUIRED if not settings.verified(c)]
    if missing:
        return "미검증 API 계약: " + ", ".join(missing) + " (AUTO_STOP_US_VERIFIED_CONTRACTS)"
    return None


def live_cancel_block_reason(settings: Settings | None = None) -> str | None:
    """자동 취소(재주문 목적) 조건: LIVE 전송 조건 + 별도 재주문 스위치."""
    settings = settings or Settings.from_env()
    reason = live_order_block_reason(settings)
    if reason:
        return reason
    if not settings.replace_enabled:
        return "AUTO_STOP_US_REPLACE_ENABLED가 꺼져 있습니다."
    missing = [c for c in REPLACE_REQUIRED if not settings.verified(c)]
    if missing:
        return "미검증 취소 계약: " + ", ".join(missing)
    return None
