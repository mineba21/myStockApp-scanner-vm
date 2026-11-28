"""PR #14 Codex 재리뷰(head 1906a3f) 결함 재현 — 1906a3f 와 현재 코드에 모두 있는 API 만 쓴다.

- P1: 동결 결정 입력 누락 (기존 FROZEN·NO_DECISION_SCAN 거래일 입력 소실, replay 도중·CURRENT 전환 직전 소실), 신규 거래일
  결정 입력 대기. 정상 NO_DECISION_SCAN·정상 빈 결정 스캔은 회귀 확인 (양쪽 통과).
- P2: 격리와 replay·신규 거래일 처리 경쟁 (격리는 독립 DB 연결이 기록).
- P2: 부분 처리·격리 상태의 성과 집계 보류.
오염 상태는 관리자 DDL(트리거 제거 후 복원)로 만든다. 임시 SHADOW 저널만 쓴다 (운영 DB·주문·운영 Migration 없음).
"""
import pytest

from tests.test_auto_buy_shadow import D1, D2, D3, Bars, job, prod, scan, shadow, sig, utc  # noqa: F401
from tests.test_auto_buy_shadow_integrity import BAR_A2, BAR_A3, BAR_B2, BAR_B3, BAR_C3, evening, final, one, q
from tests.test_auto_buy_shadow_review8_signal_set import contaminate
from tests.test_auto_buy_shadow_review9_repro import RECOVERED, _built, _current, _processed, _runs
from trading.auto_buy import settings as S
from trading.auto_buy.shadow import engine as EN
from trading.auto_buy.shadow import inputs as IN
from trading.auto_buy.shadow import report as RP
from trading.auto_buy.shadow.store import connect_rw, get_runtime, set_runtime, tx

BLOCK = "integrity_day_block"


def _ledger(shadow, run):
    return {t: q(shadow, f"SELECT * FROM {t} WHERE run_id=? ORDER BY rowid", run)
            for t in ("ab_orders", "ab_positions", "ab_daily", "ab_days", "ab_decisions", "ab_reservations")}


def _block_from_other_connection(path, day=D1, run=1):
    """독립 DB 연결이 거래일 격리를 기록한다 (다른 job·작업자)."""
    db = connect_rw(path)
    try:
        with tx(db):
            set_runtime(db, BLOCK, {"run": run, "day": day, "where": "other_connection", "issues": ["TEST"],
                                    "accounts_committed": [], "at": 0, "repeats": 1})
    finally:
        db.close()


def _rt(shadow, key):
    db = connect_rw(shadow.journal_path)
    try:
        return get_runtime(db, key)
    finally:
        db.close()


def _assert_current_kept(shadow, cur, before):
    runs = _runs(shadow)
    assert runs[cur] == "CURRENT" and list(runs.values()).count("CURRENT") == 1      # 새 CURRENT 없음
    assert _ledger(shadow, cur) == before                                            # 기존 원장 보존
    assert _rt(shadow, "replay_needed")                                              # 재계산 요청 보존


# ── P1: 결정 입력 누락 ─────────────────────────────────────────────────────

@pytest.mark.parametrize("day", [D1, D2])          # D1·D2 = FROZEN (D1 에서 기존 주문 발생)
def test_repro_existing_frozen_day_input_missing_blocks_replay(shadow, prod, day):
    _processed(shadow, prod)
    cur = _current(shadow)
    before = _ledger(shadow, cur)
    assert before["ab_orders"]
    contaminate(shadow.journal_path, f"DELETE FROM ab_day_inputs WHERE day='{day}'")
    out = job(shadow, prod, Bars(RECOVERED), utc(D3, 22, 0))
    assert out.get("error"), out
    _assert_current_kept(shadow, cur, before)
    assert _rt(shadow, BLOCK)                                                        # 거래일 격리·운영자 확인 대기
    assert not q(shadow, "SELECT 1 FROM ab_day_inputs WHERE day=?", day)             # 자동 NO_DECISION·재동결 없음


def test_repro_existing_no_decision_day_input_missing_is_not_a_new_day(shadow, prod):
    scan(prod, utc(D1, 21, 30), [sig("AAA"), sig("BBB")])
    job(shadow, prod, Bars({}), evening(D1))
    job(shadow, prod, Bars({D2: {"AAA": BAR_A2}}), final(D2))                       # D2: 정규 스캔 없음 → NO_DECISION
    assert one(shadow, "SELECT decision_status FROM ab_day_inputs WHERE day=?", D2)["decision_status"] == "NO_DECISION_SCAN"
    scan(prod, utc(D3, 21, 30), [])
    cur = _current(shadow)
    before = _ledger(shadow, cur)
    contaminate(shadow.journal_path, f"DELETE FROM ab_day_inputs WHERE day='{D2}'")
    out = job(shadow, prod, Bars(RECOVERED), utc(D3, 22, 0))
    assert out.get("error"), out
    _assert_current_kept(shadow, cur, before)
    assert not q(shadow, "SELECT 1 FROM ab_day_inputs WHERE day=?", D2)


def _drop_after_start(monkeypatch, shadow, day):
    real = IN.required_tickers
    done = {"n": 0}

    def drop_then(db, run, d):
        if done["n"] == 0:
            contaminate(shadow.journal_path, f"DELETE FROM ab_day_inputs WHERE day='{day}'")
        done["n"] += 1
        return real(db, run, d)
    monkeypatch.setattr(IN, "required_tickers", drop_then)


def test_repro_input_missing_after_replay_start(shadow, prod, monkeypatch):
    _processed(shadow, prod)
    cur = _current(shadow)
    before = _ledger(shadow, cur)
    _drop_after_start(monkeypatch, shadow, D2)
    out = job(shadow, prod, Bars(RECOVERED), utc(D3, 22, 0))
    assert out.get("error"), out
    _assert_current_kept(shadow, cur, before)


def test_repro_input_missing_right_before_current_switch(shadow, prod, monkeypatch):
    _processed(shadow, prod)
    cur = _current(shadow)
    before = _ledger(shadow, cur)
    real = IN._diff

    def diff_then_drop(db, old, new):
        out = real(db, old, new)
        contaminate(shadow.journal_path, f"DELETE FROM ab_day_inputs WHERE day='{D1}'")
        return out
    monkeypatch.setattr(IN, "_diff", diff_then_drop)
    out = job(shadow, prod, Bars(RECOVERED), utc(D3, 22, 0))
    assert out.get("error"), out
    _assert_current_kept(shadow, cur, before)
    assert "FAILED" in _runs(shadow).values()


def test_repro_new_day_without_decision_input_is_not_processed_as_no_decision(shadow, prod):
    scan(prod, utc(D1, 21, 30), [sig("AAA")])
    job(shadow, prod, Bars({}), evening(D1))
    db = connect_rw(shadow.journal_path)
    try:
        run = EN.current_run(db)
        assert db.execute("SELECT 1 FROM ab_day_inputs WHERE day=?", (D2,)).fetchone() is None
        with pytest.raises(RuntimeError):                  # 결정 입력 미확정: NO_DECISION_SCAN 으로 처리하지 않음
            IN.build_inputs(db, run, D2, {}, shadow)
    finally:
        db.close()


def test_normal_no_decision_and_empty_scan_days_replay_cleanly(shadow, prod):
    """회귀: 명시적 NO_DECISION_SCAN(D2)·정상 빈 결정 스캔(D3) 이 있는 원장의 정상 replay·CURRENT 전환."""
    scan(prod, utc(D1, 21, 30), [sig("AAA"), sig("BBB")])
    job(shadow, prod, Bars({}), evening(D1))
    job(shadow, prod, Bars({D2: {"AAA": BAR_A2}}), final(D2))                       # D2 NO_DECISION, BBB 결측
    scan(prod, utc(D3, 21, 30), [])
    cur = _current(shadow)
    out = job(shadow, prod, Bars(RECOVERED), utc(D3, 22, 0))
    assert out.get("error") is None and out["replay"]["run"] and _current(shadow) != cur
    st = {r["day"]: r["decision_status"] for r in q(shadow, "SELECT day, decision_status FROM ab_days WHERE run_id=?",
                                                     _current(shadow))}
    assert st == {D1: "FROZEN", D2: "NO_DECISION_SCAN", D3: "FROZEN"}
    assert not _rt(shadow, BLOCK)


# ── P2: 격리와 replay·신규 거래일 처리 경쟁 (독립 연결) ──────────────────────────

def _replay_direct(shadow, db):
    return IN.replay(db, Bars(RECOVERED), "BARS_RECOVERED", shadow)


def _ready_for_replay(shadow, prod):
    """D1·D2 처리(BBB 결측) 후, 복구 일봉을 저장해 재계산 요청을 만든다 (replay 는 아직 실행하지 않음)."""
    _processed(shadow, prod)
    db = connect_rw(shadow.journal_path)
    try:
        IN.recover_missing(db, Bars(RECOVERED), utc(D3, 22, 0), shadow)
        assert IN.replay_needed(db)
    finally:
        db.close()


def test_repro_block_before_replay_start(shadow, prod):
    _ready_for_replay(shadow, prod)
    cur = _current(shadow)
    before = _ledger(shadow, cur)
    _block_from_other_connection(shadow.journal_path, run=cur)
    db = connect_rw(shadow.journal_path)
    try:
        with pytest.raises(Exception):
            _replay_direct(shadow, db)
    finally:
        db.close()
    _assert_current_kept(shadow, cur, before)
    assert "BUILDING" not in _runs(shadow).values()


@pytest.mark.parametrize("when", ["during", "after_last_account", "before_switch"])
def test_repro_block_during_replay_prevents_promotion(shadow, prod, monkeypatch, when):
    _ready_for_replay(shadow, prod)
    cur = _current(shadow)
    before = _ledger(shadow, cur)
    if when == "during":
        real = IN.required_tickers
        seen = {"n": 0}

        def hook(db, run, d):
            if seen["n"] == 1:          # 두 번째 거래일 시작 시
                _block_from_other_connection(shadow.journal_path, run=cur)
            seen["n"] += 1
            return real(db, run, d)
        monkeypatch.setattr(IN, "required_tickers", hook)
    elif when == "after_last_account":
        real_acc = EN.process_account_day
        last = S.account_ids()[-1][0]

        def acc(db, run, acc_id, inp, settings=None):
            out = real_acc(db, run, acc_id, inp, settings)
            if acc_id == last and inp.day == D2 and run != cur:
                _block_from_other_connection(shadow.journal_path, run=cur)
            return out
        monkeypatch.setattr(EN, "process_account_day", acc)
    else:
        real_diff = IN._diff

        def diff(db, old, new):
            out = real_diff(db, old, new)
            _block_from_other_connection(shadow.journal_path, run=cur)
            return out
        monkeypatch.setattr(IN, "_diff", diff)
    db = connect_rw(shadow.journal_path)
    try:
        with pytest.raises(Exception):
            _replay_direct(shadow, db)
    finally:
        db.close()
    _assert_current_kept(shadow, cur, before)
    blk = _rt(shadow, BLOCK)
    assert blk["run"] == cur and blk["where"] == "other_connection"                # 덮어쓰지 않음·자동 해제 없음


def test_repro_block_from_other_job_during_new_day_processing(shadow, prod):
    db, run, inp = _built(shadow, prod)
    accounts = [a[0] for a in S.account_ids()]
    k = 7
    seen = {"n": 0}

    def on_account():
        if seen["n"] == k:
            _block_from_other_connection(shadow.journal_path, day=D1, run=run)
        seen["n"] += 1
    try:
        with pytest.raises(Exception):
            IN.process_day(db, run, inp, shadow, on_account=on_account)
        done = [r[0] for r in db.execute("SELECT account_id FROM ab_daily WHERE run_id=? AND day=? ORDER BY rowid",
                                         (run, D1))]
        assert done == accounts[:k]                       # 격리 이후 계좌는 쓰지 않음, 이전 계좌 기록 보존
        assert not db.execute("SELECT 1 FROM ab_days WHERE day=?", (D1,)).fetchone()
    finally:
        db.close()


# ── P2: 부분 처리·격리 상태의 성과 집계 ───────────────────────────────────────

def test_repro_partial_day_metrics_are_held_not_shown_as_normal(shadow, prod, monkeypatch):
    scan(prod, utc(D1, 21, 30), [sig("AAA"), sig("BBB")])
    real = EN.process_account_day
    accounts = [a[0] for a in S.account_ids()]

    def crash(db, run, acc_id, inp, settings=None):
        if acc_id == accounts[5]:
            raise RuntimeError("중단")
        return real(db, run, acc_id, inp, settings)
    monkeypatch.setattr(EN, "process_account_day", crash)
    job(shadow, prod, Bars({}), evening(D1))                                          # 5 계좌만 커밋, ab_days 없음
    assert one(shadow, "SELECT COUNT(*) n FROM ab_daily")["n"] == 5
    s = RP.summary(shadow)
    acc0 = next(a for a in s["accounts"] if a["id"] == accounts[0])
    assert acc0.get("performance_status") == "UNAVAILABLE"
    assert acc0["cum_return"] is None and acc0["orders"] is None and acc0["closed_trades"] is None
    assert s["s5"].get("performance_status") == "UNAVAILABLE"


def test_repro_quarantine_holds_performance_and_s5_never_passes(shadow, prod):
    scan(prod, utc(D1, 21, 30), [sig("AAA"), sig("BBB")])
    job(shadow, prod, Bars({}), evening(D1))                                          # 정상 처리 (46 계좌)
    _block_from_other_connection(shadow.journal_path, day=D1, run=_current(shadow))
    s = RP.summary(shadow)
    assert all(a.get("performance_status") == "UNAVAILABLE" and a["cum_return"] is None and a["pf"] is None
               for a in s["accounts"])
    assert s["s5"].get("performance_status") == "UNAVAILABLE"
    assert all(p.get("safety_pass") is False for p in s["s5"]["profiles"].values())
    assert s["s5"]["verdict"] == "NOT_RECOMMENDED" and s["s5"]["b01_status"] == "NEED_MORE_DATA"
