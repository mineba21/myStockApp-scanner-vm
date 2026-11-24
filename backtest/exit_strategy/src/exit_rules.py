"""매도 전략 정의 (S00~S09).

모든 전략은 하나의 규칙 묶음(ExitConfig)으로 표현되고 같은 시뮬레이터로 실행된다.
그래서 전략 간 차이는 '규칙' 뿐이며 매수 신호·매수가·기간·비용은 동일하다.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import List, Optional, Tuple

BREAKOUT_TYPES = ("BREAKOUT", "RE_BREAKOUT")


@dataclass(frozen=True)
class HardStop:
    method: str = "scanner"          # scanner | fixed | atr | scanner_cap | atr_cap
    pct: Optional[float] = None      # fixed: 매수가 대비 손절률
    atr_mult: Optional[float] = None
    max_loss_pct: Optional[float] = None   # *_cap: 최대 허용 손실률


@dataclass(frozen=True)
class BreakoutFailure:
    """종가 확인형 매수 근거 훼손. 종가 확정 후 다음 거래일 시가 매도."""
    kind: str = "pivot"              # pivot | upthrust | support (REBOUND: 신호 당시 MA150)
    tol_pct: float = 0.0             # 하회 허용 범위
    consec: int = 1                  # 연속 종가 이탈 일수
    vol_ratio: Optional[float] = None   # 이탈 종가 거래량 / 직전 20일 평균
    window: Optional[int] = None     # 매수 후 감시 거래일 수 (None=제한 없음)


@dataclass(frozen=True)
class TrendExit:
    ma: Optional[int] = None         # 10 | 21 | 50 | 150
    consec: int = 1
    rs_weak: bool = False
    vol_ratio: Optional[float] = None
    stage: Optional[str] = None      # STAGE3 (Stage2 이탈: 3/1) | STAGE4


@dataclass(frozen=True)
class Trailing:
    activation_pct: float = 0.10
    drawdown_pct: Optional[float] = 0.06
    atr_mult: Optional[float] = None


@dataclass(frozen=True)
class Partial:
    mode: str = "trail"              # trail | target
    activation_pct: float = 0.10     # trail: 보호 활성화 / target: 목표 수익률
    drawdown_pct: Optional[float] = 0.06
    ratio: float = 0.5
    remaining: str = "trail_pct"     # trail_pct | ma21 | ma50 | hard | breakeven
    remaining_dd: float = 0.10


@dataclass(frozen=True)
class TimeStop:
    days: int = 20
    min_ret_pct: float = 0.0         # 수익률이 이 값 '미만'이면 매도 (0% 이하 → 0.0 + inclusive)
    inclusive: bool = False          # True: ret <= min_ret (수익률 0% 이하)
    rs_weak: bool = False
    below_ma21: bool = False


@dataclass(frozen=True)
class ExitConfig:
    name: str
    family: str
    hard: Optional[HardStop] = None
    bf: Optional[BreakoutFailure] = None
    trend: Optional[TrendExit] = None
    trail: Optional[Trailing] = None
    partial: Optional[Partial] = None
    time: Optional[TimeStop] = None
    review: Tuple[str, ...] = ()     # 매도 대신 기록만 할 규칙군: "trend", "time"
    label: str = ""

    def params(self) -> dict:
        d = asdict(self)
        d.pop("name"); d.pop("family"); d.pop("label")
        return d


def _pct(x: float) -> str:
    return f"{x * 100:g}"


def catalog() -> List[ExitConfig]:
    """S00~S09 전체 Parameter grid."""
    out: List[ExitConfig] = []
    # S00 — Buy & Hold (평가 종료 시점은 시뮬레이터 horizon 으로 통일)
    out.append(ExitConfig("S00", "S00", label="Buy & Hold"))
    # S01 — 기존 Scanner Stop Loss
    out.append(ExitConfig("S01", "S01", hard=HardStop("scanner"), label="Scanner stop_loss"))
    # S02 — 고정 손절
    for p in (0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.10):
        out.append(ExitConfig(f"S02_fix{_pct(p)}", "S02", hard=HardStop("fixed", pct=p),
                              label=f"Fixed -{_pct(p)}%"))
    # S03 — ATR / 기술적 손절 + 최대 손실 결합
    for m in (1.5, 2.0, 2.5, 3.0):
        out.append(ExitConfig(f"S03_atr{m:g}", "S03", hard=HardStop("atr", atr_mult=m),
                              label=f"ATR×{m:g}"))
    for cap in (0.05, 0.07, 0.10):
        out.append(ExitConfig(f"S03_scap{_pct(cap)}", "S03", hard=HardStop("scanner_cap", max_loss_pct=cap),
                              label=f"max(Scanner stop, -{_pct(cap)}%)"))
        for m in (2.0, 3.0):
            out.append(ExitConfig(f"S03_atr{m:g}cap{_pct(cap)}", "S03",
                                  hard=HardStop("atr_cap", atr_mult=m, max_loss_pct=cap),
                                  label=f"max(ATR×{m:g}, -{_pct(cap)}%)"))
    # S04 — Early Breakout Failure (+ 기본 Hard Stop: 기존 Scanner stop)
    base_hard = HardStop("scanner")
    for tol in (0.0, 0.01, 0.02):
        for win in (3, 5, 10, None):
            w = "all" if win is None else str(win)
            variants = [
                ("A", BreakoutFailure("pivot", tol, 1, None, win)),
                ("B", BreakoutFailure("pivot", tol, 2, None, win)),
                ("C", BreakoutFailure("pivot", tol, 1, 1.2, win)),
                ("D", BreakoutFailure("pivot", tol, 1, 1.5, win)),
            ]
            for code, bf in variants:
                out.append(ExitConfig(f"S04{code}_t{_pct(tol)}_w{w}", "S04", hard=base_hard, bf=bf,
                                      label=f"{code}: pivot tol {_pct(tol)}% win {w}"))
    out.append(ExitConfig("S04E_t0_w3", "S04", hard=base_hard,
                          bf=BreakoutFailure("pivot", 0.0, 1, None, 3), label="E: 3일 내 pivot 하회"))
    out.append(ExitConfig("S04F_upthrust", "S04", hard=base_hard,
                          bf=BreakoutFailure("upthrust"), label="F: Scanner upthrust_failed"))
    for tol in (0.0, 0.01, 0.02):
        for k in (1, 2):
            out.append(ExitConfig(f"S04R_ma150_t{_pct(tol)}_c{k}", "S04", hard=base_hard,
                                  bf=BreakoutFailure("support", tol, k), label=f"REBOUND MA150 이탈 tol {_pct(tol)}% ×{k}"))
    # S05 — Trend Deterioration (+ Scanner stop)
    for ma in (10, 21, 50, 150):
        out.append(ExitConfig(f"S05_ma{ma}", "S05", hard=base_hard, trend=TrendExit(ma=ma),
                              label=f"MA{ma} 종가 이탈"))
        out.append(ExitConfig(f"S05_ma{ma}x2", "S05", hard=base_hard, trend=TrendExit(ma=ma, consec=2),
                              label=f"MA{ma} 2일 연속 이탈"))
        out.append(ExitConfig(f"S05_ma{ma}_rs", "S05", hard=base_hard, trend=TrendExit(ma=ma, rs_weak=True),
                              label=f"MA{ma} 이탈 + RS 약화"))
        out.append(ExitConfig(f"S05_ma{ma}_vol", "S05", hard=base_hard, trend=TrendExit(ma=ma, vol_ratio=1.2),
                              label=f"MA{ma} 이탈 + 거래량 1.2x"))
    out.append(ExitConfig("S05_stage3", "S05", hard=base_hard, trend=TrendExit(stage="STAGE3"),
                          label="주봉 Stage2 이탈(3/1)"))
    out.append(ExitConfig("S05_stage4", "S05", hard=base_hard, trend=TrendExit(stage="STAGE4"),
                          label="주봉 Stage4 전환"))
    # S06 — Trailing Stop (+ Scanner stop)
    for act in (0.05, 0.10, 0.15, 0.20):
        for dd in (0.03, 0.05, 0.06, 0.08, 0.10):
            out.append(ExitConfig(f"S06_a{_pct(act)}_d{_pct(dd)}", "S06", hard=base_hard,
                                  trail=Trailing(act, dd), label=f"+{_pct(act)}% 후 고점 -{_pct(dd)}%"))
        for m in (2.0, 3.0):
            out.append(ExitConfig(f"S06_a{_pct(act)}_atr{m:g}", "S06", hard=base_hard,
                                  trail=Trailing(act, None, m), label=f"+{_pct(act)}% 후 고점-ATR×{m:g}"))
    # S07 — Partial Profit Protection (+ Scanner stop)
    for act in (0.05, 0.10, 0.15, 0.20):
        for dd in (0.05, 0.06, 0.08, 0.10):
            for ratio in (0.25, 0.33, 0.5, 0.75):
                for rem, rdd in (("trail_pct", 0.08), ("trail_pct", 0.10), ("ma21", None),
                                 ("ma50", None), ("hard", None), ("breakeven", None)):
                    if rem == "trail_pct" and rdd <= dd:
                        continue    # 잔여 trailing 이 부분매도선보다 가까우면 동시 청산과 같음
                    tag = f"{rem}{_pct(rdd) if rdd else ''}"
                    out.append(ExitConfig(
                        f"S07_a{_pct(act)}_d{_pct(dd)}_r{int(ratio * 100)}_{tag}", "S07", hard=base_hard,
                        partial=Partial("trail", act, dd, ratio, rem, rdd or 0.10),
                        label=f"+{_pct(act)}% → 고점-{_pct(dd)}% {int(ratio*100)}% 매도, 잔여 {tag}"))
    for tgt in (0.10, 0.15, 0.20):
        for ratio in (0.33, 0.5):
            for rem, rdd in (("trail_pct", 0.10), ("ma50", None), ("breakeven", None)):
                tag = f"{rem}{_pct(rdd) if rdd else ''}"
                out.append(ExitConfig(
                    f"S07T_t{_pct(tgt)}_r{int(ratio * 100)}_{tag}", "S07T", hard=base_hard,
                    partial=Partial("target", tgt, None, ratio, rem, rdd or 0.10),
                    label=f"+{_pct(tgt)}% 도달 즉시 {int(ratio*100)}% 매도, 잔여 {tag}"))
    # S08 — Time Stop (+ Scanner stop)
    for days in (5, 10, 15, 20, 30):
        for code, ts in (("le0", TimeStop(days, 0.0, True)), ("lt2", TimeStop(days, 0.02)),
                         ("lt3", TimeStop(days, 0.03)), ("lt2rs", TimeStop(days, 0.02, rs_weak=True)),
                         ("lt2ma21", TimeStop(days, 0.02, below_ma21=True))):
            out.append(ExitConfig(f"S08_{days}d_{code}", "S08", hard=base_hard, time=ts,
                                  label=f"{days}일 후 {code}"))
    out.extend(hybrid_configs())
    return out


def hybrid_configs(bf: Optional[BreakoutFailure] = None, partial: Optional[Partial] = None,
                   max_loss: float = 0.07, suffix: str = "") -> List[ExitConfig]:
    """S09 Hybrid 기본 설정 (A: P1~P3 자동, P4~P5 기록 / B: 전부 규칙 매도 가정)."""
    hard = HardStop("scanner_cap", max_loss_pct=max_loss)
    bf = bf or BreakoutFailure("pivot", 0.0, 1, None, None)     # CONFIRM: 종가 확인 → 다음날 시가
    partial = partial or Partial("trail", 0.10, 0.06, 0.5, "trail_pct", 0.10)
    trend = TrendExit(ma=50, consec=2)
    time = TimeStop(20, 0.02)
    common = dict(hard=hard, bf=bf, partial=partial, trend=trend, time=time)
    return [
        ExitConfig(f"S09A{suffix}", "S09", review=("trend", "time"),
                   label="Hybrid A: P1~P3 자동, P4~P5 REVIEW(기록)", **common),
        ExitConfig(f"S09B{suffix}", "S09", label="Hybrid B: P1~P5 전부 매도(가상 승인)", **common),
    ]


def bf_active(bf: Optional[BreakoutFailure], signal_type: str) -> bool:
    if bf is None:
        return False
    if bf.kind in ("pivot", "upthrust"):
        return signal_type in BREAKOUT_TYPES
    if bf.kind == "support":
        return signal_type == "REBOUND"
    return False
