"""STEP 12 — 권장 매도 규칙 산출.

백테스트 결과만으로 실거래를 활성화하지 않는다. 최고 상태도 RECOMMENDED_FOR_PAPER(모의) 이다.
기존 보유종목에는 소급 적용하지 않는다 (REVIEW 정책 유지).
"""
from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd

from .exit_rules import ExitConfig
from .parameter_search import objective
from .performance_metrics import trade_metrics
from .stage2 import CLASSES


def _cfg_for(name: str, cat: Dict[str, ExitConfig], composed: dict, market: str, cls: str) -> ExitConfig:
    if name in cat:
        return cat[name]
    from .exit_rules import (BreakoutFailure, HardStop, Partial, TimeStop, Trailing, TrendExit)
    raw = composed[f"{market}:{cls}"]["configs"][name]

    def mk(klass, d):
        return klass(**d) if d else None
    return ExitConfig(name=raw["name"], family=raw["family"], hard=mk(HardStop, raw["hard"]),
                      bf=mk(BreakoutFailure, raw["bf"]), trend=mk(TrendExit, raw["trend"]),
                      trail=mk(Trailing, raw["trail"]), partial=mk(Partial, raw["partial"]),
                      time=mk(TimeStop, raw["time"]), review=tuple(raw["review"]), label=raw["label"])


def _seg(df, strat, idx):
    s = df[(df["strategy"] == strat) & df["sig_idx"].isin(idx)]
    m = trade_metrics(s)
    return {"trades": len(s), "avg_return": m.get("avg_return"), "objective": objective(s["return"].to_numpy()),
            "win_rate": m.get("win_rate"), "profit_factor": m.get("profit_factor"),
            "loss_10_rate": m.get("loss_10_rate"), "worst_trade": m.get("worst_trade")}


def build_recommendations(cfg, final: dict, composed: dict, allb: pd.DataFrame, sel: dict,
                          cat: Dict[str, ExitConfig]) -> dict:
    rules = []
    min_n = cfg["validation"]["min_trades_for_selection"]
    for key, f in final.items():
        market, cls = key.split(":")
        c = _cfg_for(f["pick_strategy"], cat, composed, market, cls)
        bm = allb[allb["market"] == market]
        ix = sel[market]["idx"]
        for st in CLASSES[cls]:
            bt = bm[bm["signal_type"] == st]
            n_all = int(bt.loc[bt["strategy"] == "S01", "sig_idx"].nunique())
            if n_all == 0:
                rules.append({"market": market, "signal_type": st, "sample_count": 0,
                              "recommendation_status": "NEED_MORE_DATA",
                              "note": "현재 스캐너 설정에서 이 유형의 strict 통과 신호가 재현되지 않음"})
                continue
            oos = {name: _seg(bt, s, ix["test"]) for name, s in
                   (("pick", f["pick_strategy"]), ("S01_scanner_stop", "S01"), ("S00_hold", "S00"))}
            val = {name: _seg(bt, s, ix["validation"]) for name, s in
                   (("pick", f["pick_strategy"]), ("S01_scanner_stop", "S01"))}
            n_test = oos["pick"]["trades"]
            better_test = (oos["pick"]["objective"] or -9) > (oos["S01_scanner_stop"]["objective"] or -9)
            better_val = (val["pick"]["objective"] or -9) > (val["S01_scanner_stop"]["objective"] or -9)
            tail_ok = (oos["pick"]["loss_10_rate"] or 0) <= (oos["S01_scanner_stop"]["loss_10_rate"] or 0) + 1e-9
            tol = cfg["validation"].get("expectancy_tolerance", 0.005)
            mean_gap_test = (oos["pick"]["avg_return"] or 0) - (oos["S01_scanner_stop"]["avg_return"] or 0)
            mean_gap_val = (val["pick"]["avg_return"] or 0) - (val["S01_scanner_stop"]["avg_return"] or 0)
            mean_ok = mean_gap_test >= -tol and mean_gap_val >= -tol
            tradeoff = None
            # 전체 기간 S01 대비 쌍대 block bootstrap 중앙값 (Test 표본이 작을 때의 우연 개선 차단)
            pb = f.get("paired_vs_S01_full_bootstrap") or {}
            robust_ok = pb.get("mean_p50") is None or pb["mean_p50"] >= -tol
            if n_test < min_n or n_all < 2 * min_n:
                status = "NEED_MORE_DATA"
            elif f["pick"] == "S01":
                status = "NOT_RECOMMENDED"     # 기존 Scanner stop 보다 나은 대안이 검증되지 않음
            elif better_test and better_val and tail_ok and mean_ok and not robust_ok:
                status = "NEED_MORE_DATA"
                tradeoff = (f"Validation·Test 에서는 S01 보다 나았지만 전체 기간 쌍대 비교 중앙값이 "
                            f"{pb['mean_p50']*100:+.2f}%p (개선 확률 {pb.get('prob_positive', 0)*100:.0f}%) 로 "
                            "일관되지 않다. Test 표본 확대 후 재평가.")
            elif better_test and better_val and tail_ok and mean_ok:
                status = "RECOMMENDED_FOR_PAPER"
            elif better_test and better_val and tail_ok:
                status = "NOT_RECOMMENDED"
                tradeoff = (f"손실 꼬리는 줄였지만 거래당 기대수익이 S01 대비 Test {mean_gap_test*100:+.2f}%p, "
                            f"Validation {mean_gap_val*100:+.2f}%p 로 허용폭(-{tol*100:.1f}%p)을 넘게 낮다 — "
                            "큰 손실 방지와 상승 보존의 교환 관계. 손실 한도를 우선하면 선택 가능.")
            else:
                status = "NOT_RECOMMENDED"
            hs, bf, tr, pt = c.hard, c.bf, c.trail, c.partial
            bf_on = bf is not None and ((bf.kind in ("pivot", "upthrust") and st != "REBOUND") or
                                        (bf.kind == "support" and st == "REBOUND"))
            auto = ["P1_HARD_STOP"] if hs else []
            if bf_on:
                auto.append("P2_BREAKOUT_FAILURE_CONFIRM" if bf.kind != "support" else "P2_SUPPORT_FAILURE_CONFIRM")
            if pt:
                auto.append("P3_PROFIT_PROTECTION_PARTIAL")
            if tr:
                auto.append("P3_TRAILING_STOP")
            review = []
            if c.trend:
                review.append(f"P4_TREND_MA{c.trend.ma}x{c.trend.consec}" if c.trend.ma else f"P4_TREND_{c.trend.stage}")
            if c.time:
                review.append(f"P5_TIME_{c.time.days}d_lt{c.time.min_ret_pct*100:g}pct")
            stab = f.get("rank_stability", {})
            rules.append({
                "market": market, "signal_type": st, "class": cls,
                "selected_strategy": f["pick_strategy"], "selected_label": c.label,
                "initial_stop_method": hs.method if hs else "NONE",
                "max_loss_pct": hs.max_loss_pct if hs and hs.max_loss_pct else (hs.pct if hs and hs.pct else None),
                "atr_multiplier": hs.atr_mult if hs else None,
                "breakout_failure_mode": ("CONFIRM_CLOSE_NEXT_OPEN" if bf_on and bf.kind != "upthrust" else
                                          "SCANNER_UPTHRUST_D+3" if bf_on else "NONE"),
                "breakout_confirmation_days": bf.consec if bf_on else None,
                "breakout_volume_ratio": bf.vol_ratio if bf_on else None,
                "pivot_tolerance_pct": bf.tol_pct if bf_on else None,
                "breakout_watch_window_days": bf.window if bf_on else None,
                "profit_activation_pct": pt.activation_pct if pt else (tr.activation_pct if tr else None),
                "trailing_drawdown_pct": pt.drawdown_pct if pt else (tr.drawdown_pct if tr else None),
                "partial_sell_ratio": pt.ratio if pt else None,
                "partial_mode": pt.mode if pt else None,
                "remaining_exit_method": (f"{pt.remaining}{'_' + str(pt.remaining_dd) if pt.remaining == 'trail_pct' else ''}"
                                          if pt else None),
                "time_stop_days": c.time.days if c.time else None,
                "auto_exit_rules": auto, "review_exit_rules": review,
                "sample_count": {"all": n_all, "test": n_test, "validation": val["pick"]["trades"]},
                "validation_performance": val,
                "out_of_sample_performance": oos,
                "robustness": {
                    "rank_stability_train_vs_val": {k: v for k, v in stab.items()
                                                    if k in ("S02", "S03", "S04", "S06", "S07", "S07T")},
                    "paired_vs_S01_bootstrap_full": f.get("paired_vs_S01_full_bootstrap"),
                    "test_bootstrap": f.get("test_bootstrap"),
                },
                "recommendation_status": status,
                "tradeoff_note": tradeoff,
                "expectancy_gap_vs_S01": {"test": mean_gap_test, "validation": mean_gap_val},
            })
    return {"generated_note": "백테스트 결과만으로 실거래 자동매도를 활성화하지 않는다. "
                              "기존 보유종목은 별도 REVIEW 정책을 유지하며 소급 적용하지 않는다.",
            "data_quality": "RECONSTRUCTED (현재 스캐너 로직 replay). VERIFIED_HISTORICAL 0건.",
            "rules": rules}
