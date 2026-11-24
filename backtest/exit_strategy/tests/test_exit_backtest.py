"""Exit backtest 필수 테스트 (합성 데이터 — 외부 호출·운영 DB 없음)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.exit_strategy.src.entry_builder import build_trade, mark_overlaps, upthrust_decision
from backtest.exit_strategy.src.execution_simulator import Costs, TradeData, simulate
from backtest.exit_strategy.src.exit_rules import (BreakoutFailure, ExitConfig, HardStop, Partial,
                                                   TimeStop, Trailing, TrendExit, catalog, hybrid_configs)
from backtest.exit_strategy.src.indicators import compute_indicators
from backtest.exit_strategy.src.parameter_search import chronological_split, purge_overlap
from backtest.exit_strategy.src.portfolio_simulator import run_portfolio

ZERO = Costs(0.0, 0.0, 0.0)


def td_from(bars, signal_type="BREAKOUT", pivot=100.0, stop=90.0, market="US", **kw):
    """bars: [(o,h,l,c)] or [(o,h,l,c,v)]."""
    n = len(bars)
    o = [b[0] for b in bars]; h = [b[1] for b in bars]; l = [b[2] for b in bars]; c = [b[3] for b in bars]
    v = [b[4] if len(b) > 4 else 1000.0 for b in bars]
    base = dict(dates=[f"d{i}" for i in range(n)], o=o, h=h, l=l, c=c, v=v,
                tradable=[x > 0 for x in o], prev_close=[0.0] + c[:-1],
                ma10=[0.0] * n, ma21=[0.0] * n, ma50=[0.0] * n, ma150=[0.0] * n,
                atr=[2.0] * n, volma=[1000.0] * n, rs_weak=[False] * n, stage_w=[None] * n,
                week_complete=[False] * n, signal_type=signal_type, pivot=pivot, scanner_stop=stop,
                signal_ma150=95.0, atr_at_signal=2.0, market=market)
    base.update(kw)
    return TradeData(**base)


def flat(n, px=101.0):
    return [(px, px + 0.5, px - 0.5, px)] * n


# ── 매수 시점 / 미래 정보 ───────────────────────────────────────

def _frame(n=400, start="2024-01-01", seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=n)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.01, n)))
    df = pd.DataFrame({"Open": close * 0.999, "High": close * 1.01, "Low": close * 0.99,
                       "Close": close, "Volume": 1e6}, index=idx)
    return df


def test_no_entry_before_signal_confirmation():
    df = _frame()
    ind = compute_indicators(df)
    detect = df.index[300]
    row = pd.Series({"detect_date": detect, "signal_type": "REBOUND", "signal_date": str(df.index[298].date()),
                     "stop_loss": 90.0, "pivot_price": None, "ma150": 95.0, "market": "US"})
    td, meta = build_trade(row, df, ind, 60)
    assert pd.Timestamp(td.dates[0]) > detect
    assert td.dates[0] == df.index[301].strftime("%Y-%m-%d")
    assert td.o[0] == pytest.approx(df["Open"].iloc[301])


def test_indicators_do_not_use_future_bars():
    df = _frame()
    full = compute_indicators(df)
    cut = compute_indicators(df.iloc[:250])
    for col in ("ma10", "ma21", "ma50", "ma150", "atr14", "vol_ma20_prev", "ma30w"):
        a, b = full[col].iloc[249], cut[col].iloc[249]
        assert (np.isnan(a) and np.isnan(b)) or a == pytest.approx(b), col


def test_weekly_value_appears_only_after_week_completes():
    df = _frame(400)
    ind = compute_indicators(df)
    # 주 중간(월~목) 값은 직전 완료 주의 값과 같아야 한다
    for i in range(300, 340):
        d = df.index[i]
        if d.dayofweek < 4 and not ind["week_complete"].iloc[i]:
            assert ind["ma30w"].iloc[i] == ind["ma30w"].iloc[i - 1] or np.isnan(ind["ma30w"].iloc[i])


def test_duplicate_signal_overlap_flag():
    e = pd.DataFrame({"market": ["US"] * 3, "ticker": ["A"] * 3,
                      "entry_date": ["2024-01-02", "2024-01-10", "2024-06-01"],
                      "entry_idx": [0, 6, 100]})
    flags = mark_overlaps(e, 60)
    assert list(flags) == [False, True, False]


# ── Hard Stop / Gap ─────────────────────────────────────────────

def test_hard_stop_fills_at_stop_price():
    bars = [(101, 102, 100, 101)] + [(100, 101, 89, 92)] + flat(10)
    r = simulate(td_from(bars, stop=90.0), ExitConfig("t", "t", hard=HardStop("scanner")), 60, ZERO)
    assert r["exit_reason"] == "HARD_STOP"
    assert r["fills"][0]["price"] == pytest.approx(90.0)
    assert r["fills"][0]["t"] == 1


def test_gap_down_fills_at_open():
    bars = [(101, 102, 100, 101)] + [(85, 86, 80, 84)] + flat(10)
    r = simulate(td_from(bars, stop=90.0), ExitConfig("t", "t", hard=HardStop("scanner")), 60, ZERO)
    assert r["fills"][0]["price"] == pytest.approx(85.0)
    assert r["fills"][0]["gap"] is True
    assert r["gap_excess"] == pytest.approx(5 / 101)


def test_abnormal_entry_classified():
    bars = [(89, 90, 88, 89)] + flat(5)
    r = simulate(td_from(bars, stop=90.0), ExitConfig("t", "t", hard=HardStop("scanner")), 60, ZERO)
    assert r["abnormal_entry"] is True


# ── 종가 기반 신호 → 다음 거래일 시가 ─────────────────────────────

def test_close_signal_executes_next_open():
    bars = [(101, 102, 100, 101), (100, 100.5, 98, 98.5), (97, 99, 96, 98)] + flat(5)
    cfg = ExitConfig("t", "t", bf=BreakoutFailure("pivot", 0.0, 1))
    r = simulate(td_from(bars, pivot=100.0, stop=None), cfg, 60, ZERO)
    assert r["exit_reason"] == "BF_PIVOT"
    assert r["fills"][0]["t"] == 2
    assert r["fills"][0]["price"] == pytest.approx(97.0)     # 종가 98.5 가 아니라 다음날 시가


def test_breakout_failure_not_applied_to_rebound():
    bars = [(101, 102, 100, 101), (100, 100.5, 98, 98.5)] + flat(5, 98)
    cfg = ExitConfig("t", "t", bf=BreakoutFailure("pivot", 0.0, 1))
    r = simulate(td_from(bars, signal_type="REBOUND", pivot=100.0, stop=None), cfg, 60, ZERO)
    assert r["exit_reason"] == "OPEN_AT_HORIZON"


def test_consecutive_and_volume_confirmation():
    bars = [(101, 102, 100, 101, 1000), (100, 100, 98, 99, 1000), (99, 100, 98, 99.5, 2000), (99, 99, 97, 98)] + flat(3)
    cfg2 = ExitConfig("t", "t", bf=BreakoutFailure("pivot", 0.0, 2))
    r = simulate(td_from(bars, stop=None), cfg2, 60, ZERO)
    assert r["fills"][0]["t"] == 3                           # 2일째 연속 이탈 종가(t=2) → t=3 시가
    cfgv = ExitConfig("t", "t", bf=BreakoutFailure("pivot", 0.0, 1, 1.5))
    r = simulate(td_from(bars, stop=None), cfgv, 60, ZERO)
    assert r["fills"][0]["t"] == 3                           # t=1 거래량 1.0x 불충분, t=2 2.0x


# ── Trailing / Partial ──────────────────────────────────────────

def test_protection_activation_persists_and_is_next_day():
    # t0 매수 100 → t1 고가 112 (활성화는 t2부터) → t1 저가 104 는 trail 미적용
    bars = [(100, 101, 99, 100), (105, 112, 104, 106), (106, 107, 103, 104), (104, 105, 100, 101)] + flat(5, 101)
    cfg = ExitConfig("t", "t", trail=Trailing(0.10, 0.06))
    r = simulate(td_from(bars, stop=None, pivot=None), cfg, 60, ZERO)
    assert r["fills"][0]["t"] == 2                           # 112*0.94=105.28, t2 저가 103
    assert r["fills"][0]["price"] == pytest.approx(112 * 0.94)


def test_trailing_not_deactivated_when_profit_falls_back():
    bars = [(100, 101, 99, 100), (105, 111, 104, 109), (108, 109, 106, 107)] + [(107, 108, 106.5, 107)] * 3 + \
           [(107, 107.5, 104, 104.5)]
    cfg = ExitConfig("t", "t", trail=Trailing(0.10, 0.05))
    r = simulate(td_from(bars, stop=None, pivot=None), cfg, 60, ZERO)
    assert r["exit_reason"] == "TRAIL_STOP"                  # 수익률이 +10% 아래로 내려와도 유지
    assert r["fills"][0]["price"] == pytest.approx(111 * 0.95)


def test_partial_sells_once_and_remaining_quantity():
    bars = [(100, 101, 99, 100), (105, 112, 104, 111), (111, 111, 104.5, 106), (106, 115, 105, 114),
            (114, 120, 113, 119), (119, 119, 107, 108)] + flat(3, 108)
    cfg = ExitConfig("t", "t", partial=Partial("trail", 0.10, 0.06, 0.5, "trail_pct", 0.10))
    r = simulate(td_from(bars, stop=None, pivot=None), cfg, 60, ZERO)
    partials = [f for f in r["fills"] if f["reason"] == "PARTIAL"]
    assert len(partials) == 1
    assert partials[0]["portion"] == pytest.approx(0.5)
    assert partials[0]["price"] == pytest.approx(112 * 0.94)
    remain = [f for f in r["fills"] if f["reason"] == "REMAIN_TRAIL"]
    assert remain and remain[0]["portion"] == pytest.approx(0.5)
    assert remain[0]["price"] == pytest.approx(120 * 0.90)
    assert r["open_qty"] == 0


def test_partial_breakeven_never_lowers_stop():
    bars = [(100, 101, 99, 100), (105, 112, 104, 111), (111, 111, 104, 105), (105, 106, 99, 100)] + flat(2, 100)
    cfg = ExitConfig("t", "t", hard=HardStop("fixed", pct=0.05),
                     partial=Partial("trail", 0.10, 0.06, 0.5, "breakeven"))
    r = simulate(td_from(bars, stop=None, pivot=None), cfg, 60, ZERO)
    reasons = [f["reason"] for f in r["fills"]]
    assert reasons == ["PARTIAL", "HARD_STOP"]
    assert r["fills"][1]["price"] == pytest.approx(100.0)    # 손절선이 95 → 본전 100 으로 상향


def test_same_bar_target_and_stop_ambiguity():
    bars = [(100, 101, 99, 100), (100, 111, 89, 95)] + flat(3, 95)
    cfg = ExitConfig("t", "t", hard=HardStop("scanner"), partial=Partial("target", 0.10, None, 0.5, "hard"))
    cons = simulate(td_from(bars, stop=90.0, pivot=None), cfg, 60, ZERO, scenario="conservative")
    opt = simulate(td_from(bars, stop=90.0, pivot=None), cfg, 60, ZERO, scenario="optimistic")
    assert cons["ambiguous_bar"] and opt["ambiguous_bar"]
    assert [f["reason"] for f in cons["fills"]] == ["HARD_STOP"]
    assert [f["reason"] for f in opt["fills"]] == ["PARTIAL_TARGET", "HARD_STOP"]
    assert opt["return"] > cons["return"]


# ── 정지·하한가·휴장 ─────────────────────────────────────────────

def test_halted_bar_has_no_fill():
    bars = [(101, 102, 100, 101), (0, 0, 0, 101, 0), (95, 96, 85, 86)] + flat(3, 86)
    r = simulate(td_from(bars, stop=90.0, market="KR"), ExitConfig("t", "t", hard=HardStop("scanner")), 60, ZERO)
    assert r["fills"][0]["t"] == 2


def test_kr_limit_down_lock_blocks_sale():
    bars = [(101, 102, 100, 101), (70.7, 70.7, 70.7, 70.7), (68, 72, 66, 70)] + flat(3, 70)
    r = simulate(td_from(bars, stop=90.0, market="KR"), ExitConfig("t", "t", hard=HardStop("scanner")), 60, ZERO)
    assert r["fills"][0]["t"] == 2 and r["fills"][0]["price"] == pytest.approx(68)


def test_holiday_calendar_uses_market_own_bars():
    df = _frame(300)
    df = df.drop(df.index[200])                              # 휴장일: 행 자체가 없음
    ind = compute_indicators(df)
    row = pd.Series({"detect_date": df.index[199], "signal_type": "REBOUND", "signal_date": "2024-01-01",
                     "stop_loss": 1.0, "pivot_price": None, "ma150": None, "market": "KR"})
    td, _ = build_trade(row, df, ind, 30)
    assert td.dates[0] == df.index[200].strftime("%Y-%m-%d")  # 다음 실제 거래일


def test_split_adjusted_snapshot_normalisation():
    from backtest.exit_strategy.src.entry_builder import snapshot_factor
    df = _frame(300)
    scan = df.index[250] + pd.Timedelta(hours=21)
    stored_price = float(df["Close"].iloc[250]) * 5          # 분할 전 가격으로 저장된 경우
    row = pd.Series({"scan_time": scan, "price": stored_price, "market": "US"})
    f, flag = snapshot_factor(row, df)
    assert f == pytest.approx(0.2) and flag == "factor_gt_5pct"


# ── 비용 / 미청산 / 시간 손절 ────────────────────────────────────

def test_transaction_costs_applied():
    bars = flat(5, 100)
    gross = simulate(td_from(bars, stop=None, pivot=None), ExitConfig("t", "t"), 3, ZERO)
    net = simulate(td_from(bars, stop=None, pivot=None), ExitConfig("t", "t"), 3, Costs(0.001, 0.003, 0.001))
    assert gross["return"] == pytest.approx(0.0)
    assert net["return"] < 0 and net["cost"] > 0


def test_open_position_at_horizon_is_unrealized():
    bars = [(100, 101, 99, 100), (101, 103, 100, 102), (102, 105, 101, 104)] + flat(5, 104)
    r = simulate(td_from(bars, stop=None, pivot=None), ExitConfig("t", "t"), 3, ZERO)
    assert r["exit_reason"] == "OPEN_AT_HORIZON"
    assert r["open_qty"] == 1.0 and r["realized_return_component"] == 0
    assert r["unrealized_component"] == pytest.approx(1.04)
    assert r["holding_days"] == 3


def test_time_stop_and_review_mode():
    bars = flat(30, 100.5)
    cfg = ExitConfig("t", "t", time=TimeStop(10, 0.02))
    r = simulate(td_from(bars, stop=None, pivot=None), cfg, 60, ZERO)
    assert r["exit_reason"] == "TIME_STOP" and r["fills"][0]["t"] == 10
    cfg_review = ExitConfig("t", "t", time=TimeStop(10, 0.02), review=("time",))
    r2 = simulate(td_from(bars, stop=None, pivot=None), cfg_review, 60, ZERO)
    assert r2["exit_reason"] == "OPEN_AT_HORIZON" and "TIME_STOP@9" in r2["review_flags"]


def test_upthrust_decision_uses_only_d_plus_n():
    df = _frame(60)
    sd = str(df.index[40].date())
    pivot = float(df["Close"].iloc[41]) + 1                  # D+1 종가가 pivot 아래 → 실패
    dec, failed = upthrust_decision(df, sd, pivot, 3)
    assert dec == df.index[43] and failed is True
    dec2, failed2 = upthrust_decision(df.iloc[:43], sd, pivot, 3)
    assert dec2 is None and failed2 is None                  # D+3 미도래 → 판정 보류


def test_catalog_unique_names_and_hybrid():
    names = [c.name for c in catalog()]
    assert len(names) == len(set(names))
    a, b = hybrid_configs()
    assert a.review == ("trend", "time") and b.review == ()


# ── 데이터 분리 누수 / 포트폴리오 중복 ──────────────────────────

def test_train_test_split_purges_overlapping_holdings():
    dates = pd.bdate_range("2022-01-03", periods=500)
    t = pd.DataFrame({"entry_date": dates[::5], "exit_date_max": dates[::5] + pd.Timedelta(days=90)})
    split = chronological_split(t["entry_date"], (0.6, 0.2, 0.2))
    tr = purge_overlap(t, split, "train", "validation")
    val_start = t.loc[split == "validation", "entry_date"].min()
    assert (t.loc[tr, "exit_date_max"] < val_start).all()


def test_portfolio_blocks_duplicate_ticker():
    idx = pd.bdate_range("2024-01-01", periods=30)
    closes = {"A": pd.Series(100.0, index=idx)}
    trades = pd.DataFrame([
        {"market": "US", "ticker": "A", "entry_date": idx[1], "exit_date": idx[10], "entry_price": 100.0,
         "fills": [{"date": idx[10].strftime("%Y-%m-%d"), "portion": 1.0, "price": 100.0}], "priority": 0},
        {"market": "US", "ticker": "A", "entry_date": idx[3], "exit_date": idx[12], "entry_price": 100.0,
         "fills": [{"date": idx[12].strftime("%Y-%m-%d"), "portion": 1.0, "price": 100.0}], "priority": 0},
    ])
    res = run_portfolio(trades, closes, idx, max_positions=5, position_pct=0.1, buy_cost=0, sell_cost=0)
    assert res["n_taken"] == 1 and res["n_skipped_dup"] == 1
