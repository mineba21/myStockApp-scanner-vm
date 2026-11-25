"""Entry strategy backtest — 체결·S01 경로·포트폴리오·미래 정보 차단 테스트 (합성 데이터)."""
import math

import numpy as np
import pandas as pd
import pytest

from backtest.entry_strategy.src import fills as FL
from backtest.entry_strategy.src import portfolio as PF
from backtest.entry_strategy.src import strategies as ST
from backtest.entry_strategy.src import metrics as MT

COSTS = {"buy_commission": 0.0025, "sell_commission": 0.0025, "sell_tax": 0.0, "slippage_bp": 0}


def ta_from(rows, start="2024-01-01"):
    idx = pd.bdate_range(start, periods=len(rows))
    o, h, l, c = (np.array([r[i] for r in rows], float) for i in range(4))
    return FL.TickerArrays(o, h, l, c, np.ones(len(rows), bool), np.arange(len(rows)), idx)


class Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


# ── 매수 체결 ──────────────────────────────────────────────────────

def test_limit_fill_open_intraday_unfilled_and_open_below_stop():
    stop = 90.0
    # 시가 ≤ 지정가 → 시가 체결
    assert FL._try_fill(ta_from([(100, 101, 99, 100)]), 0, 101.0, stop, 0.03)[:2] == ("FILLED", 100.0)
    # 시가 > 지정가, 저가 ≤ 지정가 → 지정가 체결
    st, px, kind = FL._try_fill(ta_from([(103, 104, 100.5, 102)]), 0, 101.0, stop, 0.03)
    assert (st, px, kind) == ("FILLED", 101.0, "limit")
    # 저가 > 지정가 → 미체결 (추격 없음)
    assert FL._try_fill(ta_from([(103, 105, 102, 104)]), 0, 101.0, stop, 0.03)[0] == "UNFILLED"
    # 시가 ≤ 손절가 → 전송 안 함
    assert FL._try_fill(ta_from([(89, 95, 88, 94)]), 0, 101.0, stop, 0.03)[0] == "SKIP_OPEN_STOP"
    # R < 3% → 전송 안 함
    assert FL._try_fill(ta_from([(92, 95, 91, 94)]), 0, 101.0, stop, 0.03)[0] == "SKIP_OPEN_STOP"


def test_plan_order_never_fills_on_decision_bar():
    ta = ta_from([(100, 102, 98, 100), (100.5, 101, 99, 100), (100, 101, 99, 100)])
    row = Row(stop=90.0, ticker_pos=0, ext_pct=1.0, stop_pct=10.0, signal_type="REBOUND",
              pivot=math.nan, signal_date=ta.index[0])
    o = FL.plan_order(row, ta, {"kind": "limit_next_day", "limit_pct": 0.01}, 0.03)
    assert o["fill_pos"] == 1 and o["planned_px"] == pytest.approx(101.0)


def test_pullback_waits_and_caps_limit():
    ta = ta_from([(100, 101, 99, 100), (104, 106, 103, 105), (103, 104, 100.5, 101), (101, 102, 100, 101)])
    row = Row(stop=90.0, ticker_pos=0, ext_pct=2.0, stop_pct=10.0, signal_type="RE_BREAKOUT",
              pivot=98.0, signal_date=ta.index[0])
    o = FL.plan_order(row, ta, {"kind": "pullback", "limit_pct": 0.01, "days": 5}, 0.03)
    assert o["planned_px"] == pytest.approx(98.98)          # min(피벗+1%, 종가+1%)
    assert o["status"] == "UNFILLED" or o["fill_pos"] >= 2


# ── S01 경로 ─────────────────────────────────────────────────────

def test_s01_gap_stop_and_horizon():
    rows = [(100, 101, 99, 100), (100, 102, 99, 101), (85, 86, 80, 82)]
    e = FL.s01_exit(ta_from(rows), 0, 90.0, None)
    assert (e["reason"], e["exit_px"], e["exit_pos"]) == ("STOP_GAP", 85.0, 2)
    rows = [(100, 101, 99, 100), (100, 102, 89, 95)]
    e = FL.s01_exit(ta_from(rows), 0, 90.0, None)
    assert (e["reason"], e["exit_px"]) == ("STOP", 90.0)
    rows = [(100, 101, 99, 100)] * 5
    e = FL.s01_exit(ta_from(rows), 0, 90.0, 3)
    assert e["reason"] == "HORIZON" and e["exit_pos"] == 2
    e = FL.s01_exit(ta_from(rows), 0, 90.0, 10)
    assert e["reason"] == "OPEN"                            # 데이터 끝 → 미청산 평가


def test_s01_day0_scenarios():
    rows = [(100, 101, 89, 95), (95, 96, 94, 95)]
    assert FL.s01_exit(ta_from(rows), 0, 90.0, None, "conservative")["reason"] == "STOP"
    e = FL.s01_exit(ta_from(rows), 0, 90.0, None, "next_day")
    assert e["reason"] == "OPEN" and e["day0"] is True


def test_s01_truncation_does_not_read_future():
    rows = [(100, 101, 99, 100)] * 4 + [(50, 50, 10, 20)]   # 절단 이후 폭락
    e = FL.s01_exit(ta_from(rows), 0, 90.0, None, end_pos=3)
    assert e["reason"] == "OPEN" and e["mark_px"] == 100


def test_trade_outcome_R():
    out = FL.trade_outcome(100.0, 90.0, 110.0, COSTS)
    assert out["R"] == pytest.approx((110 * 0.9975 - 100 * 1.0025) / 10)


# ── 포트폴리오 ───────────────────────────────────────────────────

def _si(n_days=30):
    # 후보 3개: 같은 날 결정, 두 개는 같은 종목
    tick = np.array(["AAA", "AAA", "BBB"])
    return PF.SimInput(
        ticker=tick, stop=np.array([90.0, 90.0, 45.0]), decision_ci=np.array([1, 1, 1]),
        planned_px=np.array([101.0, 101.0, 50.5]), status_filled=np.array([True, True, True]),
        fill_ci=np.array([2, 2, 2]), fill_px=np.array([100.0, 100.0, 50.0]), expire_ci=np.array([2, 2, 2]),
        exit_ci=np.array([10, 10, -1]), exit_px=np.array([90.0, 90.0, np.nan]),
        exit_reason=np.array(["STOP", "STOP", ""]), placeable=np.array([True, True, True]),
        skip_open=np.array([False, False, False])), {
        "AAA": np.full(n_days, 100.0), "BBB": np.full(n_days, 50.0)}


def test_portfolio_no_duplicate_ticker_and_topn():
    si, closes = _si()
    prof = PF.Profile(equity=50000, RISK_PCT=0.5, MAX_POSITION_PCT=10, MAX_TOTAL_HEAT_PCT=2)
    res = PF.run(si, closes, 0, 29, np.ones(3, bool), np.array([0, 1, 2.0]), 999, prof, COSTS)
    tickers = [t["ticker"] for t in res["trades"] + res["open"]]
    assert sorted(tickers) == ["AAA", "BBB"] and res["counts"]["skip_dup"] == 1
    res1 = PF.run(si, closes, 0, 29, np.ones(3, bool), np.array([0, 1, 2.0]), 1, prof, COSTS)
    assert len(res1["trades"] + res1["open"]) == 1


def test_portfolio_integer_qty_and_risk_limits():
    si, closes = _si()
    prof = PF.Profile(equity=50000, RISK_PCT=0.5, MAX_POSITION_PCT=10, MAX_TOTAL_HEAT_PCT=2)
    res = PF.run(si, closes, 0, 29, np.ones(3, bool), np.array([0, 1, 2.0]), 999, prof, COSTS)
    for t in res["trades"] + res["open"]:
        assert float(t["qty"]).is_integer()
        planned = 101.0 if t["ticker"] == "AAA" else 50.5
        assert t["qty"] * (planned - t["stop"]) <= 50000 * 0.005 + 1e-6      # 종목 위험 0.5%
        assert t["qty"] * planned <= 50000 * 0.10 + 1e-6                     # 비중 10%


def test_portfolio_segment_end_marks_open_without_future_exit():
    si, closes = _si()
    prof = PF.Profile(equity=50000, RISK_PCT=0.5, MAX_POSITION_PCT=10, MAX_TOTAL_HEAT_PCT=2)
    res = PF.run(si, closes, 0, 5, np.ones(3, bool), np.array([0, 1, 2.0]), 999, prof, COSTS)
    assert not res["trades"] and all(o["open"] for o in res["open"])          # 손절(10일) 은 구간 밖


def test_heat_blocks_new_entries():
    si, closes = _si()
    prof = PF.Profile(equity=50000, RISK_PCT=0.5, MAX_POSITION_PCT=10, MAX_TOTAL_HEAT_PCT=0.6)
    res = PF.run(si, closes, 0, 29, np.ones(3, bool), np.array([0, 1, 2.0]), 999, prof, COSTS)
    risk = sum(t["qty"] * ((101.0 if t["ticker"] == "AAA" else 50.5) - t["stop"]) for t in res["trades"] + res["open"])
    assert risk <= 50000 * 0.006 + 1e-6


# ── 점수·지표 ───────────────────────────────────────────────────

def test_score_uses_reference_percentiles():
    f = pd.DataFrame({"rs_value": [1.0, 5.0, 10.0], "dec_ext_pct": [1.0, 2.0, 3.0], "dec_stop_pct": [8, 10, 12.0]})
    ref = {"rs_value": np.array([1.0, 5.0, 10.0]), "ext_pct": np.array([1.0, 2.0, 3.0])}
    s = ST.score(f, {"rs_value": 1.0, "ext_pct": -1.0}, ref)
    assert s[2] == pytest.approx((1.0 + 0.0) / 2) and s[0] == pytest.approx((1 / 3 + 2 / 3) / 2)


def test_max_consecutive_stops():
    t = pd.DataFrame({"exit_ci": [1, 2, 3, 4, 5], "entry_ci": [0] * 5, "open": [False] * 5,
                      "reason": ["STOP", "STOP_GAP", "HORIZON", "STOP", "STOP"]})
    assert MT.max_consecutive_stops(t) == 2


# ── 캐시 기반 (있을 때만) ────────────────────────────────────────

def _cache_available():
    from backtest.entry_strategy.src import data as D
    return (D.REPO_ROOT / D.load_config()["data"]["ohlcv_pkl"]).exists()


@pytest.mark.skipif(not _cache_available(), reason="exit_strategy OHLCV 캐시 없음")
def test_candidate_features_ignore_future_bars():
    from backtest.entry_strategy.src import data as D
    cfg = D.load_config()
    D.isolate()
    import pickle
    with open(D.REPO_ROOT / cfg["data"]["ohlcv_pkl"], "rb") as fh:
        frames = pickle.load(fh)["frames"]
    sig = D.load_signals(cfg)
    sig = sig[sig["strict_filter_passed"] == True].head(40)    # noqa: E712
    cal = frames["SPY"].index
    mkt = D.Market(frames=frames, calendar=cal, closes={}, regime=pd.DataFrame(index=cal))
    a = D.build_candidates(sig, mkt)
    # 탐지일 이후 봉을 모두 변조해도 특징이 같아야 한다
    cut = sig["detect_date"].min()
    frames2 = {t: df.copy() for t, df in frames.items()}
    for t in sig["ticker"].unique():
        df = frames2[t]
        for r in sig[sig["ticker"] == t].itertuples():
            m = df.index > r.detect_date
            df.loc[m, ["Open", "High", "Low", "Close"]] *= 3.0
            break
        frames2[t] = df
    one = sig.drop_duplicates("ticker")
    b = D.build_candidates(one, D.Market(frames=frames2, calendar=cal, closes={}, regime=pd.DataFrame(index=cal)))
    a1 = D.build_candidates(one, mkt)
    cols = ["close", "ext_pct", "stop_pct", "slope150", "atr_pct", "dist_52w_high", "ma150_ext_pct"]
    pd.testing.assert_frame_equal(a1[cols].reset_index(drop=True), b[cols].reset_index(drop=True))
