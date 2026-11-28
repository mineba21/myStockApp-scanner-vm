"""P7 운영 정책 비교판(P7-OPS, 독립 구현) ↔ SHADOW 엔진 대조.

1) 합성 데이터: 거래량 0·거래량 NaN·행 없음·OHLC 불일치 봉을 넣고 23개 P7C 계좌 전체가 같은지 본다.
2) 실제 재구성 데이터(1,480거래일): 로컬 캐시가 있을 때만 실행한다 (없으면 건너뜀, 약 2~3분).
기존 P7(risk_portfolio) 결과·파일은 바꾸지 않는다. 운영 DB·VM·주문 경로와 연결되지 않는다.
"""
import tempfile
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from backtest.entry_strategy.src import ops_parity as OP
from backtest.entry_strategy.src import p7_ops as OPS
from trading.auto_buy import settings as S
from trading.auto_buy.settings import AutoBuySettings
from trading.auto_buy.shadow import calendar as CAL

PKG = Path(__file__).resolve().parents[1]
REPO = PKG.parents[1]
GRADES = [("S", "STRONG"), ("A", "STRONG"), ("A", "MODERATE"), ("B", "WEAK")]


def _costs():
    return dict(yaml.safe_load((PKG / "configs" / "entry_backtest.yaml").read_text())["costs"])


def _profile():
    cfg = yaml.safe_load((PKG / "configs" / "entry_backtest.yaml").read_text())["p7"]
    p = {**cfg["profiles"]["BAL"], **cfg["common"]}
    return OPS.Profile(initial_cash=float(cfg["equity"]), RISK_PCT=p["RISK_PCT"],
                       MAX_TOTAL_HEAT_PCT=p["MAX_TOTAL_HEAT_PCT"], MAX_POSITION_PCT=p["MAX_POSITION_PCT"],
                       min_cash_pct=p["min_cash_pct"])


def _synthetic(seed):
    rng = np.random.default_rng(seed)
    days = [d.isoformat() for d in CAL.trading_days_between(date(2026, 5, 1), date(2026, 11, 20))]
    n = len(days)
    cal = pd.DatetimeIndex(days)
    tickers = [f"T{i:02d}" for i in range(10)]
    frames = {}
    for t in tickers:
        c = [float(rng.integers(40, 120))]
        for _ in range(n - 1):
            c.append(max(5.0, round(c[-1] * (1 + rng.normal(0.002, 0.03)), 2)))
        c = np.array(c)
        o = np.round(np.r_[c[0], c[:-1]] * (1 + rng.normal(0, 0.012, n)), 2)
        gap = rng.random(n) < 0.04
        o[gap] = np.round(o[gap] * 0.88, 2)
        h = np.maximum(np.round(np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.01, n))), 2), np.maximum(o, c))
        lo = np.minimum(np.round(np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.015, n))), 2), np.minimum(o, c))
        v = rng.integers(1_000, 1_000_000, n).astype(float)
        frames[t] = pd.DataFrame({"Open": o, "High": h, "Low": lo, "Close": c, "Volume": v}, index=cal)
    # 거래 정지형 봉: 거래량 0, O=H=L=C=전일 종가 또는 손절선 아래 가격
    for t in ("T01", "T03", "T06"):
        for k in rng.choice(np.arange(10, n - 1), size=12, replace=False):
            px = frames[t]["Close"].iloc[k - 1] * (0.85 if rng.random() < 0.4 else 1.0)
            frames[t].iloc[k, :4] = round(px, 2)
            frames[t].iloc[k, 4] = 0.0
    frames["T05"].iloc[int(n * 0.55), 4] = np.nan                     # 거래량 미확인
    frames["T07"] = frames["T07"].drop(cal[int(n * 0.6)])            # 행 없음
    k = int(n * 0.7)
    frames["T08"].iloc[k, 1] = frames["T08"].iloc[k, 2] * 0.98       # 고가 < 저가 (비정상)
    cands = []
    for i in range(5, n):
        for t in rng.choice(tickers, size=int(rng.integers(0, 4)), replace=False):
            if cal[i] not in frames[t].index:
                continue
            close = float(frames[t].loc[cal[i], "Close"])
            stop = round(close * (1 - rng.uniform(0.04, 0.14)), 2)
            g, q = GRADES[int(rng.integers(0, len(GRADES)))]
            first, chase = bool(rng.random() > 0.15), bool(rng.random() < 0.1)
            stop_pct = (1 - stop / close) * 100
            cands.append({"cid": len(cands), "signal_key": f"US:{t}:BREAKOUT:{days[i]}", "ticker": str(t), "day_i": i,
                          "day": days[i], "close": close, "stop": stop, "grade": g, "quality": q,
                          "signal_type": "BREAKOUT", "first_seen": first, "ext_pct": 1.0, "stop_pct": stop_pct,
                          "market_condition": "BULL", "warnings": ["추격 주의"] if chase else [], "sector": "Tech",
                          "B00": True, "B01": first and not chase and stop_pct <= 15})
    # 일부 후보의 다음 정규장을 거래량 0 으로 (D+1 미체결 만료 시나리오를 확실히 만든다. 판단일 봉은 그대로)
    for c in cands:
        i = c["day_i"] + 1
        if c["ticker"] in ("T02", "T04") and i < n and rng.random() < 0.6 and cal[i] in frames[c["ticker"]].index:
            frames[c["ticker"]].loc[cal[i], ["Open", "High", "Low", "Close"]] = c["close"]
            frames[c["ticker"]].loc[cal[i], "Volume"] = 0.0
    for c in cands:                                                   # 판단가는 판단일 종가 (위 수정 후 다시 맞춤)
        c["close"] = float(frames[c["ticker"]].loc[cal[c["day_i"]], "Close"])
        c["stop_pct"] = (1 - c["stop"] / c["close"]) * 100
        c["B01"] = c["first_seen"] and not c["warnings"] and c["stop_pct"] <= 15 and c["stop"] < c["close"]
        c["B00"] = c["stop"] < c["close"]
    settings = AutoBuySettings(mode="SHADOW", requested_mode="SHADOW", journal_path="")
    nxt = CAL.next_trading_day(date.fromisoformat(days[-1]), settings)
    next_session = {days[i]: days[i + 1] if i + 1 < n else nxt.isoformat() for i in range(n)}
    return {"cal": cal, "cal_days": days, "start": 0, "end": n - 1, "cands": cands, "settings": settings,
            "next_session": next_session, "book": OPS.BarBook(frames, cal), "frames": frames}


@pytest.mark.parametrize("seed", [3, 17])
def test_shadow_engine_matches_ops_policy_with_zero_volume_and_gaps(tmp_path, seed):
    u = _synthetic(seed)
    sh = OP.run_shadow(u, str(tmp_path / "s.sqlite3"), replay=True)
    assert sh["replay_identical"]                      # 운영 재계산 경로(동결 입력 검증·CURRENT 전환 전 검증) 통과·같은 결과
    accts = OPS.p7c_accounts(S.B02_SEEDS)
    OPS.run(u["cands"], u["book"], u["cal_days"], u["start"], u["end"], accts, _profile(), _costs(),
            u["next_session"])
    rep = OP.compare(sh, {a.name: a for a in accts}, u["cal_days"])
    bad = {k: v for k, v in rep["accounts"].items() if not v["match"]}
    assert not bad, {k: (v["mismatches"][:2], v["decisions_shadow"], v["decisions_ops"]) for k, v in bad.items()}
    assert sum(v["trades_shadow"] for v in rep["accounts"].values()) > 100
    # 시나리오가 실제로 일어났는지 (양쪽이 같은 결과)
    assert sum(v["zero_volume_expired"] for v in rep["accounts"].values()) > 0
    assert sum(1 for a in accts for t in list(a.trades) + list(a.positions.values()) if t["zero_days"]) > 0
    assert sum(v["incomplete_days_shadow"] for v in rep["accounts"].values()) > 0


def test_ops_zero_volume_rules_unit():
    """D+1 거래량 0 → 만료 (D+2 에 체결하지 않음), 보유 중 거래량 0 → 손절 없음, 재개 첫 봉 갭 손절."""
    days = [d.isoformat() for d in CAL.trading_days_between(date(2026, 11, 2), date(2026, 11, 13))]
    cal = pd.DatetimeIndex(days)
    def fr(rows):
        return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=cal)
    a = fr([[100, 101, 99, 100, 1e5], [100, 100, 100, 100, 0], [99, 100, 98, 99.5, 1e5]] + [[99, 100, 98, 99, 1e5]] * 7)
    b = fr([[50, 51, 49, 50, 1e5], [50.2, 51, 49.8, 50.5, 1e5], [46, 46, 46, 46, 0], [44, 45, 43, 44, 1e5]]
           + [[44, 45, 43, 44, 1e5]] * 6)
    cands = [{"cid": 0, "signal_key": "A", "ticker": "AAA", "day_i": 0, "day": days[0], "close": 100.0, "stop": 92.0,
              "grade": "A", "quality": "STRONG", "B00": True, "B01": True},
             {"cid": 1, "signal_key": "B", "ticker": "BBB", "day_i": 0, "day": days[0], "close": 50.0, "stop": 46.5,
              "grade": "A", "quality": "STRONG", "B00": True, "B01": True}]
    acc = OPS.Account("B00", "B00", "GRADE", None)
    nxt = {d: (days[i + 1] if i + 1 < len(days) else None) for i, d in enumerate(days)}
    OPS.run(cands, OPS.BarBook({"AAA": a, "BBB": b}, cal), days, 0, len(days) - 1, [acc], _profile(), _costs(), nxt)
    oa = next(o for o in acc.orders if o["ticker"] == "AAA")
    assert oa["state"] == "UNFILLED" and oa["reason"] == "ZERO_VOLUME_EXPIRED"
    assert len([o for o in acc.orders if o["ticker"] == "AAA"]) == 1          # 이월·재주문 없음
    tb = acc.trades[0]
    assert tb["ticker"] == "BBB" and tb["zero_days"] == 1 and tb["reason"] == "STOP_GAP"
    assert days[tb["exit_i"]] == days[3] and tb["exit_px"] == 44           # 거래량 0 날(46<손절) 이 아니라 재개일 시가
    assert OPS.limit_floor_cent(100.005) == 101.0


REAL = all((REPO / p).exists() for p in ("backtest/exit_strategy/data/cache/ohlcv_US.pkl",
                                         "backtest/exit_strategy/data/cache/reconstructed_US.pkl"))


@pytest.mark.skipif(not REAL, reason="재구성 데이터 캐시 없음 (로컬 전용)")
def test_real_reconstructed_data_all_23_accounts_match():
    from backtest.entry_strategy.src import data as D
    from backtest.entry_strategy.src import pipeline as P
    from backtest.entry_strategy.src.pipeline import C
    P.prepare(D.load_config())
    u = OP.build_universe()
    assert u["end"] - u["start"] + 1 == 1480 and u["extra_closed"] == ["2025-01-09"] and not u["extra_open"]
    with tempfile.TemporaryDirectory() as tmp:
        sh = OP.run_shadow(u, str(Path(tmp) / "s.sqlite3"), replay=True)     # 동결 경로 + replay 경로 모두 제품 검증 통과
        assert sh["replay_identical"]
        integ = OP.journal_integrity(str(Path(tmp) / "s.sqlite3"))
    assert integ["frozen_days"] == 1480 and integ["issues"] == 0
    assert integ["valid_row_sha"] == integ["snapshots"] > 0 and integ["count_consistent"] == integ["captures"]
    ops = OP.run_ops(u)
    rep = OP.compare(sh, ops, u["cal_days"])
    assert len(rep["accounts"]) == 23
    bad = {k: v["mismatches"][:2] for k, v in rep["accounts"].items() if not v["match"]}
    assert not bad, bad
    for v in rep["accounts"].values():                                     # 거래·수량·체결가·손절·현금·Heat·평가금액
        assert v["max_rel_diff_equity"] <= 1e-12 and v["max_rel_diff_cash"] <= 1e-12
        assert v["max_abs_diff_entry_heat"] <= 1e-12 and v["max_abs_diff_current_heat"] <= 1e-12
    # SW 2021-04-16 판단 → 04-19 거래량 0: 기존 P7 은 04-20 체결, 운영 정책은 미체결 만료
    audit = OPS.zero_volume_skip_audit(C.cand, C.orders["T1_100"], u["book"])
    sw = next(r for r in audit["rows"] if r["signal_key"] == "US:SW:RE_BREAKOUT:2021-04-15")
    assert (sw["d1"], sw["d1_state"], sw["p7_fill_day"]) == ("2021-04-19", "ZERO", "2021-04-20")
    sw_orders = [o for a in ops.values() for o in a.orders if o["signal_key"] == sw["signal_key"]]
    assert sw_orders and all(o["state"] == "UNFILLED" and o["reason"] == "ZERO_VOLUME_EXPIRED" for o in sw_orders)
    assert not [t for name in ops for t in sh[name]["trades"]
                if t["ticker"] == "SW" and t["entry_day"] == "2021-04-20"]
    cmp_ = OP.vs_p7_original(ops)
    assert cmp_["verdict_maintained"] == "NOT_RECOMMENDED" and cmp_["b01_status"] == "NEED_MORE_DATA"


def test_cache_zero_is_unknown_outside_explicit_backtest_assumption():
    """재구성 캐시(변환된 DataFrame) 거래량 0 은 원본 확인 출처가 아니다. 정합성 비교 하네스 안에서만 명시적으로 확정 0
    으로 가정하고(보고서에 기록), 그 밖(운영 SHADOW 경로)에서는 UNKNOWN 이다."""
    from trading.auto_buy.shadow import vfills as VF
    bar = VF.Bar(10.0, 10.5, 9.8, 10.2, volume=0.0, volume_source=OP.CACHE_SOURCE)
    assert bar.volume_state == VF.VOLUME_UNKNOWN and not VF.zero_confirmable(OP.CACHE_SOURCE)
    with VF.assume_confirmed_zero(OP.CACHE_SOURCE):
        assert bar.volume_state == VF.VOLUME_ZERO
        assert VF.volume_state(0.0, VF.SOURCE_YF_DATAFRAME) == VF.VOLUME_UNKNOWN      # 다른 출처는 그대로
    assert bar.volume_state == VF.VOLUME_UNKNOWN                                       # 컨텍스트 종료 후 복원
    assert VF.volume_state(0.0, VF.SOURCE_YAHOO_CHART_RAW) == VF.VOLUME_ZERO
    assert "확인 불가" in OP.CACHE_ZERO_ASSUMPTION and "성과 검증 아님" in OP.CACHE_ZERO_ASSUMPTION
