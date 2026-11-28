"""매수 주문 체결 모델과 S01 경로 (일봉, 보수적).

매수 (운영: 지정가만 사용, 장 시작 후 시세 확인 뒤 전송한다고 가정)
- 결정: 탐지일(또는 T2 확인일) 장 마감 후, 그 종가까지의 정보만 사용.
- 시가 확인: 시가 ≤ 손절가 이거나 (시가-손절)/시가 < min_r 이면 주문하지 않는다 (SKIP_OPEN_STOP).
- 시가 ≤ 지정가 → 시가 체결, 저가 ≤ 지정가 → 지정가 체결, 아니면 미체결·취소 (추격 재주문 없음).

S01 매도 (매도 규칙은 고정)
- 손절가 = 매수 결정 시점의 Scanner stop_loss (재스캔 값 미사용).
- 시가 ≤ 손절 → 시가 체결(갭), 저가 ≤ 손절 → 손절가 체결.
- 매수 당일: conservative = 당일 저가 ≤ 손절이면 당일 손절 (시가 또는 하락 중 지정가 체결 후 손절가 도달),
  next_day = 자동손절 등록·감시가 다음 거래일부터라고 가정한 대안.
- H60/H120: 매수일 포함 N 거래일째 종가 청산. UNL: 손절 전까지 보유, 데이터 끝에서 미청산 평가.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Optional

import numpy as np
import pandas as pd

HORIZON_DAYS = {"H20": 20, "H60": 60, "H120": 120, "UNL": None}


@dataclass
class TickerArrays:
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    ok: np.ndarray
    cal_idx: np.ndarray
    index: pd.DatetimeIndex


def ticker_arrays(frames: Dict[str, pd.DataFrame], cal: pd.DatetimeIndex) -> Dict[str, TickerArrays]:
    out = {}
    for t, df in frames.items():
        ok = ((df["Open"] > 0) & (df["High"] > 0) & (df["Low"] > 0) & (df["Volume"] > 0)).to_numpy()
        out[t] = TickerArrays(df["Open"].to_numpy(float), df["High"].to_numpy(float),
                              df["Low"].to_numpy(float), df["Close"].to_numpy(float), ok,
                              cal.searchsorted(df.index).astype(int), df.index)
    return out


def _next_ok(ta: TickerArrays, pos: int) -> Optional[int]:
    p = pos
    while p < len(ta.o) and not ta.ok[p]:
        p += 1
    return p if p < len(ta.o) else None


def _try_fill(ta: TickerArrays, p: int, limit: Optional[float], stop: float, min_r: float):
    """p 봉에서 매수 시도. 반환 (status, price, kind)."""
    o, lo = ta.o[p], ta.l[p]
    if o <= stop or (o - stop) / o < min_r:
        return "SKIP_OPEN_STOP", None, None
    if limit is None or o <= limit:
        return "FILLED", o, "open"
    if lo <= limit:
        return "FILLED", limit, "limit"
    return "UNFILLED", None, None


def plan_order(row, ta: TickerArrays, timing: dict, min_r: float, check_days: int = 3) -> dict:
    """후보 1건의 주문 계획과 체결 결과 (포트폴리오와 무관하게 결정되는 부분)."""
    kind = timing["kind"]
    stop = row.stop
    pos = int(row.ticker_pos)                      # 결정 봉 (탐지일)
    res = {"status": None, "decision_pos": pos, "planned_px": None, "fill_pos": None,
           "fill_px": None, "fill_kind": None, "expire_pos": None,
           "dec_close": float(ta.c[pos]), "dec_ext_pct": row.ext_pct, "dec_stop_pct": row.stop_pct}
    is_bo = row.signal_type in ("BREAKOUT", "RE_BREAKOUT") and row.pivot == row.pivot
    if kind == "upthrust_confirm" and is_bo:
        from scanner.entry_control import evaluate_upthrust
        sd = ta.index.searchsorted(row.signal_date, side="right")       # D+1 위치
        decide = max(sd + check_days - 1, pos)
        if decide >= len(ta.o):
            res["status"] = "SKIP_NO_BAR"
            return res
        df = pd.DataFrame({"Open": ta.o[:decide + 1], "High": ta.h[:decide + 1], "Low": ta.l[:decide + 1],
                           "Close": ta.c[:decide + 1]}, index=ta.index[:decide + 1])
        failed, _ = evaluate_upthrust(df, str(row.signal_date.date()), row.pivot, check_days)
        if failed:
            res["status"] = "SKIP_UPTHRUST"
            res["decision_pos"] = decide
            return res
        pos = decide
        c = float(ta.c[pos])
        res.update(decision_pos=pos, dec_close=c, dec_ext_pct=(c / row.pivot - 1) * 100,
                   dec_stop_pct=(1 - stop / c) * 100)
        kind, timing = "limit_next_day", {"limit_pct": timing["limit_pct"]}
    elif kind == "upthrust_confirm":
        kind = "limit_next_day"
    if kind == "pullback" and not is_bo:
        kind = "limit_next_day"

    p = _next_ok(ta, pos + 1)
    if p is None:
        res["status"] = "SKIP_NO_BAR"
        return res
    close = float(ta.c[pos])
    if kind == "market_open":
        res["planned_px"] = close
        st, px, fk = _try_fill(ta, p, None, stop, min_r)
        res.update(status=st, fill_pos=p if st == "FILLED" else None, fill_px=px, fill_kind=fk, expire_pos=p)
        return res
    if kind == "limit_next_day":
        lim = close * (1 + timing["limit_pct"])
        res["planned_px"] = lim
        st, px, fk = _try_fill(ta, p, lim, stop, min_r)
        res.update(status=st, fill_pos=p if st == "FILLED" else None, fill_px=px, fill_kind=fk, expire_pos=p)
        return res
    if kind == "pullback":
        lim = min(row.pivot * (1 + timing["limit_pct"]), close * (1 + timing["limit_pct"]))
        res["planned_px"] = lim
        last = p
        for k in range(timing["days"]):
            q = _next_ok(ta, p) if k == 0 else _next_ok(ta, last + 1)
            if q is None:
                break
            last = q
            st, px, fk = _try_fill(ta, q, lim, stop, min_r)
            if st == "FILLED":
                res.update(status=st, fill_pos=q, fill_px=px, fill_kind=fk, expire_pos=q)
                return res
            if st == "SKIP_OPEN_STOP":
                res.update(status=st, expire_pos=q)
                return res
        res.update(status="UNFILLED", expire_pos=last)
        return res
    raise ValueError(kind)


def s01_exit(ta: TickerArrays, fill_pos: int, stop: float, horizon: Optional[int],
             scenario: str = "conservative", end_pos: Optional[int] = None) -> dict:
    """S01 경로. end_pos(포함) 이후 봉은 읽지 않는다 (구간 절단 → 미청산 평가)."""
    n = len(ta.o) if end_pos is None else min(len(ta.o), end_pos + 1)
    last = n - 1 if horizon is None else min(n - 1, fill_pos + horizon - 1)
    reached = horizon is not None and fill_pos + horizon - 1 <= (n - 1)
    day0_hit = False
    for t in range(fill_pos, last + 1):
        if not ta.ok[t]:
            continue
        if t == fill_pos:
            if ta.l[t] <= stop:
                day0_hit = True
                if scenario == "conservative":
                    return {"exit_pos": t, "exit_px": stop, "reason": "STOP", "day0": True}
            continue
        if ta.o[t] <= stop:
            return {"exit_pos": t, "exit_px": ta.o[t], "reason": "STOP_GAP", "day0": False}
        if ta.l[t] <= stop:
            return {"exit_pos": t, "exit_px": stop, "reason": "STOP", "day0": False}
    lv = last
    while lv > fill_pos and not ta.ok[lv]:
        lv -= 1
    if reached:
        return {"exit_pos": last, "exit_px": ta.c[lv], "reason": "HORIZON", "day0": day0_hit}
    return {"exit_pos": None, "exit_px": None, "reason": "OPEN", "mark_pos": lv, "mark_px": ta.c[lv],
            "day0": day0_hit}


def build_orders(cand: pd.DataFrame, tas: Dict[str, TickerArrays], timing_name: str, cfg: dict) -> pd.DataFrame:
    timing = cfg["order"]["timings"][timing_name]
    min_r = cfg["order"]["min_r_pct"] / 100.0
    rows = []
    for r in cand.itertuples(index=False):
        ta = tas[r.ticker]
        o = plan_order(r, ta, timing, min_r, timing.get("check_days", 3))
        o["cid"] = r.cid
        o["decision_ci"] = int(ta.cal_idx[o["decision_pos"]])
        o["fill_ci"] = int(ta.cal_idx[o["fill_pos"]]) if o["fill_pos"] is not None else None
        o["expire_ci"] = int(ta.cal_idx[o["expire_pos"]]) if o["expire_pos"] is not None else None
        rows.append(o)
    return pd.DataFrame(rows).set_index("cid")


def build_exits(cand: pd.DataFrame, orders: pd.DataFrame, tas, horizon: str, scenario: str) -> pd.DataFrame:
    hz = HORIZON_DAYS[horizon]
    rows = []
    for r in cand.itertuples(index=False):
        o = orders.loc[r.cid]
        if o["status"] != "FILLED":
            continue
        ta = tas[r.ticker]
        e = s01_exit(ta, int(o["fill_pos"]), r.stop, hz, scenario)
        e["cid"] = r.cid
        e["exit_ci"] = int(ta.cal_idx[e["exit_pos"]]) if e["exit_pos"] is not None else None
        rows.append(e)
    return pd.DataFrame(rows).set_index("cid")


def trade_outcome(fill_px: float, stop: float, exit_px: float, costs: dict) -> dict:
    """1주 기준 순손익률과 R-multiple (비용·slippage 반영)."""
    slip = costs["slippage_bp"] / 10000.0
    buy = fill_px * (1 + slip) * (1 + costs["buy_commission"])
    sell = exit_px * (1 - slip) * (1 - costs["sell_commission"] - costs["sell_tax"])
    risk = fill_px - stop
    return {"ret": sell / buy - 1, "R": (sell - buy) / risk if risk > 0 else math.nan}
