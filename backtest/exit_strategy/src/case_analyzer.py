"""STEP 8 — 실제 Scanner 신호(RECONSTRUCTED strict 통과) 사례 자동 선정.

'매도 이후 경로'는 사례 분류에만 쓰며 매도 판단에는 쓰지 않는다.
CASE G(스캐너 후보 탈락)는 과거 탈락 이력이 저장되지 않아, 스캐너가 '돌파 실패'
(upthrust_failed=True)를 기록한 신호를 대리 지표로 쓴다.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .indicators import compute_indicators
from .market_data_loader import load_cached

CASES = {
    "A": ("고정 손절이 큰 손실을 방어", "S02_fix7"),
    "B": ("손실 전에 Breakout 실패로 매도", "S04B_t1_wall"),
    "C": ("Trailing Stop 이 이익을 보호", "S06_a10_d8"),
    "D": ("부분매도 후 잔여 물량 추가 상승", "S07_a10_d6_r50_trail_pct10"),
    "E": ("조기 손절 후 급반등 (손절 불리)", "S02_fix7"),
    "F": ("장기 상승 종목을 너무 일찍 전량 매도", "S06_a10_d8"),
    "G": ("스캐너 '돌파 실패' 표시였지만 보유가 유리 (탈락 대리지표)", "S00"),
    "H": ("갭 하락으로 손절가보다 불리하게 체결", "S01"),
}


def _pick(df: pd.DataFrame, mask, score, n=2) -> pd.DataFrame:
    sub = df[mask].copy()
    if sub.empty:
        return sub
    sub["_score"] = score(sub)
    return sub.sort_values("_score", ascending=False).drop_duplicates("ticker").head(n)


def select_cases(cfg, allb: pd.DataFrame, base: pd.DataFrame, sig: pd.DataFrame, per_case: int = 2) -> List[Dict]:
    H = cfg["default_horizon"]
    b = allb[(allb["horizon"] == H)]
    wide = b.pivot_table(index="sig_uid", columns="strategy", values="return", aggfunc="first")
    def st(name):
        return b[b["strategy"] == name].set_index("sig_uid")
    s00, s01, s02, s04, s06, s07 = (st(n) for n in ("S00", "S01", "S02_fix7", "S04B_t1_wall", "S06_a10_d8",
                                                      "S07_a10_d6_r50_trail_pct10"))
    picks = {}
    j = s02.join(s00[["return"]].rename(columns={"return": "r00"}))
    picks["A"] = _pick(j, (j["r00"] <= -0.2) & (j["exit_reason"] == "HARD_STOP"), lambda d: d["return"] - d["r00"], per_case)
    j = s04.join(s00[["return"]].rename(columns={"return": "r00"}))
    picks["B"] = _pick(j, j["exit_reason"].eq("BF_PIVOT") & (j["return"] > -0.02) & (j["r00"] < -0.10),
                       lambda d: d["return"] - d["r00"], per_case)
    j = s06.join(s00[["return"]].rename(columns={"return": "r00"}))
    picks["C"] = _pick(j, j["exit_reason"].eq("TRAIL_STOP") & (j["return"] > 0.03), lambda d: d["return"] - d["r00"], per_case)
    j = s07[s07["first_exit_reason"] == "PARTIAL"].copy()
    if len(j):
        j["rem_gain"] = j["fills"].map(lambda f: (f[-1]["price"] / f[0]["price"] - 1) if isinstance(f, list) and len(f) > 1 else
                                       np.nan)
        picks["D"] = _pick(j, j["rem_gain"] > 0.10, lambda d: d["rem_gain"], per_case)
    picks["E"] = _pick(s02, s02["exit_reason"].eq("HARD_STOP") & (s02["post_20d"] >= 0.15), lambda d: d["post_20d"], per_case)
    picks["F"] = _pick(s06, (s06["open_qty"] <= 0) & (s06["post_60d"] >= 0.30), lambda d: d["post_60d"], per_case)
    up = sig.set_index("sig_uid")
    col = "upthrust_failed_known" if "upthrust_failed_known" in up else "upthrust_failed"
    j = s00.join(up[[col]].rename(columns={col: "ut"}), how="left", rsuffix="_s")
    ut = j["ut"] if "ut" in j else pd.Series(False, index=j.index)
    picks["G"] = _pick(j, ut.fillna(False).astype(bool) & (j["return"] >= 0.15), lambda d: d["return"], per_case)
    picks["H"] = _pick(s01, s01["gap_excess"] > 0, lambda d: d["gap_excess"], per_case)

    payloads, ind_cache, out = {}, {}, []
    from .backtest_engine import CORE
    for code, (title, strat) in CASES.items():
        df = picks.get(code)
        if df is None or df.empty:
            out.append({"case": code, "title": title, "strategy": strat, "examples": [],
                        "note": "조건에 맞는 실제 사례 없음"})
            continue
        exs = []
        for uid, row in df.iterrows():
            market = row["market"]
            if market not in payloads:
                payloads[market] = load_cached(cfg["data"]["cache_dir"], market)
            frames = payloads[market]["frames"]
            px = frames[row["ticker"]]
            key = (market, row["ticker"])
            if key not in ind_cache:
                ind_cache[key] = compute_indicators(px)
            ind = ind_cache[key]
            e = pd.Timestamp(row["entry_date"])
            pos = px.index.searchsorted(e)
            lo, hi = max(0, pos - 30), min(len(px), pos + H + 45)
            win = px.iloc[lo:hi]
            iw = ind.iloc[lo:hi]
            fills = row.get("fills") if isinstance(row.get("fills"), list) else []
            strat_returns = {s: (None if pd.isna(wide.loc[uid].get(s)) else float(wide.loc[uid].get(s)))
                             for s in CORE + ["S09A_OPT", "S09B_OPT"] if s in wide.columns}
            trail = None
            if strat.startswith("S06") or strat.startswith("S07"):
                act, dd = (0.10, 0.08) if strat.startswith("S06") else (0.10, 0.06)
                peak, on, trail = None, False, []
                ep = float(row["entry_price"])
                for i, (d, r) in enumerate(px.iloc[pos:hi].iterrows()):
                    trail.append({"date": d.strftime("%Y-%m-%d"), "v": peak * (1 - dd) if on and peak else None})
                    peak = r["High"] if peak is None else max(peak, r["High"])
                    on = on or peak >= ep * (1 + act)
            exs.append({
                "ticker": row["ticker"], "name": row.get("name"), "market": market,
                "signal_type": row["signal_type"], "signal_date": row.get("signal_date"),
                "entry_date": e.strftime("%Y-%m-%d"), "entry_price": float(row["entry_price"]),
                "pivot": None if pd.isna(row.get("pivot_price")) else float(row["pivot_price"]),
                "initial_stop": None if pd.isna(row.get("initial_stop")) else float(row["initial_stop"]),
                "exit_date": row.get("exit_date"), "exit_reason": row.get("exit_reason"),
                "return": float(row["return"]), "fills": fills,
                "post": {k: (None if pd.isna(row.get(k)) else float(row.get(k)))
                         for k in ("post_5d", "post_10d", "post_20d", "post_60d")},
                "strategy_returns": strat_returns,
                "chart": {
                    "dates": [d.strftime("%Y-%m-%d") for d in win.index],
                    "o": win["Open"].round(4).tolist(), "h": win["High"].round(4).tolist(),
                    "l": win["Low"].round(4).tolist(), "c": win["Close"].round(4).tolist(),
                    "v": win["Volume"].tolist(),
                    "ma21": [None if math.isnan(x) else round(x, 4) for x in iw["ma21"]],
                    "ma50": [None if math.isnan(x) else round(x, 4) for x in iw["ma50"]],
                    "trail": trail,
                },
            })
        out.append({"case": code, "title": title, "strategy": strat, "examples": exs})
    return out
