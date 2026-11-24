"""보수적 일봉 체결 시뮬레이터.

체결 규칙
- 매수: 신호 확인 후 첫 거래 가능일 시가 × (1 + slippage).
- 장중 Stop(Hard / Trailing / 부분매도 trailing / 잔여 trailing):
  수준은 **전 거래일 종가까지** 확인된 정보(최고가·보호 상태·ATR)로만 계산한다.
  시가 ≤ Stop → 시가 체결(갭 하락), 저가 ≤ Stop → Stop 가격 체결. 모두 불리한 slippage 적용.
- 종가 기반 신호(Breakout 실패·추세 악화·시간 손절·MA 잔여청산): 당일 종가로 체결하지
  않고 다음 거래 가능일 시가에 체결한다.
- 당일 고가로 갱신한 최고가·수익 보호 활성화는 다음 거래일부터 적용한다.
- 목표가 부분매도(상방 지정가)와 하방 Stop 이 같은 봉에서 모두 닿으면 순서를 알 수 없다
  → scenario='conservative' 는 하방 먼저, 'optimistic' 은 상방 먼저. ambiguous 플래그 기록.
- 거래정지(가격 0·거래량 0) 봉은 체결 불가. 국내 하한가 잠김(고가=저가, 전일 대비 -29.9% 이하)
  봉은 매도 불가로 처리한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .exit_rules import ExitConfig, bf_active

EPS = 1e-9


@dataclass
class TradeData:
    """매수일(index 0)부터의 일봉 경로와 매수 시점에 이미 알려진 신호 정보."""
    dates: list
    o: list
    h: list
    l: list
    c: list
    v: list
    tradable: list
    prev_close: list
    ma10: list
    ma21: list
    ma50: list
    ma150: list
    atr: list
    volma: list
    rs_weak: list
    stage_w: list
    week_complete: list
    signal_type: str
    pivot: Optional[float]
    scanner_stop: Optional[float]
    signal_ma150: Optional[float]
    atr_at_signal: Optional[float]      # 신호 확인일(매수 전일) 종가 기준 ATR14
    upthrust_day: Optional[int] = None  # upthrust 판정이 확정되는 index (매수일 기준, 0 이상)
    upthrust_failed: Optional[bool] = None
    market: str = "US"

    @property
    def n(self) -> int:
        return len(self.o)


@dataclass
class Costs:
    buy: float
    sell: float           # 수수료 + 세금
    slip: float           # 편도 slippage (비율)


def _initial_hard(cfg: ExitConfig, td: TradeData, entry: float) -> Optional[float]:
    hs = cfg.hard
    if hs is None:
        return None
    if hs.method == "scanner":
        return td.scanner_stop
    if hs.method == "fixed":
        return entry * (1 - hs.pct)
    if hs.method == "atr":
        return entry - hs.atr_mult * td.atr_at_signal if td.atr_at_signal else None
    if hs.method == "scanner_cap":
        cap = entry * (1 - hs.max_loss_pct)
        return max(td.scanner_stop, cap) if td.scanner_stop else cap
    if hs.method == "atr_cap":
        cap = entry * (1 - hs.max_loss_pct)
        return max(entry - hs.atr_mult * td.atr_at_signal, cap) if td.atr_at_signal else cap
    raise ValueError(hs.method)


def simulate(td: TradeData, cfg: ExitConfig, horizon: int, costs: Costs,
             scenario: str = "conservative", limit_pct: float = 0.30,
             post_days=(5, 10, 20, 60)) -> Dict:
    o, h, l, c = td.o, td.h, td.l, td.c
    slip = costs.slip
    raw_entry = o[0]
    entry = raw_entry * (1 + slip)
    basis = entry * (1 + costs.buy)
    last = min(horizon, td.n) - 1
    incomplete = td.n < horizon

    hard = _initial_hard(cfg, td, entry)
    initial_hard = hard
    abnormal_entry = bool(hard is not None and hard >= entry)
    qty = 1.0
    realized = 0.0                 # 매도 대금 (비용 차감 후, 매수가 1단위 기준)
    sell_cost_paid = 0.0
    fills: List[dict] = []
    pending: Optional[str] = None
    peak = None                    # 전일까지의 최고가
    trail_on = False
    partial_on = False
    partial_done = False
    partial_value = 0.0
    bf_run = 0
    trend_run = 0
    review: Dict[str, int] = {}
    ambiguous = False
    gap_excess = 0.0
    last_valid = 0
    use_bf = bf_active(cfg.bf, td.signal_type)
    pt = cfg.partial
    tr = cfg.trail

    def sell(t, portion, raw_price, reason, gap_from=None):
        nonlocal qty, realized, sell_cost_paid, partial_value, gap_excess
        portion = min(portion, qty)
        if portion <= EPS:
            return
        px = raw_price * (1 - slip)
        gross = portion * px
        realized_amt = gross * (1 - costs.sell)
        sell_cost_paid += gross * costs.sell + portion * raw_price * slip
        realized += realized_amt
        qty -= portion
        if reason == "PARTIAL":
            partial_value += realized_amt
        if gap_from is not None and raw_price < gap_from:
            gap_excess += portion * (gap_from - raw_price) / entry
        fills.append({"t": t, "date": td.dates[t], "portion": round(portion, 4),
                      "price": raw_price, "reason": reason, "gap": gap_from is not None and raw_price < gap_from})

    t = 0
    for t in range(0, last + 1):
        if not td.tradable[t]:
            continue                                  # 거래정지: 체결·판정 없음
        last_valid = t
        locked = (td.market == "KR" and h[t] == l[t] and td.prev_close[t]
                  and c[t] / td.prev_close[t] - 1 <= -(limit_pct - 0.001))

        # 1) 전일 종가 신호 → 오늘 시가
        if pending and t > 0 and not locked:
            sell(t, qty, o[t], pending)
            pending = None
            break

        # 2) 장중 주문
        if not locked:
            target_lvl = None
            if pt and pt.mode == "target" and not partial_done:
                target_lvl = entry * (1 + pt.activation_pct)
            up_hit = target_lvl is not None and h[t] >= target_lvl

            def downside_orders():
                ords = []
                if hard is not None:
                    ords.append((hard, "all", "HARD_STOP"))
                if tr and trail_on and peak is not None:
                    if tr.atr_mult is not None:
                        lvl = peak - tr.atr_mult * td.atr[t - 1] if t > 0 and td.atr[t - 1] else None
                    else:
                        lvl = peak * (1 - tr.drawdown_pct)
                    if lvl is not None:
                        ords.append((lvl, "all", "TRAIL_STOP"))
                if pt and peak is not None:
                    if pt.mode == "trail" and partial_on and not partial_done:
                        ords.append((peak * (1 - pt.drawdown_pct), "partial", "PARTIAL"))
                    if partial_done and pt.remaining == "trail_pct":
                        ords.append((peak * (1 - pt.remaining_dd), "all", "REMAIN_TRAIL"))
                return sorted(ords, key=lambda x: -x[0])

            def run_downside():
                nonlocal partial_done
                done_reasons = set()
                while qty > EPS:
                    hit = None
                    for lvl, portion, reason in downside_orders():
                        if reason in done_reasons:
                            continue
                        if o[t] <= lvl or l[t] <= lvl:
                            hit = (lvl, portion, reason)
                            break
                    if hit is None:
                        return
                    lvl, portion, reason = hit
                    px = o[t] if o[t] <= lvl else lvl
                    size = pt.ratio if portion == "partial" else qty
                    sell(t, size, px, reason, gap_from=lvl)
                    done_reasons.add(reason)
                    if reason == "PARTIAL":
                        partial_done = True

            def run_target():
                nonlocal partial_done
                if up_hit and not partial_done and qty > EPS:
                    px = o[t] if o[t] >= target_lvl else target_lvl
                    sell(t, pt.ratio, px, "PARTIAL_TARGET")
                    partial_done = True

            down_any = any(o[t] <= lvl or l[t] <= lvl for lvl, _, _ in downside_orders())
            if up_hit and o[t] >= target_lvl:
                run_target(); run_downside()          # 시가 갭 상승: 목표가 먼저 확정
            elif up_hit and down_any:
                gap_down_open = any(o[t] <= lvl for lvl, _, _ in downside_orders())
                if not gap_down_open:
                    ambiguous = True
                if scenario == "optimistic" and not gap_down_open:
                    run_target(); run_downside()
                else:
                    run_downside(); run_target()
            else:
                run_downside(); run_target()
        if qty <= EPS:
            break

        # 3) 종가 확정 신호 (다음 거래일 시가 체결)
        triggered: List[str] = []
        if use_bf:
            bf = cfg.bf
            if bf.kind == "upthrust":
                if td.upthrust_failed and td.upthrust_day is not None and t >= td.upthrust_day:
                    triggered.append("BF_UPTHRUST")
            else:
                ref = td.pivot if bf.kind == "pivot" else td.signal_ma150
                if ref:
                    below = c[t] < ref * (1 - bf.tol_pct)
                    bf_run = bf_run + 1 if below else 0
                    in_win = bf.window is None or t < bf.window
                    vol_ok = bf.vol_ratio is None or (td.volma[t] and td.v[t] >= bf.vol_ratio * td.volma[t])
                    if below and bf_run >= bf.consec and in_win and vol_ok:
                        triggered.append("BF_PIVOT" if bf.kind == "pivot" else "BF_SUPPORT")
        if pt and partial_done and pt.remaining in ("ma21", "ma50"):
            ma = td.ma21[t] if pt.remaining == "ma21" else td.ma50[t]
            if ma and c[t] < ma:
                triggered.append("REMAIN_" + pt.remaining.upper())
        if cfg.trend:
            te = cfg.trend
            fire = False
            if te.ma:
                ma = {10: td.ma10, 21: td.ma21, 50: td.ma50, 150: td.ma150}[te.ma][t]
                below = bool(ma) and c[t] < ma
                trend_run = trend_run + 1 if below else 0
                fire = below and trend_run >= te.consec
                if fire and te.rs_weak:
                    fire = bool(td.rs_weak[t])
                if fire and te.vol_ratio:
                    fire = bool(td.volma[t]) and td.v[t] >= te.vol_ratio * td.volma[t]
            elif te.stage:
                targets = ("STAGE3", "STAGE1") if te.stage == "STAGE3" else ("STAGE4",)
                fire = bool(td.week_complete[t]) and td.stage_w[t] in targets
            if fire:
                triggered.append("TREND")
        if cfg.time and t == cfg.time.days - 1:
            ts = cfg.time
            ret = c[t] / entry - 1
            fire = ret <= ts.min_ret_pct if ts.inclusive else ret < ts.min_ret_pct
            if fire and ts.rs_weak:
                fire = bool(td.rs_weak[t])
            if fire and ts.below_ma21:
                fire = bool(td.ma21[t]) and c[t] < td.ma21[t]
            if fire:
                triggered.append("TIME_STOP")
        for reason in triggered:
            fam = "trend" if reason == "TREND" else "time" if reason == "TIME_STOP" else None
            if fam and fam in cfg.review:
                review.setdefault(reason, t)
                continue
            if pending is None:
                pending = reason

        # 4) 장마감 후 상태 갱신 (다음 거래일부터 적용)
        peak = h[t] if peak is None else max(peak, h[t])
        if tr and not trail_on and peak >= entry * (1 + tr.activation_pct):
            trail_on = True
        if pt and pt.mode == "trail" and not partial_on and peak >= entry * (1 + pt.activation_pct):
            partial_on = True
        if pt and partial_done and pt.remaining == "breakeven" and hard is not None:
            hard = max(hard, entry)
        elif pt and partial_done and pt.remaining == "breakeven":
            hard = entry

    # ── 결과 ─────────────────────────────────────────────────
    exit_t = fills[-1]["t"] if qty <= EPS else last_valid
    mark = c[last_valid]
    open_value = qty * mark
    total_value = realized + open_value
    ret = total_value / basis - 1
    realized_ret = realized / basis
    path_h = h[:exit_t + 1]
    path_l = [x for x, ok in zip(l[:exit_t + 1], td.tradable[:exit_t + 1]) if ok]
    mfe = max(path_h) / entry - 1 if path_h else 0.0
    mae = min(path_l) / entry - 1 if path_l else 0.0
    res = {
        "strategy": cfg.name, "family": cfg.family,
        "entry_date": td.dates[0], "entry_price": entry, "raw_entry_open": raw_entry,
        "initial_stop": initial_hard,
        "abnormal_entry": abnormal_entry,
        "exit_date": td.dates[exit_t] if qty <= EPS else None,
        "exit_reason": fills[-1]["reason"] if qty <= EPS else ("OPEN_PENDING_" + pending if pending else "OPEN_AT_HORIZON"),
        "first_exit_reason": fills[0]["reason"] if fills else None,
        "n_fills": len(fills), "open_qty": round(qty, 4),
        "return": ret, "realized_return_component": realized_ret,
        "unrealized_component": open_value / basis,
        "partial_component": partial_value / basis if partial_value else 0.0,
        "holding_days": exit_t + 1, "mfe": mfe, "mae": mae,
        "giveback": max(mfe - ret, 0.0) if mfe > 0 else 0.0,
        "capture": (ret / mfe) if mfe > 0.01 else None,
        "cost": (basis - entry + raw_entry * slip) / basis + sell_cost_paid / basis,
        "gap_excess": gap_excess, "ambiguous_bar": ambiguous,
        "incomplete_horizon": incomplete,
        "review_flags": ",".join(f"{k}@{v}" for k, v in review.items()),
        "fills": fills,
    }
    # 매도 이후 경로 (분석 전용 — 판단에 쓰지 않음)
    if qty <= EPS:
        F = fills[-1]["price"]
        for k in post_days:
            j = exit_t + k
            res[f"post_{k}d"] = c[j] / F - 1 if j < td.n and c[j] > 0 else None
        after_h = [x for x in h[exit_t + 1:exit_t + 61] if x > 0]
        after_c20 = c[exit_t + 20] if exit_t + 20 < td.n else None
        res["post_recover_entry"] = (max(after_h) >= entry) if after_h else None
        res["post_new_high"] = (max(after_h) > max(path_h)) if after_h else None
        res["post_further_decline"] = (after_c20 < F) if after_c20 else None
        res["post_window_days"] = len(after_h)
    return res
