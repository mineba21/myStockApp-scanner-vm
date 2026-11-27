"""PR #14 Codex 재리뷰(head c27d9d0) 반영 (2) — SQLite 원장·감사 이력 불변성 (ab0005: 충돌 교체 차단).

보호 대상 표마다 UPDATE·DELETE·INSERT OR REPLACE·REPLACE INTO·UPDATE OR REPLACE·INSERT … ON CONFLICT DO UPDATE·
OLD/NEW run 이동·PK 외 UNIQUE 충돌 교체·외래키 연쇄를 시도하고, 시도 전후 보호 대상 행이 같은지 확인한다.
연결은 (1) 제품 ``connect_rw`` (recursive_triggers=ON) 와 (2) 설정 없는 별도 sqlite3 연결 (recursive_triggers=OFF) 둘 다.
임시 SHADOW 저널만 쓴다.
"""
import sqlite3

import pytest

from tests.test_auto_buy_shadow import D3, job, prod, shadow  # noqa: F401
from tests.test_auto_buy_shadow_integrity import evening, one, q
from tests.test_auto_buy_shadow_review3 import _replay_ready, request_replay
from trading.auto_buy.shadow import inputs as IN
from trading.auto_buy.shadow.journal_migrations.ddl_v5 import COVERED_ELSEWHERE, IGNORE_ON_CONFLICT, KEYS
from trading.auto_buy.shadow.store import connect_rw, event, tx

RUN_TABLES = ("ab_account_runs", "ab_decisions", "ab_orders", "ab_reservations", "ab_positions", "ab_stop_events",
              "ab_daily", "ab_days")
FULLY_IMMUTABLE = ("ab_events", "ab_bars", "ab_signal_snapshots", "ab_accounts", "ab_eligibility")
MUTABLE = ("ab_runtime", "ab_alembic_version", "sqlite_sequence")       # 운영 상태 kv·Alembic 관리 (보호 대상 아님)


def dump(path):
    c = sqlite3.connect(path)
    try:
        tabs = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                if r[0] not in MUTABLE]
        return {t: sorted(map(repr, c.execute(f"SELECT rowid, * FROM {t}"))) for t in tabs}
    finally:
        c.close()


@pytest.fixture
def ledger(shadow, prod, monkeypatch):
    """CURRENT·SUPERSEDED·FAILED run 과 그 원장, 스냅샷·일봉·거래일 입력·감사 이벤트가 있는 저널."""
    full = _replay_ready(shadow, prod)
    db = connect_rw(shadow.journal_path)
    try:
        res = IN.replay(db, full, "BARS_RECOVERED", shadow)
    finally:
        db.close()
    request_replay(shadow)
    real = IN.build_inputs
    monkeypatch.setattr(IN, "build_inputs", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    db = connect_rw(shadow.journal_path)
    try:
        with pytest.raises(RuntimeError):
            IN.replay(db, full, "BARS_RECOVERED", shadow)
    finally:
        db.close()
    monkeypatch.setattr(IN, "build_inputs", real)
    runs = {r["status"]: r["run_id"] for r in q(shadow, "SELECT run_id, status FROM ab_runs")}
    assert {"CURRENT", "SUPERSEDED", "FAILED"} <= set(runs) and runs["SUPERSEDED"] == res["parent"]
    return {"full": full, "cur": runs["CURRENT"], "old": runs["SUPERSEDED"], "failed": runs["FAILED"]}


def _conn(kind, path):
    if kind == "product":
        db = connect_rw(path)
        assert db.execute("PRAGMA recursive_triggers").fetchone()[0] == 1
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        return db
    db = sqlite3.connect(path, isolation_level=None)                     # 설정 없는 별도 쓰기 연결
    assert db.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
    return db


def _victim(db, t, ctx):
    if t in RUN_TABLES:
        r = db.execute(f"SELECT rowid FROM {t} WHERE run_id=? LIMIT 1", (ctx["old"],)).fetchone()
    elif t == "ab_fill_ledger":
        r = db.execute("SELECT f.rowid FROM ab_fill_ledger f JOIN ab_orders o ON o.id=f.order_id WHERE o.run_id=? "
                       "LIMIT 1", (ctx["old"],)).fetchone()
    elif t == "ab_runs":
        r = (ctx["old"],)
    else:
        r = db.execute(f"SELECT rowid FROM {t} LIMIT 1").fetchone()
    return r[0] if r else None


def _cols(db, t):
    info = list(db.execute(f"PRAGMA table_info({t})"))
    pk = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5]]
    return [r[1] for r in info], pk


def _attempts(db, t, ctx):
    rid = _victim(db, t, ctx)
    if rid is None:
        return []
    cols, pk = _cols(db, t)
    other = [c for c in cols if c not in pk][0]
    ops = [("INSERT OR REPLACE", f"INSERT OR REPLACE INTO {t} SELECT * FROM {t} WHERE rowid={rid}"),
           ("REPLACE INTO", f"REPLACE INTO {t} SELECT * FROM {t} WHERE rowid={rid}"),
           ("ON CONFLICT DO UPDATE", f"INSERT INTO {t} SELECT * FROM {t} WHERE rowid={rid} "
                                     f"ON CONFLICT DO UPDATE SET {other}=excluded.{other}"),
           ("DELETE", f"DELETE FROM {t} WHERE rowid={rid}")]
    if t not in ("ab_day_inputs", "ab_scan_captures"):                 # 이 둘은 정해진 열만 갱신 허용 (아래 별도 시험)
        ops.append(("UPDATE", f"UPDATE {t} SET {other}={other} WHERE rowid={rid}"))
    for_update = db.execute(f"SELECT rowid FROM {t} WHERE rowid != {rid} ORDER BY rowid DESC LIMIT 1").fetchone()
    if for_update:                                                       # 다른 행의 키를 피해 행 키로 바꿔 덮어쓰기
        sets = ", ".join(f"{c}=(SELECT {c} FROM {t} WHERE rowid={rid})" for c in pk)
        ops.append(("UPDATE OR REPLACE", f"UPDATE OR REPLACE {t} SET {sets} WHERE rowid={for_update[0]}"))
    if t in RUN_TABLES:
        ops.append(("RUN MOVE old→cur", f"UPDATE {t} SET run_id={ctx['cur']} WHERE rowid={rid}"))
        cur = db.execute(f"SELECT rowid FROM {t} WHERE run_id=? LIMIT 1", (ctx["cur"],)).fetchone()
        if cur:
            ops.append(("RUN MOVE cur→old", f"UPDATE {t} SET run_id={ctx['old']} WHERE rowid={cur[0]}"))
    if t == "ab_runs":
        ops.append(("UPDATE (status reopen)", f"UPDATE ab_runs SET status='CURRENT' WHERE run_id={ctx['failed']}"))
    return ops


def _expect_block(db, sql):
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute(sql)
    except sqlite3.DatabaseError as exc:
        db.execute("ROLLBACK")
        return str(exc)
    db.execute("ROLLBACK")
    return None


PROTECTED = sorted(KEYS)


@pytest.mark.parametrize("kind", ["product", "raw"])
def test_every_conflict_and_mutation_operation_is_blocked(shadow, ledger, kind):
    before = dump(shadow.journal_path)
    db = _conn(kind, shadow.journal_path)
    covered, passed = {}, []
    try:
        for t in PROTECTED:
            for name, sql in _attempts(db, t, ledger):
                if t in IGNORE_ON_CONFLICT and name in ("INSERT OR REPLACE", "REPLACE INTO", "ON CONFLICT DO UPDATE"):
                    _expect_block(db, sql)                               # RAISE(IGNORE): 오류 없이 기존 행 유지
                    covered.setdefault(t, []).append(name)
                    continue
                msg = _expect_block(db, sql)
                if msg is None:
                    passed.append((t, name, sql))
                covered.setdefault(t, []).append(name)
    finally:
        db.close()
    assert passed == []                                                  # 차단되지 않은 연산 없음
    assert dump(shadow.journal_path) == before                           # 보호 대상 행·원장·감사 이력 동일
    for t in ("ab_orders", "ab_fill_ledger", "ab_days", "ab_daily", "ab_runs", "ab_events", "ab_bars",
              "ab_signal_snapshots", "ab_day_inputs", "ab_scan_captures", "ab_accounts", "ab_eligibility"):
        assert {"INSERT OR REPLACE", "REPLACE INTO", "ON CONFLICT DO UPDATE", "DELETE"} <= set(covered[t]), t
    assert "UPDATE OR REPLACE" in covered["ab_orders"] and "RUN MOVE cur→old" in covered["ab_days"]


@pytest.mark.parametrize("kind", ["product", "raw"])
def test_non_pk_unique_replacement_is_blocked(shadow, ledger, kind):
    """새 PK 로 넣되 PK 외 UNIQUE 가 겹치게 해서 기존 행을 지우는 교체 (활성 run 행도 삭제 불가)."""
    before = dump(shadow.journal_path)
    db = _conn(kind, shadow.journal_path)
    try:
        o = db.execute("SELECT rowid FROM ab_orders WHERE run_id=? LIMIT 1", (ledger["cur"],)).fetchone()[0]
        cols, _ = _cols(db, "ab_orders")
        sel = ", ".join("NULL" if c == "id" else c for c in cols)
        assert _expect_block(db, f"INSERT OR REPLACE INTO ab_orders({', '.join(cols)}) SELECT {sel} FROM ab_orders "
                                 f"WHERE rowid={o}")
        o2 = db.execute("SELECT rowid FROM ab_orders WHERE run_id=? AND rowid != ? LIMIT 1", (ledger["cur"], o)).fetchone()
        assert _expect_block(db, f"UPDATE OR REPLACE ab_orders SET signal_key=(SELECT signal_key FROM ab_orders "
                                 f"WHERE rowid={o}) WHERE rowid={o2[0]}")
        s = db.execute("SELECT rowid FROM ab_signal_snapshots LIMIT 1").fetchone()[0]
        cols, _ = _cols(db, "ab_signal_snapshots")
        sel = ", ".join("NULL" if c == "id" else c for c in cols)
        assert _expect_block(db, f"INSERT OR REPLACE INTO ab_signal_snapshots({', '.join(cols)}) SELECT {sel} "
                                 f"FROM ab_signal_snapshots WHERE rowid={s}")
        p = db.execute("SELECT rowid FROM ab_positions WHERE run_id=? LIMIT 1", (ledger["cur"],)).fetchone()
        if p:
            cols, _ = _cols(db, "ab_positions")
            sel = ", ".join("NULL" if c == "id" else c for c in cols)
            assert _expect_block(db, f"INSERT OR REPLACE INTO ab_positions({', '.join(cols)}) SELECT {sel} "
                                     f"FROM ab_positions WHERE rowid={p[0]}")
    finally:
        db.close()
    assert dump(shadow.journal_path) == before


def test_keys_cover_every_pk_and_unique_constraint(shadow):
    """ab0005 트리거가 보호 표의 모든 PK·UNIQUE(부분 포함)를 다루는지 스키마에서 대조한다."""
    c = sqlite3.connect(shadow.journal_path)
    try:
        for t in KEYS:
            want = {tuple(cols) for cols, _ in KEYS[t]}
            have = set()
            for name, uniq in [(r[1], r[2]) for r in c.execute(f"PRAGMA index_list({t})")]:
                if uniq:
                    have.add(tuple(r[2] for r in c.execute(f"PRAGMA index_info({name})")))
            pk = tuple(r[1] for r in sorted(c.execute(f"PRAGMA table_info({t})"), key=lambda r: r[5]) if r[5])
            have.add(pk)
            missing = {k for k in have - want if (t, k) not in COVERED_ELSEWHERE}
            assert not missing, (t, missing)
    finally:
        c.close()


def test_foreign_keys_have_no_cascade_and_parent_changes_are_blocked(shadow, ledger):
    c = sqlite3.connect(shadow.journal_path)
    try:
        acts = {(t, r[2], r[5], r[6]) for t in KEYS for r in c.execute(f"PRAGMA foreign_key_list({t})")}
        assert acts and all(on_upd == "NO ACTION" and on_del == "NO ACTION" for _, _, on_upd, on_del in acts)
    finally:
        c.close()
    before = dump(shadow.journal_path)
    for kind in ("product", "raw"):
        db = _conn(kind, shadow.journal_path)
        try:
            for sql in ("DELETE FROM ab_accounts", "UPDATE ab_accounts SET id=id||'x'", "DELETE FROM ab_runs",
                        "DELETE FROM ab_signal_snapshots", "DELETE FROM ab_scan_captures",
                        f"UPDATE ab_orders SET id=id+100000 WHERE run_id={ledger['old']}",
                        "UPDATE ab_scan_captures SET scan_status='ERROR'"):
                assert _expect_block(db, sql), sql
        finally:
            db.close()
    assert dump(shadow.journal_path) == before


def test_normal_writes_replay_and_idempotent_records_still_work(shadow, prod, ledger):
    full = ledger["full"]
    db = connect_rw(shadow.journal_path)
    try:
        n = one(shadow, "SELECT COUNT(*) n FROM ab_events")["n"]
        with tx(db):
            event(db, "INFO", "test_event", "새 감사 이벤트는 추가된다")
        assert one(shadow, "SELECT COUNT(*) n FROM ab_events")["n"] == n + 1
        e = db.execute("SELECT * FROM ab_eligibility LIMIT 1").fetchone()
        cur = db.execute("INSERT OR IGNORE INTO ab_eligibility SELECT * FROM ab_eligibility WHERE rowid=?",
                         (db.execute("SELECT rowid FROM ab_eligibility LIMIT 1").fetchone()[0],))
        assert e is not None and cur.rowcount == 0                       # 기존 멱등 기록은 그대로 (오류 없음)
        with tx(db):                                                     # 활성 run 행 갱신·스캔 수집 수치 갱신 허용
            db.execute("UPDATE ab_days SET notes=notes WHERE run_id=?", (ledger["cur"],))
            db.execute("UPDATE ab_scan_captures SET rows_captured=rows_captured")
        res = IN.replay(db, full, "BARS_RECOVERED", shadow)              # 남은 요청(실패 후)의 정상 재시도 → 전환
    finally:
        db.close()
    assert res["run"] and one(shadow, "SELECT status FROM ab_runs WHERE run_id=?", res["run"])["status"] == "CURRENT"
    assert one(shadow, "SELECT status FROM ab_runs WHERE run_id=?", ledger["cur"])["status"] == "SUPERSEDED"
    assert job(shadow, prod, full, evening(D3)).get("error") is None


def test_write_connection_pragmas_are_verified(shadow, monkeypatch):
    from trading.auto_buy.shadow import store as ST
    db = connect_rw(shadow.journal_path)
    try:
        assert (db.execute("PRAGMA recursive_triggers").fetchone()[0], db.execute("PRAGMA foreign_keys").fetchone()[0]) \
            == (1, 1)
    finally:
        db.close()

    class NoPragma:                                                      # 설정이 적용되지 않는 연결 → 쓰지 않음
        def __init__(self):
            self.closed = False

        def execute(self, sql, *a):
            class C:
                def fetchone(self_inner):
                    return (0,)
            return C()

        def close(self):
            self.closed = True
    fake = NoPragma()
    with pytest.raises(RuntimeError, match="recursive_triggers"):
        ST.apply_write_pragmas(fake)
    assert fake.closed
