"""P7 운영 정책 비교판 (P7-OPS). 기존 P7(``risk_portfolio.run``)과 그 결과 파일은 바꾸거나 덮어쓰지 않는다.

목적: SHADOW 엔진(``trading.auto_buy.shadow``)이 실제 운영 정책대로 계산하는지 **독립 구현**으로 대조한다.
SHADOW 엔진·위험 모듈을 import 하지 않고 정책 문서만 보고 배열·딕셔너리로 다시 계산한다
(공유하는 것은 운영 수량 계산 ``scanner.sizing.calculate_position`` 과 P7 후보·필터 정의뿐이다).
SHADOW 오류에 맞추려고 P7 체결 조건을 완화하지 않는다 — 운영 정책이 기존 P7 보다 보수적인 곳만 다르다.

운영 정책 (기존 P7 과의 차이는 ``DIFF_VS_P7``)
- 지정가: 판단일 종가 × 1.01 을 센트 단위 **내림** (미국 주식 0.01 단위, +1% 상한 초과 없음). 10진수 계산.
- 주문 유효기간: 다음 정규장 1일. 그날 체결되지 않으면 만료 (D+2 이월·추격 재주문·같은 신호 재주문 없음).
- 일봉 상태: 정상(OHLC 일관 + 거래량 > 0) / 거래량 0 확정 / 미확정(행 없음·OHLC 비정상·거래량 NULL·음수·NaN).
  - 거래량 0: 매수 주문은 미체결 만료, 보유 종목은 손절 체결 없음 (종가 평가·위험 유지). 거래 재개 첫 봉에서 같은 S01 규칙.
  - 미확정: 매수 주문은 FILL_UNKNOWN (예약·위험 유지, 신규 매수 차단), 보유 종목은 가격 미확정 (이후 손절 판정 보류,
    마지막 확인 종가로 평가, 진입 위험 유지, 신규 매수 차단). 복구 데이터가 없으면 그대로 남는다.
- 체결: 시가 ≤ 손절 또는 (시가−손절)/시가 < 3% → 전송 안 함, 시가 ≤ 지정가 → 시가, 저가 ≤ 지정가 → 지정가, 그 밖 미체결.
  체결가 ≤ 손절가면 거부. 일봉 내 순서: 체결 → 같은 날 저가 ≤ 손절가면 손절가 청산(보수 가정).
- S01: 시가 ≤ 손절 → 시가 청산(갭), 저가 ≤ 손절 → 손절가 청산. 기간 제한 없음.
- 거래비용: P7 configs costs (편도 0.25% + 10bp slippage + 매도세).
- 하루 순서: (1) 그날 목표 주문 체결 판정 (2) S01 (3) 종가 평가·위험 (4) 다음 정규장 매수 판단·예약.
- Entry Heat = Σ(체결가 − 손절가)×수량 + 미체결 예약 위험 (지정가 − 손절가)×수량. 신규 매수 예산 판단에 사용.
  Current Heat = Σ max(종가 − 손절가, 0)×수량 (이탈·미확정은 진입 위험 유지) + 예약 위험. 표시 전용.
  기록 시점은 그날 판단(신규 예약) 후.
- 순자산 = 현금 + Σ 수량×종가 (미확정은 마지막 확인 종가). 현금 하한 20%: 매수 가능 = 현금 − 예약 − 순자산×20%.
- 신규 매수 차단: 손절선 이탈 미청산, 가격 미확정 보유, 미확정 주문 (Current Heat 비율로는 막지 않음).
- 같은 종목 보유·주문 중이면 매수하지 않고, signal_key 당 1회만 주문한다.
- 순위: GRADE = Grade → Signal Quality → 티커 → signal_key, RANDOM(B02) = SHA-256(seed:거래일:signal_key) → 티커.
  (B02 는 SHADOW 계좌 정의를 따른다. 기존 P7 B02 는 numpy 순열이라 같은 seed 여도 다른 계좌다.)
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal
from typing import Dict, List, Optional

import numpy as np

OHLC_REL_TOL = 1e-9
R_BAND = {"r_pct_below_min", "r_pct_above_max", "stop_must_be_below_entry"}
GRADE = {"S": 0, "A": 1, "B": 2}
QUALITY = {"STRONG": 0, "MODERATE": 1, "WEAK": 2}

DIFF_VS_P7 = {
    "limit_price": "P7: 종가×1.01 미반올림 / OPS: 센트 내림 (상한 초과 없음)",
    "zero_volume_entry": "P7: 거래량 0 봉을 건너뛰고 다음 정상 봉(D+2 이후)에 체결 시도 / OPS: D+1 거래량 0 이면 미체결 만료",
    "missing_bar_entry": "P7: 행이 없거나 비정상이면 다음 정상 봉에 체결 시도 / OPS: FILL_UNKNOWN (예약 유지·신규 매수 차단)",
    "zero_volume_stop": "P7·OPS 모두 거래량 0 봉에서는 손절 판정 없음 (OPS 는 거래 재개 첫 봉에서 판단, 갭 손실 기록)",
    "missing_bar_stop": "P7: 다음 정상 봉에서 계속 판정 / OPS: 가격 미확정으로 판정 보류·신규 매수 차단",
    "fill_cash_check": "P7: 체결 시 현금 부족이면 수량 축소 / OPS: 판단 때 예약한 현금으로 체결 (지정가 이하 체결이라 부족 없음)",
    "heat_record_time": "P7: 판단 전 기록 / OPS: 판단(신규 예약) 후 기록",
    "decision_last_day": "P7: 구간 마지막 날 판단 안 함 / OPS: 다음 정규장이 있으면 판단·예약 (미체결로 남음)",
    "b02_rank": "P7: numpy 순열 / OPS: SHADOW 계좌 정의 (SHA-256 seed·거래일·signal_key)",
}


def limit_floor_cent(close: float, pct: float = 0.01) -> float:
    return float((Decimal(repr(float(close))) * (1 + Decimal(repr(pct)))).quantize(Decimal("0.01"),
                                                                                    rounding=ROUND_FLOOR))


def random_key(seed: int, day: str, key: str) -> float:
    h = hashlib.sha256(f"{seed}:{day}:{key}".encode()).hexdigest()
    return int(h[:12], 16) / float(16 ** 12)


@dataclass(frozen=True)
class DayBar:
    state: str                      # OK | ZERO | INCOMPLETE
    o: float = math.nan
    h: float = math.nan
    l: float = math.nan
    c: float = math.nan
    v: float = math.nan


def classify(o, h, lo, c, v) -> DayBar:
    vals = (o, h, lo, c)
    if not all(isinstance(x, (int, float)) and math.isfinite(x) and x > 0 for x in vals):
        return DayBar("INCOMPLETE")
    tol = OHLC_REL_TOL * max(vals)
    if not (lo <= min(o, c) + tol and h >= max(o, c) - tol):
        return DayBar("INCOMPLETE", o, h, lo, c, v)
    if not (isinstance(v, (int, float)) and math.isfinite(v) and v >= 0):
        return DayBar("INCOMPLETE", o, h, lo, c, v)
    return DayBar("ZERO" if v == 0 else "OK", o, h, lo, c, v)


class BarBook:
    """(ticker, 캘린더 위치) → DayBar. 행이 없으면 INCOMPLETE."""

    def __init__(self, frames: dict, cal):
        self.cal = cal
        self.pos = {d: i for i, d in enumerate(cal)}
        self._idx: Dict[str, dict] = {}
        self._arr: Dict[str, tuple] = {}
        for t, df in frames.items():
            self._idx[t] = {d: k for k, d in enumerate(df.index)}
            self._arr[t] = tuple(df[c].to_numpy(float) for c in ("Open", "High", "Low", "Close", "Volume"))

    def get(self, t: str, i: int) -> DayBar:
        k = self._idx.get(t, {}).get(self.cal[i])
        if k is None:
            return DayBar("INCOMPLETE")
        o, h, lo, c, v = (float(a[k]) for a in self._arr[t])
        return classify(o, h, lo, c, v)


@dataclass
class Profile:
    initial_cash: float
    RISK_PCT: float
    MAX_TOTAL_HEAT_PCT: float
    MAX_POSITION_PCT: float
    min_cash_pct: float
    MIN_R_PCT: float = 3.0
    MAX_R_PCT: float = 15.0

    def sizing_cfg(self) -> dict:
        return {k: getattr(self, k) for k in ("RISK_PCT", "MAX_POSITION_PCT", "MAX_TOTAL_HEAT_PCT", "MIN_R_PCT",
                                              "MAX_R_PCT")}


@dataclass
class Account:
    name: str
    pool: str                       # B00 | B01
    rank: str                       # GRADE | RANDOM
    topn: Optional[int]
    seed: Optional[int] = None
    cash: float = 0.0
    realized: float = 0.0
    positions: Dict[int, dict] = field(default_factory=dict)        # 진입 순서 = 삽입 순서
    reservations: Dict[int, dict] = field(default_factory=dict)     # order id → {...}
    orders: List[dict] = field(default_factory=list)
    trades: List[dict] = field(default_factory=list)
    daily: List[dict] = field(default_factory=list)
    decisions: Dict[str, int] = field(default_factory=dict)
    ordered_keys: set = field(default_factory=set)


def run(cands: List[dict], book: BarBook, cal_days: List[str], start: int, end: int, accounts: List[Account],
        prof: Profile, costs: dict, next_session: Dict[str, Optional[str]]) -> List[Account]:
    """cands: 거래일별 후보 dict 목록 (keys: signal_key, ticker, day_i, close, stop, grade, quality, B00, B01).
    next_session: 거래일 → 다음 정규장 (없으면 None). 캘린더 위치 i 의 목표 주문은 i 에서 판정한다."""
    from scanner.sizing import calculate_position

    slip, bc = costs["slippage_bp"] / 10000.0, costs["buy_commission"]
    sc = costs["sell_commission"] + costs["sell_tax"]
    unit_cost = lambda px: px * (1 + slip) * (1 + bc)            # noqa: E731
    unit_proceeds = lambda px: px * (1 - slip) * (1 - sc)          # noqa: E731
    cfg = prof.sizing_cfg()
    by_day: Dict[int, List[dict]] = {}
    for c in cands:
        by_day.setdefault(c["day_i"], []).append(c)
    oid = 0
    for a in accounts:
        a.cash = prof.initial_cash
    for i in range(start, end + 1):
        day = cal_days[i]
        nxt = next_session.get(day)
        for a in accounts:
            # (1) 그날 목표 주문
            for o in [o for o in a.orders if o["state"] == "PENDING" and o["target_i"] == i]:
                b = book.get(o["ticker"], i)
                if b.state == "INCOMPLETE":
                    o["state"] = "FILL_UNKNOWN"
                    continue
                r = a.reservations.pop(o["id"])
                if b.state == "ZERO":
                    o.update(state="UNFILLED", reason="ZERO_VOLUME_EXPIRED")
                    continue
                if b.o <= o["stop"] or (b.o - o["stop"]) / b.o < prof.MIN_R_PCT / 100.0:
                    o["state"] = "SKIP_OPEN_STOP"
                    continue
                if b.o <= o["limit"]:
                    px, kind = b.o, "open"
                elif b.l <= o["limit"]:
                    px, kind = o["limit"], "limit"
                else:
                    o["state"] = "UNFILLED"
                    continue
                if px <= o["stop"]:
                    o["state"] = "REJECTED_AT_FILL"
                    continue
                cost = o["qty"] * unit_cost(px)
                assert cost <= r["reserved"] + 1e-6, (a.name, o, px)
                a.cash -= cost
                o.update(state="FILLED", fill_px=px, fill_kind=kind)
                a.positions[o["id"]] = {"ticker": o["ticker"], "qty": o["qty"], "entry_px": px, "stop": o["stop"],
                                        "cost": cost, "risk0": o["qty"] * (unit_cost(px) - unit_proceeds(o["stop"])),
                                        "entry_i": i, "last_close": None, "unresolved": False, "zero_days": 0,
                                        "signal_key": o["signal_key"]}
            # (2) S01
            for pid in list(a.positions):
                p = a.positions[pid]
                if p["unresolved"]:
                    continue
                b = book.get(p["ticker"], i)
                if b.state == "INCOMPLETE":
                    p["unresolved"] = True
                    continue
                if b.state == "ZERO":
                    p["last_close"] = b.c
                    p["zero_days"] += 1
                    continue
                reason = px = None
                if p["entry_i"] == i:
                    if b.l <= p["stop"]:
                        reason, px = "STOP_DAY0", p["stop"]
                elif b.o <= p["stop"]:
                    reason, px = "STOP_GAP", b.o
                elif b.l <= p["stop"]:
                    reason, px = "STOP", p["stop"]
                if reason:
                    proceeds = p["qty"] * unit_proceeds(px)
                    pnl = proceeds - p["cost"]
                    a.cash += proceeds
                    a.realized += pnl
                    a.trades.append({**p, "exit_i": i, "exit_px": px, "reason": reason, "pnl": pnl,
                                     "R": pnl / p["risk0"] if p["risk0"] > 0 else math.nan})
                    del a.positions[pid]
                else:
                    p["last_close"] = b.c
            # (3) 평가·위험
            invested = he = hc = 0.0
            breached = missing = False
            for p in a.positions.values():
                stale = p["unresolved"]
                px = (p["last_close"] or p["entry_px"]) if stale else p["last_close"]
                invested += p["qty"] * px
                r_entry = max(0.0, p["entry_px"] - p["stop"]) * p["qty"]
                he += r_entry
                if stale:
                    missing = True
                    hc += r_entry
                elif px < p["stop"]:
                    breached = True
                    hc += r_entry
                else:
                    hc += (px - p["stop"]) * p["qty"]
            reserved = sum(r["reserved"] for r in a.reservations.values())
            pend = sum(r["risk"] for r in a.reservations.values())
            unknown = any(o["state"] == "FILL_UNKNOWN" for o in a.orders)
            equity = a.cash + invested
            free = a.cash - reserved
            floor = prof.min_cash_pct * equity
            ehu, chu = he + pend, hc + pend
            blocks = []
            if breached:
                blocks.append("STOP_BREACHED_OPEN")
            if missing:
                blocks.append("PRICE_DATA_MISSING")
            if unknown:
                blocks.append("PENDING_UNRESOLVED")
            if not (math.isfinite(equity) and equity > 0):
                blocks.append("RISK_UNCOMPUTABLE")
            held = {p["ticker"] for p in a.positions.values()} | {r["ticker"] for r in a.reservations.values()}
            # (4) 판단
            pool = [c for c in by_day.get(i, []) if c[a.pool]]
            if a.rank == "RANDOM":
                pool.sort(key=lambda c: (random_key(a.seed or 0, day, c["signal_key"]), c["ticker"]))
            else:
                pool.sort(key=lambda c: (GRADE.get(c["grade"], 3) * 10 + QUALITY.get(c["quality"], 3), c["ticker"],
                                         c["signal_key"]))
            placed = 0
            for c in pool:
                reason = None
                if c["ticker"] in held or c["signal_key"] in a.ordered_keys:
                    reason = "DUPLICATE_HOLDING_OR_ORDER"
                elif a.topn is not None and placed >= a.topn:
                    reason = "DAILY_TOP_N"
                elif blocks:
                    reason = blocks[0]
                elif nxt is None:
                    reason = "NO_NEXT_SESSION"
                else:
                    lim = limit_floor_cent(c["close"])
                    unit = unit_cost(lim)
                    avail = free - floor
                    if avail < unit:
                        reason = "MIN_CASH_RESERVE" if free >= unit else "INSUFFICIENT_CASH"
                    else:
                        res = calculate_position(lim, c["stop"], equity, max(0.0, avail) / (1 + slip) / (1 + bc),
                                                 ehu, "US", cfg)
                        qty = int(res["qty"])
                        if qty <= 0:
                            rs = res.get("reasons") or ["unknown"]
                            reason = "R_BAND" if rs[0] in R_BAND else str(rs[0]).upper()
                if reason is not None:
                    a.decisions[reason] = a.decisions.get(reason, 0) + 1
                    continue
                oid += 1
                rc, pr = qty * unit, qty * max(0.0, lim - c["stop"])
                o = {"id": oid, "ticker": c["ticker"], "signal_key": c["signal_key"], "decision_i": i,
                     "target_i": i + 1 if i + 1 < len(cal_days) and cal_days[i + 1] == nxt else -1,
                     "target_day": nxt, "limit": lim, "stop": c["stop"], "qty": qty, "state": "PENDING"}
                a.orders.append(o)
                a.reservations[oid] = {"ticker": c["ticker"], "reserved": rc, "risk": pr}
                a.ordered_keys.add(c["signal_key"])
                free -= rc
                reserved += rc
                ehu += pr
                chu += pr
                held.add(c["ticker"])
                placed += 1
                a.decisions["ORDERED"] = a.decisions.get("ORDERED", 0) + 1
            a.daily.append({"day": day, "equity": equity, "cash": a.cash, "invested": invested, "reserved": reserved,
                            "entry_heat": ehu / equity if equity > 0 else math.nan,
                            "current_heat": chu / equity if equity > 0 else math.nan,
                            "n_positions": len(a.positions),
                            "incomplete": bool(missing or unknown)})
    return accounts


def open_rows(a: Account) -> List[dict]:
    return [dict(p, order_id=k) for k, p in a.positions.items()]


def p7c_accounts(seeds) -> List[Account]:
    out = [Account("B00", "B00", "GRADE", None), Account("B00N", "B00", "GRADE", 2),
           Account("B01", "B01", "GRADE", 2)]
    out += [Account(f"B02:s{s:02d}", "B01", "RANDOM", 2, seed=s) for s in seeds]
    return out


def zero_volume_skip_audit(cand, orders_t1, book: BarBook) -> dict:
    """기존 P7(T1_100) 주문 중 D+1 이 거래량 0·미확정이라 다음 봉으로 넘어가 체결/판정된 건 (P7 기존 정책 영향 범위)."""
    rows = []
    for r in cand.itertuples(index=False):
        o = orders_t1.loc[r.cid]
        if o["status"] not in ("FILLED", "UNFILLED", "SKIP_OPEN_STOP"):
            continue
        d1 = int(o["decision_ci"]) + 1
        if d1 >= len(book.cal):
            continue
        b = book.get(r.ticker, d1)
        if b.state != "OK":
            rows.append({"signal_key": r.signal_key, "ticker": r.ticker, "decision_day": str(book.cal[d1 - 1].date()),
                         "d1": str(book.cal[d1].date()), "d1_state": b.state, "p7_status": o["status"],
                         "p7_fill_day": (str(book.cal[int(o["fill_ci"])].date())
                                         if o["fill_ci"] == o["fill_ci"] and o["fill_ci"] is not None else None)})
    return {"count": len(rows), "rows": rows}


def daily_frame(a: Account):
    import pandas as pd
    return pd.DataFrame(a.daily)


def trade_rows(a: Account, cal_days: List[str]) -> List[dict]:
    rows = []
    for t in a.trades:
        rows.append({"ticker": t["ticker"], "entry_day": cal_days[t["entry_i"]], "qty": t["qty"],
                     "entry_px": t["entry_px"], "exit_day": cal_days[t["exit_i"]], "exit_px": t["exit_px"],
                     "reason": t["reason"], "pnl": t["pnl"], "state": "CLOSED"})
    for p in a.positions.values():
        rows.append({"ticker": p["ticker"], "entry_day": cal_days[p["entry_i"]], "qty": p["qty"],
                     "entry_px": p["entry_px"], "exit_day": None, "exit_px": None, "reason": None, "pnl": None,
                     "state": "OPEN", "unresolved": p["unresolved"]})
    return sorted(rows, key=lambda r: (r["entry_day"], r["ticker"]))


def _np_safe(x):
    return float(x) if isinstance(x, (np.floating,)) else x
