"""PR #14 Codex 재리뷰(head a183b99) 반영 (1) — 동결 신호 집합 불변성 (P1).

- DB: FROZEN 결정 스캔·봉인된 수집에 스냅샷 추가 거부 (모든 쓰기 방식 × 제품·기본 연결), 관계·건수·동결 조건.
- 동결 시점: 수집·건수·봉인·동결이 한 트랜잭션, 동결 직전·직후 다른 연결의 INSERT 경쟁.
- 엔진: 공통 무결성 검증 — 오염된 저널(관리자 DDL 로 트리거를 우회해 만든 상태)에서 신규 거래일 처리·replay·CURRENT 전환
  차단, 기존 CURRENT·원장 보존, 감사 이벤트·MONITOR·S5 반영, 자동 정상화 없음.
임시 SHADOW 저널만 쓴다 (운영 DB·주문 없음).
"""
import json
import sqlite3
import threading
import time

import pytest

from tests.test_auto_buy_shadow import ACCT, D1, D2, D3, Bars, job, prod, scan, shadow, sig, utc  # noqa: F401
from tests.test_auto_buy_shadow_integrity import BAR_A2, BAR_A3, BAR_B2, BAR_B3, BAR_C3, evening, final, one, q
from trading.auto_buy import settings as S
from trading.auto_buy.shadow import engine as EN
from trading.auto_buy.shadow import inputs as IN
from trading.auto_buy.shadow import integrity as IG
from trading.auto_buy.shadow import report as RP
from trading.auto_buy.shadow import snapshot as SN
from trading.auto_buy.shadow.journal_migrations import ddl_v7
from trading.auto_buy.shadow.store import connect_rw, set_runtime, tx

PROTECT = ("contract set", "contract relation", "contract insert")


def _conn(kind, path):
    if kind == "product":
        return connect_rw(path)
    db = sqlite3.connect(path, isolation_level=None, timeout=10)
    db.row_factory = sqlite3.Row
    assert db.execute("PRAGMA recursive_triggers").fetchone()[0] == 0 and db.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    return db


def state(path, sid):
    """집합 상태: 구성원 행, v1 해시, SSV2, 수집 건수, 봉인, 적격 판정."""
    db = sqlite3.connect(path)
    try:
        return (db.execute("SELECT * FROM ab_signal_snapshots WHERE scan_log_id=? ORDER BY id", (sid,)).fetchall(),
                ddl_v7.signal_set_hash_v1(db, sid), ddl_v7.member_digest(db, sid),
                db.execute("SELECT rows_captured, strict_rows FROM ab_scan_captures WHERE scan_log_id=?", (sid,)).fetchone(),
                db.execute("SELECT * FROM ab_capture_seals WHERE scan_log_id=?", (sid,)).fetchone(),
                db.execute("SELECT * FROM ab_eligibility ORDER BY snapshot_id, pool").fetchall())
    finally:
        db.close()


def decision(shadow, day):
    return one(shadow, "SELECT * FROM ab_day_inputs WHERE day=?", day)


@pytest.fixture
def d1(shadow, prod):
    scan(prod, utc(D1, 21, 30), [sig("AAA"), sig("BBB")])
    job(shadow, prod, Bars({}), evening(D1))
    di = decision(shadow, D1)
    assert di["decision_status"] == "FROZEN"
    return di["decision_scan_log_id"]


def _cols(path):
    db = sqlite3.connect(path)
    try:
        return [r[1] for r in db.execute("PRAGMA table_info(ab_signal_snapshots)")]
    finally:
        db.close()


def _copy_sql(path, sid, verb="INSERT", suffix="", key="'US:EVIL:BREAKOUT:2026-11-20'", rows=1):
    cols = _cols(path)
    sel = ", ".join("NULL" if c == "id" else key if c == "signal_key" else "'EVIL'" if c == "ticker" else c for c in cols)
    one_ = f"SELECT {sel} FROM ab_signal_snapshots WHERE scan_log_id={sid} AND id=(SELECT MIN(id) FROM ab_signal_snapshots WHERE scan_log_id={sid})"
    body = one_ if rows == 1 else one_ + " UNION ALL " + one_.replace(key, "'US:EVIL2:BREAKOUT:2026-11-20'")
    return f"{verb} INTO ab_signal_snapshots({', '.join(cols)}) {body} {suffix}"


WRITE_OPS = {
    "INSERT": ("INSERT", ""),
    "INSERT OR IGNORE": ("INSERT OR IGNORE", ""),
    "INSERT OR REPLACE": ("INSERT OR REPLACE", ""),
    "REPLACE INTO": ("REPLACE", ""),
    "ON CONFLICT DO NOTHING": ("INSERT", "ON CONFLICT DO NOTHING"),
    "ON CONFLICT DO UPDATE": ("INSERT", "ON CONFLICT(scan_log_id, signal_key) DO UPDATE SET grade=excluded.grade"),
    "multi-row INSERT": ("INSERT", ""),
    "same key OR REPLACE": ("INSERT OR REPLACE", ""),
}


# ── DB: 동결 집합에 구성원 추가 불가 ─────────────────────────────────────────────

@pytest.mark.parametrize("kind", ["product", "raw"])
@pytest.mark.parametrize("op", sorted(WRITE_OPS))
def test_frozen_decision_scan_rejects_new_snapshot(shadow, d1, kind, op):
    before = state(shadow.journal_path, d1)
    verb, suffix = WRITE_OPS[op]
    key = "signal_key" if op == "same key OR REPLACE" else "'US:EVIL:BREAKOUT:2026-11-20'"
    sql = _copy_sql(shadow.journal_path, d1, verb, suffix, key=key, rows=2 if op == "multi-row INSERT" else 1)
    db = _conn(kind, shadow.journal_path)
    try:
        with pytest.raises(sqlite3.IntegrityError) as ei:
            db.execute(sql)
        assert any(p in str(ei.value) for p in PROTECT + ("immutable", "row identity", "conflict")), ei.value
    finally:
        db.close()
    assert state(shadow.journal_path, d1) == before


@pytest.mark.parametrize("kind", ["product", "raw"])
def test_empty_frozen_decision_scan_is_valid_and_rejects_additions(shadow, prod, kind):
    sid = scan(prod, utc(D1, 21, 30), [])
    job(shadow, prod, Bars({}), evening(D1))
    di = decision(shadow, D1)
    assert di["decision_status"] == "FROZEN" and di["decision_scan_log_id"] == sid      # 정상 빈 스캔도 동결 집합
    c = one(shadow, "SELECT * FROM ab_capture_seals WHERE scan_log_id=?", sid)
    assert (c["member_count"], c["strict_count"], c["source"]) == (0, 0, "CAPTURE")
    db = connect_rw(shadow.journal_path)
    try:
        assert IG.decision_set_issues(db, D1) == []
    finally:
        db.close()
    other = scan(prod, utc(D2, 21, 30), [sig("ZZZ")])     # 다른 스캔의 행을 빈 결정 스캔으로 옮겨 넣기 시도
    job(shadow, prod, Bars({}), evening(D2))
    cols = _cols(shadow.journal_path)
    sel = ", ".join("NULL" if c == "id" else str(sid) if c == "scan_log_id" else f"'{D1}'" if c == "trading_day" else c
                    for c in cols)
    db = _conn(kind, shadow.journal_path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="contract set"):
            db.execute(f"INSERT OR IGNORE INTO ab_signal_snapshots({', '.join(cols)}) SELECT {sel} "
                       f"FROM ab_signal_snapshots WHERE scan_log_id={other}")
    finally:
        db.close()
    assert q(shadow, "SELECT COUNT(*) n FROM ab_signal_snapshots WHERE scan_log_id=?", sid)[0]["n"] == 0


def test_later_rescan_with_other_scan_id_is_captured_and_keeps_late_policy(shadow, prod, d1):
    before = state(shadow.journal_path, d1)
    late = scan(prod, utc(D1, 21, 45), [sig("AAA"), sig("ZZZ")])          # 정규 결정 이후 별도 scan_log_id 재스캔
    job(shadow, prod, Bars({}), evening(D1))
    cap = one(shadow, "SELECT * FROM ab_scan_captures WHERE scan_log_id=?", late)
    assert cap["late_after_decision"] == 1 and cap["rows_captured"] == 2
    assert one(shadow, "SELECT * FROM ab_capture_seals WHERE scan_log_id=?", late)["member_count"] == 2
    ev = one(shadow, "SELECT * FROM ab_events WHERE kind='LATE_SCAN_AFTER_DECISION'")
    assert json.loads(ev["details"])["new_strict_tickers"] == ["ZZZ"]
    roles = {r["decision_role"] for r in q(shadow, "SELECT e.decision_role FROM ab_eligibility e JOIN "
                                                     "ab_signal_snapshots s ON s.id=e.snapshot_id WHERE s.scan_log_id=?", late)}
    assert roles == {"LATE_AFTER_DECISION"}
    after = state(shadow.journal_path, d1)
    assert after[:5] == before[:5] and decision(shadow, D1)["decision_scan_log_id"] == d1   # 동결 집합 그대로


@pytest.mark.parametrize("kind", ["product", "raw"])
@pytest.mark.parametrize("case", ["no_capture", "other_day", "intraday_mismatch"])
def test_snapshot_must_belong_to_its_capture(shadow, d1, kind, case):
    cols = _cols(shadow.journal_path)
    over = {"no_capture": {"scan_log_id": "999999"}, "other_day": {"trading_day": "'2026-12-31'"},
            "intraday_mismatch": {"intraday": "1"}}[case]
    sel = ", ".join("NULL" if c == "id" else over.get(c, c) if c in over else "'US:REL:X:1'" if c == "signal_key" else c
                    for c in cols)
    db = _conn(kind, shadow.journal_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(f"INSERT INTO ab_signal_snapshots({', '.join(cols)}) SELECT {sel} FROM ab_signal_snapshots "
                       f"WHERE scan_log_id={d1} LIMIT 1")
    finally:
        db.close()


@pytest.mark.parametrize("kind", ["product", "raw"])
@pytest.mark.parametrize("sql", [
    "UPDATE ab_scan_captures SET rows_captured=rows_captured+1 WHERE scan_log_id={sid}",
    "UPDATE ab_scan_captures SET strict_rows=0 WHERE scan_log_id={sid}",
    "UPDATE ab_capture_seals SET member_count=member_count+1 WHERE scan_log_id={sid}",
    "UPDATE ab_capture_seals SET member_digest=substr(member_digest,2)||'0' WHERE scan_log_id={sid}",
    "DELETE FROM ab_capture_seals WHERE scan_log_id={sid}",
    "INSERT OR REPLACE INTO ab_capture_seals SELECT * FROM ab_capture_seals WHERE scan_log_id={sid}",
    "INSERT INTO ab_eligibility(snapshot_id, pool, strategy_version, eligible, reasons, decision_role, logic_hash, "
    "created_at) VALUES (999999, 'B00', 'SVX', 1, '[]', 'DECISION', 'x', 0)",
])
def test_counts_seal_and_relations_are_fixed(shadow, d1, kind, sql):
    before = state(shadow.journal_path, d1)
    db = _conn(kind, shadow.journal_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(sql.format(sid=d1))
    finally:
        db.close()
    assert state(shadow.journal_path, d1) == before


@pytest.mark.parametrize("kind", ["product", "raw"])
@pytest.mark.parametrize("case", ["unsealed", "incomplete", "other_day", "intraday", "non_regular", "bad_hash"])
def test_frozen_input_insert_requires_sealed_complete_regular_capture(shadow, d1, kind, case):
    db = sqlite3.connect(shadow.journal_path, isolation_level=None)
    cc = [r[1] for r in db.execute("PRAGMA table_info(ab_scan_captures)")]
    over = {"scan_log_id": "scan_log_id + 5000", "trading_day": f"'{D3}'", "rows_captured": "0", "strict_rows": "0",
            "late_after_decision": "0"}
    over.update({"incomplete": {"complete": "0", "recoverability": "'PARTIAL'"}, "intraday": {"intraday": "1"},
                 "non_regular": {"triggered_by": "'manual'"}}.get(case, {}))
    db.execute(f"INSERT INTO ab_scan_captures({', '.join(cc)}) SELECT {', '.join(over.get(c, c) for c in cc)} "
               f"FROM ab_scan_captures WHERE scan_log_id={d1}")
    new = d1 + 5000
    if case != "unsealed":
        n, ns, dg = ddl_v7.member_digest(db, new)
        db.execute(ddl_v7.SEAL_INSERT, {"sid": new, "n": n, "ns": ns, "dg": dg, "source": "CAPTURE", "now": 0})
    db.close()
    day = "2026-12-31" if case == "other_day" else D3
    h = "'short'" if case == "bad_hash" else f"'{'0' * 16}'"
    c = _conn(kind, shadow.journal_path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="contract insert"):
            c.execute(f"INSERT INTO ab_day_inputs(day, decision_status, decision_scan_log_id, frozen_at, frozen_by, "
                      f"signal_set_hash, strategy_version, config_hash, logic_hash) VALUES "
                      f"('{day}','FROZEN',{new},0,'t',{h},'SV1','c','l')")
    finally:
        c.close()


def test_freeze_decision_verifies_set_before_freezing(shadow, d1):
    """동결 함수는 공통 검증(row_sha256·건수·봉인·소속)을 먼저 한다 — 원본 JSON 과 해시가 다른 구성원이 있으면 동결하지 않는다."""
    db = connect_rw(shadow.journal_path)
    try:
        with tx(db):
            cc = [r[1] for r in db.execute("PRAGMA table_info(ab_scan_captures)")]
            over = {"scan_log_id": "scan_log_id + 7000", "trading_day": f"'{D3}'", "rows_captured": "0", "strict_rows": "0"}
            db.execute(f"INSERT INTO ab_scan_captures({', '.join(cc)}) SELECT {', '.join(over.get(c, c) for c in cc)} "
                       f"FROM ab_scan_captures WHERE scan_log_id={d1}")
            cols = _cols(shadow.journal_path)
            sel = ", ".join("NULL" if c == "id" else "scan_log_id + 7000" if c == "scan_log_id" else f"'{D3}'"
                            if c == "trading_day" else "'{\"tampered\": 1}'" if c == "row_json" else c for c in cols)
            db.execute(f"INSERT INTO ab_signal_snapshots({', '.join(cols)}) SELECT {sel} FROM ab_signal_snapshots "
                       f"WHERE scan_log_id={d1}")
            db.execute("UPDATE ab_scan_captures SET rows_captured=2, strict_rows=2 WHERE scan_log_id=?", (d1 + 7000,))
            SN.seal_capture(db, d1 + 7000, 0)
            with pytest.raises(IG.SignalSetIntegrityError, match="ROW_SHA_MISMATCH"):
                SN.freeze_decision(db, D3, d1 + 7000, "t", shadow, "l", 0)
            assert db.execute("SELECT 1 FROM ab_day_inputs WHERE day=?", (D3,)).fetchone() is None
            db.execute("ROLLBACK")
            db.execute("BEGIN IMMEDIATE")          # tx 종료 처리용
    finally:
        db.close()


# ── 동결 직전·직후 경쟁 ───────────────────────────────────────────────────────

def test_concurrent_insert_during_capture_waits_and_is_rejected_after_freeze(shadow, prod, monkeypatch):
    """결정 스캔 수집(스냅샷 → 건수 → 봉인 → 동결)이 한 BEGIN IMMEDIATE 트랜잭션이다. 그 사이 다른 연결의 INSERT 는 잠금을
    기다렸다가, 커밋 뒤에는 봉인·동결된 집합이라 거부된다. 불완전하거나 바뀐 집합이 확정되지 않는다."""
    sid = scan(prod, utc(D1, 21, 30), [sig("AAA"), sig("BBB")])
    inside, release = threading.Event(), threading.Event()
    real = SN._first_pass

    def slow(*a, **k):
        inside.set()
        release.wait(10)
        return real(*a, **k)
    monkeypatch.setattr(SN, "_first_pass", slow)
    res = {}
    t = threading.Thread(target=lambda: res.setdefault("cap", SN.capture_scan_log(
        sid, source="HOOK", settings=shadow, session_factory=prod)))
    t.start()
    assert inside.wait(10)
    errs = []

    def racer():
        db = sqlite3.connect(shadow.journal_path, isolation_level=None, timeout=10)
        try:
            db.execute("INSERT INTO ab_signal_snapshots(scan_log_id, scan_result_id, signal_key, market, ticker, "
                       "signal_type, signal_date, trading_day, scan_status, is_first_detection, strict_passed, "
                       "first_strict_pass, first_pass_basis, row_sha256, row_json, capture_source, created_at) VALUES "
                       "(?,?,?,?,?,?,?,?,?,1,1,1,'FIRST_DETECTION',?,?,'FORGED',0)",
                       (sid, 999, "US:EVIL:BREAKOUT:2026-11-20", "US", "EVIL", "BREAKOUT", "2026-11-20", D1, "DONE",
                        "x", "{}"))
        except sqlite3.DatabaseError as exc:
            errs.append(str(exc))
        finally:
            db.close()
    u = threading.Thread(target=racer)
    u.start()
    time.sleep(0.3)
    assert u.is_alive()                       # 수집 트랜잭션이 끝날 때까지 기다린다
    release.set()
    t.join(15)
    u.join(15)
    assert res["cap"]["frozen"] and res["cap"]["rows"] == 2
    assert errs and "contract set" in errs[0], errs
    assert {r["ticker"] for r in q(shadow, "SELECT ticker FROM ab_signal_snapshots WHERE scan_log_id=?", sid)} == {"AAA", "BBB"}
    db = connect_rw(shadow.journal_path)
    try:
        assert IG.decision_set_issues(db, D1) == []
    finally:
        db.close()


def test_insert_before_capture_exists_is_rejected(shadow, prod):
    """수집 기록이 생기기 전에 미리 넣어 두는 경로도 막힌다 (관계 보호). 기본 연결은 외래키가 꺼져 있어도 거부."""
    sid = scan(prod, utc(D1, 21, 30), [sig("AAA")])
    db = sqlite3.connect(shadow.journal_path, isolation_level=None)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="contract relation"):
            db.execute("INSERT INTO ab_signal_snapshots(scan_log_id, scan_result_id, signal_key, market, ticker, "
                       "signal_type, signal_date, trading_day, scan_status, is_first_detection, first_pass_basis, "
                       "row_sha256, row_json, capture_source, created_at) VALUES (?,1,'k','US','EVIL','BREAKOUT',"
                       "'2026-11-20',?,'DONE',1,'FIRST_DETECTION','x','{}','FORGED',0)", (sid, D1))
    finally:
        db.close()
    job(shadow, prod, Bars({}), evening(D1))
    assert decision(shadow, D1)["decision_status"] == "FROZEN"


# ── 엔진: 오염된 저널 ─────────────────────────────────────────────────────────

def contaminate(path, *sqls):
    """관리자 DDL 로 보호 트리거를 내렸다가 같은 정의로 다시 만든다 (이미 오염된 기존 저널 모사 — 스키마 구조는 그대로)."""
    db = sqlite3.connect(path, isolation_level=None)
    try:
        trig = db.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger'").fetchall()
        db.execute("BEGIN")
        for n, _ in trig:
            db.execute(f"DROP TRIGGER {n}")
        for s_ in sqls:
            db.execute(s_)
        for _, sql in trig:
            db.execute(sql)
        db.execute("COMMIT")
    finally:
        db.close()


def forged_member(path, sid, day, ticker="EVIL"):
    cols = _cols(path)
    rj = json.dumps({"forged": ticker})
    import hashlib
    over = {"id": "NULL", "signal_key": f"'US:{ticker}:BREAKOUT:2026-11-20'", "ticker": f"'{ticker}'",
            "row_json": f"'{rj}'", "row_sha256": f"'{hashlib.sha256(rj.encode()).hexdigest()}'"}
    return (f"INSERT INTO ab_signal_snapshots({', '.join(cols)}) SELECT {', '.join(over.get(c, c) for c in cols)} "
            f"FROM ab_signal_snapshots WHERE scan_log_id={sid} AND trading_day='{day}' LIMIT 1")


CONTAMINATIONS = {
    "extra_member": lambda p, sid, day: [forged_member(p, sid, day)],
    "member_field": lambda p, sid, day: [f"UPDATE ab_signal_snapshots SET stop_loss=stop_loss*0.5 WHERE id=(SELECT MIN(id) "
                                         f"FROM ab_signal_snapshots WHERE scan_log_id={sid})"],
    "row_json": lambda p, sid, day: [f"UPDATE ab_signal_snapshots SET row_json='{{}}' WHERE id=(SELECT MIN(id) "
                                     f"FROM ab_signal_snapshots WHERE scan_log_id={sid})"],
    "eligibility": lambda p, sid, day: [f"UPDATE ab_eligibility SET eligible=1-eligible WHERE snapshot_id=(SELECT MIN(id) "
                                        f"FROM ab_signal_snapshots WHERE scan_log_id={sid}) AND pool='B00'"],
    "counts_and_seal": lambda p, sid, day: [forged_member(p, sid, day),
                                            f"UPDATE ab_scan_captures SET rows_captured=rows_captured+1, "
                                            f"strict_rows=strict_rows+1 WHERE scan_log_id={sid}",
                                            f"UPDATE ab_capture_seals SET member_count=member_count+1, "
                                            f"strict_count=strict_count+1 WHERE scan_log_id={sid}"],
}
EXPECT = {"extra_member": "CAPTURE_COUNT_MISMATCH", "member_field": "SSV2_DIGEST_MISMATCH",
          "row_json": "ROW_SHA_MISMATCH", "eligibility": "ELIGIBILITY_MISMATCH", "counts_and_seal": "V1_HASH_MISMATCH"}


def ledger(shadow):
    return {t: q(shadow, f"SELECT * FROM {t} ORDER BY rowid") for t in
            ("ab_runs", "ab_orders", "ab_positions", "ab_daily", "ab_days", "ab_decisions", "ab_fill_ledger",
             "ab_reservations", "ab_account_runs", "ab_eligibility")}


@pytest.mark.parametrize("how", sorted(CONTAMINATIONS))
def test_contaminated_new_day_is_not_processed_and_no_orders(shadow, prod, how):
    scan(prod, utc(D1, 21, 30), [sig("AAA"), sig("BBB")])
    job(shadow, prod, Bars({}), evening(D1))
    scan(prod, utc(D2, 21, 30), [sig("CCC", signal_date=D2, price=50.0, pivot_price=49.0, stop_loss=46.0),
                                 sig("DDD", signal_date=D2, price=60.0, pivot_price=59.0, stop_loss=56.0)])
    job(shadow, prod, Bars({}), evening(D2))                                     # D2 동결 (일봉 대기)
    sid = decision(shadow, D2)["decision_scan_log_id"]
    assert not q(shadow, "SELECT 1 FROM ab_days WHERE day=?", D2)
    contaminate(shadow.journal_path, *CONTAMINATIONS[how](shadow.journal_path, sid, D2))
    before = ledger(shadow)
    out = job(shadow, prod, Bars({D2: {"AAA": BAR_A2, "BBB": BAR_B2}}), final(D2))
    assert out.get("error") == "SignalSetIntegrityError", out
    assert ledger(shadow) == before                                             # 주문·일 기록·판정 추가 없음
    assert not q(shadow, "SELECT 1 FROM ab_orders WHERE ticker='EVIL' OR decision_day=?", D2)
    ev = q(shadow, "SELECT * FROM ab_events WHERE kind='signal_set_integrity'")
    assert len(ev) == 1 and ev[0]["severity"] == "ERROR" and EXPECT[how] in ev[0]["details"]
    job(shadow, prod, Bars({D2: {"AAA": BAR_A2, "BBB": BAR_B2}}), final(D2))   # 다시 실행해도 막힘, 이벤트는 1회
    assert ledger(shadow) == before and len(q(shadow, "SELECT 1 FROM ab_events WHERE kind='signal_set_integrity'")) == 1
    h = RP.health(shadow)
    assert h["signal_set_integrity"]["day"] == D2 and h["signal_set_integrity"]["repeats"] == 2   # MONITOR 표시
    db = sqlite3.connect(shadow.journal_path)
    db.row_factory = sqlite3.Row
    try:
        sv = RP.safety_violations(db, h, shadow)
    finally:
        db.close()
    assert sv["signal_set_integrity"] == 1 and sv["current_unresolved"]["signal_set_integrity"] == 1 and not sv["pass"]
    # 자동 정상화 없음: 해시·건수·봉인 그대로, 오염 행 삭제·재동결 없음
    assert decision(shadow, D2)["decision_scan_log_id"] == sid


def _processed_two_days(shadow, prod):
    scan(prod, utc(D1, 21, 30), [sig("AAA"), sig("BBB")])
    job(shadow, prod, Bars({}), evening(D1))
    scan(prod, utc(D2, 21, 30), [sig("CCC", signal_date=D2, price=50.0, pivot_price=49.0, stop_loss=46.0)])
    job(shadow, prod, Bars({D2: {"AAA": BAR_A2}}), final(D2))                  # BBB 결측 → 복구·replay 대상
    scan(prod, utc(D3, 21, 30), [])


@pytest.mark.parametrize("how", ["extra_member", "member_field", "eligibility"])
def test_contaminated_journal_replay_fails_and_current_is_preserved(shadow, prod, how):
    _processed_two_days(shadow, prod)
    cur = EN.current_run(connect_rw(shadow.journal_path))
    sid = decision(shadow, D2)["decision_scan_log_id"]
    contaminate(shadow.journal_path, *CONTAMINATIONS[how](shadow.journal_path, sid, D2))
    before = {t: [r for r in v if r.get("run_id", cur) == cur] for t, v in ledger(shadow).items() if t != "ab_runs"}
    out = job(shadow, prod, Bars({D2: {"BBB": BAR_B2}, D3: {"AAA": BAR_A3, "BBB": BAR_B3, "CCC": BAR_C3}}),
              utc(D3, 22, 0))
    assert out.get("error") == "SignalSetIntegrityError", out
    runs = {r["run_id"]: r["status"] for r in q(shadow, "SELECT * FROM ab_runs")}
    assert runs[cur] == "CURRENT" and list(runs.values()).count("CURRENT") == 1        # 기존 CURRENT 유지
    where = json.loads(one(shadow, "SELECT details FROM ab_events WHERE kind='signal_set_integrity'")["details"])["where"]
    if how == "extra_member":       # 판정 미기록 구성원 → 적격 판정 기록 전 검증이 먼저 막는다 (재계산 run 을 만들기 전)
        assert where == "record_eligibility" and "FAILED" not in runs.values()
    else:                           # replay 시작 검증(head 1906a3f P1: parent 거래일 전체)이 run 을 만들기 전에 막는다
        assert where == "replay_start" and "FAILED" not in runs.values()
    after = {t: [r for r in v if r.get("run_id", cur) == cur] for t, v in ledger(shadow).items() if t != "ab_runs"}
    assert after["ab_orders"] == before["ab_orders"] and after["ab_daily"] == before["ab_daily"]
    kinds = [r["kind"] for r in q(shadow, "SELECT kind FROM ab_events WHERE severity='ERROR'")]
    assert "signal_set_integrity" in kinds and "replay_failed" not in kinds


def test_contamination_between_build_and_current_switch_is_caught_by_final_check(shadow, prod, monkeypatch):
    """replay 의 거래일 처리가 끝난 뒤 CURRENT 전환 직전에 집합이 바뀌면 전환 트랜잭션 안의 최종 검증이 막는다."""
    _processed_two_days(shadow, prod)
    db0 = connect_rw(shadow.journal_path)
    cur = EN.current_run(db0)
    db0.close()
    sid = decision(shadow, D1)["decision_scan_log_id"]
    real = IN._diff

    def diff_then_contaminate(db, old, new):
        out = real(db, old, new)
        contaminate(shadow.journal_path, *CONTAMINATIONS["member_field"](shadow.journal_path, sid, D1))
        return out
    monkeypatch.setattr(IN, "_diff", diff_then_contaminate)
    out = job(shadow, prod, Bars({D2: {"BBB": BAR_B2}, D3: {"AAA": BAR_A3, "BBB": BAR_B3, "CCC": BAR_C3}}),
              utc(D3, 22, 0))
    assert out.get("error") == "SignalSetIntegrityError"
    runs = {r["run_id"]: r["status"] for r in q(shadow, "SELECT * FROM ab_runs")}
    assert runs[cur] == "CURRENT" and "FAILED" in runs.values()
    ev = one(shadow, "SELECT * FROM ab_events WHERE kind='signal_set_integrity'")
    assert json.loads(ev["details"])["where"] == "replay_current_switch"


def test_candidates_changed_after_verification_are_rejected_in_account_transaction(shadow, prod):
    """검증과 후보 사용 사이 경쟁: 계좌별 처리 트랜잭션이 지문을 다시 확인한다 (검증 때와 다르면 주문 없음)."""
    sid0 = scan(prod, utc(D1, 21, 30), [sig("AAA"), sig("BBB")])
    assert SN.capture_scan_log(sid0, source="HOOK", settings=shadow, session_factory=prod)["frozen"]
    db = connect_rw(shadow.journal_path)
    try:
        EN.ensure_accounts(db, D1, shadow)
        run = EN.ensure_run(db, shadow)
        EN.record_eligibility(db)
        inp = IN.build_inputs(db, run, D1, {}, shadow)
        assert inp.set_fingerprint is not None and inp.cands["B00"]
        sid = inp.decision_scan_log_id
        contaminate(shadow.journal_path, forged_member(shadow.journal_path, sid, D1))
        orders = db.execute("SELECT COUNT(*) FROM ab_orders").fetchone()[0]
        with pytest.raises(IG.SignalSetIntegrityError, match="FINGERPRINT_CHANGED"):
            IN.process_day(db, run, inp, shadow)
        assert db.execute("SELECT COUNT(*) FROM ab_orders").fetchone()[0] == orders
        assert db.execute("SELECT COUNT(*) FROM ab_daily WHERE day=?", (D1,)).fetchone()[0] == 0
        forged = EN.DayInputs(**{**inp.__dict__, "cands": {"B00": [{"scan_log_id": sid + 1, "id": 1}], "B01": []}})
        with pytest.raises(IG.IntegrityDayBlocked):          # CURRENT 거래일 격리 이후에는 계좌 트랜잭션이 먼저 막는다
            EN.process_account_day(db, run, S.account_ids()[0][0], forged, shadow)
        with tx(db):                                         # (운영자 해제를 가정) 격리와 별개로 위조 후보 검사도 확인
            set_runtime(db, IG.DAY_BLOCK_KEY, None)
        with pytest.raises(IG.SignalSetIntegrityError):
            EN.process_account_day(db, run, S.account_ids()[0][0], forged, shadow)
    finally:
        db.close()


def test_clean_journal_passes_every_verification_path(shadow, prod):
    _processed_two_days(shadow, prod)
    out = job(shadow, prod, Bars({D2: {"BBB": BAR_B2}, D3: {"AAA": BAR_A3, "BBB": BAR_B3, "CCC": BAR_C3}}),
              utc(D3, 22, 0))
    assert out.get("error") is None and out["replay"]["run"]
    assert not q(shadow, "SELECT 1 FROM ab_events WHERE kind='signal_set_integrity'")
    assert RP.health(shadow).get("signal_set_integrity") is None
    db = connect_rw(shadow.journal_path)
    try:
        for d in (D1, D2, D3):
            assert IG.decision_set_issues(db, d) == []
    finally:
        db.close()
