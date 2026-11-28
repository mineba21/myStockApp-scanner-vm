"""PR #14 Codex 재리뷰(head d95beab) 결함 재현 — d95beab 와 현재 코드에 모두 있는 API 만 쓴다.

각 테스트는 수정 후 기대 동작을 단언하므로 d95beab 에서는 동작 단언으로 실패해야 한다 (모듈·API 부재가 아님).
- P1: 적격 판정 집합 완전성 (B00 만·B01 만 누락, 과거 버전 판정만 남음, 정상 부적격 판정 누락)
- P2-2: 후보 검증 이후 정규화 값·적격 판정 변경 (첫 계좌 전·일부 계좌 후·CURRENT 승격 직전)
- P2-3: ab0006 오염 저널 Migration 거부, ab0007 backfill 로 봉인된 오염 저널의 런타임 거부
오염 상태는 관리자 DDL(트리거 제거 후 복원)로 만든다. 임시 SHADOW 저널만 쓴다 (운영 DB·주문·운영 Migration 없음).
"""
import json
import os
import sqlite3

import pytest

from scripts import auto_buy_shadow_journal_migrate as M
from tests.test_auto_buy_shadow import D1, D2, D3, Bars, job, prod, scan, shadow, sig, utc  # noqa: F401
from tests.test_auto_buy_shadow_integrity import BAR_A2, BAR_A3, BAR_B2, BAR_B3, BAR_C3, evening, final, one, q
from tests.test_auto_buy_shadow_review5_s5 import _mode, _to, menv  # noqa: F401
from tests.test_auto_buy_shadow_review8_migration import data, to_ab0006
from tests.test_auto_buy_shadow_review8_signal_set import contaminate, decision
from trading.auto_buy import settings as S
from trading.auto_buy.shadow import engine as EN
from trading.auto_buy.shadow import inputs as IN
from trading.auto_buy.shadow import integrity as IG
from trading.auto_buy.shadow import snapshot as SN
from trading.auto_buy.shadow.journal_migrations import ddl_v7
from trading.auto_buy.shadow.schema import schema_status
from trading.auto_buy.shadow.store import connect_rw, get_runtime


RECOVERED = {D2: {"BBB": BAR_B2}, D3: {"AAA": BAR_A3, "BBB": BAR_B3, "CCC": BAR_C3}}


def _runs(shadow):
    return {r["run_id"]: r["status"] for r in q(shadow, "SELECT run_id, status FROM ab_runs")}


def _current(shadow):
    return one(shadow, "SELECT run_id FROM ab_runs WHERE status='CURRENT'")["run_id"]


def _processed(shadow, prod, extra_d1=()):
    """D1 결정(AAA·BBB + extra) → D1 처리·주문, D2 처리(BBB 결측 → 재계산 요청), D3 빈 결정 스캔."""
    scan(prod, utc(D1, 21, 30), [sig("AAA"), sig("BBB"), *extra_d1])
    job(shadow, prod, Bars({}), evening(D1))
    scan(prod, utc(D2, 21, 30), [sig("CCC", signal_date=D2, price=50.0, pivot_price=49.0, stop_loss=46.0)])
    job(shadow, prod, Bars({D2: {"AAA": BAR_A2}}), final(D2))
    scan(prod, utc(D3, 21, 30), [])
    return decision(shadow, D1)["decision_scan_log_id"]


def _elig_sql(sid, ticker, pools):
    pl = ",".join(f"'{p}'" for p in pools)
    return (f"DELETE FROM ab_eligibility WHERE pool IN ({pl}) AND strategy_version='{S.STRATEGY_VERSION}' AND snapshot_id "
            f"IN (SELECT id FROM ab_signal_snapshots WHERE scan_log_id={sid} AND ticker='{ticker}')")


def _orders(shadow, run, ticker, pool_like):
    return q(shadow, "SELECT * FROM ab_orders WHERE run_id=? AND ticker=? AND account_id LIKE ? ORDER BY id",
             run, ticker, f"%:{pool_like}%")


# ── P1: 적격 판정 집합 완전성 ────────────────────────────────────────────────

@pytest.mark.parametrize("missing,pool_like", [(("B00",), "B00"), (("B01",), "B01")])
def test_repro_partial_eligibility_is_detected_in_replay_and_current_is_kept(shadow, prod, missing, pool_like):
    """기존 CURRENT 에 B00(B01) 주문 → 그 판정만 제거(다른 풀 유지) → replay: 불완전 탐지, CURRENT·주문 보존, 전환 없음."""
    sid = _processed(shadow, prod)
    cur = _current(shadow)
    before = _orders(shadow, cur, "AAA", pool_like)
    assert before                                                   # 기존 CURRENT 에 해당 풀 주문 존재
    contaminate(shadow.journal_path, _elig_sql(sid, "AAA", missing))
    out = job(shadow, prod, Bars(RECOVERED), utc(D3, 22, 0))
    assert out.get("error") == "SignalSetIntegrityError", out
    runs = _runs(shadow)
    assert runs[cur] == "CURRENT" and list(runs.values()).count("CURRENT") == 1      # 새 CURRENT 전환 없음
    assert _orders(shadow, cur, "AAA", pool_like) == before                         # 기존 주문 보존
    ev = q(shadow, "SELECT details FROM ab_events WHERE kind='signal_set_integrity'")
    assert ev and "ELIGIBILITY" in ev[0]["details"]


def test_repro_old_version_only_eligibility_is_not_regenerated(shadow, prod):
    """사용된 결정 스캔의 현재 버전 판정이 모두 없어지고 과거 버전 판정만 남음 → 자동 재생성으로 숨기지 않는다."""
    sid = _processed(shadow, prod)
    cur = _current(shadow)
    before = q(shadow, "SELECT * FROM ab_orders WHERE run_id=? ORDER BY id", cur)
    old = (f"INSERT INTO ab_eligibility(snapshot_id, pool, strategy_version, eligible, reasons, decision_role, "
           f"logic_hash, created_at) SELECT snapshot_id, pool, 'SV0_OLD', eligible, reasons, decision_role, logic_hash, "
           f"created_at FROM ab_eligibility WHERE snapshot_id IN (SELECT id FROM ab_signal_snapshots WHERE scan_log_id={sid})")
    drop = (f"DELETE FROM ab_eligibility WHERE strategy_version='{S.STRATEGY_VERSION}' AND snapshot_id IN "
            f"(SELECT id FROM ab_signal_snapshots WHERE scan_log_id={sid})")
    contaminate(shadow.journal_path, old, drop)
    out = job(shadow, prod, Bars(RECOVERED), utc(D3, 22, 0))
    assert out.get("error") == "SignalSetIntegrityError", out
    assert not q(shadow, "SELECT 1 FROM ab_eligibility e JOIN ab_signal_snapshots s ON s.id=e.snapshot_id "
                         "WHERE s.scan_log_id=? AND e.strategy_version=?", sid, S.STRATEGY_VERSION)   # 재생성 없음
    runs = _runs(shadow)
    assert runs[cur] == "CURRENT" and list(runs.values()).count("CURRENT") == 1
    assert q(shadow, "SELECT * FROM ab_orders WHERE run_id=? ORDER BY id", cur) == before


def test_repro_missing_ineligible_judgement_differs_from_valid_ineligible(shadow, prod):
    """eligible=0 정상 부적격 판정은 유효(정상 처리), 그 판정의 누락은 불완전으로 탐지."""
    sid = _processed(shadow, prod, extra_d1=[sig("NNN", strict_filter_passed=False)])
    n = one(shadow, "SELECT COUNT(*) n FROM ab_eligibility e JOIN ab_signal_snapshots s ON s.id=e.snapshot_id "
                    "WHERE s.ticker='NNN' AND e.eligible=0")["n"]
    assert n == 2                                                   # 정상 부적격 판정 B00·B01
    assert not q(shadow, "SELECT 1 FROM ab_events WHERE kind='signal_set_integrity'")
    cur = _current(shadow)
    contaminate(shadow.journal_path, _elig_sql(sid, "NNN", ("B00",)))
    out = job(shadow, prod, Bars(RECOVERED), utc(D3, 22, 0))
    assert out.get("error") == "SignalSetIntegrityError", out
    assert _runs(shadow)[cur] == "CURRENT"


# ── P2-2: 검증 이후 변경 · 계좌별 쓰기 ────────────────────────────────────────

def _built(shadow, prod):
    sid0 = scan(prod, utc(D1, 21, 30), [sig("AAA"), sig("BBB")])
    assert SN.capture_scan_log(sid0, source="HOOK", settings=shadow, session_factory=prod)["frozen"]
    db = connect_rw(shadow.journal_path)
    EN.ensure_accounts(db, D1, shadow)
    run = EN.ensure_run(db, shadow)
    EN.record_eligibility(db)
    inp = IN.build_inputs(db, run, D1, {}, shadow)
    assert inp.cands["B00"]
    return db, run, inp


def _counts(db):
    return {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("ab_orders", "ab_fill_ledger", "ab_positions", "ab_daily", "ab_days", "ab_decisions",
                      "ab_reservations")}


STOP_EDIT = "UPDATE ab_signal_snapshots SET stop_loss=stop_loss*0.99 WHERE id=(SELECT MIN(id) FROM ab_signal_snapshots)"


@pytest.mark.parametrize("edit", ["stop_loss", "eligibility"])
def test_repro_change_after_verification_before_first_account_commits_nothing(shadow, prod, edit):
    db, run, inp = _built(shadow, prod)
    try:
        before = _counts(db)
        n0 = db.execute("SELECT COUNT(*) FROM ab_signal_snapshots").fetchone()[0]
        sql = STOP_EDIT if edit == "stop_loss" else _elig_sql(inp.decision_scan_log_id, "AAA", ("B00",))
        contaminate(shadow.journal_path, sql)
        assert db.execute("SELECT COUNT(*) FROM ab_signal_snapshots").fetchone()[0] == n0      # 구성원 수 유지
        with pytest.raises(IG.SignalSetIntegrityError):
            IN.process_day(db, run, inp, shadow)
        assert _counts(db) == before                     # 주문·체결·포지션·ab_daily·ab_days 신규 커밋 없음
    finally:
        db.close()


def test_repro_change_after_some_accounts_quarantines_day(shadow, prod):
    db, run, inp = _built(shadow, prod)
    accounts = [a[0] for a in S.account_ids()]
    k = 5
    seen = {"n": 0}

    def on_account():
        if seen["n"] == k:
            contaminate(shadow.journal_path, STOP_EDIT)
        seen["n"] += 1
    try:
        with pytest.raises(IG.SignalSetIntegrityError):
            IN.process_day(db, run, inp, shadow, on_account=on_account)
        done = [r[0] for r in db.execute("SELECT account_id FROM ab_daily WHERE run_id=? AND day=? ORDER BY rowid",
                                         (run, D1))]
        assert done == accounts[:k]                       # 앞선 계좌 기록은 보존(삭제 없음), 이후 계좌는 기록 없음
        assert not db.execute("SELECT 1 FROM ab_days WHERE day=?", (D1,)).fetchone()      # COMPLETE 아님
        assert not db.execute("SELECT 1 FROM ab_orders WHERE account_id IN (%s)" % ",".join("?" * len(accounts[k:])),
                              accounts[k:]).fetchone()
        blk = get_runtime(db, "integrity_day_block")
        assert blk and blk["day"] == D1 and blk["run"] == run and blk["accounts_committed"] == accounts[:k]
    finally:
        db.close()


def test_repro_change_right_before_current_switch_blocks_promotion(shadow, prod, monkeypatch):
    sid = _processed(shadow, prod)
    cur = _current(shadow)
    real = IN._diff

    def diff_then_drop(db, old, new):
        out = real(db, old, new)
        contaminate(shadow.journal_path, _elig_sql(sid, "AAA", ("B00",)))
        return out
    monkeypatch.setattr(IN, "_diff", diff_then_drop)
    out = job(shadow, prod, Bars(RECOVERED), utc(D3, 22, 0))
    assert out.get("error") == "SignalSetIntegrityError", out
    runs = _runs(shadow)
    assert runs[cur] == "CURRENT" and "FAILED" in runs.values()
    ev = one(shadow, "SELECT details FROM ab_events WHERE kind='signal_set_integrity'")
    assert json.loads(ev["details"])["where"] == "replay_current_switch"


# ── P2-3: 원본 ↔ 정규화 필드 (Migration·봉인) ──────────────────────────────────

@pytest.fixture
def ledger06(shadow, prod):
    scan(prod, utc(D1, 21, 30), [sig("AAA"), sig("BBB")])
    job(shadow, prod, Bars({}), evening(D1))
    scan(prod, utc(D2, 21, 30), [sig("CCC", signal_date=D2, price=50.0, pivot_price=49.0, stop_loss=46.0)])
    job(shadow, prod, Bars({D2: {"AAA": BAR_A2}}), final(D2))
    to_ab0006(shadow.journal_path)
    return shadow


def _version(path):
    db = sqlite3.connect(path)
    try:
        return db.execute("SELECT version_num FROM ab_alembic_version").fetchone()[0]
    finally:
        db.close()


def _dump(path):
    db = sqlite3.connect(path)
    try:
        return list(db.iterdump())
    finally:
        db.close()


def test_repro_ab0006_stop_loss_only_contamination_is_refused_by_migration(ledger06, menv):
    s = ledger06
    snap = q(s, "SELECT id, row_json, row_sha256 FROM ab_signal_snapshots ORDER BY id")
    caps = q(s, "SELECT scan_log_id, rows_captured, strict_rows FROM ab_scan_captures ORDER BY scan_log_id")
    contaminate(s.journal_path, STOP_EDIT)
    os.chmod(s.journal_path, 0o600)
    assert q(s, "SELECT id, row_json, row_sha256 FROM ab_signal_snapshots ORDER BY id") == snap     # 원본·해시 유지
    assert q(s, "SELECT scan_log_id, rows_captured, strict_rows FROM ab_scan_captures ORDER BY scan_log_id") == caps
    dump = _dump(s.journal_path)
    with pytest.raises(M.Abort) as ei:
        M.run(s.journal_path, str(menv / "bk"))
    assert ei.value.code in (2, 3)
    assert _dump(s.journal_path) == dump and _version(s.journal_path) == "ab0006"      # 전체 덤프·리비전 보존
    assert not schema_status(s.journal_path)["schema_ready"]


def _backfill_like_d95beab(path):
    """d95beab 의 ab0007 이 하던 그대로: 사전 검사(건수·row_sha256·v1)만 보고 현재 정규화 값을 봉인 (오염 봉인 재현)."""
    db = sqlite3.connect(path, isolation_level=None)
    try:
        assert ddl_v7.precheck(db) == []                 # d95beab 사전 검사는 정규화 필드 오염을 보지 못한다
        db.execute("BEGIN")
        seals = ddl_v7.backfill_seal_rows(db, 1.0)
        for stmt in ddl_v7.DDL_V7:
            db.execute(stmt)
        for row in seals:
            db.execute(ddl_v7.SEAL_INSERT, {**row, "source": "AB0007_BACKFILL"})
        db.execute("UPDATE ab_alembic_version SET version_num='ab0007'")
        db.execute("COMMIT")
    finally:
        db.close()
    os.chmod(path, 0o600)


def test_repro_ab0007_backfill_sealed_contamination_is_refused(ledger06, prod, menv):
    s = ledger06
    contaminate(s.journal_path, STOP_EDIT)
    _backfill_like_d95beab(s.journal_path)
    assert schema_status(s.journal_path)["schema_ready"]
    dump = _dump(s.journal_path)
    with pytest.raises(M.Abort):                         # 이미 최신이어도 원본 정합성 검사
        M.run(s.journal_path, str(menv / "bk"))
    assert _dump(s.journal_path) == dump
    cur = _current(s)
    orders = q(s, "SELECT * FROM ab_orders ORDER BY id")
    out = job(s, prod, Bars(RECOVERED), utc(D3, 22, 0))  # 런타임: 봉인이 일치해도 원본 대조로 사용 거부
    assert out.get("error") == "SignalSetIntegrityError", out
    assert _runs(s)[cur] == "CURRENT" and q(s, "SELECT * FROM ab_orders WHERE run_id=? ORDER BY id", cur) == \
        [o for o in orders if o["run_id"] == cur]
    assert "RAW" in one(s, "SELECT details FROM ab_events WHERE kind='signal_set_integrity'")["details"]
