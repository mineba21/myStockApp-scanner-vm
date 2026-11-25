"""자동매수 SHADOW 정합성 재현 테스트 (재리뷰 P1·P2 수정분, docs/auto_buy_shadow.md).

운영 DB 대신 메모리 SQLite, SHADOW 저널은 임시 파일, 일봉·계좌는 가짜 공급자. 실주문·운영 DB 없음.
시각은 UTC naive (2026-11 은 EST = UTC-5). 11-26 추수감사절 휴장, 11-27 13:00 ET 조기폐장.
"""
import json
import os
import stat
from datetime import datetime, timezone

import pytest

from database.models import ScanLog, ScanResult
from tests.test_auto_buy_shadow import (ACCT, D1, D2, D3, Bars, job, prod, rows, scan, shadow, sig,  # noqa: F401
                                        utc)
from trading.auto_buy import settings as S
from trading.auto_buy.shadow import eligibility as EL
from trading.auto_buy.shadow import engine as EN
from trading.auto_buy.shadow import inputs as IN
from trading.auto_buy.shadow import report as RP
from trading.auto_buy.shadow import snapshot as SN
from trading.auto_buy.shadow.snapshot import capture_scan_log, catch_up
from trading.auto_buy.shadow.store import connect_rw
from trading.auto_buy.shadow.vfills import Bar

D4, D5 = "2026-11-27", "2026-11-30"           # D4 조기폐장 (13:00 ET)


def evening(day):          # 17:30 ET
    return utc(day, 22, 30)


def final(day):            # 23:30 ET (데이터 마감 23:20 이후) = 다음 날 04:30 UTC
    from datetime import date, timedelta
    d = date.fromisoformat(day) + timedelta(days=1)
    return utc(d.isoformat(), 4, 30)


def q(shadow, sql, *a):
    return rows(shadow.journal_path, sql, *a)


def one(shadow, sql, *a):
    r = q(shadow, sql, *a)
    return r[0] if r else None


def run_ids(shadow):
    return {r["status"]: r["run_id"] for r in q(shadow, "SELECT * FROM ab_runs")}


# ── P1-1 당일 후보 확정 후 재스캔 ─────────────────────────────────────

def test_post_decision_rescan_is_recorded_but_not_added(shadow, prod):
    scan(prod, utc(D1, 21, 30), [sig("AAA")])
    job(shadow, prod, Bars({}), evening(D1))
    di = one(shadow, "SELECT * FROM ab_day_inputs WHERE day=?", D1)
    assert di["decision_status"] == "FROZEN" and di["decision_scan_log_id"] == 1
    # 같은 저녁 수동 재스캔: NEW 가 새로 Strict 통과 (다음 정규장 전)
    late_id = scan(prod, utc(D1, 23, 0), [sig("AAA"), sig("NEW")], trigger="manual")
    r = capture_scan_log(late_id, settings=shadow, session_factory=prod)
    assert r["late"] and not r["frozen"]
    cap = one(shadow, "SELECT * FROM ab_scan_captures WHERE scan_log_id=?", late_id)
    assert cap["late_after_decision"] == 1 and cap["complete"] == 1
    snaps = q(shadow, "SELECT * FROM ab_signal_snapshots WHERE scan_log_id=?", late_id)
    assert {s["ticker"] for s in snaps} == {"AAA", "NEW"} and all(s["late_after_decision"] == 1 for s in snaps)
    ev = q(shadow, "SELECT * FROM ab_events WHERE kind='LATE_SCAN_AFTER_DECISION'")
    assert len(ev) == 1 and json.loads(ev[0]["details"])["new_strict_tickers"] == ["NEW"]
    assert one(shadow, "SELECT * FROM ab_day_inputs WHERE day=?", D1)["decision_scan_log_id"] == 1    # 소급 변경 없음
    job(shadow, prod, Bars({}), evening(D1).replace(hour=23, minute=40))
    new_el = one(shadow, "SELECT e.* FROM ab_eligibility e JOIN ab_signal_snapshots s ON s.id=e.snapshot_id "
                         "WHERE s.ticker='NEW' AND e.pool='B00'")
    assert new_el["eligible"] == 0 and "LATE_SCAN_AFTER_DECISION" in json.loads(new_el["reasons"])
    assert new_el["decision_role"] == "LATE_AFTER_DECISION"
    assert not q(shadow, "SELECT * FROM ab_orders WHERE ticker='NEW'")
    # 다음 날 정규 스캔에 NEW 가 그대로 나와도 '신규'가 아니다 (이력은 ALREADY_PASSED_NOT_DECIDED 로 남음)
    scan(prod, utc(D2, 21, 30), [sig("NEW"), sig("IND", signal_date=D2)])
    job(shadow, prod, Bars({D2: {"AAA": Bar(100.5, 103, 99, 102)}}), evening(D2))
    s_new = one(shadow, "SELECT * FROM ab_signal_snapshots WHERE ticker='NEW' AND trading_day=?", D2)
    assert s_new["first_strict_pass"] == 0 and s_new["first_pass_basis"] == "PRIOR_STRICT_NOT_DECIDED"
    el = one(shadow, "SELECT reasons FROM ab_eligibility WHERE snapshot_id=? AND pool='B00'", s_new["id"])
    assert "ALREADY_PASSED_NOT_DECIDED" in json.loads(el["reasons"])
    assert not q(shadow, "SELECT * FROM ab_orders WHERE ticker='NEW'")
    assert q(shadow, "SELECT * FROM ab_orders WHERE ticker='IND' AND account_id='SV1:P7C:B01'")   # 독립 신규 신호는 정상 평가
    summ = RP.summary(shadow)
    assert summ["late_scans"][0]["details"]["new_strict_tickers"] == ["NEW"]
    assert summ["capture_stats"]["late"] == 1 and "outbox" in summ["s1_limit_notice"]


def test_no_regular_scan_by_signal_cutoff_means_no_decision(shadow, prod):
    scan(prod, utc(D1, 21, 30), [sig("AAA")], trigger="manual")          # 정규 스캔이 아님
    out = job(shadow, prod, Bars({}), evening(D1))
    assert "후보 확정 대기" in out["waiting"] and one(shadow, "SELECT * FROM ab_day_inputs") is None
    out = job(shadow, prod, Bars({}), utc(D2, 3, 0))                     # 22:00 ET 신호 마감 이후
    di = one(shadow, "SELECT * FROM ab_day_inputs WHERE day=?", D1)
    assert di["decision_status"] == "NO_DECISION_SCAN" and out["days"] == [D1]
    assert not q(shadow, "SELECT * FROM ab_orders")
    el = one(shadow, "SELECT reasons, decision_role FROM ab_eligibility WHERE pool='B00'")
    assert el["decision_role"] == "NON_DECISION" and "NOT_DECISION_SCAN" in json.loads(el["reasons"])
    # 신호 마감 이후 도착한 정규 스캔도 늦은 스캔으로만 남는다
    sid = scan(prod, utc(D2, 3, 30), [sig("BBB")])
    assert capture_scan_log(sid, settings=shadow, session_factory=prod)["late"]


# ── P1-2 중단 스캔 ───────────────────────────────────────────────────

def _running_scan(factory, started, tickers):
    with factory() as s:
        log = ScanLog(market="US", status="RUNNING", started_at=started, finished_at=None, triggered_by="scheduler")
        s.add(log)
        s.flush()
        t = started.replace(minute=started.minute + 1)
        for tk in tickers:
            s.add(ScanResult(market="US", scan_time=t, first_detected_at=t, **sig(tk)))
        s.commit()
        return log.id


def test_killed_running_scan_is_recorded_abandoned_and_blocks_first_pass(shadow, prod, monkeypatch):
    scan(prod, utc(D1, 16, 0), [sig("OLD", strict_filter_passed=False)])      # 수집 시작점 (장중)
    catch_up(shadow, session_factory=prod, now=utc(D1, 16, 10))
    dead = _running_scan(prod, utc(D1, 21, 30), ["KIL"])                    # 프로세스 종료로 RUNNING 잔존
    s2 = scan(prod, utc(D2, 21, 30), [sig("KIL", price=101.0)])             # 다음 스캔이 KIL 행을 덮어씀
    caps = catch_up(shadow, session_factory=prod, now=utc(D2, 22, 0))
    assert {c["scan_log_id"]: c["status"] for c in caps} == {dead: "ABANDONED", s2: "DONE"}
    c = one(shadow, "SELECT * FROM ab_scan_captures WHERE scan_log_id=?", dead)
    assert c["complete"] == 0 and c["recoverability"] == "UNRECOVERABLE" and c["scan_finished_at"] is None
    k = one(shadow, "SELECT * FROM ab_signal_snapshots WHERE ticker='KIL'")
    assert k["scan_log_id"] == s2 and k["first_strict_pass"] is None and k["first_pass_basis"] == "CAPTURE_GAP"
    assert "FIRST_PASS_UNKNOWN" in EL.evaluate(k)["B00"][1]
    rec = json.loads(one(shadow, "SELECT value FROM ab_runtime WHERE key='scan_reconciliation'")["value"])
    assert rec["abandoned"] == 1 and rec["unrecoverable"] == 1 and rec["uncaptured"] == 0
    assert rec["scanner_total"] == rec["captured"] == 3


def test_abandoned_without_later_scan_is_partial_after_restart(shadow, prod, monkeypatch):
    scan(prod, utc(D1, 16, 0), [])
    catch_up(shadow, session_factory=prod, now=utc(D1, 16, 10))
    dead = _running_scan(prod, utc(D1, 21, 30), ["AAA"])
    monkeypatch.setattr(SN, "process_started_at", lambda: utc(D1, 22, 0))  # 스캔 시작 후 프로세스 재시작
    caps = catch_up(shadow, session_factory=prod, now=utc(D1, 22, 30))
    assert caps[0]["status"] == "ABANDONED"
    assert one(shadow, "SELECT recoverability FROM ab_scan_captures WHERE scan_log_id=?", dead)["recoverability"] \
        == "PARTIAL"


def test_legitimately_long_running_scan_is_not_abandoned(shadow, prod, monkeypatch):
    scan(prod, utc(D1, 16, 0), [])
    catch_up(shadow, session_factory=prod, now=utc(D1, 16, 10))
    live = _running_scan(prod, utc(D1, 21, 30), ["AAA"])
    assert catch_up(shadow, session_factory=prod, scan_running=lambda: True, now=utc(D1, 23, 30)) == []
    monkeypatch.setattr(SN, "process_started_at", lambda: utc(D1, 12, 0))  # 같은 프로세스에서 2시간째 실행 중
    assert catch_up(shadow, session_factory=prod, now=utc(D1, 23, 30)) == []
    assert one(shadow, "SELECT * FROM ab_scan_captures WHERE scan_log_id=?", live) is None
    rec = json.loads(one(shadow, "SELECT value FROM ab_runtime WHERE key='scan_reconciliation'")["value"])
    assert rec["scanner_running"] == 1 and rec["abandoned"] == 0


# ── P1-3 일봉 수집·마감·복구 ─────────────────────────────────────────

BAR_A2, BAR_B2 = Bar(100.5, 103, 99, 102), Bar(100.2, 102, 99.5, 101)
BAR_A3, BAR_B3, BAR_C3 = Bar(102, 104, 101, 103), Bar(101, 102, 100, 101.5), Bar(50.2, 51, 49.8, 50.5)


def _two_orders(shadow, prod):
    scan(prod, utc(D1, 21, 30), [sig("AAA"), sig("BBB")])
    job(shadow, prod, Bars({}), evening(D1))
    scan(prod, utc(D2, 21, 30), [sig("CCC", signal_date=D2, price=50.0, pivot_price=49.0, stop_loss=46.0)])


def test_partial_missing_bars_wait_then_finalize_incomplete_then_recover_and_replay(shadow, prod):
    _two_orders(shadow, prod)
    partial = Bars({D2: {"AAA": BAR_A2}})
    assert "잠정치" in job(shadow, prod, partial, utc(D2, 21, 50))["waiting"]       # 마감 후 60분 전
    out = job(shadow, prod, partial, evening(D2))
    assert "1/2" in out["waiting"] and out["days"] == []                             # 데이터 마감 전: 재조회 대기
    assert one(shadow, "SELECT * FROM ab_bars WHERE ticker='BBB'") is None
    out = job(shadow, prod, partial, final(D2))
    assert out["days"] == [D2]
    run1 = run_ids(shadow)["CURRENT"]
    assert one(shadow, "SELECT status FROM ab_bars WHERE ticker='BBB' AND day=?", D2)["status"] == "MISSING"
    assert one(shadow, "SELECT * FROM ab_days WHERE day=?", D2)["status"] == "DATA_INCOMPLETE"
    o = one(shadow, "SELECT * FROM ab_orders WHERE account_id='SV1:P7C:B00' AND ticker='BBB'")
    assert o["state"] == "FILL_UNKNOWN"
    assert one(shadow, "SELECT state FROM ab_reservations WHERE order_id=?", o["id"])["state"] == "ACTIVE"
    d = one(shadow, "SELECT * FROM ab_daily WHERE account_id='SV1:P7C:B00' AND day=?", D2)
    assert d["data_status"] == "INCOMPLETE" and d["unresolved_orders"] == 1 and d["stress_full_loss"] > 0
    assert "PENDING_UNRESOLVED" in json.loads(d["safety_blocks"])
    dec = one(shadow, "SELECT d.* FROM ab_decisions d JOIN ab_signal_snapshots s ON s.id=d.snapshot_id "
                      "WHERE d.account_id='SV1:P7C:B00' AND s.ticker='CCC'")
    assert dec["outcome"] == "BLOCKED" and dec["reason"] == "PENDING_UNRESOLVED"
    snaps_before = q(shadow, "SELECT id, row_sha256 FROM ab_signal_snapshots ORDER BY id")
    inputs_before = q(shadow, "SELECT * FROM ab_day_inputs ORDER BY day")
    # 다음 날 BBB 의 D2 일봉이 확인됨 → 새 run 으로 전체 재계산
    scan(prod, utc(D3, 21, 30), [])
    full = Bars({D2: {"AAA": BAR_A2, "BBB": BAR_B2}, D3: {"AAA": BAR_A3, "BBB": BAR_B3, "CCC": BAR_C3}})
    out = job(shadow, prod, full, evening(D3))
    assert out["replay"]["parent"] == run1 and out["days"] == [D3]
    runs = {r["run_id"]: r for r in q(shadow, "SELECT * FROM ab_runs")}
    run2 = out["replay"]["run"]
    assert runs[run1]["status"] == "SUPERSEDED" and runs[run2]["status"] == "CURRENT"
    assert runs[run2]["reason"] == "BARS_RECOVERED" and json.loads(runs[run2]["summary"])["first_divergent_day"] == D2
    bb = q(shadow, "SELECT * FROM ab_bars WHERE ticker='BBB' AND day=? ORDER BY input_rev", D2)
    assert [(b["input_rev"], b["status"], b["purpose"]) for b in bb] == [(1, "MISSING", "DAILY"), (2, "OK", "RECOVERY")]
    # 원래 기록 보존 + 재계산 결과 분리
    old = one(shadow, "SELECT state FROM ab_orders WHERE run_id=? AND account_id='SV1:P7C:B00' AND ticker='BBB'", run1)
    new = one(shadow, "SELECT * FROM ab_orders WHERE run_id=? AND account_id='SV1:P7C:B00' AND ticker='BBB'", run2)
    assert old["state"] == "FILL_UNKNOWN" and new["state"] == "FILLED"
    fl = one(shadow, "SELECT * FROM ab_fill_ledger WHERE order_id=?", new["id"])
    assert fl["bar_input_rev"] == 2 and fl["bar_open"] == BAR_B2.open
    dec2 = one(shadow, "SELECT d.* FROM ab_decisions d JOIN ab_signal_snapshots s ON s.id=d.snapshot_id "
                       "WHERE d.run_id=? AND d.account_id='SV1:P7C:B00' AND s.ticker='CCC'", run2)
    assert dec2["outcome"] == "ORDERED"                                        # 신규 판단도 다시 계산
    c1 = one(shadow, "SELECT cash FROM ab_daily WHERE run_id=? AND account_id='SV1:P7C:B00' AND day=?", run1, D2)
    c2 = one(shadow, "SELECT cash, data_status FROM ab_daily WHERE run_id=? AND account_id='SV1:P7C:B00' AND day=?",
             run2, D2)
    assert c2["cash"] < c1["cash"] and c2["data_status"] == "COMPLETE"
    assert one(shadow, "SELECT * FROM ab_days WHERE run_id=? AND day=?", run2, D2)["status"] == "COMPLETE"
    assert q(shadow, "SELECT id, row_sha256 FROM ab_signal_snapshots ORDER BY id")[:len(snaps_before)] == snaps_before
    assert q(shadow, "SELECT * FROM ab_day_inputs ORDER BY day")[:len(inputs_before)] == inputs_before
    summ = RP.summary(shadow)
    assert summ["health"]["current_run"] == run2 and summ["runs"][0]["run_id"] == run2
    assert summ["validation"]["data_incomplete_days"] == 0


def test_missed_day_stop_is_not_assumed_then_recovered_next_day(shadow, prod):
    scan(prod, utc(D1, 21, 30), [sig("AAA")])
    job(shadow, prod, Bars({}), evening(D1))
    scan(prod, utc(D2, 21, 30), [])
    job(shadow, prod, Bars({D2: {"AAA": BAR_A2}}), evening(D2))
    scan(prod, utc(D3, 21, 30), [])
    no_d3 = Bars({D2: {"AAA": BAR_A2}})
    job(shadow, prod, no_d3, final(D3))
    run1 = run_ids(shadow)["CURRENT"]
    p = one(shadow, "SELECT * FROM ab_positions WHERE account_id='SV1:P7C:B01'")
    assert p["state"] == "OPEN" and p["price_status"] == "UNRESOLVED" and p["unresolved_since"] == D3
    assert not q(shadow, "SELECT * FROM ab_stop_events")                      # 결측일 = 손절 없음으로 보지 않음
    d3 = one(shadow, "SELECT * FROM ab_daily WHERE account_id='SV1:P7C:B01' AND day=?", D3)
    assert d3["data_status"] == "INCOMPLETE" and d3["unresolved_positions"] == 1
    assert d3["stress_full_loss"] == pytest.approx(p["qty"] * BAR_A2.close)
    assert d3["entry_heat"] > 0 and "PRICE_DATA_MISSING" in json.loads(d3["safety_blocks"])
    # 조기폐장일(11-27): D3 실제 일봉(저가가 손절가 아래)이 확인됨 → 재계산에서 D3 손절
    scan(prod, utc(D4, 21, 30), [])
    real = Bars({D2: {"AAA": BAR_A2}, D3: {"AAA": Bar(95, 96, 90, 91)}})
    out = job(shadow, prod, real, utc(D4, 21, 40))
    assert out["replay"] and out["days"] == [D4]
    run2 = run_ids(shadow)["CURRENT"]
    p_old = one(shadow, "SELECT * FROM ab_positions WHERE run_id=? AND account_id='SV1:P7C:B01'", run1)
    p_new = one(shadow, "SELECT * FROM ab_positions WHERE run_id=? AND account_id='SV1:P7C:B01'", run2)
    assert p_old["state"] == "OPEN" and p_old["price_status"] == "UNRESOLVED"
    assert (p_new["state"], p_new["exit_day"], p_new["exit_reason"], p_new["exit_price"]) == ("CLOSED", D3, "STOP", 92.0)
    ev = one(shadow, "SELECT * FROM ab_stop_events WHERE run_id=?", run2)
    assert ev["day"] == D3 and json.loads(ev["details"])["input_rev"] == 2
    cash = one(shadow, "SELECT cash FROM ab_account_runs WHERE run_id=? AND account_id='SV1:P7C:B01'", run2)["cash"]
    assert cash == pytest.approx(50000 + p_new["pnl"])


def test_long_halt_or_delisting_stays_unresolved_with_stress(shadow, prod):
    scan(prod, utc(D1, 21, 30), [sig("AAA")])
    job(shadow, prod, Bars({}), evening(D1))
    scan(prod, utc(D2, 21, 30), [])
    bars = Bars({D2: {"AAA": BAR_A2}})
    job(shadow, prod, bars, evening(D2))
    days = [D3, D4, D5, "2026-12-01", "2026-12-02", "2026-12-03"]
    for d in days:
        scan(prod, utc(d, 21, 30) if d != D4 else utc(D4, 21, 30), [])
        job(shadow, prod, bars, final(d))
    run = run_ids(shadow)["CURRENT"]
    assert len(q(shadow, "SELECT * FROM ab_runs")) == 1                       # 복구된 일봉이 없으면 재계산 없음
    p = one(shadow, "SELECT * FROM ab_positions WHERE run_id=? AND account_id='SV1:P7C:B01'", run)
    assert p["state"] == "OPEN" and p["price_status"] == "UNRESOLVED"
    assert not q(shadow, "SELECT * FROM ab_bars WHERE ticker='AAA' AND day>?", D3)     # 이후 일은 가격을 만들지 않음
    db = connect_rw(shadow.journal_path)
    try:
        u = RP.unresolved(db, run, shadow, now=final("2026-12-03"))
    finally:
        db.close()
    row = next(x for x in u["positions"] if x["account_id"] == "SV1:P7C:B01")
    assert row["missing_trading_days"] == 6 and row["classification"] == "NO_DATA_SUSPECTED_HALT_OR_DELISTING"
    assert row["entry_risk"] > 0 and u["stress_full_loss_total"] > 0 and "별도 정책" in u["policy"]
    last = one(shadow, "SELECT * FROM ab_daily WHERE run_id=? AND account_id='SV1:P7C:B01' ORDER BY day DESC", run)
    assert "PRICE_DATA_MISSING" in json.loads(last["safety_blocks"]) and last["unresolved_value"] > 0


def test_invalid_bar_is_not_used_and_recorded_at_cutoff(shadow, prod):
    scan(prod, utc(D1, 21, 30), [sig("AAA")])
    job(shadow, prod, Bars({}), evening(D1))
    scan(prod, utc(D2, 21, 30), [])
    bad = Bars({D2: {"AAA": Bar(100, 99, 101, 100)}})                         # high < low
    assert "대기" in job(shadow, prod, bad, evening(D2))["waiting"]
    job(shadow, prod, bad, final(D2))
    assert one(shadow, "SELECT status FROM ab_bars WHERE ticker='AAA'")["status"] == "INVALID"
    assert one(shadow, "SELECT state FROM ab_orders WHERE account_id='SV1:P7C:B01'")["state"] == "FILL_UNKNOWN"


# ── P2-4.3 입력 고정 · 재실행 ────────────────────────────────────────

def test_rerun_and_replay_reuse_frozen_inputs(shadow, prod):
    calls = []

    def acct(_l):
        calls.append(1)
        return dict(ACCT, equity=ACCT["equity"] + 1000 * len(calls))        # 호출할 때마다 달라지는 계좌

    scan(prod, utc(D1, 21, 30), [sig("AAA")])
    from trading.auto_buy.shadow.runner import run_shadow_job
    kw = dict(settings=shadow, session_factory=prod, scan_running=lambda: False, account_provider=acct)
    run_shadow_job(now=evening(D1), bars_provider=Bars({}), **kw)
    scan(prod, utc(D2, 21, 30), [])
    run_shadow_job(now=evening(D2), bars_provider=Bars({D2: {"AAA": BAR_A2}}), **kw)
    run_shadow_job(now=final(D2), bars_provider=Bars({D2: {"AAA": Bar(1, 1, 1, 1)}}), **kw)
    assert len(calls) == 2                                                     # 거래일당 1회
    snap = json.loads(one(shadow, "SELECT account_snapshot FROM ab_day_inputs WHERE day=?", D1)["account_snapshot"])
    assert snap["equity"] == ACCT["equity"] + 1000
    db = connect_rw(shadow.journal_path)
    try:
        def boom(*_a):
            raise AssertionError("재계산이 저장된 입력 대신 공급자를 다시 불렀습니다")
        res = IN.replay(db, boom, "TEST_RERUN")
    finally:
        db.close()
    assert res["changed_accounts"] == 0 and res["first_divergent_day"] is None
    assert len(calls) == 2


def test_partial_account_failure_rolls_back_only_that_account(shadow, prod, monkeypatch):
    scan(prod, utc(D1, 21, 30), [sig("AAA"), sig("BBB")])
    real = EN.process_account_day
    state = {"n": 0}

    def flaky(db, run, acc_id, inp, settings=None):
        if acc_id == "SV1:OPS:B02:s05" and state["n"] == 0:
            state["n"] += 1
            raise RuntimeError("중간 실패")
        return real(db, run, acc_id, inp, settings)
    monkeypatch.setattr(EN, "process_account_day", flaky)
    out = job(shadow, prod, Bars({}), evening(D1))
    assert out["error"] == "RuntimeError" and not q(shadow, "SELECT * FROM ab_days")
    n = one(shadow, "SELECT COUNT(*) n FROM ab_daily")["n"]
    assert n < 46 and not q(shadow, "SELECT * FROM ab_orders WHERE account_id='SV1:OPS:B02:s05'")
    out = job(shadow, prod, Bars({}), evening(D1).replace(minute=50))
    assert out["days"] == [D1] and one(shadow, "SELECT COUNT(*) n FROM ab_daily")["n"] == 46
    # 계좌 독립: 각 P7C 계좌 현금은 자기 주문 예약만 반영
    for a in q(shadow, "SELECT * FROM ab_account_runs WHERE account_id LIKE 'SV1:P7C:%'"):
        assert a["cash"] == pytest.approx(50000.0)
        own = q(shadow, "SELECT * FROM ab_orders WHERE account_id=?", a["account_id"])
        assert all(o["run_id"] == a["run_id"] for o in own)
    assert len(q(shadow, "SELECT * FROM ab_account_runs")) == 46


# ── P2-4.2 판정 로직 해시 ─────────────────────────────────────────────

def test_logic_hash_ignores_docstrings_comments_but_not_code():
    from trading.auto_buy import version as V
    a = V._norm('def f(x):\n    """doc"""\n    return x + 1\n')
    b = V._norm('def f(x):\n    # 주석\n    return x + 1   # 끝\n')
    c = V._norm('def f(x):\n    return x + 2\n')
    assert a == b and a != c
    comps = V.logic_components()
    assert "trading/auto_buy/shadow/inputs.py" in comps and any(k.startswith("scanner/sizing.py") for k in comps)


def test_logic_change_blocks_processing_but_keeps_capturing_and_results(shadow, prod, monkeypatch):
    scan(prod, utc(D1, 21, 30), [sig("AAA")])
    job(shadow, prod, Bars({}), evening(D1))
    before = {t: one(shadow, f"SELECT COUNT(*) n FROM {t}")["n"] for t in ("ab_daily", "ab_orders", "ab_accounts")}
    monkeypatch.setattr("trading.auto_buy.version.logic_hash", lambda: "changedlogic0000")
    scan(prod, utc(D2, 21, 30), [sig("BBB", signal_date=D2)])
    out = job(shadow, prod, Bars({D2: {"AAA": BAR_A2}}), evening(D2))
    assert out["error"] == "VersionMismatch" and out["captures"] and out["days"] == []
    assert {t: one(shadow, f"SELECT COUNT(*) n FROM {t}")["n"] for t in before} == before
    assert q(shadow, "SELECT * FROM ab_events WHERE kind='version_mismatch'")
    assert RP.health(shadow)["version_block"]["message"]


# ── 하트비트 (휴장·조기폐장) ─────────────────────────────────────────

def _hb(shadow, last_job_at, processed, now):
    db = connect_rw(shadow.journal_path)
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT OR IGNORE INTO ab_runs(run_id, status, reason, strategy_version, config_hash, logic_hash, "
                   "created_at) VALUES (1,'CURRENT','INITIAL','SV1','x','y',0)")
        db.execute("DELETE FROM ab_days")
        for d in processed:
            db.execute("INSERT INTO ab_days(run_id, day, status, decision_status, candidates, bars_requested, "
                       "bars_missing, processed_at) VALUES (1,?,'COMPLETE','FROZEN',0,0,0,0)", (d,))
        db.execute("INSERT OR REPLACE INTO ab_runtime(key, value, updated_at) VALUES ('last_job', ?, 0)",
                   (json.dumps({"at": last_job_at}),))
        db.execute("COMMIT")
        return RP.heartbeat(db, shadow, now=now)
    finally:
        db.close()


def test_heartbeat_follows_exchange_calendar():
    slot = RP.expected_slot(datetime(2026, 11, 29, 17, 0, tzinfo=timezone.utc), 20)       # 일요일 12:00 ET
    assert slot == datetime(2026, 11, 29, 4, 40, tzinfo=timezone.utc)                     # 토요일 23:40 ET


def test_heartbeat_holiday_and_early_close(shadow):
    thanks = datetime(2026, 11, 27, 1, 5, tzinfo=timezone.utc)                            # 11-26(휴장) 20:05 ET
    hb = _hb(shadow, datetime(2026, 11, 27, 1, 0, 5, tzinfo=timezone.utc).timestamp(), [D3], thanks)
    assert hb["status"] == "OK" and hb["last_session"] == D3 and hb["processing"] == "CURRENT"
    hb = _hb(shadow, datetime(2026, 11, 27, 0, 0, tzinfo=timezone.utc).timestamp(), [D3], thanks)
    assert hb["status"] == "STALE"
    early = datetime(2026, 11, 27, 22, 0, tzinfo=timezone.utc)                            # 11-27 17:00 ET
    hb = _hb(shadow, early.timestamp(), [D3], early)
    assert hb["last_session"] == D4 and hb["last_session_early_close"] and hb["processing"] == "WAITING"
    assert hb["bars_cutoff"].startswith("2026-11-28T01:20")                               # 13:00 + 7:20 ET
    late = datetime(2026, 11, 28, 2, 0, tzinfo=timezone.utc)
    assert _hb(shadow, late.timestamp(), [D3], late)["processing"] == "LAGGING"
    assert _hb(shadow, late.timestamp(), [D3, D4], late)["processing"] == "CURRENT"


def test_cutoffs_follow_early_close():
    from datetime import date
    assert IN.bars_cutoff(date(2026, 11, 24)) == datetime(2026, 11, 25, 4, 20, tzinfo=timezone.utc)
    assert IN.signal_cutoff(date(2026, 11, 27)) == datetime(2026, 11, 28, 0, 0, tzinfo=timezone.utc)
    assert IN.bars_earliest(date(2026, 11, 27)) == datetime(2026, 11, 27, 19, 0, tzinfo=timezone.utc)
    assert IN.bars_cutoff(date(2026, 11, 26)) is None                                     # 휴장


# ── 계좌 스냅샷 대조 ─────────────────────────────────────────────────

def _ctx(summary, market):
    ov = summary["overseas"]
    return ov["cash"] + ov["evaluation_amount"], ov["orderable_cash"]


@pytest.mark.parametrize("rows_,ledger,status,reason", [
    ([{"ticker": "X", "eval_amount": 1000.0, "currency": "USD"}], 1003.0, "OK", None),
    ([{"ticker": "X", "eval_amount": 1000.0, "currency": "USD"}], 1100.0, "UNAVAILABLE", "잔고 불일치"),
    ([{"ticker": "X", "eval_amount": 1000.0, "currency": "KRW"}], 1000.0, "UNAVAILABLE", "USD 아님"),
    ([], 0.0, "OK", None),
])
def test_account_snapshot_reconciles_holdings_with_ledger(rows_, ledger, status, reason):
    from trading.auto_buy.shadow.market_data import build_account_snapshot
    summary = {"updated_at": "t", "overseas": {"cash": 5000.0, "orderable_cash": 4000.0,
                                                "evaluation_amount": ledger}}
    r = build_account_snapshot(summary, [dict(x, price_updated_at="t") for x in rows_], None, _ctx)
    assert r["status"] == status and (reason is None or reason in r["reason"])
    assert build_account_snapshot(dict(summary, error="원장 미확인"), [], None, _ctx)["status"] == "UNAVAILABLE"
    stale = [dict(x, price_updated_at="other") for x in rows_]
    if rows_ and status == "OK":
        assert "시점" in build_account_snapshot(summary, stale, None, _ctx)["reason"]


# ── Migration 권한 · env 파일 ─────────────────────────────────────────

def test_migrate_permissions_env_file_and_check_allow_create(tmp_path, monkeypatch, capsys):
    from scripts import auto_buy_shadow_journal_migrate as M
    j = tmp_path / "j" / "s.sqlite3"
    (tmp_path / "j").mkdir()
    env = tmp_path / ".env"
    env.write_text(f"AUTO_BUY_SHADOW_JOURNAL_PATH={j}\nKIWOOM_APP_SECRET=supersecretvalue\n")
    monkeypatch.delenv("AUTO_BUY_SHADOW_JOURNAL_PATH", raising=False)
    monkeypatch.setenv("SCANNER_ORDER_JOURNAL_PATH", str(tmp_path / "o.sqlite3"))
    old = os.umask(0o022)
    try:
        assert M.main(["--env-file", str(env), "--backup-dir", str(tmp_path / "bk"), "--check", "--allow-create"]) == 0
        assert not j.exists()
        assert M.main(["--env-file", str(env), "--backup-dir", str(tmp_path / "bk"), "--check"]) == 2
        assert M.main(["--env-file", str(env), "--backup-dir", str(tmp_path / "bk"), "--allow-create"]) == 0
    finally:
        os.umask(old)
    out = capsys.readouterr()
    assert "supersecretvalue" not in out.out + out.err
    assert stat.S_IMODE(j.stat().st_mode) == 0o600
    for f in (tmp_path / "bk").iterdir():
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert "KIWOOM_APP_SECRET" not in os.environ or os.environ["KIWOOM_APP_SECRET"] != "supersecretvalue"


def test_scan_started_before_decision_but_captured_later_is_not_late(shadow, prod):
    early = scan(prod, utc(D1, 21, 10), [sig("EAR")], trigger="manual", minutes=3)    # 정규 스캔보다 먼저, HOOK 누락
    dec = scan(prod, utc(D1, 21, 30), [sig("AAA")])
    capture_scan_log(dec, settings=shadow, session_factory=prod)                     # 정규 스캔 HOOK → 확정
    assert capture_scan_log(early, settings=shadow, session_factory=prod)["late"] is False
    job(shadow, prod, Bars({}), evening(D1))
    el = one(shadow, "SELECT e.decision_role, e.reasons FROM ab_eligibility e JOIN ab_signal_snapshots s "
                     "ON s.id=e.snapshot_id WHERE s.ticker='EAR' AND e.pool='B00'")
    assert el["decision_role"] == "NON_DECISION" and "NOT_DECISION_SCAN" in json.loads(el["reasons"])
    assert not q(shadow, "SELECT * FROM ab_events WHERE kind='LATE_SCAN_AFTER_DECISION'")

