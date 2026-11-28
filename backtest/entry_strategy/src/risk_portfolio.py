"""P7 위험 예산 시뮬레이터 (Entry Heat / Current Heat 분리, 현금 하한, 차단 사유, 집중도).

portfolio.run 과 같은 하루 순서·체결·S01 경로를 쓰고 다음을 추가한다.

- Entry Heat  = Σ(체결가 − 고정 손절가) × 수량  + 미체결 매수의 (지정가 상한 − 손절가) × 주문수량
- Current Heat = Σ max(종가 − 손절가, 0) × 수량 + 미체결 예약 (운영 calculate_open_risk 방식)
- 둘 다 그날 종가 기준 순자산 대비 비율. 신규 매수 수량은 운영 ``scanner.sizing.calculate_position`` 에
  heat_def 로 고른 위험 합계를 open_risk_sum 으로 넘겨 계산한다 (운영 코드 변경 없음).
- min_cash_pct: 현금 − 예약 − 순자산×비율 만큼만 매수 가능 현금으로 넘긴다.
- current_block: Current Heat(신규 주문 위험 포함)가 이 비율 이상이면 신규 매수 차단.
- 손절가 미등록 수동 보유: SPY 로 대리한 수동 보유 비중을 첫날 매수해 유지하고, 위험 = 평가금액 × assumed_risk_pct
  로 두 Heat 에 합산한다 (0 이면 현행 운영처럼 위험 0).
- 손절선 이탈 미청산(종가 < 손절가인데 아직 보유): 위험 해소로 보지 않는다. Current Heat 계산에서
  (진입가 − 손절가) × 수량을 유지하고 별도 카운트하며 그동안 신규 매수를 차단한다.
  (일봉 S01 경로에서는 저가 ≤ 손절가이면 그날 청산되므로 데이터 결측일 외에는 발생하지 않는다.)
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np

from .portfolio import SimInput

R_BAND = {"r_pct_below_min", "r_pct_above_max", "stop_must_be_below_entry"}


@dataclass
class RiskProfile:
    equity: float
    RISK_PCT: float
    MAX_TOTAL_HEAT_PCT: float
    MAX_POSITION_PCT: float
    min_cash_pct: float = 0.0
    heat_def: str = "entry"                 # entry | current
    current_block: Optional[float] = None
    current_warn: float = 0.10
    manual_pct: float = 0.0
    manual_risk_pct: float = 0.0
    MIN_R_PCT: float = 3.0
    MAX_R_PCT: float = 15.0

    def sizing_cfg(self) -> dict:
        return {k: getattr(self, k) for k in ("RISK_PCT", "MAX_POSITION_PCT", "MAX_TOTAL_HEAT_PCT",
                                              "MIN_R_PCT", "MAX_R_PCT")}


def run(si: SimInput, closes: Dict[str, np.ndarray], start: int, end: int, eligible: np.ndarray,
        rank: np.ndarray, top_n: int, prof: RiskProfile, costs: dict, sectors: Dict[str, str],
        manual_series: Optional[np.ndarray] = None) -> dict:
    from scanner.sizing import calculate_position

    slip = costs["slippage_bp"] / 10000.0
    buy_c = costs["buy_commission"]
    sell_c = costs["sell_commission"] + costs["sell_tax"]
    cfg = prof.sizing_cfg()

    idx = np.where(eligible & si.placeable & (si.decision_ci >= start) & (si.decision_ci < end))[0]
    by_dec: Dict[int, List[int]] = {}
    for c in idx[np.lexsort((idx, rank[idx], si.decision_ci[idx]))]:
        by_dec.setdefault(int(si.decision_ci[c]), []).append(int(c))

    cash = prof.equity
    manual_qty = 0.0
    if prof.manual_pct > 0 and manual_series is not None:
        px0 = manual_series[start]
        manual_qty = prof.equity * prof.manual_pct / (px0 * (1 + slip) * (1 + buy_c))
        cash -= manual_qty * px0 * (1 + slip) * (1 + buy_c)
    reserved = 0.0
    pos: Dict[str, dict] = {}
    pending: Dict[str, dict] = {}
    fills_at: Dict[int, List[str]] = {}
    expire_at: Dict[int, List[str]] = {}
    exits_at: Dict[int, List[str]] = {}
    trades: List[dict] = []
    n = end - start + 1
    rec = {k: np.zeros(n) for k in ("equity", "invested", "heat_entry", "heat_current", "max_pos_w",
                                    "max_sector_w", "cash_ratio", "manual_value")}
    npos = np.zeros(n, dtype=int)
    counts = {"decided": 0, "orders": 0, "filled": 0, "unfilled": 0, "skip_open_stop": 0}
    block: Dict[str, int] = {}
    bind: Dict[str, int] = {}
    warn_days = 0
    breached_days = 0
    worst_day = 0.0

    def blk(k):
        block[k] = block.get(k, 0) + 1

    def close_pos(tk, i, px, reason):
        nonlocal cash
        p = pos.pop(tk)
        proceeds = p["qty"] * px * (1 - slip) * (1 - sell_c)
        cash += proceeds
        pnl = proceeds - p["cost"]
        trades.append({"cid": p["cid"], "ticker": tk, "entry_ci": p["entry_ci"], "exit_ci": i, "qty": p["qty"],
                       "entry_px": p["entry_px"], "exit_px": px, "stop": p["stop"], "cost": p["cost"], "pnl": pnl,
                       "risk0": p["risk0"], "R": pnl / p["risk0"] if p["risk0"] > 0 else math.nan,
                       "ret": pnl / p["cost"], "reason": reason, "open": False, "hold_days": i - p["entry_ci"] + 1,
                       "gap_excess": (p["stop"] - px) * p["qty"] if px < p["stop"] else 0.0})

    prev_eq = prof.equity
    for k, i in enumerate(range(start, end + 1)):
        for tk in fills_at.pop(i, []):
            od = pending.pop(tk, None)
            if od is None:
                continue
            c = od["cid"]
            reserved -= od["reserve"]
            px = si.fill_px[c]
            qty = od["qty"]
            unit = px * (1 + slip) * (1 + buy_c)
            if qty * unit > cash - reserved:
                qty = int(max(0.0, cash - reserved) // unit)
            if qty <= 0 or px <= si.stop[c]:             # 체결가 재검증: 손절가 이하 체결 금지
                counts["unfilled"] += 1
                continue
            cost = qty * unit
            cash -= cost
            counts["filled"] += 1
            pos[tk] = {"cid": c, "qty": qty, "entry_px": px, "stop": si.stop[c], "cost": cost, "entry_ci": i,
                       "risk0": qty * (unit - si.stop[c] * (1 - slip) * (1 - sell_c))}
            xc = si.exit_ci[c]
            if 0 <= xc <= end:
                if xc == i:
                    close_pos(tk, i, si.exit_px[c], si.exit_reason[c])
                else:
                    exits_at.setdefault(int(xc), []).append(tk)
        for tk in exits_at.pop(i, []):
            if tk in pos:
                close_pos(tk, i, si.exit_px[pos[tk]["cid"]], si.exit_reason[pos[tk]["cid"]])
        for tk in expire_at.pop(i, []):
            od = pending.get(tk)
            if od is not None and od["expire_ci"] == i:
                pending.pop(tk)
                reserved -= od["reserve"]
                counts["skip_open_stop" if od["skip_open"] else "unfilled"] += 1
        # 종가 평가
        invested = 0.0
        hc = he = 0.0
        breached = 0
        sec_val: Dict[str, float] = {}
        max_pv = 0.0
        for tk, p in pos.items():
            px = closes[tk][i]
            if not px == px:
                px = p["entry_px"]
            v = p["qty"] * px
            invested += v
            max_pv = max(max_pv, v)
            s = sectors.get(tk, "Unknown")
            sec_val[s] = sec_val.get(s, 0.0) + v
            he += max(0.0, p["entry_px"] - p["stop"]) * p["qty"]
            if px < p["stop"]:
                breached += 1
                hc += max(0.0, p["entry_px"] - p["stop"]) * p["qty"]
            else:
                hc += (px - p["stop"]) * p["qty"]
        mval = manual_qty * manual_series[i] if manual_qty else 0.0
        mrisk = mval * prof.manual_risk_pct / 100.0
        he += mrisk
        hc += mrisk
        equity = cash + invested + mval
        pend_risk = sum(od["planned_risk"] for od in pending.values())
        rec["equity"][k] = equity
        rec["invested"][k] = invested
        rec["manual_value"][k] = mval
        rec["heat_entry"][k] = (he + pend_risk) / equity
        rec["heat_current"][k] = (hc + pend_risk) / equity
        rec["max_pos_w"][k] = max_pv / equity
        rec["max_sector_w"][k] = max(sec_val.values()) / equity if sec_val else 0.0
        rec["cash_ratio"][k] = cash / equity
        npos[k] = len(pos)
        worst_day = min(worst_day, equity / prev_eq - 1)
        prev_eq = equity
        if rec["heat_current"][k] >= prof.current_warn:
            warn_days += 1
        if breached:
            breached_days += 1
        if i >= end:
            continue
        cands = by_dec.get(i)
        if not cands:
            continue
        open_risk = (he if prof.heat_def == "entry" else hc) + pend_risk
        cur_risk = hc + pend_risk
        placed = 0
        for c in cands:
            counts["decided"] += 1
            tk = si.ticker[c]
            if tk in pos or tk in pending:
                blk("duplicate_holding_or_order"); continue
            if placed >= top_n:
                blk("daily_top_n"); continue
            if breached:
                blk("stop_breached_open_position"); continue
            planned = si.planned_px[c]
            unit = planned * (1 + slip) * (1 + buy_c)
            free = cash - reserved
            avail = free - prof.min_cash_pct * equity
            if avail < unit and free >= unit:
                blk("min_cash_reserve"); continue
            res = calculate_position(planned, si.stop[c], equity, max(0.0, avail) / (1 + slip) / (1 + buy_c),
                                     open_risk, "US", cfg)
            qty = int(res["qty"])
            if qty <= 0:
                rs = res["reasons"] or ["unknown"]
                blk("r_band" if rs[0] in R_BAND else rs[0]); continue
            prisk = qty * max(0.0, planned - si.stop[c])
            if prof.current_block is not None and (cur_risk + prisk) / equity >= prof.current_block:
                blk("current_heat_block"); continue
            bind[res["constrained_by"]] = bind.get(res["constrained_by"], 0) + 1
            reserve = qty * unit
            reserved += reserve
            open_risk += prisk
            cur_risk += prisk
            od = {"cid": c, "qty": qty, "reserve": reserve, "planned_risk": prisk,
                  "expire_ci": int(si.expire_ci[c]), "skip_open": bool(si.skip_open[c])}
            pending[tk] = od
            placed += 1
            counts["orders"] += 1
            if si.status_filled[c] and si.fill_ci[c] <= end:
                fills_at.setdefault(int(si.fill_ci[c]), []).append(tk)
            else:
                ex = int(si.expire_ci[c])
                od["expire_ci"] = min(ex, end) if ex >= 0 else end
                expire_at.setdefault(od["expire_ci"], []).append(tk)
    open_rows = []
    for tk, p in pos.items():
        px = closes[tk][end]
        if not px == px:
            px = p["entry_px"]
        val = p["qty"] * px * (1 - slip) * (1 - sell_c)
        open_rows.append({"cid": p["cid"], "ticker": tk, "entry_ci": p["entry_ci"], "exit_ci": end, "qty": p["qty"],
                          "entry_px": p["entry_px"], "exit_px": px, "stop": p["stop"], "cost": p["cost"],
                          "pnl": val - p["cost"], "risk0": p["risk0"],
                          "R": (val - p["cost"]) / p["risk0"] if p["risk0"] > 0 else math.nan,
                          "ret": val / p["cost"] - 1, "reason": "OPEN", "open": True,
                          "hold_days": end - p["entry_ci"] + 1, "mkt_value": p["qty"] * px, "gap_excess": 0.0})
    return {"rec": rec, "positions": npos, "trades": trades, "open": open_rows, "counts": counts,
            "block": block, "bind": bind, "warn_days": warn_days, "breached_days": breached_days,
            "worst_day": worst_day, "start": start, "end": end, "initial": prof.equity, "pool_size": int(len(idx))}
