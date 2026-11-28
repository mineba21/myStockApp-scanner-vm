"""PR #14 Codex 재리뷰(head 1906a3f) 반영 — 수정 후 계약 테스트.

- 결정 입력 상태 구분 (신규 대기·명시적 NO_DECISION·입력 소실·상태 불일치), 재동결·자동 NO_DECISION 금지, 격리·job 차단
- 격리 표시: 덮어쓰지 않음·자동 해제 없음, replay 경쟁 지점별 예외·run 상태
- 성과 보류: account_detail 구분, 정상 상태에서는 AVAILABLE
임시 SHADOW 저널만 쓴다 (운영 DB·주문·운영 Migration 없음).
"""
import pytest

from tests.test_auto_buy_shadow import D1, D2, D3, Bars, job, prod, scan, shadow, sig, utc  # noqa: F401
from tests.test_auto_buy_shadow_integrity import BAR_A2, evening, final, one, q
from tests.test_auto_buy_shadow_review8_signal_set import contaminate
from tests.test_auto_buy_shadow_review9_repro import RECOVERED, _current, _processed, _runs
from tests.test_auto_buy_shadow_review10_repro import BLOCK, _block_from_other_connection, _ready_for_replay, _rt
from trading.auto_buy.shadow import inputs as IN
from trading.auto_buy.shadow import integrity as IG
from trading.auto_buy.shadow import report as RP
from trading.auto_buy.shadow import snapshot as SN
from trading.auto_buy.shadow.store import connect_rw, tx


def _issues(shadow, day):
    db = connect_rw(shadow.journal_path)
    try:
        return IG.decision_set_issues(db, day)
    finally:
        db.close()


def test_decision_input_states_are_distinguished(shadow, prod):
    scan(prod, utc(D1, 21, 30), [sig("AAA")])
    job(shadow, prod, Bars({}), evening(D1))
    job(shadow, prod, Bars({D2: {"AAA": BAR_A2}}), final(D2))                       # D2 명시적 NO_DECISION_SCAN
    assert _issues(shadow, D1) == [] and _issues(shadow, D2) == []
    assert _issues(shadow, D3) == []                                                # 신규 거래일·입력 미확정 = 오류 아님
    contaminate(shadow.journal_path, f"DELETE FROM ab_day_inputs WHERE day='{D1}'")
    assert _issues(shadow, D1)[0].startswith("DAY_INPUT_MISSING:recorded=['FROZEN']")
    contaminate(shadow.journal_path, f"DELETE FROM ab_day_inputs WHERE day='{D2}'")
    assert _issues(shadow, D2)[0].startswith("DAY_INPUT_MISSING:recorded=['NO_DECISION_SCAN']")


def test_status_mismatch_between_day_record_and_input(shadow, prod):
    scan(prod, utc(D1, 21, 30), [sig("AAA")])
    job(shadow, prod, Bars({}), evening(D1))
    contaminate(shadow.journal_path, f"DELETE FROM ab_day_inputs WHERE day='{D1}'",
                f"INSERT INTO ab_day_inputs(day, decision_status, frozen_at, frozen_by, strategy_version, config_hash, "
                f"logic_hash) VALUES ('{D1}', 'NO_DECISION_SCAN', 0, 'X', 'v', 'c', 'l')")
    iss = _issues(shadow, D1)
    assert any(i.startswith("DAY_RECORD_STATUS_MISMATCH") for i in iss)
    assert any(i.startswith("DAY_RECORD_SCAN_MISMATCH") for i in iss)


def test_missing_input_is_never_refrozen_or_marked_no_decision(shadow, prod):
    scan(prod, utc(D1, 21, 30), [sig("AAA")])
    job(shadow, prod, Bars({}), evening(D1))
    contaminate(shadow.journal_path, f"DELETE FROM ab_day_inputs WHERE day='{D1}'")
    db = connect_rw(shadow.journal_path)
    try:
        with pytest.raises(IG.SignalSetIntegrityError, match="DAY_INPUT_MISSING"):
            IN.freeze_no_decision(db, D1, "test", shadow)
    finally:
        db.close()
    assert not q(shadow, "SELECT 1 FROM ab_day_inputs WHERE day=?", D1)
    late = scan(prod, utc(D1, 21, 50), [sig("AAA")])                                # 같은 거래일 정규 재스캔
    res = SN.capture_scan_log(late, source="HOOK", settings=shadow, session_factory=prod)
    assert res["refreeze_blocked"] and not res["frozen"]
    assert not q(shadow, "SELECT 1 FROM ab_day_inputs WHERE day=?", D1)
    blk = _rt(shadow, BLOCK)
    assert blk and blk["day"] == D1                                                 # 격리·운영자 확인 대기
    out = job(shadow, prod, Bars({}), final(D2))
    assert out["error"] == "IntegrityDayBlocked" and out["days"] == []


def test_block_is_not_overwritten_or_cleared(shadow, prod):
    _processed(shadow, prod)
    cur = _current(shadow)
    _block_from_other_connection(shadow.journal_path, day=D1, run=cur)
    contaminate(shadow.journal_path, f"DELETE FROM ab_day_inputs WHERE day='{D2}'")
    db = connect_rw(shadow.journal_path)
    try:
        err = IG.SignalSetIntegrityError(D2, None, IG.decision_set_issues(db, D2), "test")
        IG.record_failure(db, err, 99)
        blk = IG.day_block(db)
    finally:
        db.close()
    assert (blk["run"], blk["day"], blk["where"]) == (cur, D1, "other_connection")   # 원래 표시 유지
    assert blk["also"][0]["day"] == D2                                               # 추가 격리는 덧붙임
    job(shadow, prod, Bars(RECOVERED), utc(D3, 22, 0))
    assert _rt(shadow, BLOCK)["day"] == D1                                           # 자동 해제 없음


@pytest.mark.parametrize("when,exc,failed", [("before", IG.IntegrityDayBlocked, False),
                                             ("switch", IG.IntegrityDayBlocked, True)])
def test_replay_block_points_raise_and_keep_runs(shadow, prod, monkeypatch, when, exc, failed):
    _ready_for_replay(shadow, prod)
    cur = _current(shadow)
    if when == "before":
        _block_from_other_connection(shadow.journal_path, run=cur)
    else:
        real = IN._diff

        def diff(db, old, new):
            out = real(db, old, new)
            _block_from_other_connection(shadow.journal_path, run=cur)
            return out
        monkeypatch.setattr(IN, "_diff", diff)
    db = connect_rw(shadow.journal_path)
    try:
        with pytest.raises(exc):
            IN.replay(db, Bars(RECOVERED), "BARS_RECOVERED", shadow)
    finally:
        db.close()
    runs = _runs(shadow)
    assert runs[cur] == "CURRENT" and ("FAILED" in runs.values()) == failed and "BUILDING" not in runs.values()
    assert _rt(shadow, "replay_needed") and not _rt(shadow, "replay_in_progress")


def test_replay_start_checks_parent_days_before_creating_run(shadow, prod):
    _ready_for_replay(shadow, prod)
    n_runs = len(_runs(shadow))
    contaminate(shadow.journal_path, f"DELETE FROM ab_day_inputs WHERE day='{D1}'")
    db = connect_rw(shadow.journal_path)
    try:
        with pytest.raises(IG.SignalSetIntegrityError) as ei:
            IN.replay(db, Bars(RECOVERED), "BARS_RECOVERED", shadow)
    finally:
        db.close()
    assert ei.value.where == "replay_start" and len(_runs(shadow)) == n_runs       # run 생성 없음
    assert _rt(shadow, "replay_needed") and _rt(shadow, BLOCK)


def test_performance_available_when_clean_and_held_in_account_detail_when_blocked(shadow, prod):
    scan(prod, utc(D1, 21, 30), [sig("AAA"), sig("BBB")])
    job(shadow, prod, Bars({}), evening(D1))
    s = RP.summary(shadow)
    assert s["performance"]["status"] == "AVAILABLE" and s["s5"]["performance_status"] == "AVAILABLE"
    assert all(a["performance_status"] == "AVAILABLE" and a["orders"] is not None for a in s["accounts"])
    acc = s["accounts"][0]["id"]
    _block_from_other_connection(shadow.journal_path, run=_current(shadow))
    d = RP.account_detail(acc, shadow)
    assert d["performance_status"] == "UNAVAILABLE" and "INTEGRITY_DAY_BLOCK" in d["performance_hold_reasons"]
    assert d["metrics"]["cum_return"] is None and d["metrics"]["diagnostic_raw"]["orders"] >= 1
    assert d["records_notice"] and d["orders"]                                       # 원본은 진단용으로 유지·구분
    s = RP.summary(shadow)
    assert s["performance"]["status"] == "UNAVAILABLE" and s["validation"]["complete"] is False
    assert s["b02"]["P7C"]["median_return"] is None and s["s5"]["adoption"] == "UNAVAILABLE"
    assert s["s5"]["safety_pass"] is False and s["health"]["integrity_day_block"]
