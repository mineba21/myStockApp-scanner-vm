"""시간 순서 분리 · purge · walk-forward · block bootstrap.

- Train/Validation/Test 는 '매수일' 기준 시간 순서로 나눈다 (같은 날짜는 같은 구간).
- 앞 구간 거래 중 보유 가능 기간(horizon 끝)이 다음 구간 시작일 이후에 걸치는 거래는
  선택 과정에서 제외(purge)한다 → 다음 구간 가격 경로가 선택에 섞이지 않는다.
- Test 구간은 Train 에서 고른 설정을 평가하는 데만 사용한다.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Tuple

import numpy as np
import pandas as pd


def chronological_split(entry_dates: pd.Series, fracs=(0.6, 0.2, 0.2)) -> pd.Series:
    d = pd.to_datetime(entry_dates)
    uniq = np.sort(d.unique())
    n = len(uniq)
    if n == 0:
        return pd.Series([], dtype=object)
    c1 = uniq[min(n - 1, int(n * fracs[0]))]
    c2 = uniq[min(n - 1, int(n * (fracs[0] + fracs[1])))]
    lab = np.where(d < c1, "train", np.where(d < c2, "validation", "test"))
    return pd.Series(lab, index=entry_dates.index)


def purge_overlap(trades: pd.DataFrame, split: pd.Series, seg: str, nxt: str) -> pd.Index:
    nxt_start = pd.to_datetime(trades.loc[split == nxt, "entry_date"]).min()
    in_seg = trades.index[split == seg]
    if pd.isna(nxt_start):
        return in_seg
    end = pd.to_datetime(trades.loc[in_seg, "exit_date_max"])
    return in_seg[(end < nxt_start).to_numpy()]


def objective(returns: np.ndarray) -> float:
    """선택 기준: 기대값(평균 순수익률) − 0.25 × 하위 5% 평균손실 크기.

    단순 최고 평균 대신 큰 손실 꼬리를 벌점으로 줘 '큰 손실 방지' 목표를 반영한다.
    """
    if len(returns) == 0:
        return float("nan")
    tail = np.sort(returns)[:max(1, int(len(returns) * 0.05))].mean()
    return float(returns.mean() + 0.25 * min(tail, 0.0))


def block_bootstrap(returns: pd.Series, dates: pd.Series, block_days: int = 20,
                    n: int = 1000, seed: int = 7) -> Dict:
    """매수일 기준 block(영업일 묶음) 단위 재표본 → 신호 중첩·군집 의존성 반영."""
    if len(returns) < 5:
        return {"mean_p05": None, "mean_p50": None, "mean_p95": None, "prob_positive": None}
    d = pd.to_datetime(dates)
    blocks = ((d - d.min()).dt.days // max(1, int(block_days * 7 / 5))).to_numpy()
    groups = {}
    for b, r in zip(blocks, returns.to_numpy()):
        groups.setdefault(b, []).append(r)
    keys = list(groups)
    rng = np.random.default_rng(seed)
    means = []
    for _ in range(n):
        pick = rng.choice(len(keys), size=len(keys), replace=True)
        vals = np.concatenate([groups[keys[i]] for i in pick])
        means.append(vals.mean())
    means = np.array(means)
    return {"mean_p05": float(np.percentile(means, 5)), "mean_p50": float(np.percentile(means, 50)),
            "mean_p95": float(np.percentile(means, 95)), "prob_positive": float((means > 0).mean())}


def select_by_family(results: pd.DataFrame, idx_train: pd.Index, min_trades: int) -> Dict[str, str]:
    """family 별 Train objective 최대 설정."""
    tr = results[results["sig_idx"].isin(idx_train)]
    best = {}
    for fam, g in tr.groupby("family"):
        scores = g.groupby("strategy")["return"].agg(lambda s: objective(s.to_numpy()))
        counts = g.groupby("strategy")["return"].size()
        scores = scores[counts >= min_trades]
        if len(scores):
            best[fam] = scores.idxmax()
    return best


def rank_stability(results: pd.DataFrame, idx_a: pd.Index, idx_b: pd.Index) -> Dict[str, float]:
    """family 내 설정 순위의 구간 간 Spearman 상관 (낮으면 과적합 위험)."""
    out = {}
    for fam, g in results.groupby("family"):
        if g["strategy"].nunique() < 4:
            continue
        a = g[g["sig_idx"].isin(idx_a)].groupby("strategy")["return"].agg(lambda s: objective(s.to_numpy()))
        b = g[g["sig_idx"].isin(idx_b)].groupby("strategy")["return"].agg(lambda s: objective(s.to_numpy()))
        j = pd.concat([a, b], axis=1, keys=["a", "b"]).dropna()
        if len(j) >= 4:
            out[fam] = float(j["a"].rank().corr(j["b"].rank()))
    return out


def walk_forward(results: pd.DataFrame, sig: pd.DataFrame, train_months: int, test_months: int,
                 min_trades: int) -> pd.DataFrame:
    """rolling 창마다 family 별 최적 설정을 고르고 바로 다음 창 성과를 기록."""
    d = pd.to_datetime(sig["entry_date"])
    start, end = d.min(), d.max()
    rows = []
    cur = start + pd.DateOffset(months=train_months)
    while cur < end:
        tr_mask = (d >= cur - pd.DateOffset(months=train_months)) & (d < cur) & \
                  (pd.to_datetime(sig["exit_date_max"]) < cur)
        te_mask = (d >= cur) & (d < cur + pd.DateOffset(months=test_months))
        idx_tr, idx_te = sig.index[tr_mask], sig.index[te_mask]
        if len(idx_te):
            best = select_by_family(results, idx_tr, min_trades)
            te = results[results["sig_idx"].isin(idx_te)]
            for fam, strat in best.items():
                r = te.loc[te["strategy"] == strat, "return"]
                rows.append({"window_start": cur.date().isoformat(), "family": fam, "strategy": strat,
                             "train_trades": int(len(idx_tr)), "test_trades": int(len(r)),
                             "test_avg_return": float(r.mean()) if len(r) else None})
        cur = cur + pd.DateOffset(months=test_months)
    return pd.DataFrame(rows)
