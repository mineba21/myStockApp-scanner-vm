"""자동매수 위험 예산 (A6·A9·A10, Sleeve). 운영 ``scanner.sizing.calculate_position`` 은 호출만 한다.

- Entry Heat (A9, 신규 매수 예산 판단): Σ (확정 체결가 − 고정 손절가) × 남은 수량 + 미체결 주문의 최대 예정 위험
  (지정가 − 손절가) × 남은 주문 수량. 손절선 이탈 미청산도 위험 0 으로 보지 않는다 (체결가 기준이라 그대로 남는다).
- Current Heat (A10, 표시·경고 전용): Σ max(종가 − 손절가, 0) × 수량 + 미체결 예정 위험. 이탈 포지션은 체결가 기준 위험 유지.
  10% 경고, 15% 위험 경고 (관찰용). **Current Heat 비율만으로 매수를 막지 않는다.**
- 자동매수 Heat 에는 자동매수(가상) 포지션·미체결 예약만 넣는다 (A6). 수동 보유는 계좌 전체 평가·투자 비중·집중도·현금 계산에
  넣고, 손절가 없는 수동 보유 위험은 0 이 아닌 **미확정 위험**(평가금액 × 15% 보수 추정)으로 따로 표시한다.
- OPS(운영 검증형): 위험·종목·섹터 비중은 자동매수 전용 예산(계좌 순자산 × 30%) 기준. 계좌 전체 현금 하한 20%.
  주가 상승으로 상한을 넘으면 신규 매수만 제한하고 강제 매도하지 않는다. 섹터 불명 후보는 상한을 우회할 수 없도록 차단한다.
"""
from __future__ import annotations

import math
from decimal import ROUND_FLOOR, Decimal
from typing import Optional

from trading.auto_buy.settings import (ACCOUNT_TOTAL_RISK_WATCH, COSTS, CURRENT_HEAT_DANGER, CURRENT_HEAT_WARN,
                                       LIMIT_PCT, MANUAL_ASSUMED_RISK_PCT)
from trading.auto_buy.shadow.vfills import unit_cost

R_BAND = {"r_pct_below_min", "r_pct_above_max", "stop_must_be_below_entry"}
UNKNOWN_SECTOR = "UNKNOWN"


def _fin(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)


def sector_of(name) -> str:
    s = str(name or "").strip()
    return s if s and s.upper() not in ("UNKNOWN", "NONE", "N/A", "-") else UNKNOWN_SECTOR


def heat_level(ratio: float) -> str:
    if not _fin(ratio):
        return "UNKNOWN"
    return "DANGER" if ratio >= CURRENT_HEAT_DANGER else ("WARN" if ratio >= CURRENT_HEAT_WARN else "OK")


def limit_price(decision_price: float) -> float:
    """판단가 × (1 + 1%) 를 센트 단위로 **내림** (미국 주식 지정가는 0.01 단위). 반올림하면 +1% 상한을 넘을 수 있어
    내림으로 상한 초과를 막는다. 부동소수점 오차 없이 10진수로 계산한다 (판단가의 최단 10진 표현 기준)."""
    lim = (Decimal(repr(float(decision_price))) * (1 + Decimal(repr(LIMIT_PCT)))).quantize(Decimal("0.01"),
                                                                                           rounding=ROUND_FLOOR)
    return float(lim)


def account_state(prof: dict, positions: list, reservations: list, cash: float, realized: float,
                  initial: Optional[float], acct: Optional[dict]) -> dict:
    """positions: {ticker, sector, qty, entry_price, stop_price, cost, close(None=결측), unresolved(bool)}
    reservations: {ticker, sector, reserved_cash, planned_risk, unknown(bool: FILL_UNKNOWN 주문)}.

    가격 미확정(결측·미복구) 포지션은 마지막 확인 종가로 평가 금액만 표시하고 위험은 체결가 기준을 유지한다
    (위험 0 처리·삭제 없음). 전액 손실 가정 스트레스 금액을 따로 계산한다."""
    invested = cost_open = he = hc = 0.0
    unresolved_value = stress = 0.0
    breached, missing, unresolved = [], [], []
    sec_val: dict = {}
    pos_val: dict = {}
    for p in positions:
        px = p["close"]
        stale = bool(p.get("unresolved")) or not _fin(px)
        if stale:
            missing.append(p["ticker"])
            px = p.get("last_close") or p["entry_price"]
        v = p["qty"] * px
        invested += v
        cost_open += p["cost"]
        pos_val[p["ticker"]] = pos_val.get(p["ticker"], 0.0) + v
        s = sector_of(p.get("sector"))
        sec_val[s] = sec_val.get(s, 0.0) + v
        r_entry = max(0.0, p["entry_price"] - p["stop_price"]) * p["qty"]
        he += r_entry
        if stale:
            unresolved.append(p["ticker"])
            unresolved_value += v
            stress += v
            hc += r_entry
        elif px < p["stop_price"]:
            breached.append(p["ticker"])
            hc += r_entry
        else:
            hc += (px - p["stop_price"]) * p["qty"]
    reserved = sum(r["reserved_cash"] for r in reservations)
    pend_risk = sum(r["planned_risk"] for r in reservations)
    unknown_orders = [r["ticker"] for r in reservations if r.get("unknown")]
    stress += sum(r["reserved_cash"] for r in reservations if r.get("unknown"))
    for r in reservations:
        s = sector_of(r.get("sector"))
        sec_val[s] = sec_val.get(s, 0.0) + r["reserved_cash"]
    st = {"invested": invested, "cost_open": cost_open, "reserved": reserved, "pending_risk": pend_risk,
          "entry_heat_usd": he + pend_risk, "current_heat_usd": hc + pend_risk, "breached": breached,
          "missing": missing, "unresolved": unresolved, "unknown_orders": unknown_orders,
          "unresolved_value": unresolved_value, "stress_full_loss": stress,
          "sector_values": sec_val, "position_values": pos_val, "cash": cash,
          "realized": realized, "unrealized": invested - cost_open, "acct": None}
    if prof["kind"] == "standalone":
        equity = cash + invested
        st.update(equity=equity, heat_base=equity, sizing_equity=equity, sleeve_budget=None, sleeve_used=None,
                  free_cash=cash - reserved, min_cash_floor=prof["min_cash_pct"] * equity)
    else:
        sleeve_used = invested + reserved
        if acct is None:
            st.update(equity=(initial or 0.0) + cash + invested if initial else float("nan"),
                      heat_base=float("nan"), sizing_equity=float("nan"), sleeve_budget=None,
                      sleeve_used=sleeve_used, free_cash=float("nan"), min_cash_floor=float("nan"))
        else:
            # 가상 매매가 실제 계좌에서 일어났다면의 계좌 (cash 는 가상 매매 누적 현금 흐름, 시작 0)
            eq_if = acct["equity"] + cash + invested
            cash_if = acct["cash"] + cash
            sleeve = prof["sleeve_pct"] * eq_if
            manual_val = sum(h["value"] for h in acct["holdings"])
            manual_unc = manual_val * MANUAL_ASSUMED_RISK_PCT / 100.0
            acc_pos = dict(pos_val)
            acc_sec = {k: v for k, v in sec_val.items()}
            for h in acct["holdings"]:
                acc_pos[h["ticker"]] = acc_pos.get(h["ticker"], 0.0) + h["value"]
                s = sector_of(h.get("sector"))
                acc_sec[s] = acc_sec.get(s, 0.0) + h["value"]
            st["acct"] = {
                "equity_as_if": eq_if, "cash_as_if": cash_if, "real_equity": acct["equity"],
                "real_cash": acct["cash"], "manual_value": manual_val, "manual_count": len(acct["holdings"]),
                "manual_uncertain_risk": manual_unc,
                "manual_unknown_sector_value": acc_sec.get(UNKNOWN_SECTOR, 0.0) - sec_val.get(UNKNOWN_SECTOR, 0.0),
                "account_total_risk_pct": (he + pend_risk + manual_unc) / eq_if if eq_if > 0 else float("nan"),
                "account_total_risk_watch": ACCOUNT_TOTAL_RISK_WATCH,
                "invested_pct": (manual_val + invested) / eq_if if eq_if > 0 else float("nan"),
                "cash_pct": cash_if / eq_if if eq_if > 0 else float("nan"),
                "max_stock_w": max(acc_pos.values()) / eq_if if acc_pos and eq_if > 0 else 0.0,
                "max_sector_w": max(acc_sec.values()) / eq_if if acc_sec and eq_if > 0 else 0.0,
                "max_sector": max(acc_sec, key=acc_sec.get) if acc_sec else None,
                "as_of": acct.get("as_of"),
            }
            st.update(equity=(initial if initial else sleeve) + cash + invested, heat_base=sleeve,
                      sizing_equity=sleeve, sleeve_budget=sleeve, sleeve_used=sleeve_used,
                      free_cash=cash_if - reserved, min_cash_floor=prof["min_cash_pct"] * eq_if)
    base = st["heat_base"]
    ok = _fin(base) and base > 0
    st["entry_heat"] = st["entry_heat_usd"] / base if ok else float("nan")
    st["current_heat"] = st["current_heat_usd"] / base if ok else float("nan")
    st["current_heat_level"] = heat_level(st["current_heat"])
    st["max_pos_w"] = max(pos_val.values()) / base if pos_val and ok else 0.0
    st["max_sector_w"] = max(sec_val.values()) / base if sec_val and ok else 0.0
    return st


def safety_blocks(prof: dict, st: dict, acct_available: bool, stale_pending: int) -> list:
    """신규 매수를 막는 안전 조건 (Current Heat 비율은 여기에 넣지 않는다)."""
    b = []
    if st["breached"]:
        b.append("STOP_BREACHED_OPEN")
    if st["missing"]:
        b.append("PRICE_DATA_MISSING")
    if stale_pending or st.get("unknown_orders"):
        b.append("PENDING_UNRESOLVED")
    if prof["kind"] == "sleeve" and not acct_available:
        b.append("ACCOUNT_SNAPSHOT_UNAVAILABLE")
    if not (_fin(st["heat_base"]) and st["heat_base"] > 0) and "ACCOUNT_SNAPSHOT_UNAVAILABLE" not in b:
        b.append("RISK_UNCOMPUTABLE")
    return b


def size_order(prof: dict, st: dict, decision_price: float, stop: float, sector: str) -> tuple:
    """(qty, reason, info). reason None 이면 주문. 상태(st)는 호출 측이 주문 후 갱신한다."""
    from scanner.sizing import calculate_position

    slip, bc = COSTS["slippage_bp"] / 10000.0, COSTS["buy_commission"]
    lim = limit_price(decision_price)
    unit = unit_cost(lim)
    cfg = {k: prof[k] for k in ("RISK_PCT", "MAX_POSITION_PCT", "MAX_TOTAL_HEAT_PCT", "MIN_R_PCT", "MAX_R_PCT")}
    info = {"limit_price": lim, "unit_cost": unit}
    free = st["free_cash"]
    avail = free - st["min_cash_floor"]
    if prof["kind"] == "standalone":
        if avail < unit:
            return 0, ("MIN_CASH_RESERVE" if free >= unit else "INSUFFICIENT_CASH"), info
        cash_cap = avail
    else:
        sec = sector_of(sector)
        if sec == UNKNOWN_SECTOR:
            return 0, "SECTOR_UNKNOWN", info
        sleeve_room = st["sleeve_budget"] - st["sleeve_used"]
        if sleeve_room < unit:
            return 0, "SLEEVE_FULL", info
        if avail < unit:
            return 0, ("ACCOUNT_MIN_CASH" if free >= unit else "INSUFFICIENT_CASH"), info
        sector_room = prof["sector_cap_pct"] / 100.0 * st["sleeve_budget"] - st["sector_values"].get(sec, 0.0)
        if sector_room < unit:
            return 0, "SECTOR_CAP", info
        cash_cap = min(sleeve_room, avail, sector_room)
        info.update(sleeve_room=sleeve_room, sector_room=sector_room)
    info["cash_cap"] = cash_cap
    res = calculate_position(lim, stop, st["sizing_equity"], max(0.0, cash_cap) / (1 + slip) / (1 + bc),
                             st["entry_heat_usd"], "US", cfg)
    qty = int(res["qty"])
    info.update(constrained_by=res.get("constrained_by"), r_pct=res.get("r_pct"))
    if qty <= 0:
        rs = res.get("reasons") or ["unknown"]
        return 0, ("R_BAND" if rs[0] in R_BAND else str(rs[0]).upper()), info
    info["planned_risk"] = qty * max(0.0, lim - stop)
    info["reserved_cash"] = qty * unit
    return qty, None, info
