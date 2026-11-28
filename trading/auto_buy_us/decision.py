"""SHADOW/LIVE 공통 판단 (순수 함수): 진입 계획, 개장 확인, 손절 판정, 청산 지정가."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Optional

from trading.auto_buy_us import strategy
from trading.auto_buy_us.settings import RISK, STRATEGIES
from trading.auto_buy_us.sizing import SleeveState, sector_of, size_order
from trading.auto_stop import quotes

ZERO = Decimal("0")
MIN_EXIT_PRICE = Decimal("1.00")


@dataclass(frozen=True)
class EntryPlan:
    strategy: str
    session_day: str
    signal_id: int
    ticker: str
    exchange: Optional[str]
    verdict: str   # PLANNED, INELIGIBLE, HELD, RANK_CUT, SIZE_ZERO, EXCHANGE_UNRESOLVED
    reasons: list
    qty: int
    decision_price: Decimal
    limit_price: Optional[Decimal]
    stop_price: Decimal
    planned_risk: float
    reserved_cash: float
    entry_heat: float


def _dec(v) -> Decimal:
    """유한 Decimal 로 변환. 없거나 비정상이면 0."""
    try:
        d = Decimal(str(v))
    except Exception:
        return ZERO
    return d if d.is_finite() else ZERO


def _plan(sig, strat, day, exch, verdict, reasons, state, qty=0, lim=None, risk=0.0, reserved=0.0, heat=None):
    return EntryPlan(strategy=strat, session_day=day, signal_id=sig["id"], ticker=sig["ticker"], exchange=exch,
                     verdict=verdict, reasons=list(reasons), qty=qty, decision_price=_dec(sig.get("price")),
                     limit_price=lim, stop_price=_dec(sig.get("stop_loss")), planned_risk=risk,
                     reserved_cash=reserved, entry_heat=state.entry_heat_usd if heat is None else heat)


def plan_entries(signals: list, strategy_name: str, state: SleeveState, held: set,
                 exchanges: dict, session_day: str) -> list:
    """신호마다 계획 행 1개. 반환 순서: 순위 정렬된 적격 신호(PLANNED/SIZE_ZERO/RANK_CUT) 뒤에
    나머지(INELIGIBLE/HELD/EXCHANGE_UNRESOLVED)를 입력 순서대로. PLANNED 마다 state 를 제자리 갱신한다."""
    cfg = STRATEGIES[strategy_name]
    topn = cfg["topn"]
    by_id: dict = {}
    candidates: list = []
    for sig in signals:
        ok, reasons = strategy.evaluate(sig)[cfg["pool"]]
        exch = exchanges.get(sig["ticker"])
        if not ok:
            by_id[sig["id"]] = _plan(sig, strategy_name, session_day, exch, "INELIGIBLE", reasons, state)
        elif sig["ticker"] in held:
            by_id[sig["id"]] = _plan(sig, strategy_name, session_day, exch, "HELD", ["HELD"], state)
        elif exch is None:
            by_id[sig["id"]] = _plan(sig, strategy_name, session_day, exch, "EXCHANGE_UNRESOLVED",
                                     ["EXCHANGE_UNRESOLVED"], state)
        else:
            candidates.append(sig)

    out: list = []
    for rank, sig in enumerate(strategy.ordered(candidates), start=1):
        exch = exchanges[sig["ticker"]]
        if topn is not None and rank > topn:
            out.append(_plan(sig, strategy_name, session_day, exch, "RANK_CUT", ["RANK_CUT"], state))
            continue
        price, stop = Decimal(str(sig["price"])), Decimal(str(sig["stop_loss"]))
        qty, reason, info = size_order(state, price, stop, sig.get("sector_name"))
        if reason is not None:
            out.append(_plan(sig, strategy_name, session_day, exch, "SIZE_ZERO", [reason], state,
                             lim=info.get("limit_price")))
            continue
        state.reserved_cash += info["reserved_cash"]
        state.entry_heat_usd += info["planned_risk"]
        sec = sector_of(sig.get("sector_name"))
        state.sector_values[sec] = state.sector_values.get(sec, 0.0) + info["reserved_cash"]
        out.append(_plan(sig, strategy_name, session_day, exch, "PLANNED", [], state, qty=qty,
                         lim=info["limit_price"], risk=info["planned_risk"], reserved=info["reserved_cash"]))
    out_ids = {p.signal_id for p in out}
    rest = [by_id[s["id"]] for s in signals if s["id"] in by_id and s["id"] not in out_ids]
    return out + rest


@dataclass(frozen=True)
class OpenVerdict:
    ok: bool
    reason: Optional[str] = None


def open_check(stop: Decimal, price: Decimal) -> OpenVerdict:
    if price <= stop:
        return OpenVerdict(False, "OPEN_BELOW_STOP")
    if (price - stop) / price * 100 < Decimal(str(RISK["MIN_R_PCT"])):
        return OpenVerdict(False, "OPEN_R_TOO_SMALL")
    return OpenVerdict(True, None)


@dataclass(frozen=True)
class StopVerdict:
    action: Literal["NONE", "TRIGGER", "CONFIRM", "REVIEW"]
    reason: Optional[str] = None


def stop_check(stop: Decimal, price: Decimal, prev_price: Optional[Decimal], settings) -> StopVerdict:
    if price > stop:
        return StopVerdict("NONE")
    if quotes.is_jump(prev_price, price, settings):
        return StopVerdict("CONFIRM", "QUOTE_JUMP")
    return StopVerdict("TRIGGER")


def confirm_stop(first: Decimal, second: Decimal, stop: Decimal, settings) -> StopVerdict:
    if not quotes.consistent(first, second, settings):
        return StopVerdict("REVIEW", "QUOTE_JUMP_UNCONFIRMED")
    return StopVerdict("TRIGGER") if second <= stop else StopVerdict("NONE")


def exit_limit(price: Decimal, settings) -> Decimal:
    """매도 지정가. 기준가 또는 결과가 $1 미만이면 ValueError (호출 측이 PRICE_BELOW_1 로 검토 처리)."""
    if price < MIN_EXIT_PRICE:
        raise ValueError("PRICE_BELOW_1")
    lim = quotes.limit_price(price, settings.slip_bps)
    if lim < MIN_EXIT_PRICE:
        raise ValueError("PRICE_BELOW_1")
    return lim
