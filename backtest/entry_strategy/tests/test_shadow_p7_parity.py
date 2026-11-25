"""P7 ↔ SHADOW 엔진 대조 (격리, 합성 데이터). 같은 신호·일봉·위험·보유·수수료·체결 규칙이면 같은 결과인지 본다.

입력은 하나: 합성 OHLC 와 후보 목록. P7 쪽은 백테스트 자체 함수(fills.build_orders/build_exits → portfolio.make_input →
risk_portfolio.run)로, SHADOW 쪽은 운영 엔진(trading.auto_buy.shadow.engine.process_account_day)으로 계산한다.

P7 프로필 중 운영 가정과 다른 부분은 운영(SHADOW) 기준으로 맞춘다 (차이·원인은 문서와 아래 상수에 기록).
- 비용: P7 configs 의 sell_tax 0.0000278 (SEC fee 가정) ↔ SHADOW COSTS sell_tax 0.0 → 운영 COSTS 로 맞춤
- 지정가: P7 은 종가×1.01 미반올림, SHADOW 는 센트 반올림 → 종가를 정수 달러로 만들어 두 값이 같게 함
- Entry Heat 기록 시점: P7 rec 은 판단 전, SHADOW ab_daily 는 판단(신규 예약) 후 → 비교 때 그날 신규 예약 위험을 뺌
- 체결 당일 손절 사유: P7 'STOP'(day0 플래그) ↔ SHADOW 'STOP_DAY0' → 같은 사유로 대응
- P7 은 마지막 날 판단을 하지 않음 → 합성 데이터의 마지막 날에는 후보를 두지 않음
"""
import json
import time

import numpy as np
import pandas as pd
import pytest

from backtest.entry_strategy.src import fills as F
from backtest.entry_strategy.src import portfolio as PF
from backtest.entry_strategy.src import risk_portfolio as RP
from trading.auto_buy import settings as S
from trading.auto_buy.shadow import calendar as CAL
from trading.auto_buy.shadow import eligibility as EL
from trading.auto_buy.shadow import engine as EN
from trading.auto_buy.shadow.schema import upgrade_journal
from trading.auto_buy.shadow.store import connect_rw, tx
from trading.auto_buy.shadow.vfills import Bar

ACC = "SV1:P7C:B01"
GRADES = [("S", "STRONG"), ("A", "STRONG"), ("A", "MODERATE"), ("B", "WEAK")]


def _calendar():
    from datetime import date
    return [d.isoformat() for d in CAL.trading_days_between(date(2026, 9, 1), date(2026, 11, 20))]


def _data(seed):
    rng = np.random.default_rng(seed)
    days = _calendar()
    n = len(days)
    tickers = [f"T{i:02d}" for i in range(10)]
    frames, ohlc = {}, {}
    for t in tickers:
        c = [float(rng.integers(40, 120))]
        for _ in range(n - 1):
            c.append(max(5.0, round(c[-1] * (1 + rng.normal(0.002, 0.03)))))
        c = np.array(c, float)
        o = np.r_[c[0], c[:-1]] * (1 + rng.normal(0, 0.012, n))
        gap = rng.random(n) < 0.03
        o[gap] *= 0.88                                                   # 가끔 갭하락
        h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.01, n)))
        lo = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.015, n)))
        o, h, lo = (np.round(x, 2) for x in (o, h, lo))
        h, lo = np.maximum(h, np.maximum(o, c)), np.minimum(lo, np.minimum(o, c))
        frames[t] = pd.DataFrame({"Open": o, "High": h, "Low": lo, "Close": c, "Volume": 1e6},
                                 index=pd.DatetimeIndex(days))
        ohlc[t] = (o, h, lo, c)
    cands = []
    for i in range(5, n - 1):
        for t in rng.choice(tickers, size=int(rng.integers(0, 4)), replace=False):
            close = ohlc[t][3][i]
            stop = round(close * (1 - rng.uniform(0.04, 0.13)), 2)
            g, q = GRADES[int(rng.integers(0, len(GRADES)))]
            cands.append({"ticker": str(t), "pos": i, "close": close, "stop": stop, "grade": g, "quality": q})
    return days, tickers, frames, ohlc, cands


def _p7(days, frames, cands, costs):
    cal = pd.DatetimeIndex(days)
    rows = []
    for c in cands:
        rows.append({"ticker": c["ticker"], "ticker_pos": c["pos"], "stop": c["stop"], "signal_type": "BREAKOUT",
                     "pivot": c["close"], "ext_pct": 0.0, "stop_pct": (1 - c["stop"] / c["close"]) * 100,
                     "signal_date": cal[c["pos"]], "grade": c["grade"], "quality": c["quality"], "close": c["close"]})
    cand = pd.DataFrame(rows)
    cand["rank"] = [EL.grade_rank({"grade": g, "signal_quality": q}) for g, q in zip(cand["grade"], cand["quality"])]
    cand = cand.sort_values(["ticker_pos", "rank", "ticker"], kind="stable").reset_index(drop=True)
    cand["cid"] = np.arange(len(cand))
    tas = F.ticker_arrays(frames, cal)
    cfg = {"order": {"timings": {"T1_100": {"kind": "limit_next_day", "limit_pct": S.LIMIT_PCT}},
                     "min_r_pct": S.MIN_R_PCT}}
    orders = F.build_orders(cand, tas, "T1_100", cfg)
    exits = F.build_exits(cand, orders, tas, "UNL", "conservative")
    si = PF.make_input(cand, orders, exits)
    prof = RP.RiskProfile(equity=50000.0, RISK_PCT=0.75, MAX_TOTAL_HEAT_PCT=4.0, MAX_POSITION_PCT=15.0,
                          min_cash_pct=0.20, heat_def="entry")
    closes = {t: frames[t]["Close"].to_numpy(float) for t in frames}
    res = RP.run(si, closes, 0, len(days) - 1, np.ones(len(cand), bool), cand["rank"].to_numpy(float), 2, prof,
                 costs, {}, None)
    return cand, res


def _shadow(tmp_path, days, tickers, ohlc, cand):
    path = str(tmp_path / "parity.sqlite3")
    upgrade_journal(path, allow_create=True)
    db = connect_rw(path)
    EN.ensure_accounts(db, days[0])
    run = EN.ensure_run(db)
    by_day = {}
    now = time.time()
    with tx(db):
        for i, d in enumerate(days):
            db.execute("INSERT INTO ab_scan_captures(scan_log_id, scan_market, scan_status, scan_started_at, "
                       "trading_day, source, complete, recoverability, captured_at) VALUES (?,?,?,?,?,?,?,?,?)",
                       (i + 1, "US", "DONE", d, d, "HOOK", 1, "FULL", now))
        for r in cand.itertuples():
            key = f"US:{r.ticker}:BREAKOUT:{days[r.ticker_pos]}"
            cur = db.execute(
                "INSERT INTO ab_signal_snapshots(scan_log_id, scan_result_id, signal_key, market, ticker, signal_type, "
                "signal_date, trading_day, scan_status, is_first_detection, strict_passed, first_strict_pass, "
                "first_pass_basis, grade, signal_quality, market_condition, decision_price, stop_loss, sector_name, "
                "row_sha256, row_json, capture_source, created_at) VALUES (?,?,?,?,?,?,?,?,?,1,1,1,?,?,?,?,?,?,?,?,?,?,?)",
                (r.ticker_pos + 1, int(r.cid), key, "US", r.ticker, "BREAKOUT", days[r.ticker_pos], days[r.ticker_pos],
                 "DONE", "FIRST_DETECTION", r.grade, r.quality, "BULL", r.close, r.stop, "Tech", "x", "{}", "TEST", now))
            by_day.setdefault(days[r.ticker_pos], []).append(cur.lastrowid)
    for i, d in enumerate(days):
        ids = by_day.get(d, [])
        cands = {"B01": [dict(x) for x in db.execute(
            "SELECT * FROM ab_signal_snapshots WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)] if ids else []}
        bars = {t: EN.BarRec(Bar(*(float(a[i]) for a in ohlc[t])), "synthetic", 0.0, 1) for t in tickers}
        inp = EN.DayInputs(day=d, decision_status="FROZEN", decision_scan_log_id=i + 1, required=tickers, bars=bars,
                           cands=cands, acct=None, account_status=None, next_day=days[i + 1] if i + 1 < len(days)
                           else None)
        EN.process_account_day(db, run, ACC, inp)
    return db, run


@pytest.mark.parametrize("seed", [7, 11, 23])
def test_shadow_engine_matches_p7_on_identical_inputs(tmp_path, seed, monkeypatch):
    days, tickers, frames, ohlc, cands = _data(seed)
    cand, p7 = _p7(days, frames, cands, S.COSTS)                            # P7 비용을 운영 COSTS 로 맞춤
    db, run = _shadow(tmp_path, days, tickers, ohlc, cand)
    try:
        # 체결·손절·거래 목록
        sh = db.execute("SELECT p.*, o.decision_day FROM ab_positions p JOIN ab_orders o ON o.id=p.order_id "
                        "WHERE p.run_id=? AND p.account_id=? ORDER BY p.entry_day, p.ticker", (run, ACC)).fetchall()
        p7_trades = sorted(p7["trades"] + p7["open"], key=lambda t: (t["entry_ci"], t["ticker"]))
        assert len(sh) == len(p7_trades) > 5
        for s, t in zip(sh, p7_trades):
            assert (s["ticker"], s["entry_day"], s["qty"]) == (t["ticker"], days[t["entry_ci"]], t["qty"])
            assert s["entry_price"] == pytest.approx(t["entry_px"], abs=1e-9)
            assert s["cost"] == pytest.approx(t["cost"], rel=1e-12)
            if t["open"]:
                assert s["state"] == "OPEN"
            else:
                reason = {"STOP_DAY0": "STOP"}.get(s["exit_reason"], s["exit_reason"])
                assert (s["state"], s["exit_day"], reason) == ("CLOSED", days[t["exit_ci"]], t["reason"])
                assert s["exit_price"] == pytest.approx(t["exit_px"], abs=1e-9)
                assert s["pnl"] == pytest.approx(t["pnl"], abs=1e-6)
                assert s["r_multiple"] == pytest.approx(t["R"], abs=1e-9)
        # 주문 건수·체결 결과
        oc = {r["state"]: r["n"] for r in db.execute(
            "SELECT state, COUNT(*) n FROM ab_orders WHERE run_id=? AND account_id=? GROUP BY state", (run, ACC))}
        assert sum(oc.values()) == p7["counts"]["orders"]
        assert oc.get("FILLED", 0) == p7["counts"]["filled"]
        assert oc.get("SKIP_OPEN_STOP", 0) == p7["counts"]["skip_open_stop"]
        assert oc.get("UNFILLED", 0) + oc.get("REJECTED_AT_FILL", 0) == p7["counts"]["unfilled"]
        # 순위·Top-N: 하루 주문 수 ≤ 2, P7 차단 사유 daily_top_n 건수와 SHADOW DAILY_TOP_N 일치
        top = db.execute("SELECT COUNT(*) FROM ab_decisions WHERE run_id=? AND account_id=? AND reason='DAILY_TOP_N'",
                         (run, ACC)).fetchone()[0]
        assert top == p7["block"].get("daily_top_n", 0)
        blocks = {r[0].lower(): r[1] for r in db.execute(
            "SELECT reason, COUNT(*) FROM ab_decisions WHERE run_id=? AND account_id=? AND outcome!='ORDERED' "
            "GROUP BY reason", (run, ACC))}
        assert blocks == p7["block"]                                         # 모든 차단 사유·건수 일치
        # 일별 순자산·현금·Entry Heat(판단 전)
        daily = db.execute("SELECT * FROM ab_daily WHERE run_id=? AND account_id=? ORDER BY day", (run, ACC)).fetchall()
        assert [d["day"] for d in daily] == days
        new_risk = {r[0]: r[1] for r in db.execute(
            "SELECT decision_day, SUM(qty * (limit_price - stop_price)) FROM ab_orders WHERE run_id=? AND account_id=? "
            "GROUP BY decision_day", (run, ACC))}
        for k, d in enumerate(daily):
            assert d["equity"] == pytest.approx(p7["rec"]["equity"][k], rel=1e-12)
            pre = d["entry_heat"] * d["heat_base"] - new_risk.get(d["day"], 0.0)
            assert pre == pytest.approx(p7["rec"]["heat_entry"][k] * p7["rec"]["equity"][k], abs=1e-6)
            assert d["n_positions"] == p7["positions"][k]
        cash = db.execute("SELECT cash FROM ab_account_runs WHERE run_id=? AND account_id=?", (run, ACC)).fetchone()[0]
        assert cash == pytest.approx(p7["rec"]["cash_ratio"][-1] * p7["rec"]["equity"][-1], rel=1e-12)
        assert daily[-1]["equity"] / 50000 - 1 == pytest.approx(p7["rec"]["equity"][-1] / 50000 - 1, abs=1e-12)
    finally:
        db.close()


def test_cost_assumption_difference_is_explicit():
    """P7 설정 비용과 운영 SHADOW 비용의 차이를 숨기지 않는다 (대조는 운영 기준으로 맞춰 수행)."""
    import yaml
    from pathlib import Path
    cfg = yaml.safe_load((Path(__file__).resolve().parents[1] / "configs" / "entry_backtest.yaml").read_text())
    p7c = {k: cfg["costs"][k] for k in S.COSTS}
    diff = {k: (p7c[k], S.COSTS[k]) for k in S.COSTS if p7c[k] != S.COSTS[k]}
    assert diff == {"sell_tax": (0.0000278, 0.0)}, json.dumps(diff)
