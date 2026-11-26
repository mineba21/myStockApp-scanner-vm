"""PR #14 Codex 최종 재리뷰 반영 회귀 테스트 (4) — S5 검증 기간 이력 집계.

현재 미해결 / 관찰 기간 위반 / 복구됨 / 복구 불가를 분리하고, 마감 전 재시도와 최종 DATA_INCOMPLETE 를 구분하며,
연속 5거래일 정상 운영을 평가한다. S5 투자 성과 기준은 바꾸지 않는다.
"""
import time

from tests.test_auto_buy_shadow import D1, D2, D3, Bars, job, prod, scan, shadow, sig, utc  # noqa: F401
from tests.test_auto_buy_shadow_integrity import BAR_A2, BAR_A3, BAR_B2, BAR_B3, BAR_C3, _two_orders, final
from trading.auto_buy import settings as S
from trading.auto_buy.shadow import report as RP
from trading.auto_buy.shadow.store import connect_rw, event, tx

DAYS = ["2026-11-23", "2026-11-24", "2026-11-25", "2026-11-27", "2026-11-30", "2026-12-01", "2026-12-02",
        "2026-12-03", "2026-12-04", "2026-12-07", "2026-12-08", "2026-12-09"]       # 11-26 휴장, 11-27 조기폐장


def evening(day):
    return utc(day, 22, 30)


def s5(shadow):
    return RP.summary(shadow)["s5"]["profiles"]["P7C"]


def test_replay_recovery_keeps_observation_history_and_splits_current_recovered(shadow, prod):
    _two_orders(shadow, prod)
    job(shadow, prod, Bars({D2: {"AAA": BAR_A2}}), final(D2))           # BBB 결측 → DATA_INCOMPLETE·FILL_UNKNOWN
    before = s5(shadow)["safety_breakdown"]
    assert before["current_unresolved"]["data_incomplete_days"] == 1 and before["data_incomplete_days"] == 1
    scan(prod, utc(D3, 21, 30), [])
    out = job(shadow, prod, Bars({D2: {"AAA": BAR_A2, "BBB": BAR_B2},
                                  D3: {"AAA": BAR_A3, "BBB": BAR_B3, "CCC": BAR_C3}}), evening(D3))
    assert out["replay"]                                                 # 복구·재계산 성공
    p = s5(shadow)
    b = p["safety_breakdown"]
    assert b["current_unresolved"]["data_incomplete_days"] == 0 and b["current_unresolved"]["fill_unknown_orders"] == 0
    assert b["data_incomplete_days"] == 1 and b["fill_unknown_orders"] >= 1          # 관찰 기간 이력 유지
    assert b["recovered"]["data_incomplete_days"] == 1 and b["recovered"]["fill_unknown_orders"] >= 1
    assert p["safety_observation_period"] == b["total"] >= 2 and p["safety_current_unresolved"] == 0
    assert p["safety_recovered"] >= 2 and p["safety_pass"] is False                   # 복구 성공 ≠ 무오류 운영
    c = b["continuity"]
    days = {x["day"]: x for x in c["violation_days"]}
    assert "DATA_INCOMPLETE_AT_FIRST_PROCESSING" in days[D2]["reasons"]               # 처음 처리 결과 기준
    assert c["initial_window"] == "FAIL" and c["functional_pass"] is False
    assert S.S5_CRITERIA["performance"]["safety_violations_max"] == 0
    assert RP.summary(shadow)["s5"]["verdict"] == "NOT_RECOMMENDED"
    assert RP.summary(shadow)["s5"]["b01_status"] == "NEED_MORE_DATA"


def test_pre_cutoff_retry_is_not_a_violation_but_final_incomplete_is(shadow, prod):
    _two_orders(shadow, prod)
    partial = Bars({D2: {"AAA": BAR_A2}})
    assert "1/2" in job(shadow, prod, partial, evening(D2))["waiting"]          # 마감 전 재시도
    assert job(shadow, prod, Bars({D2: {"AAA": BAR_A2, "BBB": BAR_B2}}), utc(D2, 23, 30))["days"] == [D2]
    b = s5(shadow)["safety_breakdown"]
    pre = b["pre_cutoff_retries"]
    assert pre["days_with_retries"] == 1 and pre["resolved_before_cutoff"] == [D2] and pre["final_incomplete"] == []
    assert b["data_incomplete_days"] == 0 and b["fill_unknown_orders"] == 0
    assert D2 not in {x["day"] for x in b["continuity"]["violation_days"]}


def test_abandoned_and_unrecoverable_scans_are_observation_and_unrecoverable(shadow, prod):
    scan(prod, utc(D1, 21, 30), [])
    job(shadow, prod, Bars({}), evening(D1))
    db = connect_rw(shadow.journal_path)
    try:
        with tx(db):
            db.execute("INSERT INTO ab_scan_captures(scan_log_id, scan_market, scan_status, scan_started_at, "
                       "trading_day, source, complete, recoverability, captured_at) VALUES (?,?,?,?,?,?,?,?,?)",
                       (900, "US", "ABANDONED", D1 + "T20:00:00", D1, "CATCHUP", 0, "UNRECOVERABLE", time.time()))
    finally:
        db.close()
    p = s5(shadow)
    b = p["safety_breakdown"]
    assert b["abandoned_scans"] == 1 and b["unrecoverable"]["unrecoverable_scans"] == 1
    assert p["safety_unrecoverable"] >= 1 and p["safety_pass"] is False
    assert "SCAN_ABANDONED_OR_UNRECOVERABLE" in {r for x in b["continuity"]["violation_days"] for r in x["reasons"]}


def _run_days(shadow, prod, days, error_on=None):
    for d in days:
        scan(prod, utc(d, 21, 30) if d != "2026-11-27" else utc(d, 18, 30), [])
        if d == error_on:
            db = connect_rw(shadow.journal_path)
            try:
                with tx(db):
                    event(db, "ERROR", "job_failed", "테스트: 그날 job 오류")
            finally:
                db.close()
        out = job(shadow, prod, Bars({}), evening(d) if d != "2026-11-27" else utc(d, 19, 30))
        assert out["days"] == [d], out


def test_five_consecutive_clean_days_required_and_reevaluated_after_violation(shadow, prod):
    _run_days(shadow, prod, DAYS[:4])
    c = s5(shadow)["functional_5d"]
    assert c["initial_window"] == "PENDING" and c["current_clean_streak"] == 4 and not c["functional_pass"]
    _run_days(shadow, prod, DAYS[4:5])
    c = s5(shadow)["functional_5d"]
    assert c["initial_window"] == "PASS" and c["current_clean_streak"] == 5 and c["functional_pass"]
    _run_days(shadow, prod, DAYS[5:6], error_on=DAYS[5])               # 6번째 거래일 job 오류
    p = s5(shadow)
    c = p["functional_5d"]
    assert c["current_clean_streak"] == 0 and not c["functional_pass"] and c["last_violation_day"] == DAYS[5]
    assert c["initial_window"] == "PASS"                                 # 최초 5거래일 결과는 그대로
    _run_days(shadow, prod, DAYS[6:10])
    assert s5(shadow)["functional_5d"]["current_clean_streak"] == 4 and not s5(shadow)["functional_5d"]["functional_pass"]
    _run_days(shadow, prod, DAYS[10:11])
    p = s5(shadow)
    c = p["functional_5d"]
    assert c["current_clean_streak"] == 5 and c["functional_pass"] and c["last_violation_day"] == DAYS[5]   # 이력 보존
    assert p["safety_breakdown"]["error_events"] == 1 and p["safety_pass"] is False   # 관찰 기간 위반은 남는다


def test_late_processing_breaks_streak(shadow, prod):
    scan(prod, utc(D1, 21, 30), [])
    job(shadow, prod, Bars({}), utc(D2, 6, 0))                           # 데이터 마감 + 30분 이후 처리 (01:00 ET)
    c = s5(shadow)["functional_5d"]
    assert c["violation_days"][0]["day"] == D1 and "LATE_PROCESSING" in c["violation_days"][0]["reasons"]
