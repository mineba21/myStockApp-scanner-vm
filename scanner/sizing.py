"""Van Tharp R-multiple position sizing.

이 모듈은 의도적으로 DB와 애플리케이션 설정을 import하지 않는다. 호출자가
시장별 설정과 자산/보유 스냅샷을 넘기면 결정적인 dict를 반환한다.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping


DEFAULTS = {
    "RISK_PCT": 1.0,
    "MAX_POSITION_PCT": 20.0,
    "MAX_TOTAL_HEAT_PCT": 6.0,
    "MIN_R_PCT": 3.0,
    "MAX_R_PCT": 15.0,
}


def _cfg_value(cfg: Any, name: str) -> float:
    if isinstance(cfg, Mapping):
        value = cfg.get(name, DEFAULTS[name])
    else:
        value = getattr(cfg, name, DEFAULTS[name])
    return float(value)


def _empty_result(market: str, reasons: list[str], *,
                  constrained_by: str = "none",
                  r_per_share: float | None = None,
                  r_pct: float | None = None) -> dict:
    return {
        "qty": 0,
        "r_per_share": r_per_share,
        "risk_amount": 0.0,
        "position_value": 0.0,
        "position_pct": 0.0,
        "risk_pct_actual": 0.0,
        "constrained_by": constrained_by,
        "reasons": reasons,
        "currency": "KRW" if str(market).upper() == "KR" else "USD",
        "r_pct": r_pct,
        "calculated_qty": 0,
    }


def calculate_position(entry: float, stop: float, equity: float, cash: float,
                       open_risk_sum: float, market: str, cfg) -> dict:
    """네 가지 리스크 제약과 현금 한도로 정수 매수 수량을 계산한다.

    적용 순서는 S1 종목 리스크 → S2 포지션 상한 → S3 heat → S4 R 범위이며,
    현금 한도는 마지막에 적용한다. S4는 수량을 줄이는 제약이 아니라 거래
    가능 여부를 결정하는 gate이므로 범위 밖이면 즉시 0주를 반환한다.
    """
    market = str(market).upper()
    if market not in {"KR", "US"}:
        return _empty_result(market, ["invalid_market"])

    values = (entry, stop, equity, cash, open_risk_sum)
    try:
        entry, stop, equity, cash, open_risk_sum = map(float, values)
    except (TypeError, ValueError):
        return _empty_result(market, ["invalid_numeric_input"])
    if not all(math.isfinite(v) for v in (entry, stop, equity, cash, open_risk_sum)):
        return _empty_result(market, ["invalid_numeric_input"])
    if entry <= 0:
        return _empty_result(market, ["entry_must_be_positive"])
    if equity <= 0:
        return _empty_result(market, ["equity_must_be_positive"])
    if cash < 0:
        return _empty_result(market, ["cash_must_not_be_negative"])

    r_per_share = entry - stop
    r_pct = r_per_share / entry * 100.0
    if r_per_share <= 0:
        return _empty_result(
            market, ["stop_must_be_below_entry"],
            r_per_share=r_per_share, r_pct=r_pct,
        )

    min_r_pct = _cfg_value(cfg, "MIN_R_PCT")
    max_r_pct = _cfg_value(cfg, "MAX_R_PCT")
    if r_pct < min_r_pct:
        return _empty_result(
            market, ["r_pct_below_min"], constrained_by="none",
            r_per_share=r_per_share, r_pct=r_pct,
        )
    if r_pct > max_r_pct:
        return _empty_result(
            market, ["r_pct_above_max"], constrained_by="none",
            r_per_share=r_per_share, r_pct=r_pct,
        )

    # S1 — 종목당 리스크 예산
    risk_budget = equity * _cfg_value(cfg, "RISK_PCT") / 100.0
    risk_qty = max(0, math.floor(risk_budget / r_per_share))
    qty = risk_qty
    constrained_by = "risk"
    reasons: list[str] = []
    if qty == 0:
        reasons.append("risk_budget_too_small")

    # S2 — 종목당 비중 상한
    position_cap_qty = max(
        0,
        math.floor(equity * _cfg_value(cfg, "MAX_POSITION_PCT") / 100.0 / entry),
    )
    if position_cap_qty < qty:
        qty = position_cap_qty
        constrained_by = "position_cap"
        if qty == 0:
            reasons.append("position_cap_too_small")

    # S3 — 시장별 총 heat 한도
    heat_cap = equity * _cfg_value(cfg, "MAX_TOTAL_HEAT_PCT") / 100.0
    remaining_heat = max(0.0, heat_cap - max(0.0, open_risk_sum))
    heat_qty = max(0, math.floor(remaining_heat / r_per_share))
    if heat_qty < qty:
        qty = heat_qty
        constrained_by = "heat"
        if qty == 0:
            reasons.append("heat_limit_exceeded")

    # 추가 현금 제약 — 미국도 증권사 소수점 수량을 사용하지 않는다.
    cash_qty = max(0, math.floor(cash / entry))
    if cash_qty < qty:
        qty = cash_qty
        constrained_by = "cash"
        if qty == 0:
            reasons.append("insufficient_cash")

    risk_amount = qty * r_per_share
    position_value = qty * entry
    return {
        "qty": int(qty),
        "r_per_share": round(r_per_share, 6),
        "risk_amount": round(risk_amount, 6),
        "position_value": round(position_value, 6),
        "position_pct": round(position_value / equity * 100.0, 4),
        "risk_pct_actual": round(risk_amount / equity * 100.0, 4),
        "constrained_by": constrained_by,
        "reasons": reasons,
        "currency": "KRW" if market == "KR" else "USD",
        "r_pct": round(r_pct, 4),
        "calculated_qty": int(risk_qty),
        "position_cap_qty": int(position_cap_qty),
        "heat_cap_qty": int(heat_qty),
        "cash_cap_qty": int(cash_qty),
        "remaining_heat_amount": round(remaining_heat, 6),
    }


def _value(row: Any, name: str):
    if isinstance(row, Mapping):
        return row.get(name)
    return getattr(row, name, None)


def calculate_open_risk(holdings: Iterable[Any], market: str) -> dict:
    """한 시장의 활성 보유 스냅샷에서 현재 open risk를 합산한다.

    stop 또는 현재가를 알 수 없는 행은 추정하지 않는다. 손절선 아래로 이미
    내려간 보유는 risk=0으로 처리하되 별도 경고 카운트에 포함한다.
    """
    market = str(market).upper()
    total = 0.0
    missing_stop = 0
    missing_price = 0
    breached_stop = 0

    for holding in holdings:
        if str(_value(holding, "market") or "").upper() != market:
            continue
        quantity = _value(holding, "quantity")
        stop = _value(holding, "current_stop_loss")
        current = _value(holding, "current_price")
        if not quantity or float(quantity) <= 0:
            continue
        if stop is None:
            missing_stop += 1
            continue
        if current is None:
            missing_price += 1
            continue
        current = float(current)
        stop = float(stop)
        if current < stop:
            breached_stop += 1
            continue
        total += max(0.0, current - stop) * float(quantity)

    warnings = []
    if missing_stop:
        warnings.append(f"손절가 미등록 {missing_stop}건")
    if missing_price:
        warnings.append(f"현재가 미등록 {missing_price}건")
    if breached_stop:
        warnings.append(f"손절선 이탈 보유 {breached_stop}건")
    return {
        "open_risk_sum": round(total, 6),
        "missing_stop_count": missing_stop,
        "missing_price_count": missing_price,
        "breached_stop_count": breached_stop,
        "warnings": warnings,
    }
