"""자동매수 SHADOW 독립 리뷰(PR #14, FAIL) 지적 사항 회귀 테스트 (docs/auto_buy_shadow.md).

운영 DB 대신 메모리 SQLite, SHADOW 저널은 임시 파일, 일봉·계좌는 가짜 공급자. 실주문·운영 DB·네트워크 없음.
시각은 UTC naive (2026-11 은 EST = UTC-5). 정상일 신호 마감 22:00 ET = 다음 날 03:00 UTC.
"""
import dataclasses
import json
import os
import sqlite3
import sys
import types
from datetime import date

import pandas as pd
import pytest

from tests.test_auto_buy_shadow import (D1, D2, D3, Bars, job, prod, scan, shadow, sig, utc)  # noqa: F401
from tests.test_auto_buy_shadow_integrity import (BAR_A2, BAR_A3, BAR_B2, BAR_B3, BAR_C3, _running_scan,
                                                  _two_orders, evening, final, one, q, run_ids)
from trading.auto_buy import settings as S
from trading.auto_buy.shadow import calendar as CAL
from trading.auto_buy.shadow import engine as EN
from trading.auto_buy.shadow import inputs as IN
from trading.auto_buy.shadow import report as RP
from trading.auto_buy.shadow.market_data import bars_from_download, yfinance_bars
from trading.auto_buy.shadow.runner import run_shadow_job
from trading.auto_buy.shadow.snapshot import capture_scan_log
from trading.auto_buy.shadow.store import connect_rw, event, get_runtime, tx
from trading.auto_buy.shadow.vfills import Bar


# ── P1 스케줄러: 60분 간격 ──────────────────────────────────────────

@pytest.mark.parametrize("minutes,expr", [(5, "*/5"), (20, "*/20"), (45, "*/45"), (60, "0"), (90, "0"), (1, "*/5")])
def test_job_minute_expression(minutes, expr):
    import scheduler
    assert scheduler._job_minute_expr(minutes) == expr


def _paused_scheduler(monkeypatch):
    import scheduler
    from apscheduler.schedulers.background import BackgroundScheduler

    class Paused(BackgroundScheduler):          # 실제 job 은 실행하지 않고 등록·시작(다음 실행 시각 계산)만 확인
        def start(self, *a, **k):
            super().start(paused=True)

    monkeypatch.setattr(scheduler, "BackgroundScheduler", Paused)
    monkeypatch.setattr(scheduler, "_sched", None)
    return scheduler


def test_sixty_minute_shadow_interval_starts_with_s01(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTO_BUY_US_MODE", "SHADOW")
    monkeypatch.setenv("AUTO_BUY_SHADOW_JOURNAL_PATH", str(tmp_path / "s.sqlite3"))
    monkeypatch.setenv("SCANNER_ORDER_JOURNAL_PATH", str(tmp_path / "orders.sqlite3"))
    monkeypatch.setenv("AUTO_BUY_SHADOW_JOB_MINUTES", "60")
    scheduler = _paused_scheduler(monkeypatch)
    scheduler.start_scheduler()
    try:
        sched = scheduler._sched
        assert sched.running
        ids = {j.id for j in sched.get_jobs()}
        assert {scheduler.AUTO_STOP_JOB_ID, scheduler.AUTO_BUY_SHADOW_JOB_ID} <= ids
        assert "minute='0'" in str(sched.get_job(scheduler.AUTO_BUY_SHADOW_JOB_ID).trigger)
    finally:
        scheduler._sched.shutdown(wait=False)


def test_shadow_registration_failure_does_not_block_s01(monkeypatch):
    scheduler = _paused_scheduler(monkeypatch)

    def boom(_s):
        raise ValueError("bad trigger")

    monkeypatch.setattr(scheduler, "_add_auto_buy_shadow_job", boom)
    scheduler.start_scheduler()
    try:
        assert scheduler._sched.running and scheduler._sched.get_job(scheduler.AUTO_STOP_JOB_ID) is not None
    finally:
        scheduler._sched.shutdown(wait=False)


# ── P1 yfinance 응답 형태 ──────────────────────────────────────────

def _frame(o, h, l_, c, day="2026-11-24"):
    return pd.DataFrame({"Open": [o], "High": [h], "Low": [l_], "Close": [c], "Volume": [1000]},
                        index=pd.DatetimeIndex([day]))


def test_single_and_multi_ticker_download_shapes():
    a, b = _frame(10, 11, 9.5, 10.5), _frame(20, 22, 19, 21)
    tick_price = pd.concat({"AAA": a}, axis=1)                # (Ticker, Price) — 단일 종목도 다중 열
    price_tick = tick_price.swaplevel(0, 1, axis=1)           # (Price, Ticker)
    want = Bar(10.0, 11.0, 9.5, 10.5)
    for raw in (a, tick_price, price_tick):
        assert bars_from_download(raw, ["AAA"], "2026-11-24") == {"AAA": want}
    both = pd.concat({"AAA": a, "BBB": b}, axis=1)
    assert bars_from_download(both, ["AAA", "BBB"], "2026-11-24") == {"AAA": want, "BBB": Bar(20.0, 22.0, 19.0, 21.0)}
    assert bars_from_download(both, ["AAA", "CCC"], "2026-11-24")["CCC"] is None
    assert bars_from_download(both, ["AAA"], "2026-11-25") == {"AAA": None}           # 날짜가 다른 봉은 쓰지 않음
    assert bars_from_download(a, ["AAA", "BBB"], "2026-11-24") == {"AAA": None, "BBB": None}   # 평평한 열은 모호


def test_recovery_of_single_ticker_uses_raw_chart_response():
    """복구(단일 종목) 조회도 Yahoo chart 원본 응답 1건에서 OHLC·거래량을 읽는다 (DataFrame 변환 경로 아님)."""
    from tests.test_auto_buy_shadow_review4 import chart_http
    got = yfinance_bars(["AAA"], "2026-11-24", http_get=chart_http({"AAA": {"2026-11-24": (10, 11, 9.5, 10.5, 1000)}}))
    assert got == {"AAA": Bar(10.0, 11.0, 9.5, 10.5)} and got["AAA"].volume == 1000


# ── P1 부동소수점 OHLC 경계 ─────────────────────────────────────────

def test_float_rounding_bar_is_valid_but_real_anomaly_is_not():
    h = 70.12
    assert Bar(69.0, h, 68.0, h + 1.42e-14).valid()            # 수정주가 계산 오차
    assert Bar(h + 1e-14, h, 68.0, 69.0).valid()
    assert Bar(69.0, h, 68.0 + 1e-14, 68.0).valid()
    assert not Bar(69.0, h, 68.0, h * 1.001).valid()           # 종가 > 고가 0.1%: 비정상
    assert not Bar(69.0, h, 68.5, 68.4).valid()                # 종가 < 저가
    assert not Bar(69.0, 0.0, 68.0, 69.0).valid()


def test_float_rounding_bar_fills_b01_and_does_not_block(shadow, prod):
    scan(prod, utc(D1, 21, 30), [sig("AAA")])
    job(shadow, prod, Bars({}), evening(D1))
    scan(prod, utc(D2, 21, 30), [])
    tiny = Bar(100.5, 103.0, 99.0, 103.0 + 1.42e-14)
    assert job(shadow, prod, Bars({D2: {"AAA": tiny}}), final(D2))["days"] == [D2]
    assert one(shadow, "SELECT status FROM ab_bars WHERE day=? AND ticker='AAA'", D2)["status"] == "OK"
    o = one(shadow, "SELECT * FROM ab_orders WHERE account_id='SV1:P7C:B01' AND ticker='AAA'")
    assert o["state"] == "FILLED"
    d = one(shadow, "SELECT * FROM ab_daily WHERE account_id='SV1:P7C:B01' AND day=?", D2)
    assert d["data_status"] == "COMPLETE" and "PRICE_DATA_MISSING" not in json.loads(d["safety_blocks"])


# ── P1 replay 실패 후 재시도 ────────────────────────────────────────

def test_failed_replay_keeps_recompute_needed_until_swap(shadow, prod, monkeypatch):
    _two_orders(shadow, prod)
    job(shadow, prod, Bars({D2: {"AAA": BAR_A2}}), final(D2))
    run1 = run_ids(shadow)["CURRENT"]
    assert one(shadow, "SELECT state FROM ab_orders WHERE run_id=? AND account_id='SV1:P7C:B00' AND ticker='BBB'",
               run1)["state"] == "FILL_UNKNOWN"
    scan(prod, utc(D3, 21, 30), [])
    full = Bars({D2: {"AAA": BAR_A2, "BBB": BAR_B2}, D3: {"AAA": BAR_A3, "BBB": BAR_B3, "CCC": BAR_C3}})
    orig = IN._diff

    def boom(*a, **k):
        raise RuntimeError("injected replay failure")

    monkeypatch.setattr(IN, "_diff", boom)
    out = job(shadow, prod, full, evening(D3))
    assert out.get("error") == "RuntimeError"
    assert run_ids(shadow)["CURRENT"] == run1 and "FAILED" in run_ids(shadow)        # 현재 run 보존
    assert one(shadow, "SELECT status FROM ab_bars WHERE day=? AND ticker='BBB' ORDER BY input_rev DESC", D2)["status"] == "OK"
    db = connect_rw(shadow.journal_path)
    try:
        assert IN.replay_needed(db)                                 # 최신 봉이 OK 여도 재계산 필요 상태 유지
    finally:
        db.close()
    monkeypatch.setattr(IN, "_diff", orig)
    out = job(shadow, prod, full, evening(D3).replace(minute=50))
    assert out["replay"]["parent"] == run1 and out["days"] == [D3]
    run2 = run_ids(shadow)["CURRENT"]
    assert run2 != run1
    assert one(shadow, "SELECT state FROM ab_orders WHERE run_id=? AND account_id='SV1:P7C:B00' AND ticker='BBB'",
               run2)["state"] == "FILLED"
    db = connect_rw(shadow.journal_path)
    try:
        assert not IN.replay_needed(db)
    finally:
        db.close()


# ── P1 job 의 적격 기록 직후 Scanner hook 이 그날을 확정하는 순서 ─────────

def test_hook_freezes_day_after_job_recorded_eligibility(shadow, prod, monkeypatch):
    scan(prod, utc(D1, 21, 30), [sig("AAA")])
    job(shadow, prod, Bars({}), evening(D1))
    orig, fired = EN.record_eligibility, []

    def racing(db):
        n = orig(db)
        if not fired:                     # job 의 첫 적격 기록 직후 정규 스캔이 끝나 hook 이 D2 를 확정한다
            fired.append(1)
            sid = scan(prod, utc(D2, 21, 30), [sig("NEW", signal_date=D2)])
            assert capture_scan_log(sid, settings=shadow, session_factory=prod)["frozen"]
        return n

    monkeypatch.setattr(EN, "record_eligibility", racing)
    out = job(shadow, prod, Bars({D2: {"AAA": BAR_A2}}), evening(D2))
    assert fired and out["days"] == [D2]
    assert one(shadow, "SELECT candidates FROM ab_days WHERE day=?", D2)["candidates"] == 1
    assert q(shadow, "SELECT * FROM ab_orders WHERE ticker='NEW' AND account_id='SV1:P7C:B00'")


def test_build_inputs_refuses_when_decision_eligibility_missing(shadow, prod):
    scan(prod, utc(D1, 21, 30), [sig("AAA")])
    job(shadow, prod, Bars({}), evening(D1))
    sid = scan(prod, utc(D2, 21, 30), [sig("NEW", signal_date=D2)])
    capture_scan_log(sid, settings=shadow, session_factory=prod)
    db = connect_rw(shadow.journal_path)
    try:
        run = EN.current_run(db)
        assert EN.eligibility_pending(db, sid) == 1
        with pytest.raises(RuntimeError, match="적격 판정 미기록"):
            IN.build_inputs(db, run, D2, {}, shadow)
        EN.record_eligibility(db)
        assert EN.eligibility_pending(db, sid) == 0
        assert IN.build_inputs(db, run, D2, {}, shadow).cands["B00"]
    finally:
        db.close()


# ── P1 신호 마감 이후 새로 시작한 스캔 ──────────────────────────────

def test_regular_scan_started_after_signal_cutoff_is_not_decision(shadow, prod):
    scan(prod, utc(D1, 21, 30), [sig("AAA")], trigger="manual")          # 정규 스캔 아님 (수집 시작점)
    job(shadow, prod, Bars({}), evening(D1))
    sid = scan(prod, utc(D2, 3, 15), [sig("BBB")])                        # D1 22:15 ET 에 새로 시작한 정규 스캔
    r = capture_scan_log(sid, settings=shadow, session_factory=prod)
    assert not r["frozen"] and not r["late"]
    assert one(shadow, "SELECT * FROM ab_day_inputs WHERE day=?", D1) is None
    assert "SCAN_AFTER_SIGNAL_CUTOFF" in one(shadow, "SELECT note FROM ab_scan_captures WHERE scan_log_id=?", sid)["note"]
    assert q(shadow, "SELECT * FROM ab_events WHERE kind='SCAN_AFTER_SIGNAL_CUTOFF'")
    out = job(shadow, prod, Bars({}), utc(D2, 3, 30))
    assert one(shadow, "SELECT decision_status FROM ab_day_inputs WHERE day=?", D1)["decision_status"] == "NO_DECISION_SCAN"
    assert out["days"] == [D1] and not q(shadow, "SELECT * FROM ab_orders")


def _job_running(shadow, prod, now):
    return run_shadow_job(now=now, settings=shadow, bars_provider=Bars({}), account_provider=lambda _l: None,
                          session_factory=prod, scan_running=lambda: True)


def test_cutoff_waits_only_for_scan_started_before_cutoff(shadow, prod):
    scan(prod, utc(D1, 21, 30), [sig("AAA")], trigger="manual")
    job(shadow, prod, Bars({}), evening(D1))
    _running_scan(prod, utc(D2, 2, 50), ["AAA"])                         # 21:50 ET 시작, 아직 실행 중
    out = _job_running(shadow, prod, utc(D2, 3, 20))
    assert "후보 확정 대기" in out["waiting"] and one(shadow, "SELECT * FROM ab_day_inputs") is None


def test_cutoff_does_not_wait_for_scan_started_after_cutoff(shadow, prod):
    scan(prod, utc(D1, 21, 30), [sig("AAA")], trigger="manual")
    job(shadow, prod, Bars({}), evening(D1))
    _running_scan(prod, utc(D2, 3, 5), ["AAA"])                          # 22:05 ET 새로 시작
    out = _job_running(shadow, prod, utc(D2, 3, 20))
    assert out["days"] == [D1]
    assert one(shadow, "SELECT decision_status FROM ab_day_inputs WHERE day=?", D1)["decision_status"] == "NO_DECISION_SCAN"


def test_pre_cutoff_scan_finishing_after_cutoff_still_decides(shadow, prod):
    scan(prod, utc(D1, 21, 30), [sig("AAA")], trigger="manual")
    job(shadow, prod, Bars({}), evening(D1))
    sid = _running_scan(prod, utc(D2, 2, 50), ["BBB"])                     # 21:50 ET 시작
    from database.models import ScanLog
    with prod() as s:                                                       # 22:30 ET 에 DONE
        log = s.get(ScanLog, sid)
        log.status, log.finished_at = "DONE", utc(D2, 3, 30)
        s.commit()
    assert capture_scan_log(sid, settings=shadow, session_factory=prod)["frozen"]
    assert one(shadow, "SELECT decision_scan_log_id FROM ab_day_inputs WHERE day=?", D1)["decision_scan_log_id"] == sid


# ── P2 결측 복구 순환·20거래일 경계 ─────────────────────────────────

def _seed_missing(shadow, days, ticker="AAA"):
    db = connect_rw(shadow.journal_path)
    try:
        with tx(db):
            for d in days:
                IN._store_bar(db, d, ticker, None, IN.DEFAULT_PROVIDER, "DAILY", True)
    finally:
        db.close()


def _sessions(start, n, settings):
    out, d = [], date.fromisoformat(start)
    while len(out) < n:
        if CAL.session(d, settings):
            out.append(d.isoformat())
        d = date.fromordinal(d.toordinal() + 1)
    return out


def test_recovery_window_is_exactly_twenty_sessions(shadow):
    days = _sessions("2026-12-01", 25, shadow)
    last = date.fromisoformat(days[-1])
    floor = IN.recovery_floor(last, shadow)
    assert len(CAL.trading_days_between(floor, last, shadow)) == S.RECOVERY_WINDOW_DAYS == 20


def test_missing_bar_last_auto_retry_and_first_manual_review_day(shadow):
    days = _sessions("2026-12-01", 25, shadow)
    _seed_missing(shadow, [days[0]])
    db = connect_rw(shadow.journal_path)
    try:
        last_auto = Bars({})
        IN.recover_missing(db, last_auto, final(days[19]), shadow)          # days[0] 이 창의 20번째(가장 오래된) 세션
        assert [c[1] for c in last_auto.calls] == [days[0]]
        assert IN.expired_missing_bars(db, IN.recovery_floor(date.fromisoformat(days[19]), shadow)) == []
        first_manual = Bars({})
        IN.recover_missing(db, first_manual, final(days[20]), shadow)       # 21번째 세션: 자동 재조회 종료
        assert first_manual.calls == []
        ev = db.execute("SELECT details FROM ab_events WHERE kind='MANUAL_REVIEW_REQUIRED'").fetchall()
        assert len(ev) == 1 and days[0] in ev[0][0]
    finally:
        db.close()


def test_recovery_rotates_so_later_days_are_not_starved(shadow):
    days = _sessions("2026-12-01", 12, shadow)
    _seed_missing(shadow, days)
    db = connect_rw(shadow.journal_path)
    try:
        p1, p2 = Bars({}), Bars({})
        IN.recover_missing(db, p1, final(days[-1]), shadow)
        IN.recover_missing(db, p2, final(days[-1]), shadow)
        first, second = [c[1] for c in p1.calls], [c[1] for c in p2.calls]
        assert first == days[:10]
        assert second[:2] == days[10:12] and len(second) == S.RECOVERY_MAX_DAYS_PER_RUN
        assert set(first) | set(second) == set(days)
    finally:
        db.close()


# ── P2 설정 동결·버전 전환 ─────────────────────────────────────────

def test_calendar_overrides_and_runner_are_part_of_frozen_versions(shadow):
    from trading.auto_buy.version import LOGIC_FILES
    other = dataclasses.replace(shadow, extra_closed_dates=frozenset({"2026-12-01"}))
    early = dataclasses.replace(shadow, extra_early_close_dates=frozenset({"2026-12-24"}))
    assert len({S.config_hash(shadow), S.config_hash(other), S.config_hash(early)}) == 3
    assert S.frozen_config(other)["calendar_overrides"]["extra_closed"] == ["2026-12-01"]
    assert "trading/auto_buy/shadow/runner.py" in LOGIC_FILES


def test_strategy_version_bump_starts_new_run_and_keeps_old(shadow, prod, monkeypatch):
    scan(prod, utc(D1, 21, 30), [sig("AAA")])
    job(shadow, prod, Bars({}), evening(D1))
    run1 = run_ids(shadow)["CURRENT"]
    monkeypatch.setattr(S, "STRATEGY_VERSION", "SV2")
    scan(prod, utc(D2, 21, 30), [sig("NEW", signal_date=D2)])
    out = job(shadow, prod, Bars({D2: {"AAA": BAR_A2}}), evening(D2))
    assert "error" not in out and out["days"] == [D2]
    runs = {r["run_id"]: r for r in q(shadow, "SELECT * FROM ab_runs")}
    run2 = run_ids(shadow)["CURRENT"]
    assert runs[run1]["status"] == "SUPERSEDED" and runs[run1]["strategy_version"] == "SV1"
    assert runs[run2]["strategy_version"] == "SV2" and runs[run2]["reason"] == "VERSION_START"
    assert one(shadow, "SELECT start_day FROM ab_accounts WHERE id='SV2:P7C:B00'")["start_day"] == D2
    assert [r["day"] for r in q(shadow, "SELECT day FROM ab_days WHERE run_id=? ORDER BY day", run2)] == [D2]
    assert q(shadow, "SELECT * FROM ab_orders WHERE run_id=? AND account_id='SV2:P7C:B00' AND ticker='NEW'", run2)
    assert q(shadow, "SELECT * FROM ab_daily WHERE run_id=? AND day=?", run1, D1)          # 이전 버전 결과 보존
    scan(prod, utc(D3, 21, 30), [])
    out = job(shadow, prod, Bars({D3: {"NEW": Bar(100.5, 103, 99, 102)}}), evening(D3))   # 다음 날도 처리
    assert "error" not in out and out["days"] == [D3]
    assert RP.summary(shadow)["health"]["current_run"] == run2


# ── P2 S5 지표 ─────────────────────────────────────────────────────

def test_profit_factor_incl_open_is_per_trade_like_p7_and_open_value_has_sell_tax(shadow, prod):
    scan(prod, utc(D1, 21, 30), [sig("AAA")])
    job(shadow, prod, Bars({}), evening(D1))
    scan(prod, utc(D2, 21, 30), [])
    job(shadow, prod, Bars({D2: {"AAA": BAR_A2}}), final(D2))
    db = connect_rw(shadow.journal_path)
    try:
        run = EN.current_run(db)
        acc = db.execute("SELECT * FROM ab_accounts WHERE id='SV1:P7C:B01'").fetchone()
        pos = db.execute("SELECT * FROM ab_positions WHERE run_id=? AND account_id=?", (run, acc["id"])).fetchone()
        m = RP.account_metrics(db, acc, run)
        from trading.auto_buy.shadow.vfills import unit_proceeds
        want = pos["qty"] * unit_proceeds(pos["last_close"]) - pos["cost"]
        assert m["open_pnl"] == pytest.approx(want)
        assert S.COSTS["sell_tax"] > 0
        assert want < pos["qty"] * pos["last_close"] * (1 - S.COSTS["slippage_bp"] / 1e4) \
            * (1 - S.COSTS["sell_commission"]) - pos["cost"]                     # 매도세 반영
    finally:
        db.close()


def test_pf_does_not_net_open_positions_and_matches_p7_definition():
    from backtest.entry_strategy.src.metrics import portfolio_metrics
    closed, opened = [100.0, -50.0], [30.0, -60.0]
    assert RP.profit_factor(closed + opened) == pytest.approx(130 / 110)        # 이전(상계) 계산은 100/80
    assert RP.profit_factor([10.0, 5.0]) is None                                 # P7: 총손실 0 이면 None
    cal = pd.DatetimeIndex(["2026-11-23", "2026-11-24"])
    tr = [{"pnl": p, "R": 0.0, "reason": "STOP", "risk0": 1.0, "ret": 0.0, "hold_days": 1, "open": False, "entry_ci": 0, "exit_ci": 1} for p in closed]
    op = [{"pnl": p, "R": 0.0, "reason": "OPEN", "risk0": 1.0, "ret": 0.0, "hold_days": 1, "open": True, "entry_ci": 0, "exit_ci": 1,
           "mkt_value": 1.0} for p in opened]
    res = {"start": 0, "end": 1, "equity": [100.0, 101.0], "invested": [0.0, 1.0], "trades": tr, "open": op,
           "positions": [0, 2], "heat_current": [0, 0], "heat_entry": [0, 0], "pool_size": 4, "initial": 100.0,
           "counts": {"orders": 4, "filled": 4}, "size_reasons": {}}
    p7 = portfolio_metrics(res, cal)
    assert RP.profit_factor(closed) == pytest.approx(p7["profit_factor"])
    assert RP.profit_factor(closed + opened) == pytest.approx(p7["profit_factor_incl_open"])


def test_safety_violations_count_job_errors_and_collection_stops(shadow, prod):
    scan(prod, utc(D1, 21, 30), [sig("AAA")])
    job(shadow, prod, Bars({}), evening(D1))
    assert RP.summary(shadow)["s5"]["profiles"]["P7C"]["safety_violations"] == 0
    db = connect_rw(shadow.journal_path)
    try:
        with tx(db):
            event(db, "ERROR", "job_failed", "SHADOW job 실패: injected")
    finally:
        db.close()
    s5 = RP.summary(shadow)["s5"]["profiles"]["P7C"]
    assert s5["safety_violations"] >= 1 and s5["safety_breakdown"]["error_events"] == 1
    assert s5["safety_breakdown"]["non_virtual_rows"] == 0


# ── P2 Migration 사전 점검 ─────────────────────────────────────────

def test_check_fails_on_incomplete_schema_and_hardlinked_journal(tmp_path, monkeypatch):
    from scripts.auto_buy_shadow_journal_migrate import Abort, run
    partial = tmp_path / "partial.sqlite3"
    db = sqlite3.connect(partial)
    db.execute("CREATE TABLE ab_runs(run_id INTEGER PRIMARY KEY)")
    db.commit()
    db.close()
    before = partial.read_bytes()
    with pytest.raises(Abort) as e:
        run(str(partial), None, check=True)
    assert e.value.code == 2 and "사본 upgrade 시험 실패" in str(e.value)
    assert partial.read_bytes() == before                                       # 원본 변경 없음
    orders = tmp_path / "orders.sqlite3"
    orders.write_bytes(b"")
    link = tmp_path / "shadow_link.sqlite3"
    os.link(orders, link)
    monkeypatch.setenv("SCANNER_ORDER_JOURNAL_PATH", str(orders))
    with pytest.raises(Abort, match="SCANNER_ORDER_JOURNAL_PATH"):
        run(str(link), None, check=True)
    monkeypatch.setenv("AUTO_BUY_US_MODE", "SHADOW")
    monkeypatch.setenv("AUTO_BUY_SHADOW_JOURNAL_PATH", str(link))
    assert "SCANNER_ORDER_JOURNAL_PATH" in (S.AutoBuySettings.from_env().config_error or "")


def test_check_dry_run_passes_for_empty_new_journal(tmp_path):
    from scripts.auto_buy_shadow_journal_migrate import run
    empty = tmp_path / "empty.sqlite3"
    sqlite3.connect(empty).close()
    os.chmod(empty, 0o600)                         # 권한 검증(최종 재리뷰)은 review4 Migration 테스트에서 따로 확인
    rep = run(str(empty), None, check=True)
    assert rep["result"] == "CHECK_ONLY" and rep["dry_run"]["ok"]
    assert sqlite3.connect(empty).execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0


# ── P2 Scanner hook: DONE 커밋 뒤 ORM 재조회 없음 ────────────────────

def test_scan_hook_gets_id_without_reloading_expired_log(monkeypatch):
    import database.models as models
    import scanner.market_analysis as market_analysis
    from scanner import scan_engine

    created = []

    class ExpiringLog:
        def __init__(self, **kw):
            self.__dict__.update(kw)
            self._id, self.expired = 7, False
            created.append(self)

        @property
        def id(self):
            if self.expired:              # 커밋 뒤 만료된 객체 속성 접근 = 운영 DB 추가 SELECT
                raise RuntimeError("SELECT after commit")
            return self._id

    class Chain:
        def filter(self, *a, **k):
            return self

        def distinct(self):
            return self

        def all(self):
            return []

    class FakeDb:
        commits = 0

        def add(self, row): pass
        def refresh(self, row): pass
        def query(self, *a): return Chain()
        def close(self): pass

        def commit(self):
            FakeDb.commits += 1
            if FakeDb.commits >= 2:
                for log in created:
                    log.expired = True

    seen = []
    monkeypatch.setattr(models, "SessionLocal", FakeDb)
    monkeypatch.setattr(models, "ScanLog", ExpiringLog)
    monkeypatch.setattr(market_analysis, "get_market_stages", lambda *a, **k: {})
    monkeypatch.setattr(market_analysis, "get_benchmark_close", lambda *a, **k: None)
    monkeypatch.setattr(scan_engine, "_scan_kr", lambda *a, **k: ([], 0))
    monkeypatch.setattr(scan_engine, "_check_holdings", lambda *a, **k: [])
    monkeypatch.setattr(scan_engine, "_auto_buy_snapshot_hook", lambda sid, market: seen.append((sid, market)))
    scan_engine.scan_status["is_running"] = False
    try:
        res = scan_engine.run_scan(market="KR")          # OFF·KR 스캔에서도 DONE 커밋 뒤 log.id 를 다시 읽지 않는다
    finally:
        scan_engine.scan_status["is_running"] = False
    assert res["status"] == "done" and seen == [(7, "KR")]
