"""PR #14 Codex 재리뷰(2차) 반영 회귀 테스트: 거래량 0·미확인, 일 완료 판정, 재계산 BUILDING 복구, S5 집계, 지정가, ab0002.

운영 DB 대신 메모리 SQLite, SHADOW 저널은 임시 파일, 일봉·계좌는 가짜 공급자. 실주문 없음.
"""
import json
import math
import os
import socket
import sqlite3
import subprocess
import sys
import time
from decimal import Decimal

import pandas as pd
import pytest

from tests.test_auto_buy_shadow import D1, D2, D3, Bars, job, prod, rows, scan, shadow, sig, utc  # noqa: F401
from tests.test_auto_buy_shadow_integrity import BAR_A2, BAR_A3, BAR_B2, BAR_B3, BAR_C3, _two_orders, final, one, q
from trading.auto_buy import settings as S
from trading.auto_buy.shadow import eligibility as EL
from trading.auto_buy.shadow import engine as EN
from trading.auto_buy.shadow import inputs as IN
from trading.auto_buy.shadow import report as RP
from trading.auto_buy.shadow import risk as RK
from trading.auto_buy.shadow.market_data import bars_from_download
from trading.auto_buy.shadow.store import connect_rw, get_runtime, set_runtime, tx
from trading.auto_buy.shadow.vfills import SOURCE_YAHOO_CHART_RAW as RAW_SRC
from trading.auto_buy.shadow.vfills import Bar, volume_state

D4 = "2026-11-27"                     # 조기폐장일 (정상 거래일)


def evening(day):
    return utc(day, 22, 30)


class Raw:
    """거래량을 그대로 넘기는 공급자 (테스트 공통 Bars 의 기본 거래량 보정 없이 None 도 그대로)."""

    def __init__(self, table):
        self.table = table

    def __call__(self, tickers, day):
        return {t: self.table.get(day, {}).get(t) for t in tickers}


def vbar(o, h, lo, c, v):
    """엔진 단계 주입용 봉 (원본 확인 출처로 표시). 수집 경로 전체는 test_auto_buy_shadow_review4.py 에서 검증."""
    return Bar(o, h, lo, c, volume=v, volume_source=RAW_SRC)


# ── 거래량 상태 ────────────────────────────────────────────────────

@pytest.mark.parametrize("v,state", [(1500.0, "POSITIVE"), (0, "ZERO"), (0.0, "ZERO"), (None, "UNKNOWN"),
                                     (-1.0, "UNKNOWN"), (float("nan"), "UNKNOWN"), ("100", "UNKNOWN"),
                                     (True, "UNKNOWN")])
def test_volume_state_distinguishes_zero_from_unknown(v, state):
    assert volume_state(v, RAW_SRC) == state
    # 원본 확인 출처가 아니면 0 은 확정이 아니다 (yfinance 변환 시 null → 0)
    assert volume_state(v, "yfinance_dataframe") == ("UNKNOWN" if state == "ZERO" else state)
    assert volume_state(v) == ("UNKNOWN" if state == "ZERO" else state)


def test_download_parses_volume_zero_and_unknown():
    idx = pd.DatetimeIndex(["2026-11-24"])
    mk = lambda v: pd.DataFrame({"Open": [10.0], "High": [11.0], "Low": [9.5], "Close": [10.5], "Volume": [v]},  # noqa
                                index=idx)
    raw = pd.concat({"POS": mk(1200), "ZER": mk(0), "NAN": mk(float("nan")), "NEG": mk(-3)}, axis=1)
    got = bars_from_download(raw, ["POS", "ZER", "NAN", "NEG"], "2026-11-24")
    # 변환 DataFrame 의 0 은 원본 null 이었을 수 있으므로 확정 0 이 아니다
    assert [got[t].volume_state for t in ("POS", "ZER", "NAN", "NEG")] == ["POSITIVE", "UNKNOWN", "UNKNOWN", "UNKNOWN"]
    assert got["ZER"].volume == 0 and got["ZER"].volume_source == "yfinance_dataframe" and got["NAN"].volume is None
    no_vol = pd.concat({"AAA": mk(1).drop(columns=["Volume"])}, axis=1)
    assert bars_from_download(no_vol, ["AAA"], "2026-11-24")["AAA"].volume is None     # 열 없음 = 미확인


# ── SW 2021-04-19 형태: 다음 정규장 거래량 0 → 미체결 만료, 이월·재매수 없음 ─────────

SW_CLOSE, SW_STOP = 41.736687, 36.6441                     # 2021-04-16 판단 (재구성 데이터 값)
SW_ZERO = vbar(41.736687, 41.736687, 41.736687, 41.736687, 0)       # 2021-04-19: O=H=L=C, 거래량 0
SW_NEXT = vbar(39.898758, 41.555367, 39.898758, 41.555367, 2200)    # 2021-04-20: 이월했다면 시가 체결됐을 봉


def _sw_signal(**kw):
    return sig("SW", signal_type="RE_BREAKOUT", price=SW_CLOSE, pivot_price=41.0, stop_loss=SW_STOP, **kw)


def test_sw_zero_volume_next_session_expires_without_carry_or_rebuy(shadow, prod):
    scan(prod, utc(D1, 21, 30), [_sw_signal()])
    job(shadow, prod, Raw({}), evening(D1))
    orders = q(shadow, "SELECT * FROM ab_orders WHERE ticker='SW'")
    assert orders and all(o["target_day"] == D2 for o in orders)
    assert orders[0]["limit_price"] == 42.15                         # 41.736687×1.01=42.154… → 센트 내림
    scan(prod, utc(D2, 21, 30), [_sw_signal()])                     # 같은 신호 재탐지 (같은 signal_key)
    # 데이터 마감 전 거래량 0 은 잠정 — 저장·처리하지 않고 기다린다
    out = job(shadow, prod, Raw({D2: {"SW": SW_ZERO}}), evening(D2))
    assert out["days"] == [] and "대기" in out["waiting"]
    assert one(shadow, "SELECT * FROM ab_bars WHERE ticker='SW' AND day=?", D2) is None
    out = job(shadow, prod, Raw({D2: {"SW": SW_ZERO}}), final(D2))
    assert out["days"] == [D2]
    b = one(shadow, "SELECT * FROM ab_bars WHERE ticker='SW' AND day=?", D2)
    assert (b["status"], b["volume_status"], b["volume"]) == ("OK", "ZERO", 0)
    for o in q(shadow, "SELECT * FROM ab_orders WHERE ticker='SW'"):
        assert o["state"] == "UNFILLED" and json.loads(o["details"])["reason"] == "ZERO_VOLUME_EXPIRED"
        assert one(shadow, "SELECT state FROM ab_reservations WHERE order_id=?", o["id"])["state"] == "RELEASED"
    assert one(shadow, "SELECT COUNT(*) n FROM ab_fill_ledger")["n"] == 0
    day = one(shadow, "SELECT * FROM ab_days WHERE day=?", D2)
    assert day["status"] == "COMPLETE" and "ZERO_VOLUME:1" in json.loads(day["notes"])     # 0 확정은 결측 아님
    assert one(shadow, "SELECT COUNT(*) n FROM ab_events WHERE kind='ZERO_VOLUME'")["n"] == 1
    c = json.loads(one(shadow, "SELECT counts FROM ab_daily WHERE account_id='SV1:P7C:B01' AND day=?", D2)["counts"])
    assert c["unfilled_zero_volume"] == 1
    # D+2: 이월 체결 없음, 같은 신호로 재주문 없음
    scan(prod, utc(D3, 21, 30), [_sw_signal()])
    job(shadow, prod, Raw({D2: {"SW": SW_ZERO}, D3: {"SW": SW_NEXT}}), final(D3))
    assert one(shadow, "SELECT COUNT(*) n FROM ab_positions")["n"] == 0
    per_acc = q(shadow, "SELECT account_id, COUNT(*) n FROM ab_orders WHERE ticker='SW' GROUP BY account_id")
    assert per_acc and all(r["n"] == 1 for r in per_acc)
    later = q(shadow, "SELECT e.reasons FROM ab_eligibility e JOIN ab_signal_snapshots s ON s.id=e.snapshot_id "
                      "WHERE s.ticker='SW' AND s.trading_day!=? AND e.pool='B00'", D1)
    assert later and all("ALREADY_PASSED" in json.loads(r["reasons"]) for r in later)


@pytest.mark.parametrize("vol", [None, -1.0, float("nan")])
def test_volume_unknown_is_incomplete_fill_unknown_then_recovered(shadow, prod, vol):
    _two_orders(shadow, prod)
    bad = Raw({D2: {"AAA": vbar(100.5, 103, 99, 102, 5e5), "BBB": vbar(100.2, 102, 99.5, 101, vol)}})
    out = job(shadow, prod, bad, evening(D2))
    assert out["days"] == [] and "1/2" in out["waiting"]               # 마감 전: 미확인은 다시 받는다
    job(shadow, prod, bad, final(D2))
    b = one(shadow, "SELECT * FROM ab_bars WHERE ticker='BBB' AND day=?", D2)
    assert (b["status"], b["volume_status"], b["volume"]) == ("INVALID", "UNKNOWN", None)
    assert b["open"] == 100.2                                          # 근거용 OHLC 는 남긴다
    o = one(shadow, "SELECT * FROM ab_orders WHERE account_id='SV1:P7C:B00' AND ticker='BBB'")
    assert o["state"] == "FILL_UNKNOWN"                                # 0 으로 보지 않음 (UNFILLED 아님)
    day = one(shadow, "SELECT * FROM ab_days WHERE day=?", D2)
    assert day["status"] == "DATA_INCOMPLETE" and day["bars_missing"] == 1
    assert "VOLUME_UNKNOWN:1" in json.loads(day["notes"])
    # 다음 날 거래량이 확인되면 결측 복구와 같이 재계산
    scan(prod, utc(D3, 21, 30), [])
    good = Bars({D2: {"AAA": BAR_A2, "BBB": BAR_B2}, D3: {"AAA": BAR_A3, "BBB": BAR_B3, "CCC": BAR_C3}})
    out = job(shadow, prod, good, evening(D3))
    assert out["replay"] and out["days"] == [D3]
    new = one(shadow, "SELECT * FROM ab_orders WHERE run_id=? AND account_id='SV1:P7C:B00' AND ticker='BBB'",
              out["replay"]["run"])
    assert new["state"] == "FILLED"
    assert one(shadow, "SELECT bar_volume FROM ab_fill_ledger WHERE order_id=?", new["id"])["bar_volume"] == 1e6


def test_legacy_rows_without_volume_are_treated_as_unknown():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE t (status TEXT, volume_status TEXT, open REAL, high REAL, low REAL, close REAL, "
               "volume REAL, provider TEXT, fetched_at REAL, input_rev INT, volume_source TEXT)")
    db.execute("INSERT INTO t VALUES ('OK', NULL, 1, 1, 1, 1, NULL, 'x', 0, 1, NULL)")    # ab0002 이전에 저장된 행
    db.execute("INSERT INTO t VALUES ('OK', 'ZERO', 1, 1, 1, 1, 0, 'x', 0, 2, 'yahoo_chart_raw')")
    db.execute("INSERT INTO t VALUES ('OK', 'ZERO', 1, 1, 1, 1, 0, 'x', 0, 3, NULL)")     # ab0002 시절 출처 없는 0
    legacy, zero, unsourced = db.execute("SELECT * FROM t ORDER BY input_rev").fetchall()
    assert IN._rec(legacy) is None and not IN.bar_complete(legacy)
    assert IN._rec(zero).bar.no_volume
    assert IN._rec(unsourced) is None and not IN.bar_complete(unsourced)


# ── 거래량 0 보유 종목: 손절 체결 없음 → 거래 재개 첫 봉에서 갭 손절 ─────────────

def test_zero_volume_holding_keeps_risk_then_gap_stop_after_resume(shadow, prod):
    scan(prod, utc(D1, 21, 30), [sig("AAA")])
    job(shadow, prod, Bars({}), evening(D1))
    job(shadow, prod, Bars({D2: {"AAA": BAR_A2}}), final(D2))
    pos = one(shadow, "SELECT * FROM ab_positions WHERE account_id='SV1:P7C:B01'")
    assert pos["state"] == "OPEN" and pos["entry_price"] == 100.5 and pos["stop_price"] == 92.0
    halted = vbar(90.0, 90.0, 90.0, 90.0, 0)                           # 손절가 아래 가격이지만 거래 없음
    job(shadow, prod, Raw({D3: {"AAA": halted}}), final(D3))
    pos = one(shadow, "SELECT * FROM ab_positions WHERE account_id='SV1:P7C:B01'")
    assert pos["state"] == "OPEN" and pos["zero_volume_days"] == 1 and pos["last_zero_volume_day"] == D3
    assert pos["price_status"] == "OK" and one(shadow, "SELECT COUNT(*) n FROM ab_stop_events")["n"] == 0
    d = one(shadow, "SELECT * FROM ab_daily WHERE account_id='SV1:P7C:B01' AND day=?", D3)
    risk0 = (100.5 - 92.0) * pos["qty"]
    assert "STOP_BREACHED_OPEN" in json.loads(d["safety_blocks"])      # 미청산 위험 유지 → 신규 매수 차단
    assert d["entry_heat"] * d["heat_base"] == pytest.approx(risk0)
    assert d["current_heat"] * d["heat_base"] == pytest.approx(risk0)  # 이탈: 진입 위험 유지 (0 아님)
    assert json.loads(d["counts"])["zero_volume_hold"] == 1
    resume = vbar(85.0, 88.0, 84.0, 86.0, 1200)
    job(shadow, prod, Raw({D3: {"AAA": halted}, D4: {"AAA": resume}}), final(D4))
    ev = one(shadow, "SELECT * FROM ab_stop_events WHERE account_id='SV1:P7C:B01'")
    assert ev["reason"] == "STOP_GAP" and ev["exit_price"] == 85.0 and ev["day"] == D4
    det = json.loads(ev["details"])
    assert det["zero_volume_history"]["days"] == 1 and det["zero_volume_history"]["first_bar_after_resume"]
    assert det["gap_loss_vs_stop_usd"] == pytest.approx((92.0 - 85.0) * pos["qty"])
    assert det["volume"] == 1200


# ── P2-1 일별 요약 저장 직전 중단 → 재실행 ──────────────────────────────

def test_crash_before_daily_summary_then_rerun_keeps_fill_unknown_incomplete(shadow, prod, monkeypatch):
    _two_orders(shadow, prod)
    partial = Bars({D2: {"AAA": BAR_A2}})                               # BBB 결측 → FILL_UNKNOWN
    real = IN.day_completeness
    monkeypatch.setattr(IN, "day_completeness", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash")))
    out = job(shadow, prod, partial, final(D2))
    assert out["error"] == "RuntimeError"
    assert one(shadow, "SELECT COUNT(*) n FROM ab_daily WHERE day=?", D2)["n"] == len(S.account_ids())  # 46 처리됨
    assert one(shadow, "SELECT * FROM ab_days WHERE day=?", D2) is None                               # 요약 없음
    assert one(shadow, "SELECT state FROM ab_orders WHERE account_id='SV1:P7C:B00' AND ticker='BBB'")["state"] \
        == "FILL_UNKNOWN"
    db = connect_rw(shadow.journal_path)
    try:
        assert "BBB" in IN.required_tickers(db, EN.current_run(db), D2)     # FILL_UNKNOWN 종목이 빠지지 않음
    finally:
        db.close()
    monkeypatch.setattr(IN, "day_completeness", real)
    out = job(shadow, prod, partial, final(D2))
    assert out["days"] == [D2]
    day = one(shadow, "SELECT * FROM ab_days WHERE day=?", D2)
    notes = json.loads(day["notes"])
    assert day["status"] == "DATA_INCOMPLETE" and day["bars_missing"] == 1
    assert any(n.startswith("FILL_UNKNOWN:") for n in notes) and any(n.startswith("ACCOUNTS_INCOMPLETE:") for n in notes)


def test_day_is_not_complete_while_unresolved_state_remains(shadow, prod):
    _two_orders(shadow, prod)
    job(shadow, prod, Bars({D2: {"AAA": BAR_A2}}), final(D2))          # BBB FILL_UNKNOWN 남음
    job(shadow, prod, Bars({D3: {"AAA": BAR_A3}}), final(D3))          # D3 일봉은 모두 있음 (BBB 주문은 D2 목표)
    d3 = one(shadow, "SELECT * FROM ab_days WHERE day=?", D3)
    assert d3["status"] == "DATA_INCOMPLETE" and d3["bars_missing"] == 0
    assert "FILL_UNKNOWN:" + str(one(shadow, "SELECT COUNT(*) n FROM ab_orders WHERE state='FILL_UNKNOWN'")["n"]) \
        in json.loads(d3["notes"])


# ── P2-2 재계산 BUILDING 잔존 ─────────────────────────────────────────

def _dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _set_lease(shadow, **kw):
    db = connect_rw(shadow.journal_path)
    try:
        with tx(db):
            lease = get_runtime(db, "replay_in_progress") or {}
            lease.update(kw)
            set_runtime(db, "replay_in_progress", lease)
    finally:
        db.close()


def request_replay(shadow, bars=(("2026-11-24", "BBB"),)):
    """재계산 요청을 운영 경로와 같은 함수(``_mark_replay_needed``)로 남긴다. 반환: 요청 세대."""
    db = connect_rw(shadow.journal_path)
    try:
        with tx(db):
            IN._mark_replay_needed(db, [tuple(b) for b in bars])
        return IN.replay_request(db)["generation"]
    finally:
        db.close()


def _replay_ready(shadow, prod):
    _two_orders(shadow, prod)
    job(shadow, prod, Bars({D2: {"AAA": BAR_A2}}), final(D2))
    scan(prod, utc(D3, 21, 30), [])
    request_replay(shadow)                         # 직접 IN.replay 를 부르는 테스트용 (job 경로는 recover_missing 이 남김)
    return Bars({D2: {"AAA": BAR_A2, "BBB": BAR_B2}, D3: {"AAA": BAR_A3, "BBB": BAR_B3, "CCC": BAR_C3}})


def test_replay_killed_midway_leaves_building_then_reaped_and_retried(shadow, prod, monkeypatch):
    full = _replay_ready(shadow, prod)
    parent = one(shadow, "SELECT run_id FROM ab_runs WHERE status='CURRENT'")["run_id"]
    real = IN.build_inputs
    monkeypatch.setattr(IN, "build_inputs", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))  # 프로세스 종료
    with pytest.raises(KeyboardInterrupt):
        job(shadow, prod, full, evening(D3))
    monkeypatch.setattr(IN, "build_inputs", real)
    building = one(shadow, "SELECT run_id FROM ab_runs WHERE status='BUILDING'")["run_id"]
    assert one(shadow, "SELECT run_id FROM ab_runs WHERE status='CURRENT'")["run_id"] == parent
    _set_lease(shadow, token="dead-process", pid=_dead_pid())         # 다른(종료된) 프로세스의 실행권으로 남음
    h = RP.health(shadow)
    assert h["replay_state"]["status"] == "ABANDONED_SUSPECTED"
    s5 = RP.summary(shadow)["s5"]["profiles"]["P7C"]
    assert s5["safety_breakdown"]["replay_abandoned_suspected"] == 1 and not s5["safety_pass"]
    assert RP.summary(shadow)["validation"]["replay_in_progress"] is False      # '재계산 중'으로 계속 보이지 않음
    out = job(shadow, prod, full, evening(D3))                         # 다음 job: 정리 후 다시 재계산
    assert out["abandoned_builds"] == [building]
    runs = {r["run_id"]: r for r in q(shadow, "SELECT * FROM ab_runs")}
    assert runs[building]["status"] == "FAILED" and json.loads(runs[building]["summary"])["reason"] == "ABANDONED_BUILD"
    assert runs[parent]["status"] == "SUPERSEDED" and runs[out["replay"]["run"]]["status"] == "CURRENT"
    assert one(shadow, "SELECT COUNT(*) n FROM ab_daily WHERE run_id=?", building)["n"] >= 0   # 감사 이력 보존(삭제 없음)
    assert one(shadow, "SELECT COUNT(*) n FROM ab_events WHERE kind='replay_abandoned' AND severity='ERROR'")["n"] == 1
    h = RP.health(shadow)
    assert h["replay_state"] is None and h["building_runs"] == 0 and h["abandoned_replays"] == 1


@pytest.mark.parametrize("case,alive", [("other_live_pid", True), ("other_host_fresh", True),
                                        ("stale", True),             # 살아 있는 원래 작업자: heartbeat 지연만으로 정리 안 함
                                        ("other_host_stale", True),  # 다른 호스트: 사망 확인 불가 → 정리 안 함
                                        ("dead_pid", False), ("pid_reused", False), ("this_process", True)])
def test_lease_liveness(case, alive):
    """최종 재리뷰 이후 정책: 사망이 확인된 경우(DEAD)만 실행권을 정리한다 (상세는 review4 실행권 테스트)."""
    now = time.time()
    base = {"run_id": 9, "host": socket.gethostname(), "pid": os.getppid(), "token": "other", "heartbeat_at": now,
            "proc_start": IN.proc_start_id(os.getppid()), "boot_id": IN.boot_id()}
    lease = {
        "other_live_pid": base, "other_host_fresh": {**base, "host": "elsewhere", "pid": 1},
        "stale": {**base, "heartbeat_at": now - IN.REPLAY_LEASE_STALE_SEC - 1},
        "other_host_stale": {**base, "host": "elsewhere", "heartbeat_at": now - IN.REPLAY_LEASE_STALE_SEC - 1},
        "dead_pid": {**base, "pid": _dead_pid()}, "pid_reused": {**base, "pid": os.getpid()},
        "this_process": {**base, "pid": os.getpid(), "token": IN.PROCESS_TOKEN},
    }[case]
    assert IN.lease_alive(lease, now) is alive


def test_live_replay_of_other_worker_is_not_terminated(shadow, prod):
    full = _replay_ready(shadow, prod)
    db = connect_rw(shadow.journal_path)
    try:
        with tx(db):
            other = EN.create_run(db, "BARS_RECOVERED", EN.current_run(db), "BUILDING", shadow)
            set_runtime(db, "replay_in_progress", {"run_id": other, "host": socket.gethostname(), "pid": os.getppid(),
                                                   "token": "other-worker", "at": time.time(),
                                                   "heartbeat_at": time.time()})
        assert IN.reap_abandoned_builds(db) == []
        with pytest.raises(IN.ReplayBusy):
            IN.replay(db, full, "BARS_RECOVERED", shadow)
    finally:
        db.close()
    out = job(shadow, prod, full, evening(D3))
    assert "진행 중" in out["waiting"] and out["days"] == [] and out["abandoned_builds"] == []
    assert one(shadow, "SELECT status FROM ab_runs WHERE run_id=?", other)["status"] == "BUILDING"
    assert RP.health(shadow)["replay_state"]["status"] == "RUNNING"
    _set_lease(shadow, heartbeat_at=time.time() - IN.REPLAY_LEASE_STALE_SEC - 5)     # 작업자 응답 없음 (살아 있음)
    out = job(shadow, prod, full, evening(D3))
    assert out["abandoned_builds"] == [] and "응답 없음" in out["waiting"] and out["replay"] is None   # 자동 탈취 없음
    assert one(shadow, "SELECT status FROM ab_runs WHERE run_id=?", other)["status"] == "BUILDING"
    assert RP.health(shadow)["replay_state"]["status"] == "STALLED"
    assert one(shadow, "SELECT COUNT(*) n FROM ab_events WHERE kind='replay_stalled'")["n"] == 1
    _set_lease(shadow, pid=_dead_pid())                                             # 작업자 사망 확인
    out = job(shadow, prod, full, evening(D3))
    assert out["abandoned_builds"] == [other] and out["replay"]["run"] != other


def test_replay_that_lost_its_lease_never_becomes_current(shadow, prod, monkeypatch):
    full = _replay_ready(shadow, prod)
    parent = one(shadow, "SELECT run_id FROM ab_runs WHERE status='CURRENT'")["run_id"]
    real = IN.process_day

    def stolen(db, run, inp, settings=None, **kw):      # 다른 작업자가 이 재계산을 비정상 종료로 판정한 상황
        res = real(db, run, inp, settings, **kw)
        with tx(db):
            db.execute("UPDATE ab_runs SET status='FAILED' WHERE run_id=? AND status='BUILDING'", (run,))
            set_runtime(db, "replay_in_progress", None)
        return res
    monkeypatch.setattr(IN, "process_day", stolen)
    out = job(shadow, prod, full, evening(D3))
    assert out["error"] == "ReplayLeaseLost"
    assert one(shadow, "SELECT run_id FROM ab_runs WHERE status='CURRENT'")["run_id"] == parent
    assert one(shadow, "SELECT COUNT(*) n FROM ab_events WHERE kind='replay_lease_lost'")["n"] == 1


# ── P2-3 S5 안전·데이터 완전성 집계 ──────────────────────────────────────

def test_s5_counts_abandoned_unrecoverable_incomplete_and_fill_unknown(shadow, prod):
    _two_orders(shadow, prod)
    job(shadow, prod, Bars({D2: {"AAA": BAR_A2}}), final(D2))          # FILL_UNKNOWN + DATA_INCOMPLETE
    db = connect_rw(shadow.journal_path)
    try:
        with tx(db):
            for sid, st, rec in ((900, "ABANDONED", "UNRECOVERABLE"), (901, "ERROR", "UNRECOVERABLE"),
                                 (902, "ABANDONED", "PARTIAL")):
                db.execute("INSERT INTO ab_scan_captures(scan_log_id, scan_market, scan_status, scan_started_at, "
                           "trading_day, source, complete, recoverability, captured_at) VALUES (?,?,?,?,?,?,?,?,?)",
                           (sid, "US", st, D2 + "T21:00:00", D2, "CATCHUP", 0, rec, time.time()))
    finally:
        db.close()
    s5 = RP.summary(shadow)["s5"]["profiles"]["P7C"]
    b = s5["safety_breakdown"]
    assert b["data_integrity"] == {"abandoned_scans": 2, "unrecoverable_scans": 1, "uncaptured_scans": 0,
                                   "data_incomplete_days": 1, "fill_unknown_orders": b["fill_unknown_orders"],
                                   "unresolved_positions": 0, "manual_review_bars": 0}
    assert b["fill_unknown_orders"] >= 1
    # 시뮬레이션 시각이 D1 저녁 → D2 마감 후로 건너뛰므로 예정 실행 공백 1건이 운영 이력(heartbeat_gap)으로 남는다
    assert b["operational"]["heartbeat_stale"] == 1 and b["operational_total"] == 1
    assert b["data_integrity_total"] == 4 + b["fill_unknown_orders"]
    assert s5["safety_violations"] == b["total"] and s5["safety_pass"] is False
    assert s5["trading_days"] < S.S5_CRITERIA["milestones"]["safety_days"]     # 5거래일 검증 중에도 통과 아님
    assert S.S5_CRITERIA["performance"]["safety_violations_max"] == 0          # 기준 변경 없음


# ── 지정가 센트 내림·B02 순위 정의 (P7-OPS 와 같은 규칙) ─────────────────────

def test_limit_price_floors_to_cent_and_never_exceeds_cap():
    from backtest.entry_strategy.src.p7_ops import limit_floor_cent, random_key
    import random
    rnd = random.Random(3)
    prices = [100.005, 99.995, 41.736687, 57.0, 0.995] + [round(rnd.uniform(5, 900), rnd.choice([2, 4, 6]))
                                                        for _ in range(3000)]
    for p in prices:
        lim = RK.limit_price(p)
        assert lim == limit_floor_cent(p)
        assert Decimal(repr(lim)) <= Decimal(repr(p)) * Decimal("1.01")              # 상한 초과 없음
        assert Decimal(repr(p)) * Decimal("1.01") - Decimal(repr(lim)) < Decimal("0.01")
        assert Decimal(repr(lim)) == Decimal(repr(lim)).quantize(Decimal("0.01"))     # 운영 지정가 형식 (0.01)
    assert round(100.005 * 1.01, 2) > 100.005 * 1.01 and RK.limit_price(100.005) == 101.0   # 반올림이면 상한 초과
    assert all(EL.random_rank(s, "2026-11-24", k) == random_key(s, "2026-11-24", k)
               for s in (1, 7, 20) for k in ("US:A:BREAKOUT:2026-11-20", "US:B:REBOUND:2026-11-21"))


# ── Migration ab0002 ───────────────────────────────────────────────

def _v1(path):
    from alembic import command
    from alembic.config import Config
    from trading.auto_buy.shadow.schema import MIGRATIONS_DIR
    open(path, "w").close()
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["journal_path"] = path
    command.upgrade(cfg, "ab0001")


def test_ab0002_upgrades_v1_journal_keeping_rows_and_runtime_refuses_v1(tmp_path):
    from scripts.auto_buy_shadow_journal_migrate import run
    from trading.auto_buy.shadow.schema import schema_status, upgrade_journal
    p = str(tmp_path / "j.sqlite3")
    _v1(p)
    db = sqlite3.connect(p)
    db.execute("INSERT INTO ab_bars(day, ticker, input_rev, status, open, high, low, close, provider, fetched_at, "
               "purpose) VALUES ('2026-11-24','AAA',1,'OK',1,1,1,1,'x',0,'DAILY')")
    db.commit()
    db.close()
    st = schema_status(p)
    assert not st["schema_ready"] and st["schema_version"] == "ab0001" and "ab0007" in st["schema_error"]
    os.chmod(p, 0o600)
    rep = run(p, str(tmp_path / "bk"), check=True)
    assert rep["result"] == "CHECK_ONLY" and rep["dry_run"] == {"ok": True, "schema_version": "ab0007"}
    assert schema_status(p)["schema_version"] == "ab0001"                     # --check 는 원본을 바꾸지 않음
    assert upgrade_journal(p) == "ab0007"
    db = sqlite3.connect(p)
    assert db.execute("SELECT ticker, status, volume, volume_status FROM ab_bars").fetchall() == [("AAA", "OK", None,
                                                                                                    None)]
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO ab_bars(day, ticker, input_rev, status, provider, fetched_at, purpose, volume_status) "
                   "VALUES ('2026-11-25','AAA',1,'OK','x',0,'DAILY','BOGUS')")
    with pytest.raises(sqlite3.IntegrityError):                                # 기존 불변 트리거 유지
        db.execute("UPDATE ab_bars SET volume=1")
    db.close()


def test_ab0002_refuses_tampered_v1_structure(tmp_path):
    from trading.auto_buy.shadow.schema import schema_status, upgrade_journal
    p = str(tmp_path / "j.sqlite3")
    _v1(p)
    db = sqlite3.connect(p)
    db.execute("ALTER TABLE ab_bars ADD COLUMN volume REAL")                  # 손으로 일부만 바꾼 저널
    db.commit()
    db.close()
    with pytest.raises(Exception, match="ab0001 구조"):
        upgrade_journal(p)
    assert schema_status(p)["schema_version"] == "ab0001"                     # 롤백
