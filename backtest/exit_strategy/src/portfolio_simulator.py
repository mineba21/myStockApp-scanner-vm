"""실제 포트폴리오 단위 시뮬레이션 (시장별, 통화 비합산).

- 최대 보유 종목 수, 진입 시점 평가금액 대비 고정 비중.
- 같은 종목 보유 중 재신호는 매수하지 않는다 (기본).
- 매도는 개별 거래 시뮬레이션의 체결(날짜·비중·가격)을 그대로 따른다.
- horizon 종료 시 미청산 수량은 마지막 평가일 종가로 정리한다 (포트폴리오 회전 가정).
"""
from __future__ import annotations

from typing import Dict, List

import numpy as np
import pandas as pd

from .performance_metrics import equity_metrics


def run_portfolio(trades: pd.DataFrame, closes: Dict[str, pd.Series], calendar: pd.DatetimeIndex,
                  max_positions: int = 10, position_pct: float = 0.10, buy_cost: float = 0.0,
                  sell_cost: float = 0.0, slip: float = 0.0, initial: float = 1.0) -> Dict:
    trades = trades.copy()
    trades["entry_date"] = pd.to_datetime(trades["entry_date"])
    by_entry = {d: g.sort_values("priority") for d, g in trades.groupby("entry_date")}
    cash = initial
    pos: Dict[str, dict] = {}
    eq_hist, inv_hist, npos_hist = [], [], []
    taken, skip_dup, skip_full = 0, 0, 0
    buys_total = 0.0
    last_px: Dict[str, float] = {}
    for d in calendar:
        ds = d.strftime("%Y-%m-%d")
        # 1) 매도 체결 (시가·장중 체결은 당일 매수보다 먼저)
        for tk in list(pos):
            p = pos[tk]
            for f in p["fills"]:
                if f["date"] == ds and not f.get("_done"):
                    sh = p["shares0"] * f["portion"]
                    sh = min(sh, p["shares"])
                    cash += sh * f["price"] * (1 - slip) * (1 - sell_cost)
                    p["shares"] -= sh
                    f["_done"] = True
            if p["horizon_end"] == ds and p["shares"] > 1e-12:
                px = p["horizon_close"]
                cash += p["shares"] * px * (1 - sell_cost)
                p["shares"] = 0.0
            if p["shares"] <= 1e-12:
                del pos[tk]
        # 평가금액(전일 종가) → 신규 매수 금액 기준
        equity_prev = cash + sum(p["shares"] * last_px.get(tk, p["entry_price"]) for tk, p in pos.items())
        # 2) 매수 (시가)
        for _, t in by_entry.get(d, pd.DataFrame()).iterrows():
            tk = t["ticker"]
            if tk in pos:
                skip_dup += 1
                continue
            if len(pos) >= max_positions:
                skip_full += 1
                continue
            alloc = min(position_pct * equity_prev, cash)
            if alloc <= equity_prev * position_pct * 0.5:
                skip_full += 1
                continue
            shares = alloc / (t["entry_price"] * (1 + buy_cost))
            cash -= alloc
            buys_total += alloc
            fills = [dict(f) for f in t["fills"]]
            pos[tk] = {"shares0": shares, "shares": shares, "entry_price": t["entry_price"], "fills": fills,
                       "horizon_end": t.get("last_date"), "horizon_close": t.get("last_close")}
            taken += 1
            # 매수 당일 장중 매도(당일 손절 등)
            for f in fills:
                if f["date"] == ds:
                    sh = min(shares * f["portion"], pos[tk]["shares"])
                    cash += sh * f["price"] * (1 - slip) * (1 - sell_cost)
                    pos[tk]["shares"] -= sh
                    f["_done"] = True
            if pos[tk]["shares"] <= 1e-12:
                del pos[tk]
        # 3) 종가 평가
        inv = 0.0
        for tk, p in pos.items():
            s = closes.get(tk)
            if s is not None and d in s.index and s.loc[d] > 0:
                last_px[tk] = float(s.loc[d])
            inv += p["shares"] * last_px.get(tk, p["entry_price"])
        eq_hist.append(cash + inv)
        inv_hist.append(inv)
        npos_hist.append(len(pos))
    equity = pd.Series(eq_hist, index=calendar)
    invested = pd.Series(inv_hist, index=calendar)
    npos = pd.Series(npos_hist, index=calendar)
    m = equity_metrics(equity, invested, npos, buys_total)
    m.update({"n_taken": taken, "n_skipped_dup": skip_dup, "n_skipped_capacity": skip_full})
    return {**m, "equity": equity, "invested": invested, "positions": npos}
