"""PR #14 Codex 재리뷰(head d95beab) 반영 — 수정 후 계약 테스트.

- 원본 row_json ↔ 정규화 필드 계약 (정상 정규화 오탐 없음, 필드별 오염 탐지, P7-OPS 재구성 형식, 알 수 없는 형식)
- 적격 판정 집합 상태 분류 (완료·정상 부적격·빈 스캔·생성 전·과거 버전만(미사용)·일부 누락·사용 후 누락)
- 일부 계좌 커밋 후 무결성 오류 → 거래일 격리: 신규 판단·재계산 차단, 성과 제외, MONITOR·S5, 기록 삭제 없음
- Migration: 신규·ab0001·ab0006·ab0007(이미 최신)·재실행 멱등·오염 거부(--check 포함)·스크립트 우회 직접 upgrade 거부
임시 SHADOW 저널만 쓴다 (운영 DB·주문·운영 Migration 없음).
"""
import json
import os
import sqlite3

import pytest

from backtest.entry_strategy.src.ops_parity import write_scan_capture
from scripts import auto_buy_shadow_journal_migrate as M
from tests.test_auto_buy_shadow import D1, D2, D3, Bars, job, prod, scan, shadow, sig, utc  # noqa: F401
from tests.test_auto_buy_shadow_integrity import BAR_A2, evening, final, one, q
from tests.test_auto_buy_shadow_review5_s5 import _mode, _to, menv  # noqa: F401
from tests.test_auto_buy_shadow_review8_migration import data, to_ab0006
from tests.test_auto_buy_shadow_review8_signal_set import contaminate, decision
from tests.test_auto_buy_shadow_review9_repro import STOP_EDIT, _built, _dump, _elig_sql, _version
from trading.auto_buy import settings as S
from trading.auto_buy.shadow import engine as EN
from trading.auto_buy.shadow import inputs as IN
from trading.auto_buy.shadow import integrity as IG
from trading.auto_buy.shadow import raw_contract as RC
from trading.auto_buy.shadow import report as RP
from trading.auto_buy.shadow.schema import EXPECTED_SCHEMA_VERSION, schema_status, upgrade_journal
from trading.auto_buy.shadow.store import connect_rw, get_runtime, tx
from trading.auto_buy.version import logic_hash


# ── 원본 ↔ 정규화 필드 계약 ──────────────────────────────────────────────────

VARIANTS = [sig("AAA"),
            sig("RBD", signal_type="REBOUND", pivot_price=None, rs_value=1.234567, entry_warnings='["추격 주의"]'),
            sig("TXT", price=123.456789, pivot_price=0.0, stop_loss=None, entry_warnings="plain text warning",
                sector_source="GICS", volume_ratio=None, weekly_volume_ratio=2.5),
            sig("NOS", strict_filter_passed=None, grade=None, signal_quality=None, market_condition="BEAR",
                filter_reasons='["G2"]', stop_loss=101.0),
            sig("REJ", strict_filter_passed=False, name="이름 유니코드", sector_name=None)]


def _captured(shadow, prod):
    scan(prod, utc(D1, 21, 30), VARIANTS)
    job(shadow, prod, Bars({}), evening(D1))
    db = connect_rw(shadow.journal_path)
    try:
        return [dict(r) for r in db.execute("SELECT * FROM ab_signal_snapshots ORDER BY id")]
    finally:
        db.close()


def test_normal_normalization_is_not_flagged(shadow, prod):
    rows = _captured(shadow, prod)
    assert len(rows) == len(VARIANTS)
    assert {r["ticker"]: RC.row_issues(r) for r in rows} == {r["ticker"]: [] for r in rows}
    txt = next(r for r in rows if r["ticker"] == "TXT")
    assert txt["ext_pct"] is None and txt["stop_pct"] is None and txt["stop_loss"] is None   # 정상 파생 NULL
    db = connect_rw(shadow.journal_path)
    try:
        assert RC.journal_issues(db) == [] and IG.decision_set_issues(db, D1) == []
    finally:
        db.close()


def _mutate(v):
    if isinstance(v, bool) or v in (0, 1) and isinstance(v, int):
        return 1 - int(v)
    if isinstance(v, (int, float)):
        return v + 0.01
    if v is None:
        return 7.5
    return str(v) + "X"


@pytest.mark.parametrize("field", RC.VERIFIED_FIELDS["SCANNER_ROW"])
def test_each_verified_scanner_field_mismatch_is_detected(shadow, prod, field):
    rows = _captured(shadow, prod)
    for r in rows:
        bad = dict(r, **{field: _mutate(r[field])})
        assert field in RC.row_issues(bad), (r["ticker"], field)


def _p7_row(cid=1, close=100.0, stop=92.0, **kw):
    c = {"cid": cid, "signal_key": f"US:T{cid}:BREAKOUT:{D1}", "ticker": f"T{cid}", "signal_type": "BREAKOUT",
         "day": D1, "close": close, "stop": stop, "grade": "A", "quality": "STRONG", "market_condition": "BULL",
         "first_seen": True, "ext_pct": 1.5, "stop_pct": (1 - stop / close) * 100, "warnings": ["추격"],
         "sector": "Tech"}
    c.update(kw)
    return c


@pytest.mark.parametrize("field", RC.VERIFIED_FIELDS["P7_RECONSTRUCTED"])
def test_p7_reconstructed_rows_pass_and_each_field_mismatch_is_detected(shadow, field):
    db = connect_rw(shadow.journal_path)
    try:
        with tx(db):
            write_scan_capture(db, 1, D1, [_p7_row(1), _p7_row(2, close=33.33, stop=30.01, first_seen=False,
                                                                ext_pct=None, warnings=[])], 1.0, shadow, logic_hash())
        rows = [dict(r) for r in db.execute("SELECT * FROM ab_signal_snapshots ORDER BY id")]
        assert all(RC.row_issues(r) == [] for r in rows) and IG.decision_set_issues(db, D1) == []
        for r in rows:
            assert field in RC.row_issues(dict(r, **{field: _mutate(r[field])}))
    finally:
        db.close()


def test_unknown_or_unparseable_raw_profile_is_an_issue_not_a_bypass(shadow, prod):
    r = _captured(shadow, prod)[0]
    assert RC.row_issues(dict(r, capture_source="BACKTEST_REPLAY"))[0].startswith("RAW_PROFILE_UNKNOWN")
    assert RC.row_issues(dict(r, capture_source="OTHER"))[0].startswith("RAW_PROFILE_UNKNOWN")
    assert RC.row_issues(dict(r, row_json="not json")) == ["RAW_UNPARSEABLE"]
    p7 = json.dumps({"source": RC.P7_RAW_SOURCE})
    assert RC.row_issues(dict(r, row_json=p7))[0].startswith("RAW_PROFILE_UNKNOWN")      # 운영 출처는 P7 형식 불가


# ── 적격 판정 집합 상태 분류 ──────────────────────────────────────────────────

def _state(shadow, day):
    db = connect_rw(shadow.journal_path)
    try:
        row = db.execute("SELECT * FROM ab_day_inputs WHERE day=?", (day,)).fetchone()
        return IG.eligibility_state(db, row["decision_scan_log_id"], row)
    finally:
        db.close()


def test_eligibility_state_classification(shadow, prod):
    sid0 = scan(prod, utc(D1, 21, 30), [sig("AAA"), sig("NNN", strict_filter_passed=False)])
    from trading.auto_buy.shadow import snapshot as SN
    assert SN.capture_scan_log(sid0, source="HOOK", settings=shadow, session_factory=prod)["frozen"]
    sid = decision(shadow, D1)["decision_scan_log_id"]
    assert _state(shadow, D1) == ([], 2)                                       # 생성 전 (미사용) = pending
    old = (f"INSERT INTO ab_eligibility(snapshot_id, pool, strategy_version, eligible, reasons, decision_role, "
           f"logic_hash, created_at) SELECT id, p.pool, 'SV0_OLD', 1, '[]', 'DECISION', 'x', 0 FROM ab_signal_snapshots, "
           f"(SELECT 'B00' pool UNION SELECT 'B01') p WHERE scan_log_id={sid}")
    contaminate(shadow.journal_path, old)
    assert _state(shadow, D1) == ([], 2)                                       # 과거 버전만 + 미사용 = 정상 최초 생성 대상
    db = connect_rw(shadow.journal_path)
    try:
        assert EN.eligibility_pending(db, sid) == 2
        EN.record_eligibility(db)                                              # 정상 최초 생성 허용
        assert EN.eligibility_pending(db, sid) == 0
    finally:
        db.close()
    assert _state(shadow, D1) == ([], 0)                                       # 완료 (eligible=0 정상 부적격 포함)
    assert one(shadow, "SELECT COUNT(*) n FROM ab_eligibility e JOIN ab_signal_snapshots s ON s.id=e.snapshot_id "
                       "WHERE s.ticker='NNN' AND e.eligible=0 AND e.strategy_version=?", S.STRATEGY_VERSION)["n"] == 2
    contaminate(shadow.journal_path, _elig_sql(sid, "NNN", ("B01",)))
    issues, pending = _state(shadow, D1)
    assert pending == 0 and issues[0].startswith("ELIGIBILITY_INCOMPLETE") and "B01" in issues[0]
    db = connect_rw(shadow.journal_path)
    try:
        with pytest.raises(IG.SignalSetIntegrityError, match="ELIGIBILITY_INCOMPLETE"):
            EN.eligibility_pending(db, sid)
        with pytest.raises(IG.SignalSetIntegrityError, match="ELIGIBILITY_INCOMPLETE"):
            EN.candidates(db, sid)
        n = db.execute("SELECT COUNT(*) FROM ab_eligibility").fetchone()[0]
        EN.record_eligibility(db)                                              # 일부 누락은 다시 만들지 않는다
        assert db.execute("SELECT COUNT(*) FROM ab_eligibility").fetchone()[0] == n
    finally:
        db.close()


def test_used_day_with_all_current_judgements_missing_is_an_issue(shadow, prod):
    scan(prod, utc(D1, 21, 30), [sig("AAA")])
    job(shadow, prod, Bars({}), evening(D1))                                   # D1 처리 → 판정 사용됨
    sid = decision(shadow, D1)["decision_scan_log_id"]
    contaminate(shadow.journal_path, _elig_sql(sid, "AAA", ("B00", "B01")))
    issues, pending = _state(shadow, D1)
    assert pending == 0 and issues[0].startswith("ELIGIBILITY_MISSING_AFTER_USE")
    db = connect_rw(shadow.journal_path)
    try:
        with pytest.raises(IG.SignalSetIntegrityError, match="ELIGIBILITY_MISSING_AFTER_USE"):
            EN.record_eligibility(db)                                          # 재생성으로 숨기지 않음
        assert db.execute("SELECT COUNT(*) FROM ab_eligibility WHERE strategy_version=?",
                          (S.STRATEGY_VERSION,)).fetchone()[0] == 0
    finally:
        db.close()


def test_normal_empty_decision_scan_is_complete(shadow, prod):
    scan(prod, utc(D1, 21, 30), [])
    out = job(shadow, prod, Bars({}), evening(D1))
    assert out.get("error") is None and out["days"] == [D1]
    assert _state(shadow, D1) == ([], 0)


# ── 거래일 격리 (일부 계좌 커밋 후 무결성 오류) ─────────────────────────────────

def test_quarantined_day_blocks_new_days_and_is_excluded_from_performance(shadow, prod):
    db, run, inp = _built(shadow, prod)
    seen = {"n": 0}

    def on_account():
        if seen["n"] == 3:
            contaminate(shadow.journal_path, STOP_EDIT)
        seen["n"] += 1
    try:
        with pytest.raises(IG.SignalSetIntegrityError):
            IN.process_day(db, run, inp, shadow, on_account=on_account)
    finally:
        db.close()
    committed = q(shadow, "SELECT * FROM ab_daily WHERE day=? ORDER BY rowid", D1)
    assert len(committed) == 3
    # 다음 job: 신규 거래일·재계산 없음, 수집은 계속, 기록 삭제 없음
    scan(prod, utc(D2, 21, 30), [sig("CCC", signal_date=D2, price=50.0, pivot_price=49.0, stop_loss=46.0)])
    out = job(shadow, prod, Bars({D2: {"AAA": BAR_A2}}), final(D2))
    assert out["error"] == "IntegrityDayBlocked" and out["blocked"]["day"] == D1 and out["days"] == []
    assert out["captures"] and not q(shadow, "SELECT 1 FROM ab_days")
    assert q(shadow, "SELECT * FROM ab_daily WHERE day=? ORDER BY rowid", D1) == committed
    assert len(q(shadow, "SELECT 1 FROM ab_events WHERE kind='integrity_day_block'")) == 1
    h = RP.health(shadow)
    assert h["integrity_day_block"]["accounts_committed"] == [r["account_id"] for r in committed]
    assert h["signal_set_integrity"]                                           # 기존 MONITOR 표시 그대로 사용
    db = sqlite3.connect(shadow.journal_path)
    db.row_factory = sqlite3.Row
    try:
        sv = RP.safety_violations(db, h, shadow)
        acc = db.execute("SELECT * FROM ab_accounts WHERE id=?", (committed[0]["account_id"],)).fetchone()
        m = RP.account_metrics(db, acc, run)
    finally:
        db.close()
    assert sv["integrity_day_block"] == 1 and sv["current_unresolved"]["integrity_day_block"] == 1 and not sv["pass"]
    # 격리 중 성과 보류 (head 1906a3f P2-3): 성과 값은 NULL, 원본 수는 진단용으로만
    assert m["performance_status"] == "UNAVAILABLE" and m["days"] is None and m["equity"] is None
    assert m["diagnostic_raw"]["daily_rows"] == 1


# ── Migration 경로 ───────────────────────────────────────────────────────────

@pytest.fixture
def ledger06(shadow, prod):
    scan(prod, utc(D1, 21, 30), [sig("AAA"), sig("BBB")])
    job(shadow, prod, Bars({}), evening(D1))
    scan(prod, utc(D2, 21, 30), [sig("CCC", signal_date=D2, price=50.0, pivot_price=49.0, stop_loss=46.0)])
    job(shadow, prod, Bars({D2: {"AAA": BAR_A2}}), final(D2))
    scan(prod, utc(D3, 21, 30), [])
    job(shadow, prod, Bars({}), evening(D3))
    to_ab0006(shadow.journal_path)
    return shadow


def test_new_journal_and_older_revisions_upgrade_and_rerun_is_idempotent(menv):
    j = str(menv / "new.sqlite3")
    assert M.run(j, str(menv / "bk"), allow_create=True)["result"] == "OK"
    assert M.run(j, str(menv / "bk"))["result"] == "ALREADY_CURRENT"
    for start in ("ab0001", "ab0006"):
        p = str(menv / f"{start}.sqlite3")
        _to(p, start)
        os.chmod(p, 0o600)
        assert M.run(p, str(menv / "bk"))["schema_version"] == EXPECTED_SCHEMA_VERSION
        dump = _dump(p)
        assert M.run(p, str(menv / "bk"))["result"] == "ALREADY_CURRENT" and _dump(p) == dump


def test_clean_ab0006_and_ab0007_ledgers_pass_raw_contract(ledger06, menv):
    s = ledger06
    before = data(s.journal_path)
    rep = M.run(s.journal_path, str(menv / "bk"))
    assert rep["result"] == "OK" and rep["raw_contract_issues"] == [] and data(s.journal_path) == before
    rep = M.run(s.journal_path, str(menv / "bk"))                             # ab0007 → 최신 (이미 최신)
    assert rep["result"] == "ALREADY_CURRENT" and rep["raw_contract_issues"] == []


def test_contaminated_ab0006_check_mode_and_direct_upgrade_are_refused(ledger06, menv):
    s = ledger06
    contaminate(s.journal_path, STOP_EDIT)
    os.chmod(s.journal_path, 0o600)
    dump = _dump(s.journal_path)
    with pytest.raises(M.Abort) as ei:
        M.run(s.journal_path, str(menv / "bk"), check=True)
    assert ei.value.code == 2 and _dump(s.journal_path) == dump
    reports = [json.loads(p.read_text()) for p in (menv / "bk").glob("*.json")]
    assert any(r.get("result") == "RAW_CONTRACT_FAILED" and r["raw_contract_issues"] for r in reports)
    # 배포 스크립트를 거치지 않은 직접 upgrade 도 같은 트랜잭션의 사전 검사가 막는다 (ab0007 backfill 전에 롤백)
    with pytest.raises(Exception, match="원본 row_json"):
        upgrade_journal(s.journal_path)
    assert _dump(s.journal_path) == dump and _version(s.journal_path) == "ab0006"
    assert not schema_status(s.journal_path)["schema_ready"]
