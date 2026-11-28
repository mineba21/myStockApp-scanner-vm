"""PR #14 재리뷰(head a183b99) 결함 재현 — a183b99 와 현재 코드에 모두 있는 API 만 쓴다.
각 테스트는 수정 후 기대 동작을 단언하므로 a183b99 에서는 동작 단언으로 실패해야 한다 (모듈·API 부재가 아님).
``test_repro_late_rescan_with_other_id_is_kept`` 는 결함이 아닌 기존 정책 회귀 확인 (양쪽 통과)."""
import os
import sqlite3
import threading
import time

import pytest

from scripts import auto_buy_shadow_journal_migrate as M
from tests.test_auto_buy_shadow import D1, D2, D3, Bars, job, prod, scan, shadow, sig, utc  # noqa: F401
from tests.test_auto_buy_shadow_integrity import BAR_A2, BAR_A3, BAR_B2, BAR_B3, BAR_C3, evening, final, one, q
from tests.test_auto_buy_shadow_review5_s5 import _sha, _to, menv  # noqa: F401
from trading.auto_buy.shadow import runner as RN
from trading.auto_buy.shadow import snapshot as SN
from trading.auto_buy.shadow.schema import schema_status
from trading.auto_buy.shadow.store import connect_rw, get_runtime, set_runtime, tx


def _conn(kind, path):
    return connect_rw(path) if kind == "product" else sqlite3.connect(path, isolation_level=None, timeout=10)


def _copy(path, sid, day=None, ticker="EVIL"):
    db = sqlite3.connect(path)
    cols = [r[1] for r in db.execute("PRAGMA table_info(ab_signal_snapshots)")]
    db.close()
    over = {"id": "NULL", "signal_key": f"'US:{ticker}:BREAKOUT:2026-11-20'", "ticker": f"'{ticker}'"}
    return (f"INSERT INTO ab_signal_snapshots({', '.join(cols)}) SELECT {', '.join(over.get(c, c) for c in cols)} "
            f"FROM ab_signal_snapshots WHERE scan_log_id={sid} LIMIT 1")


def _members(path, sid):
    db = sqlite3.connect(path)
    try:
        return db.execute("SELECT * FROM ab_signal_snapshots WHERE scan_log_id=? ORDER BY id", (sid,)).fetchall()
    finally:
        db.close()


def contaminated(path, sql):
    """이미 오염된 저널을 만든다: 직접 INSERT 가 되면 그대로(수정 전), 막히면 관리자 DDL 로 트리거를 우회해 만든다(수정 후)."""
    db = sqlite3.connect(path, isolation_level=None)
    try:
        try:
            db.execute(sql)
            return
        except sqlite3.IntegrityError:
            pass
        trig = db.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger'").fetchall()
        db.execute("BEGIN")
        for n, _ in trig:
            db.execute(f"DROP TRIGGER {n}")
        db.execute(sql)
        for _, s_ in trig:
            db.execute(s_)
        db.execute("COMMIT")
    finally:
        db.close()


def _freeze_d1(shadow, prod, rows):
    scan(prod, utc(D1, 21, 30), rows)
    job(shadow, prod, Bars({}), evening(D1))
    di = one(shadow, "SELECT * FROM ab_day_inputs WHERE day=?", D1)
    assert di["decision_status"] == "FROZEN"
    return di["decision_scan_log_id"]


# (1) FROZEN 결정 스캔에 사후 신호 추가
@pytest.mark.parametrize("kind", ["product", "raw"])
@pytest.mark.parametrize("verb", ["INSERT", "INSERT OR IGNORE"])
def test_repro_frozen_decision_scan_accepts_post_insert(shadow, prod, kind, verb):
    sid = _freeze_d1(shadow, prod, [sig("AAA"), sig("BBB")])
    before = _members(shadow.journal_path, sid)
    db = _conn(kind, shadow.journal_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):                       # 기대: 동결 집합에 추가 거부
            db.execute(_copy(shadow.journal_path, sid).replace("INSERT", verb, 1))
    finally:
        db.close()
    assert _members(shadow.journal_path, sid) == before


# (2) 빈 결정 스캔에 사후 신호 추가
@pytest.mark.parametrize("kind", ["product", "raw"])
def test_repro_empty_decision_scan_accepts_post_insert(shadow, prod, kind):
    sid = _freeze_d1(shadow, prod, [])
    other = scan(prod, utc(D2, 21, 30), [sig("ZZZ")])
    job(shadow, prod, Bars({}), evening(D2))
    db = sqlite3.connect(shadow.journal_path)
    cols = [r[1] for r in db.execute("PRAGMA table_info(ab_signal_snapshots)")]
    db.close()
    sel = ", ".join("NULL" if c == "id" else str(sid) if c == "scan_log_id" else f"'{D1}'" if c == "trading_day" else c
                    for c in cols)
    db = _conn(kind, shadow.journal_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(f"INSERT INTO ab_signal_snapshots({', '.join(cols)}) SELECT {sel} FROM ab_signal_snapshots "
                       f"WHERE scan_log_id={other}")
    finally:
        db.close()
    assert _members(shadow.journal_path, sid) == []


# (3) 이미 오염된 기존 저널에서 신규 거래일 처리
def test_repro_contaminated_journal_new_day_processing(shadow, prod):
    scan(prod, utc(D1, 21, 30), [sig("AAA"), sig("BBB")])
    job(shadow, prod, Bars({}), evening(D1))
    scan(prod, utc(D2, 21, 30), [sig("CCC", signal_date=D2, price=50.0, pivot_price=49.0, stop_loss=46.0)])
    job(shadow, prod, Bars({}), evening(D2))
    sid = one(shadow, "SELECT decision_scan_log_id FROM ab_day_inputs WHERE day=?", D2)["decision_scan_log_id"]
    contaminated(shadow.journal_path, _copy(shadow.journal_path, sid))
    job(shadow, prod, Bars({D2: {"AAA": BAR_A2, "BBB": BAR_B2}}), final(D2))
    assert not q(shadow, "SELECT 1 FROM ab_orders WHERE ticker='EVIL'")           # 기대: 오염 신호로 주문 없음
    assert not q(shadow, "SELECT 1 FROM ab_orders WHERE decision_day=?", D2)       # 그날 신규 주문 없음
    assert not q(shadow, "SELECT 1 FROM ab_days WHERE day=?", D2)                  # 처리 중단
    assert q(shadow, "SELECT 1 FROM ab_events WHERE kind='signal_set_integrity' AND severity='ERROR'")


# (4) 불일치 상태에서 replay → 기존 CURRENT 보존
def test_repro_contaminated_journal_replay_replaces_current(shadow, prod):
    scan(prod, utc(D1, 21, 30), [sig("AAA"), sig("BBB")])
    job(shadow, prod, Bars({}), evening(D1))
    scan(prod, utc(D2, 21, 30), [sig("CCC", signal_date=D2, price=50.0, pivot_price=49.0, stop_loss=46.0)])
    job(shadow, prod, Bars({D2: {"AAA": BAR_A2}}), final(D2))                     # BBB 결측 → 복구·replay 대상
    scan(prod, utc(D3, 21, 30), [])
    cur = one(shadow, "SELECT run_id FROM ab_runs WHERE status='CURRENT'")["run_id"]
    orders = q(shadow, "SELECT * FROM ab_orders WHERE run_id=? ORDER BY id", cur)
    sid = one(shadow, "SELECT decision_scan_log_id FROM ab_day_inputs WHERE day=?", D2)["decision_scan_log_id"]
    contaminated(shadow.journal_path, _copy(shadow.journal_path, sid))
    job(shadow, prod, Bars({D2: {"BBB": BAR_B2}, D3: {"AAA": BAR_A3, "BBB": BAR_B3, "CCC": BAR_C3}}), utc(D3, 22, 0))
    assert one(shadow, "SELECT run_id FROM ab_runs WHERE status='CURRENT'")["run_id"] == cur   # 기대: CURRENT 유지
    assert q(shadow, "SELECT * FROM ab_orders WHERE run_id=? ORDER BY id", cur) == orders
    assert not q(shadow, "SELECT 1 FROM ab_orders WHERE ticker='EVIL'")


# (5) 동결 직전·직후 동시 INSERT 경쟁
def test_repro_concurrent_insert_around_freeze(shadow, prod, monkeypatch):
    sid = scan(prod, utc(D1, 21, 30), [sig("AAA"), sig("BBB")])
    inside, release = threading.Event(), threading.Event()
    real = SN._first_pass

    def slow(*a, **k):
        inside.set()
        release.wait(10)
        return real(*a, **k)
    monkeypatch.setattr(SN, "_first_pass", slow)
    t = threading.Thread(target=lambda: SN.capture_scan_log(sid, source="HOOK", settings=shadow, session_factory=prod))
    t.start()
    assert inside.wait(10)
    result = []

    def racer():
        db = sqlite3.connect(shadow.journal_path, isolation_level=None, timeout=10)
        try:
            db.execute("INSERT INTO ab_signal_snapshots(scan_log_id, scan_result_id, signal_key, market, ticker, "
                       "signal_type, signal_date, trading_day, scan_status, is_first_detection, strict_passed, "
                       "first_strict_pass, first_pass_basis, row_sha256, row_json, capture_source, created_at) VALUES "
                       "(?,999,'US:EVIL:BREAKOUT:2026-11-20','US','EVIL','BREAKOUT','2026-11-20',?,'DONE',1,1,1,"
                       "'FIRST_DETECTION','x','{}','FORGED',0)", (sid, D1))
            result.append("inserted")
        except sqlite3.DatabaseError as exc:
            result.append(str(exc))
        finally:
            db.close()
    u = threading.Thread(target=racer)
    u.start()
    time.sleep(0.3)
    release.set()
    t.join(15)
    u.join(15)
    assert result and result[0] != "inserted", result                            # 기대: 커밋 뒤 동결 집합이라 거부
    assert {r["ticker"] for r in q(shadow, "SELECT ticker FROM ab_signal_snapshots WHERE scan_log_id=?", sid)} == {"AAA", "BBB"}


# (6) 별도 ID 의 정규 결정 이후 재스캔 정상 보존 (기존 정책 — 결함 아님, 양쪽 통과)
def test_repro_late_rescan_with_other_id_is_kept(shadow, prod):
    sid = _freeze_d1(shadow, prod, [sig("AAA"), sig("BBB")])
    late = scan(prod, utc(D1, 21, 45), [sig("ZZZ")])
    job(shadow, prod, Bars({}), evening(D1))
    cap = one(shadow, "SELECT * FROM ab_scan_captures WHERE scan_log_id=?", late)
    assert cap["late_after_decision"] == 1 and cap["rows_captured"] == 1
    assert q(shadow, "SELECT 1 FROM ab_events WHERE kind='LATE_SCAN_AFTER_DECISION'")
    assert one(shadow, "SELECT decision_scan_log_id FROM ab_day_inputs WHERE day=?", D1)["decision_scan_log_id"] == sid


# (7) 수집 건수 0 / 실제 스냅샷 1건 저널의 Migration
def test_repro_migration_accepts_recorded_zero_actual_one(menv):
    j = str(menv / "zero.sqlite3")
    _to(j, "ab0005")
    db = sqlite3.connect(j)
    db.execute("INSERT INTO ab_scan_captures(scan_log_id, scan_market, scan_status, triggered_by, scan_started_at, "
               "trading_day, source, complete, recoverability, captured_at) VALUES "
               "(1,'US','DONE','scheduler','2026-11-23T21:30:00','2026-11-23','HOOK',1,'FULL',0)")
    db.execute("INSERT INTO ab_signal_snapshots(scan_log_id, scan_result_id, signal_key, market, ticker, signal_type, "
               "signal_date, trading_day, scan_status, is_first_detection, strict_passed, first_pass_basis, row_sha256, "
               "row_json, capture_source, created_at) VALUES (1,1,'k','US','AAA','BREAKOUT','2026-11-20','2026-11-23',"
               "'DONE',1,1,'FIRST_DETECTION','x','{}','HOOK',0)")
    db.commit()
    db.close()
    os.chmod(j, 0o600)
    sha = _sha(j)
    with pytest.raises(M.Abort):                                                   # 기대: 사전 검사 거부
        M.run(j, str(menv / "bk"))
    assert schema_status(j)["schema_version"] == "ab0005" and _sha(j) == sha


# (8) 두 독립 연결의 동시 429 집계
def test_repro_concurrent_rate_limit_lost_update(shadow, monkeypatch):
    db = connect_rw(shadow.journal_path)
    with tx(db):
        set_runtime(db, "yahoo_rate_limit", {"consecutive_jobs": 1, "last_limited_at": 1.0, "warned": False})
    db.close()
    barrier = threading.Barrier(2, timeout=1.0)
    real = RN.get_runtime

    def meet(db, key, *a, **k):
        v = real(db, key, *a, **k)
        if key == "yahoo_rate_limit":
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
        return v
    monkeypatch.setattr(RN, "get_runtime", meet)

    def work():
        c = connect_rw(shadow.journal_path)
        try:
            RN._record_rate_limit(c, {"requests": 1, "halted": "RATE_LIMITED", "contract_ok": 0})
        finally:
            c.close()
    ts = [threading.Thread(target=work) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    monkeypatch.setattr(RN, "get_runtime", real)
    db = connect_rw(shadow.journal_path)
    try:
        assert get_runtime(db, "yahoo_rate_limit")["consecutive_jobs"] == 3              # 기대: 유실 없음
        assert db.execute("SELECT COUNT(*) FROM ab_events WHERE kind='yahoo_rate_limited_repeated'").fetchone()[0] == 1
    finally:
        db.close()
