"""개별 검사. 각 검사는 CheckContext를 받아 CheckResult 하나를 돌려준다.

검사를 추가할 때는 함수 하나를 쓰고 BUY_CHECKS / SELL_CHECKS / COMMON_CHECKS 중
해당 목록에 넣으면 된다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from .config import GuardConfig
from .infra import LedgerView, is_regular_session
from .models import AccountSnapshot, CheckResult, OrderIntent, OrderType


@dataclass(frozen=True)
class CheckContext:
    intent: OrderIntent
    account: AccountSnapshot
    ledger: LedgerView
    config: GuardConfig
    now: datetime
    kill_switch_active: bool
    kill_reason: str

    @property
    def sleeve_equity(self) -> float:
        """사이징 기준 자금 = min(계좌 평가금액, 에이전트 자금 상한)."""
        return min(self.account.equity, self.config.capital_cap)


Check = Callable[[CheckContext], CheckResult]


def _ok(name: str, msg: str = "ok", max_qty: int | None = None) -> CheckResult:
    return CheckResult(name, True, msg, max_qty)


def _fail(name: str, msg: str) -> CheckResult:
    return CheckResult(name, False, msg)


# ---------------- 공통 ----------------

def check_kill_switch(ctx: CheckContext) -> CheckResult:
    if ctx.kill_switch_active:
        return _fail("kill_switch", f"킬 스위치 활성: {ctx.kill_reason}")
    return _ok("kill_switch")


def check_intent_sanity(ctx: CheckContext) -> CheckResult:
    i = ctx.intent
    problems = []
    if not i.symbol:
        problems.append("종목코드 없음")
    if not isinstance(i.qty, int) or isinstance(i.qty, bool) or i.qty < 1:
        problems.append(f"수량은 1 이상의 정수여야 함 (받은 값 {i.qty!r})")
    if not (isinstance(i.reference_price, (int, float)) and math.isfinite(i.reference_price)
            and i.reference_price > 0):
        problems.append("기준가가 유효하지 않음")
    if i.order_type is OrderType.LIMIT and (i.limit_price is None or not i.limit_price > 0):
        problems.append("지정가 주문인데 지정가가 없거나 0 이하")
    if problems:
        return _fail("intent_sanity", "; ".join(problems))
    return _ok("intent_sanity")


def check_market_enabled(ctx: CheckContext) -> CheckResult:
    if ctx.intent.market not in ctx.config.enabled_markets:
        return _fail("market_enabled", f"{ctx.intent.market} 시장은 허용 목록에 없음")
    return _ok("market_enabled")


def check_order_type(ctx: CheckContext) -> CheckResult:
    if ctx.intent.order_type.value not in ctx.config.allowed_order_types:
        return _fail("order_type", f"{ctx.intent.order_type.value} 주문은 허용되지 않음")
    return _ok("order_type")


def check_symbol_lists(ctx: CheckContext) -> CheckResult:
    s, cfg = ctx.intent.symbol, ctx.config
    if s in cfg.symbol_blocklist:
        return _fail("symbol_lists", f"{s} 는 차단 목록에 있음")
    if cfg.symbol_allowlist and s not in cfg.symbol_allowlist:
        return _fail("symbol_lists", f"{s} 는 허용 목록에 없음")
    return _ok("symbol_lists")


def check_market_hours(ctx: CheckContext) -> CheckResult:
    cfg = ctx.config
    if not cfg.market_hours_only:
        return _ok("market_hours", "장 시간 검사 꺼짐")
    if not is_regular_session(ctx.now, cfg.market_timezone, cfg.market_holidays):
        return _fail("market_hours", "정규장 시간이 아님")
    return _ok("market_hours")


def check_price_deviation(ctx: CheckContext) -> CheckResult:
    i = ctx.intent
    if i.limit_price is None:
        return _ok("price_deviation", "지정가 없음")
    dev = abs(i.limit_price - i.reference_price) / i.reference_price
    if dev > ctx.config.max_price_deviation_pct:
        return _fail("price_deviation",
                     f"지정가 {i.limit_price:g} 가 기준가 {i.reference_price:g} 대비 {dev:.1%} 벗어남")
    return _ok("price_deviation", f"괴리 {dev:.2%}")


def check_duplicate(ctx: CheckContext) -> CheckResult:
    window = timedelta(minutes=ctx.config.duplicate_window_minutes)
    for r in ctx.ledger.recent:
        if r.symbol == ctx.intent.symbol and r.side == ctx.intent.side.value and ctx.now - r.ts < window:
            mins = int((ctx.now - r.ts).total_seconds() // 60)
            return _fail("duplicate", f"{mins}분 전 같은 종목·방향 주문이 있음 ({r.decision_id})")
    return _ok("duplicate")


# ---------------- 매도 ----------------

def check_sell_within_holdings(ctx: CheckContext) -> CheckResult:
    held = ctx.account.held_qty(ctx.intent.symbol)
    if ctx.config.allow_short:
        return _ok("sell_holdings", "공매도 허용 설정")
    if ctx.intent.qty > held:
        return _fail("sell_holdings", f"보유 {held}주보다 많은 {ctx.intent.qty}주 매도 (공매도 금지)")
    return _ok("sell_holdings", f"보유 {held}주")


# ---------------- 매수 ----------------

def check_stop(ctx: CheckContext) -> CheckResult:
    i, cfg = ctx.intent, ctx.config
    if i.stop_price is None:
        if cfg.require_stop_for_buy:
            return _fail("stop", "손절가 없는 매수는 허용되지 않음")
        return _ok("stop", "손절가 없음 (검사 꺼짐)")
    entry = i.entry_price
    if not i.stop_price < entry:
        return _fail("stop", f"손절가 {i.stop_price:g} 가 진입가 {entry:g} 이상")
    dist = (entry - i.stop_price) / entry
    if dist > cfg.max_stop_distance_pct:
        return _fail("stop", f"손절 거리 {dist:.1%} 가 상한 {cfg.max_stop_distance_pct:.0%} 초과")
    return _ok("stop", f"손절 거리 {dist:.1%}")


def check_open_positions(ctx: CheckContext) -> CheckResult:
    held = ctx.account.open_symbols
    if ctx.intent.symbol not in held and len(held) >= ctx.config.max_open_positions:
        return _fail("open_positions", f"보유 종목 {len(held)}개로 상한 {ctx.config.max_open_positions}개 도달")
    return _ok("open_positions", f"보유 {len(held)}개")


def check_daily_orders(ctx: CheckContext) -> CheckResult:
    n, cap = ctx.ledger.orders_today, ctx.config.max_orders_per_day
    if n >= cap:
        return _fail("daily_orders", f"오늘 매수 주문 {n}건으로 상한 {cap}건 도달")
    return _ok("daily_orders", f"오늘 {n}/{cap}건")


def check_new_positions_today(ctx: CheckContext) -> CheckResult:
    if ctx.intent.symbol in ctx.account.open_symbols:
        return _ok("new_positions_today", "기존 보유 종목 추가매수")
    n, cap = len(ctx.ledger.new_positions_today), ctx.config.max_new_positions_per_day
    if n >= cap:
        return _fail("new_positions_today", f"오늘 신규 종목 {n}개로 상한 {cap}개 도달")
    return _ok("new_positions_today", f"오늘 신규 {n}/{cap}개")


def check_position_sizing(ctx: CheckContext) -> CheckResult:
    """R 기준 사이징과 금액 한도들 중 가장 작은 수량을 상한으로 낸다."""
    i, cfg, acct = ctx.intent, ctx.config, ctx.account
    entry = i.entry_price
    sleeve = ctx.sleeve_equity
    held_value = acct.held_qty(i.symbol) * entry

    limits: dict[str, int] = {}
    if i.stop_price is not None and i.stop_price < entry:
        risk_budget = sleeve * cfg.risk_per_trade_pct
        limits["R 리스크"] = math.floor(risk_budget / (entry - i.stop_price))
    elif cfg.require_stop_for_buy:
        return _fail("position_sizing", "유효한 손절가가 없어 사이징 불가")

    position_cap = min(cfg.max_position_notional, sleeve * cfg.max_position_pct)
    limits["종목 금액"] = math.floor(max(position_cap - held_value, 0) / entry)
    remaining_day = cfg.max_buy_notional_per_day - ctx.ledger.buy_notional_today
    limits["일일 매수액"] = math.floor(max(remaining_day, 0) / entry)
    limits["현금"] = math.floor(max(acct.cash, 0) / entry)

    max_qty = max(min(limits.values()), 0)
    detail = ", ".join(f"{k} {v}주" for k, v in limits.items())
    if max_qty < 1:
        return _fail("position_sizing", f"한도 안에서 1주도 살 수 없음 ({detail})")
    return _ok("position_sizing", f"최대 {max_qty}주 ({detail})", max_qty=max_qty)


COMMON_CHECKS: tuple[Check, ...] = (
    check_kill_switch,
    check_intent_sanity,
    check_market_enabled,
    check_order_type,
    check_symbol_lists,
    check_market_hours,
    check_price_deviation,
    check_duplicate,
)
BUY_CHECKS: tuple[Check, ...] = (
    check_stop,
    check_open_positions,
    check_daily_orders,
    check_new_positions_today,
    check_position_sizing,
)
SELL_CHECKS: tuple[Check, ...] = (check_sell_within_holdings,)
