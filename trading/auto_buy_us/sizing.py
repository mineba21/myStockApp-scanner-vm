"""가상 sleeve 상태·Entry/Current Heat·주문 수량 (순수 함수). 계좌 현금 하한 없음 (설계 §18-4)."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal

from trading.auto_buy_us.settings import COSTS, LIMIT_PCT, RISK, SECTOR_CAP_PCT

UNKNOWN_SECTOR = "UNKNOWN"
R_BAND = {"r_pct_below_min", "r_pct_above_max", "stop_must_be_below_entry"}


def sector_of(name) -> str:
    s = str(name or "").strip()
    return s if s and s.upper() not in ("UNKNOWN", "NONE", "N/A", "-") else UNKNOWN_SECTOR


def limit_price(decision_price: Decimal) -> Decimal:
    """판단가 +1% 를 센트 단위로 내림 (상한 초과 방지)."""
    return (Decimal(decision_price) * (1 + LIMIT_PCT)).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)


def unit_cost(px) -> float:
    return float(px) * (1 + COSTS["slippage_bp"] / 10000.0) * (1 + COSTS["buy_commission"])


def unit_proceeds(px) -> float:
    return (float(px) * (1 - COSTS["slippage_bp"] / 10000.0)
            * (1 - COSTS["sell_commission"] - COSTS["sell_tax"]))


@dataclass
class SleeveState:
    cash: float
    equity: float
    positions_value: float
    reserved_cash: float
    entry_heat_usd: float
    current_heat_usd: float | None
    sector_values: dict = field(default_factory=dict)
    unpriced: list = field(default_factory=list)


def sleeve_state(cash: float, positions: list[dict], open_buys: list[dict]) -> SleeveState:
    """equity = cash + Σ qty×(last 또는 entry). 예약금은 체결 전까지 cash 안에 있으므로 더하지 않는다 (R4)."""
    value = entry_heat = cur_heat = 0.0
    sectors: dict = {}
    unpriced: list = []
    for p in positions:
        qty, entry, stop, last = p["qty"], p["entry_price"], p["initial_stop"], p.get("last_price")
        v = qty * (last if last is not None else entry)
        value += v
        s = sector_of(p.get("sector"))
        sectors[s] = sectors.get(s, 0.0) + v
        entry_heat += max(entry - stop, 0.0) * qty
        if last is None:
            unpriced.append(p["ticker"])
        else:
            cur_heat += max(last - stop, 0.0) * qty
    reserved = sum(b["reserved_cash"] for b in open_buys)
    planned = sum(b["planned_risk"] for b in open_buys)
    for b in open_buys:
        s = sector_of(b.get("sector"))
        sectors[s] = sectors.get(s, 0.0) + b["reserved_cash"]
    return SleeveState(cash=cash, equity=cash + value, positions_value=value, reserved_cash=reserved,
                       entry_heat_usd=entry_heat + planned,
                       current_heat_usd=None if unpriced else cur_heat + planned,
                       sector_values=sectors, unpriced=unpriced)


def size_order(st: SleeveState, decision_price: Decimal, stop: Decimal, sector) -> tuple[int, str | None, dict]:
    """(qty, reason, info). reason None 이면 주문. 상태 갱신은 호출 측이 한다."""
    from scanner.sizing import calculate_position

    lim = limit_price(decision_price)
    unit = unit_cost(lim)
    info = {"limit_price": lim}
    sec = sector_of(sector)
    if sec == UNKNOWN_SECTOR:
        return 0, "SECTOR_UNKNOWN", info
    avail = st.cash - st.reserved_cash
    if avail < unit:
        return 0, "INSUFFICIENT_CASH", info
    room = SECTOR_CAP_PCT / 100.0 * st.equity - st.sector_values.get(sec, 0.0)
    if room < unit:
        return 0, "SECTOR_CAP", info
    slip, bc = COSTS["slippage_bp"] / 10000.0, COSTS["buy_commission"]
    res = calculate_position(float(lim), float(stop), st.equity, min(avail, room) / (1 + slip) / (1 + bc),
                             st.entry_heat_usd, "US", RISK)
    qty = int(res["qty"])
    info.update(r_pct=res.get("r_pct"), constrained_by=res.get("constrained_by"))
    if qty <= 0:
        rs = res.get("reasons") or ["unknown"]
        return 0, ("R_BAND" if rs[0] in R_BAND else str(rs[0]).upper()), info
    info["planned_risk"] = qty * max(0.0, float(lim - stop))
    info["reserved_cash"] = qty * unit
    return qty, None, info
