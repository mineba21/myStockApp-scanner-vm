"""골든 테스트: 자동매수 SHADOW 적격 판정(trading.auto_buy.shadow.eligibility)이 P7 백테스트 B01 정의
(HARD_EXCL+NO_CHASE, M1 BULL)와 백테스트 후보 전체에서 같은 판정을 내는지. 로컬 산출물이 없으면 건너뛴다."""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backtest.entry_strategy.src.data import WARN_PATTERNS
from trading.auto_buy.shadow import eligibility as EL

PKG = Path(__file__).resolve().parents[1]
CAND = PKG / "output" / "candidates.csv"
REGIME = PKG / "data" / "regime.pkl"


@pytest.mark.skipif(not (CAND.exists() and REGIME.exists()), reason="백테스트 산출물 없음")
def test_shadow_b01_matches_p7_b01_on_all_backtest_candidates():
    c = pd.read_csv(CAND)
    m1 = pd.read_pickle(REGIME)["market_condition"].to_numpy()[c["decision_ci"].to_numpy()] == "BULL"
    ext = c["ext_pct"]
    p7 = (c["first_seen"] & (ext.isna() | (ext >= 0)) & (c["stop_pct"] <= 15) & ~c["w_chase"]).to_numpy() & m1

    def snap(r):
        warns = [WARN_PATTERNS[k] for k in WARN_PATTERNS if r[k]]
        return {"scan_status": "DONE", "intraday": 0, "strict_passed": 1, "first_strict_pass": 1,
                "is_first_detection": int(r["first_seen"]), "decision_price": r["close"], "stop_loss": r["stop"],
                "stop_pct": r["stop_pct"], "ext_pct": None if pd.isna(r["ext_pct"]) else r["ext_pct"],
                "market_condition": r["market_condition"], "signal_type": r["signal_type"],
                "entry_warnings": json.dumps(warns, ensure_ascii=False)}

    shadow = np.array([EL.evaluate(snap(r))["B01"][0] for _, r in c.iterrows()])
    assert len(c) > 2000
    assert (shadow == p7).all(), int((shadow != p7).sum())
    b00 = np.array([EL.evaluate(snap(r))["B00"][0] for _, r in c.iterrows()])
    assert b00.all()                                       # 백테스트 후보 = 전원 B00 후보
