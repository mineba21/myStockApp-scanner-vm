"""신호 → 매수 시점·매수가·TradeData.

- 매수일 = 신호 확인일(detect_date: 스캔 완료일) **다음** 첫 거래 가능일. 확인일 당일이나
  그 이전 봉으로는 절대 매수하지 않는다.
- 매수가 = 그날 시가 (+slippage 는 시뮬레이터가 적용).
- 손절가·pivot·MA150 은 신호 확인 시점 스냅샷을 쓴다.
  SNAPSHOT_ONLY 행은 저장 당시 수정주가와 현재 수정주가의 비율(factor)로 정규화한다.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from .execution_simulator import TradeData
from .indicators import compute_indicators


def _num(x) -> float:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(f) else f


def _opt(x) -> Optional[float]:
    f = _num(x)
    return f if f else None


class IndicatorCache:
    def __init__(self, frames: Dict[str, pd.DataFrame], bench: pd.Series):
        self.frames = frames
        self.bench = bench
        self._ind: Dict[str, pd.DataFrame] = {}

    def get(self, ticker: str) -> Optional[pd.DataFrame]:
        if ticker not in self._ind:
            df = self.frames.get(ticker)
            self._ind[ticker] = None if df is None else compute_indicators(df, self.bench)
        return self._ind[ticker]


def upthrust_decision(df: pd.DataFrame, signal_date: str, pivot: Optional[float],
                      check_days: int = 3) -> Tuple[Optional[pd.Timestamp], Optional[bool]]:
    """운영 entry_control.evaluate_upthrust 를 D+N 봉 종가까지의 데이터로만 호출.

    entry_control 은 pandas 만 import 하므로 환경변수 격리가 필요 없다 (테스트 세션 오염 방지).
    """
    import sys
    from .common import REPO_ROOT
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from scanner.entry_control import evaluate_upthrust

    if not pivot or not signal_date:
        return None, None
    sd = pd.Timestamp(signal_date)
    pos = df.index.searchsorted(sd, side="right")       # D+1 의 위치
    decide = pos + check_days - 1
    if decide >= len(df):
        return None, None
    failed, _ = evaluate_upthrust(df.iloc[:decide + 1], signal_date, pivot, check_days)
    return df.index[decide], failed


def build_trade(row: pd.Series, df: pd.DataFrame, ind: pd.DataFrame, max_days: int,
                factor: float = 1.0, upthrust_days: int = 3) -> Tuple[Optional[TradeData], dict]:
    """row: 신호 1건 (detect_date 필수). 반환: (TradeData | None, 메타)."""
    meta = {"exclude_reason": None}
    detect = pd.Timestamp(row["detect_date"])
    idx = df.index
    start = idx.searchsorted(detect, side="right")      # detect 다음 봉부터
    tradable = ind["tradable"].to_numpy()
    e = start
    while e < len(idx) and not tradable[e]:
        e += 1
    if e >= len(idx):
        meta["exclude_reason"] = "no_entry_bar_yet"
        return None, meta
    end = min(len(idx), e + max_days)
    sl = slice(e, end)
    prev_close = df["Close"].shift(1).to_numpy()[sl]
    wk_complete = ind["week_complete"]
    stop = _opt(row.get("stop_loss"))
    pivot = _opt(row.get("pivot_price"))
    ma150s = _opt(row.get("ma150"))
    stop = stop * factor if stop else None
    pivot = pivot * factor if pivot else None
    ma150s = ma150s * factor if ma150s else None
    atr_sig = _num(ind["atr14"].iloc[e - 1]) if e > 0 else 0.0

    up_day, up_failed = (None, None)
    if row.get("signal_type") in ("BREAKOUT", "RE_BREAKOUT") and pivot:
        dec_date, up_failed = upthrust_decision(df, str(row.get("signal_date")), pivot, upthrust_days)
        if dec_date is not None:
            up_day = max(0, int(idx.searchsorted(dec_date)) - e)

    def L(col, frame=ind):
        return [_num(x) for x in frame[col].to_numpy()[sl]]

    td = TradeData(
        dates=[d.strftime("%Y-%m-%d") for d in idx[sl]],
        o=L("Open", df), h=L("High", df), l=L("Low", df), c=L("Close", df), v=L("Volume", df),
        tradable=[bool(x) for x in tradable[sl]],
        prev_close=[_num(x) for x in prev_close],
        ma10=L("ma10"), ma21=L("ma21"), ma50=L("ma50"), ma150=L("ma150"),
        atr=L("atr14"), volma=L("vol_ma20_prev"),
        rs_weak=[bool(x) if x == x else False for x in ind["rs_weak"].to_numpy()[sl]],
        stage_w=[x if isinstance(x, str) else None for x in ind["stage_w"].to_numpy()[sl]],
        week_complete=[bool(x) for x in wk_complete.to_numpy()[sl]],
        signal_type=row.get("signal_type"), pivot=pivot, scanner_stop=stop,
        signal_ma150=ma150s, atr_at_signal=atr_sig or None,
        upthrust_day=up_day, upthrust_failed=up_failed, market=row.get("market", "US"),
    )
    meta.update({
        "entry_idx": e, "entry_date": td.dates[0], "available_days": td.n,
        "entry_vs_pivot_pct": (td.o[0] / pivot - 1) if pivot else None,
        "below_pivot_entry": bool(pivot and td.o[0] < pivot),
        "stop_dist_pct": (1 - stop / td.o[0]) if stop else None,
        "stop_at_or_above_open": bool(stop and stop >= td.o[0]),
        "upthrust_failed_known": up_failed, "factor": factor,
    })
    return td, meta


def snapshot_factor(row: pd.Series, df: pd.DataFrame) -> Tuple[float, Optional[str]]:
    """MCP 저장값(마지막 scan_time 기준)과 현재 수정주가의 비율."""
    scan = row.get("scan_time")
    price = _opt(row.get("price"))
    if scan is None or pd.isna(scan) or not price:
        return 1.0, "no_scan_price"
    local = scan + (pd.Timedelta(hours=9) if row["market"] == "KR" else -pd.Timedelta(hours=5))
    pos = df.index.searchsorted(local.normalize(), side="right") - 1
    if pos < 0:
        return 1.0, "scan_before_history"
    cur = float(df["Close"].iloc[pos])
    f = cur / price
    flag = None if abs(f - 1) < 0.05 else "factor_gt_5pct"
    return f, flag


def mark_overlaps(entries: pd.DataFrame, horizon: int) -> pd.Series:
    """같은 종목의 앞선 진입이 아직 보유기간(horizon 거래일) 안에 있으면 True."""
    flags = pd.Series(False, index=entries.index)
    for (_, _), g in entries.sort_values("entry_date").groupby(["market", "ticker"]):
        last_end = None
        for i, r in g.iterrows():
            if last_end is not None and r["entry_idx"] < last_end:
                flags[i] = True
            last_end = max(last_end or 0, r["entry_idx"] + horizon)
    return flags
