"""위험 기반 포트폴리오 시뮬레이터 (미국 단일 시장, USD, 정수 주식).

하루 순서 (캘린더 인덱스 i)
1. 매수 체결 (예약 현금 사용; 체결가 기준 실제 위험 기록). 같은 봉 손절이면 바로 청산.
2. S01 매도 체결 (사전 계산된 경로).
3. 미체결 주문 만료 → 예약 해제.
4. 종가 평가 (평가금액, 투자금, Heat).
5. 매수 결정 (그날 종가까지의 정보): 후보 필터 → 보유·주문 중 종목 제외 → 순위 → 상위 N개까지
   운영 ``scanner.sizing.calculate_position`` 으로 수량 계산 (계획가 = 지정가 상한).

- 구간 [start, end] 밖의 가격은 읽지 않는다. end 이후의 매도·체결은 무시하고 end 종가로 미청산 평가한다.
- 같은 종목 보유 중이거나 주문 대기 중이면 매수하지 않는다 (중복 매수·추가매수 금지).
- 한 신호(signal_key)는 한 번만 주문한다 (후보 1행 = 신호 1개).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd


@dataclass
class SimInput:
    """전략과 무관한 후보 단위 배열 (cid 순서)."""
    ticker: np.ndarray
    stop: np.ndarray
    decision_ci: np.ndarray
    planned_px: np.ndarray
    status_filled: np.ndarray
    fill_ci: np.ndarray            # -1 = 미체결
    fill_px: np.ndarray
    expire_ci: np.ndarray
    exit_ci: np.ndarray            # -1 = 미청산(데이터 끝까지)
    exit_px: np.ndarray
    exit_reason: np.ndarray
    placeable: np.ndarray          # 주문 계획이 있는 후보 (SKIP_NO_BAR·SKIP_UPTHRUST 는 False)
    skip_open: np.ndarray          # 시가 확인에서 손절가 이하·R 부족으로 전송 안 함
    market_open_order: bool = False


def make_input(cand: pd.DataFrame, orders: pd.DataFrame, exits: pd.DataFrame) -> SimInput:
    o = orders.reindex(cand["cid"])
    e = exits.reindex(cand["cid"])
    filled = (o["status"] == "FILLED").to_numpy()
    return SimInput(
        ticker=cand["ticker"].to_numpy(),
        stop=cand["stop"].to_numpy(float),
        decision_ci=o["decision_ci"].to_numpy(int),
        planned_px=o["planned_px"].astype(float).to_numpy(),
        status_filled=filled,
        fill_ci=o["fill_ci"].fillna(-1).astype(int).to_numpy(),
        fill_px=o["fill_px"].astype(float).to_numpy(),
        expire_ci=o["expire_ci"].fillna(-1).astype(int).to_numpy(),
        exit_ci=e["exit_ci"].fillna(-1).astype(int).to_numpy(),
        exit_px=e["exit_px"].astype(float).to_numpy(),
        exit_reason=e["reason"].fillna("").to_numpy(),
        placeable=o["status"].isin(["FILLED", "UNFILLED", "SKIP_OPEN_STOP"]).to_numpy(),
        skip_open=(o["status"] == "SKIP_OPEN_STOP").to_numpy(),
    )


@dataclass
class Profile:
    equity: float
    RISK_PCT: float
    MAX_POSITION_PCT: float
    MAX_TOTAL_HEAT_PCT: float
    MIN_R_PCT: float = 3.0
    MAX_R_PCT: float = 15.0

    def sizing_cfg(self) -> dict:
        return {k: getattr(self, k) for k in ("RISK_PCT", "MAX_POSITION_PCT", "MAX_TOTAL_HEAT_PCT",
                                              "MIN_R_PCT", "MAX_R_PCT")}


def run(si: SimInput, closes: Dict[str, np.ndarray], start: int, end: int, eligible: np.ndarray,
        rank: np.ndarray, top_n: int, prof: Profile, costs: dict, heat_def: str = "current",
        sizing_mode: str = "risk", fixed_pct: float = 0.10, max_positions: int = 10,
        record: bool = True) -> dict:
    """eligible: 후보별 bool (필터·시장 조건 반영). rank: 낮을수록 우선."""
    from scanner.sizing import calculate_position

    slip = costs["slippage_bp"] / 10000.0
    buy_c = costs["buy_commission"]
    sell_c = costs["sell_commission"] + costs["sell_tax"]
    cfg = prof.sizing_cfg()

    # 결정일별 후보 (우선순위 정렬)
    idx = np.where(eligible & si.placeable & (si.decision_ci >= start) & (si.decision_ci < end))[0]
    by_dec: Dict[int, List[int]] = {}
    for c in idx[np.lexsort((idx, rank[idx], si.decision_ci[idx]))]:
        by_dec.setdefault(int(si.decision_ci[c]), []).append(int(c))

    cash = prof.equity
    reserved = 0.0
    pos: Dict[str, dict] = {}         # ticker → position
    pending: Dict[str, dict] = {}     # ticker → order
    fills_at: Dict[int, List[str]] = {}
    expire_at: Dict[int, List[str]] = {}
    exits_at: Dict[int, List[str]] = {}
    trades: List[dict] = []
    n_days = end - start + 1
    eq = np.zeros(n_days); inv = np.zeros(n_days); npos = np.zeros(n_days, dtype=int)
    heat_cur = np.zeros(n_days); heat_ent = np.zeros(n_days)
    counts = {"decided": 0, "orders": 0, "filled": 0, "unfilled": 0, "skip_open_stop": 0,
              "skip_dup": 0, "skip_size0": 0, "skip_topn": 0, "fill_cash_reduced": 0}
    size_reasons: Dict[str, int] = {}

    def close_pos(tk, i, px, reason):
        nonlocal cash
        p = pos.pop(tk)
        proceeds = p["qty"] * px * (1 - slip) * (1 - sell_c)
        cash += proceeds
        pnl = proceeds - p["cost"]
        trades.append({"cid": p["cid"], "ticker": tk, "entry_ci": p["entry_ci"], "exit_ci": i,
                       "qty": p["qty"], "entry_px": p["entry_px"], "exit_px": px, "stop": p["stop"],
                       "cost": p["cost"], "pnl": pnl, "risk0": p["risk0"],
                       "R": pnl / p["risk0"] if p["risk0"] > 0 else math.nan,
                       "ret": pnl / p["cost"], "reason": reason, "open": False,
                       "hold_days": i - p["entry_ci"] + 1})

    for k, i in enumerate(range(start, end + 1)):
        # 1) 매수 체결
        for tk in fills_at.pop(i, []):
            od = pending.pop(tk, None)
            if od is None:
                continue
            c = od["cid"]
            reserved -= od["reserve"]
            px = si.fill_px[c]
            qty = od["qty"]
            unit = px * (1 + slip) * (1 + buy_c)
            avail = cash - reserved
            if qty * unit > avail:                 # 시가 매수(T0) 등 계획가 초과 시 수량 축소
                qty = int(avail // unit)
                counts["fill_cash_reduced"] += 1
            if qty <= 0:
                counts["unfilled"] += 1
                continue
            cost = qty * unit
            cash -= cost
            counts["filled"] += 1
            pos[tk] = {"cid": c, "qty": qty, "entry_px": px, "stop": si.stop[c], "cost": cost,
                       "risk0": qty * (px * (1 + slip) * (1 + buy_c) - si.stop[c] * (1 - slip) * (1 - sell_c)),
                       "entry_ci": i}
            xc = si.exit_ci[c]
            if 0 <= xc <= end:
                if xc == i:
                    close_pos(tk, i, si.exit_px[c], si.exit_reason[c])
                else:
                    exits_at.setdefault(int(xc), []).append(tk)
        # 2) S01 매도
        for tk in exits_at.pop(i, []):
            if tk in pos:
                c = pos[tk]["cid"]
                close_pos(tk, i, si.exit_px[c], si.exit_reason[c])
        # 3) 만료
        for tk in expire_at.pop(i, []):
            od = pending.get(tk)
            if od is not None and od["expire_ci"] == i:
                pending.pop(tk)
                reserved -= od["reserve"]
                if od["skip_open"]:
                    counts["skip_open_stop"] += 1
                else:
                    counts["unfilled"] += 1
        # 4) 종가 평가
        invested = 0.0
        hc = he = 0.0
        for tk, p in pos.items():
            px = closes[tk][i]
            if not px == px:
                px = p["entry_px"]
            invested += p["qty"] * px
            hc += max(0.0, px - p["stop"]) * p["qty"]
            he += max(0.0, p["entry_px"] - p["stop"]) * p["qty"]
        equity = cash + invested
        eq[k] = equity; inv[k] = invested; npos[k] = len(pos)
        heat_cur[k] = hc / equity if equity > 0 else 0.0
        heat_ent[k] = he / equity if equity > 0 else 0.0
        # 5) 매수 결정
        if i >= end:
            continue
        cands = by_dec.get(i)
        if not cands:
            continue
        open_risk = (hc if heat_def == "current" else he)
        open_risk += sum(od["planned_risk"] for od in pending.values())
        placed = 0
        for c in cands:
            counts["decided"] += 1
            tk = si.ticker[c]
            if tk in pos or tk in pending:
                counts["skip_dup"] += 1
                continue
            if placed >= top_n:
                counts["skip_topn"] += 1
                continue
            planned = si.planned_px[c]
            unit = planned * (1 + slip) * (1 + buy_c)
            avail = max(0.0, cash - reserved)
            if sizing_mode == "risk":
                res = calculate_position(planned, si.stop[c], equity, avail / (1 + slip) / (1 + buy_c),
                                         open_risk, "US", cfg)
                qty = int(res["qty"])
                if qty <= 0:
                    counts["skip_size0"] += 1
                    for rsn in res["reasons"] or ["r_band"]:
                        size_reasons[rsn] = size_reasons.get(rsn, 0) + 1
                    continue
            else:                                   # fixed: 평가금액 × fixed_pct, 최대 종목 수
                if len(pos) + len(pending) >= max_positions:
                    counts["skip_size0"] += 1
                    size_reasons["max_positions"] = size_reasons.get("max_positions", 0) + 1
                    continue
                qty = int(min(fixed_pct * equity, avail) // unit)
                if qty <= 0 or qty * unit < 0.5 * fixed_pct * equity:
                    counts["skip_size0"] += 1
                    size_reasons["insufficient_cash"] = size_reasons.get("insufficient_cash", 0) + 1
                    continue
            reserve = qty * unit
            reserved += reserve
            prisk = qty * max(0.0, planned - si.stop[c])
            open_risk += prisk
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
    # 구간 끝 미청산 평가
    open_rows = []
    for tk, p in pos.items():
        px = closes[tk][end]
        if not px == px:
            px = p["entry_px"]
        val = p["qty"] * px * (1 - slip) * (1 - sell_c)
        open_rows.append({"cid": p["cid"], "ticker": tk, "entry_ci": p["entry_ci"], "exit_ci": end,
                          "qty": p["qty"], "entry_px": p["entry_px"], "exit_px": px, "stop": p["stop"],
                          "cost": p["cost"], "pnl": val - p["cost"], "risk0": p["risk0"],
                          "R": (val - p["cost"]) / p["risk0"] if p["risk0"] > 0 else math.nan,
                          "ret": val / p["cost"] - 1, "reason": "OPEN", "open": True,
                          "hold_days": end - p["entry_ci"] + 1, "mkt_value": p["qty"] * px})
    return {"equity": eq, "invested": inv, "positions": npos, "heat_current": heat_cur,
            "heat_entry": heat_ent, "trades": trades, "open": open_rows, "counts": counts,
            "size_reasons": size_reasons, "start": start, "end": end, "initial": prof.equity,
            "pool_size": int(len(idx))}
