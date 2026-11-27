"""PR #14 Codex 재리뷰(head 36e5d2f) 반영 (1) — 테이블별 데이터 불변성 계약 (ab0006) 전수 검증.

- 실제 SQLite 스키마(표·컬럼·PK·UNIQUE·부분 UNIQUE)와 ``ddl_v6.CONTRACT`` 를 대조한다. 분류되지 않은 표·컬럼이 있으면 실패.
- 보호 대상 표마다 **컬럼별** 변경, **SQL 연산별**(일반·OR REPLACE·REPLACE INTO·OR IGNORE·ON CONFLICT·숨은 rowid·음수
  rowid·다중 행·rowid/oid/_rowid_ 변경·DELETE) 시도를 제품 ``connect_rw``(recursive_triggers=ON)와 설정 없는 기본
  sqlite3 연결(recursive_triggers=OFF) 각각에서 하고, **같은 트랜잭션 안에서** 보호 대상 전체 덤프가 그대로인지 본다
  (공격이 통과했다면 롤백 전에 드러난다).
- 정상 경로(활성 run 갱신·런타임 upsert·계좌 1회 저장·수집 건수·멱등 판정 기록)는 그대로 동작해야 한다.
임시 SHADOW 저널만 쓴다 (운영 DB·주문 없음).
"""
import json
import os
import re
import sqlite3

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import Base, ScanLog, ScanResult
from tests.test_auto_buy_shadow import D1, D3, job, prod, scan, shadow, sig, utc  # noqa: F401
from tests.test_auto_buy_shadow import Bars
from tests.test_auto_buy_shadow_integrity import evening
from tests.test_auto_buy_shadow_review3 import _replay_ready, request_replay
from trading.auto_buy import settings as S
from trading.auto_buy.shadow import engine as EN
from trading.auto_buy.shadow import inputs as IN
from trading.auto_buy.shadow.journal_migrations.ddl_v6 import (CATEGORIES, CONTRACT, COVERED_ELSEWHERE,
                                                               ELIGIBILITY_JUDGEMENT, PROTECTED, RUNTIME,
                                                               _immutable_columns)
from trading.auto_buy.shadow.schema import upgrade_journal
from trading.auto_buy.shadow.store import connect_rw, event, get_runtime, set_runtime, tx

PROTECT = re.compile(r"immutable|contract|fenced|terminal|conflict|row identity|fixed|only one CURRENT")
FUTURE_DAY = "2026-12-01"


# ── 공통: 다양한 상태의 원장을 한 번 만들고 (모듈 단위), 시도는 트랜잭션 안에서 하고 롤백한다 ──────────────

@pytest.fixture(scope="module")
def template(tmp_path_factory):
    """CURRENT·SUPERSEDED·FAILED run, CURRENT run 의 OPEN·CLOSED 포지션·손절 이벤트·ACTIVE·CONVERTED 예약, 계좌가 있는
    거래일 입력과 계좌가 아직 없는 거래일 입력(미래일 NO_DECISION_SCAN)이 있는 저널."""
    mp, mp2 = pytest.MonkeyPatch(), pytest.MonkeyPatch()
    tmp = tmp_path_factory.mktemp("contract")
    path = str(tmp / "auto_buy_shadow.sqlite3")
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    try:
        mp.setenv("AUTO_BUY_US_MODE", "SHADOW")
        mp.setenv("AUTO_BUY_SHADOW_JOURNAL_PATH", path)
        mp.setenv("SCANNER_ORDER_JOURNAL_PATH", str(tmp / "orders.sqlite3"))
        upgrade_journal(path, allow_create=True)
        settings = S.AutoBuySettings.from_env()
        Base.metadata.create_all(engine, tables=[ScanResult.__table__, ScanLog.__table__])
        factory = sessionmaker(bind=engine)
        full = _replay_ready(settings, factory)
        db = connect_rw(path)
        try:
            IN.replay(db, full, "BARS_RECOVERED", settings)
        finally:
            db.close()
        request_replay(settings)
        mp2.setattr(IN, "build_inputs", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        db = connect_rw(path)
        try:
            with pytest.raises(RuntimeError):
                IN.replay(db, full, "BARS_RECOVERED", settings)
        finally:
            db.close()
        mp2.undo()
        db = connect_rw(path)
        try:
            runs = {r["status"]: r["run_id"] for r in db.execute("SELECT run_id, status FROM ab_runs")}
            with tx(db):
                for p in db.execute("SELECT * FROM ab_positions WHERE run_id=? AND state='OPEN' ORDER BY id LIMIT 2",
                                    (runs["CURRENT"],)).fetchall():
                    EN.close_position(db, p, D3, p["stop_price"], "STOP")
            IN.freeze_no_decision(db, FUTURE_DAY, "contract_test", settings)
        finally:
            db.close()
        ctx = {"cur": runs["CURRENT"], "old": runs["SUPERSEDED"], "failed": runs["FAILED"], "path": path}
        db = sqlite3.connect(path)
        try:
            ctx["baseline"] = dump(db)
        finally:
            db.close()
        yield ctx
    finally:
        mp2.undo()
        mp.undo()
        engine.dispose()


def dump(db):
    """보호 대상 표 전체 (rowid 포함) — 런타임 상태 표 제외."""
    return {t: sorted(repr(tuple(r)) for r in db.execute(f"SELECT rowid, * FROM {t}")) for t in PROTECTED}


def _open(kind, path):
    if kind == "product":
        db = connect_rw(path)
        assert tuple(db.execute("PRAGMA recursive_triggers").fetchone()) == (1,)
    else:
        db = sqlite3.connect(path, isolation_level=None)
        db.row_factory = sqlite3.Row
        assert db.execute("PRAGMA recursive_triggers").fetchone()[0] == 0     # 기본 연결: OFF
    return db


def attempt(ctx, kind, sql, params=()):
    """트랜잭션 안에서 시도하고, 롤백 전에 보호 대상 덤프를 비교한다. (오류 메시지 또는 None, 덤프 동일 여부)."""
    db = _open(kind, ctx["path"])
    try:
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute(sql, params)
            err = None
        except sqlite3.DatabaseError as exc:
            err = str(exc)
        same = dump(db) == ctx["baseline"]
        db.execute("ROLLBACK")
        return err, same
    finally:
        db.close()


def _info(db, t):
    rows = list(db.execute(f"PRAGMA table_info({t})"))
    pk = [r[1] for r in sorted(rows, key=lambda r: r[5]) if r[5]]
    alias = len(pk) == 1 and next(r[2] for r in rows if r[1] == pk[0]).upper() == "INTEGER"
    return [r[1] for r in rows], {r[1]: r[2].upper() for r in rows}, pk, alias


def _victim(db, t, ctx, other=False):
    """시도 대상 행 rowid. run 단위 표는 CURRENT run 행(가장 허용적인 상태), 거래일 입력은 계좌 미입력 행.
    ``other`` 는 대상과 다른 행 (키·rowid 를 빼앗을 상대)."""
    if other:
        r = db.execute(f"SELECT rowid FROM {t} WHERE rowid != ? ORDER BY rowid DESC LIMIT 1",
                       (_victim(db, t, ctx),)).fetchone()
        assert r is not None, f"{t}: 상대 행이 없음"
        return r[0]
    order = "ASC"
    if "run_id" in _info(db, t)[0] and t != "ab_runs":
        sql, a = f"SELECT rowid FROM {t} WHERE run_id=? ORDER BY rowid {order} LIMIT 1", (ctx["cur"],)
    elif t == "ab_fill_ledger":
        sql, a = (f"SELECT f.rowid FROM ab_fill_ledger f JOIN ab_orders o ON o.id=f.order_id WHERE o.run_id=? "
                  f"ORDER BY f.rowid {order} LIMIT 1"), (ctx["cur"],)
    elif t == "ab_runs":
        sql, a = "SELECT run_id FROM ab_runs WHERE run_id=?", (ctx["cur"],)
    elif t == "ab_day_inputs":
        sql, a = "SELECT rowid FROM ab_day_inputs WHERE day=?", (FUTURE_DAY,)
    else:
        sql, a = f"SELECT rowid FROM {t} ORDER BY rowid {order} LIMIT 1", ()
    r = db.execute(sql, a).fetchone()
    assert r is not None, f"{t}: 시험할 행이 없음"
    return r[0]


def _mod(col, typ, alias_col=None):
    return f"{col} + 100000" if "INT" in typ or "REAL" in typ else f"{col} || '~n'"


def _change(col):
    return (f"CASE WHEN typeof({col})='null' THEN 1 WHEN typeof({col}) IN ('integer','real') THEN {col} + 1 "
            f"ELSE {col} || '~' END")


# ── 1. 실제 스키마 ↔ 계약 대조 (누락 시 실패) ─────────────────────────────────────────

def test_every_table_and_column_is_classified(template):
    db = sqlite3.connect(template["path"])
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == set(CONTRACT), (tables - set(CONTRACT), set(CONTRACT) - tables)
        for t, c in CONTRACT.items():
            cols = _info(db, t)[0]
            assert list(c["columns"]) == cols, (t, set(cols) ^ set(c["columns"]))
            assert set(c["mutable"]) <= set(cols), t
            assert c["category"] in CATEGORIES, t
            for k in ("identity", "insert", "change", "delete", "conflict"):
                assert c[k], (t, k)
            if c["category"] == RUNTIME:
                assert t not in PROTECTED
            else:
                assert t in PROTECTED
                assert not db.execute("SELECT sql FROM sqlite_master WHERE name=?", (t,)).fetchone()[0] \
                    .upper().rstrip().endswith("WITHOUT ROWID")          # 모두 rowid 표 → 숨은 rowid 보호 대상
    finally:
        db.close()


def test_every_pk_and_unique_constraint_is_in_the_contract(template):
    db = sqlite3.connect(template["path"])
    try:
        for t, c in CONTRACT.items():
            if t == "sqlite_sequence":
                continue
            want = {tuple(cols) for cols, _ in c["keys"]}
            cols, _, pk, alias = _info(db, t)
            have = {tuple(pk)} if pk else set()
            for name, uniq, partial in [(r[1], r[2], r[4]) for r in db.execute(f"PRAGMA index_list({t})")]:
                if uniq:
                    have.add(tuple(r[2] for r in db.execute(f"PRAGMA index_info({name})")))
            missing = {k for k in have - want if (t, k) not in COVERED_ELSEWHERE}
            assert not missing, (t, missing)
            assert want <= have, (t, want - have)                         # 계약에 있는 키는 실제로 있어야 함
            assert ("정수 PK" in c["identity"]) == alias, (t, c["identity"], alias)
            assert ("숨은 rowid" in c["identity"]) == (not alias and t not in ("sqlite_sequence",)
                                                       and t != "ab_alembic_version"), t
    finally:
        db.close()


def test_every_protected_table_has_contract_triggers(template):
    db = sqlite3.connect(template["path"])
    try:
        trig = {r[0]: r[1] for r in db.execute("SELECT name, tbl_name FROM sqlite_master WHERE type='trigger'")}
        for t in PROTECTED:
            for suffix in ("v6_rowid_insert", "v6_rowid_positive", "v6_allowlist"):
                assert trig.get(f"{t}_{suffix}") == t, (t, suffix)
            assert any(tb == t and ("no_delete" in n) for n, tb in trig.items()), (t, "삭제 거부")
        assert "ab_eligibility_no_replace_insert" not in trig          # ab0006 이 판정 충돌 정책으로 교체
        assert {"ab_eligibility_v6_conflict", "ab_eligibility_v6_same"} <= set(trig)
    finally:
        db.close()


# ── 2. 컬럼별: 변경 허용 목록에 없는 컬럼(키 포함)은 최초 INSERT 이후 바꿀 수 없다 ─────────────────────

COLUMN_CASES = [(t, c) for t in PROTECTED for c in _immutable_columns(t)] + [(t, "rowid") for t in PROTECTED]


@pytest.mark.parametrize("kind", ["product", "raw"])
@pytest.mark.parametrize("table,column", COLUMN_CASES, ids=[f"{t}.{c}" for t, c in COLUMN_CASES])
def test_immutable_column_cannot_change(template, kind, table, column):
    db = sqlite3.connect(template["path"])
    try:
        rid = _victim(db, table, template)
    finally:
        db.close()
    expr = "rowid + 100000" if column == "rowid" else _change(column)
    err, same = attempt(template, kind, f"UPDATE {table} SET {column} = {expr} WHERE rowid = {rid}")
    assert err and PROTECT.search(err), (table, column, err)
    assert same


def test_day_inputs_frozen_fields_listed_by_reviewer_are_immutable():
    for c in ("day", "decision_status", "decision_scan_log_id", "frozen_at", "frozen_by", "signal_set_hash",
              "strategy_version", "config_hash", "logic_hash"):
        assert c in _immutable_columns("ab_day_inputs"), c
    assert set(CONTRACT["ab_day_inputs"]["mutable"]) == {"account_status", "account_reason", "account_snapshot",
                                                         "account_fetched_at"}


# ── 3. SQL 연산별 (표 × 연산 × 연결) ───────────────────────────────────────────────

OPS = ("INSERT", "INSERT OR REPLACE", "REPLACE INTO", "INSERT OR IGNORE", "ON CONFLICT DO UPDATE",
       "ON CONFLICT DO NOTHING", "INSERT OR REPLACE hidden rowid", "INSERT negative rowid",
       "INSERT multi-row OR REPLACE", "UPDATE rowid", "UPDATE oid", "UPDATE _rowid_", "UPDATE OR REPLACE rowid",
       "UPDATE OR REPLACE key", "UPDATE multi-row", "UPDATE OR REPLACE multi-row", "DELETE", "DELETE all")
NO_ERROR_REQUIRED = ("INSERT OR IGNORE", "ON CONFLICT DO NOTHING")
# ab_eligibility: 같은 키에 같은 판단을 다시 넣는 시도는 정책상 멱등 무시 (오류 없이 기존 행 유지 — 아래 6절)
ELIGIBILITY_SAME = ("INSERT", "INSERT OR REPLACE", "REPLACE INTO", "ON CONFLICT DO UPDATE")


def _sql(db, t, op, ctx):
    cols, types, pk, alias = _info(db, t)
    rid, other = _victim(db, t, ctx), _victim(db, t, ctx, other=True)
    keys = [k for ks, _ in CONTRACT[t]["keys"] for k in ks]
    last = pk[-1]
    plain = [c for c in _immutable_columns(t) if c not in keys] or [c for c in cols if c not in pk] or cols
    collist = ", ".join(cols)

    lasts = {ks[-1] for ks, _ in CONTRACT[t]["keys"]}

    def new_row(rowid_expr, src):
        """기존 행을 복사하되 각 PK·UNIQUE 의 마지막 컬럼을 바꾼 새 키 행. rowid(정수 PK 표는 id)는 따로 지정."""
        exprs = []
        for c in cols:
            if alias and c == pk[0]:
                exprs.append(rowid_expr)
            elif c in lasts:
                exprs.append(_mod(c, types[c]))
            else:
                exprs.append(c)
        if alias:
            return f"SELECT {', '.join(exprs)} FROM {t} WHERE rowid={src}", collist
        return f"SELECT {rowid_expr}, {', '.join(exprs)} FROM {t} WHERE rowid={src}", "rowid, " + collist

    if op == "INSERT":
        return f"INSERT INTO {t} SELECT * FROM {t} WHERE rowid={rid}"
    if op in ("INSERT OR REPLACE", "INSERT OR IGNORE"):
        return f"{op} INTO {t} SELECT * FROM {t} WHERE rowid={rid}"
    if op == "REPLACE INTO":
        return f"REPLACE INTO {t} SELECT * FROM {t} WHERE rowid={rid}"
    if op == "ON CONFLICT DO UPDATE":
        return (f"INSERT INTO {t} SELECT * FROM {t} WHERE rowid={rid} ON CONFLICT DO UPDATE SET "
                f"{plain[0]} = {_change('excluded.' + plain[0])}")
    if op == "ON CONFLICT DO NOTHING":
        return f"INSERT INTO {t} SELECT * FROM {t} WHERE rowid={rid} ON CONFLICT DO NOTHING"
    if op == "INSERT OR REPLACE hidden rowid":            # 새 키 + 기존 rowid (정수 PK 표는 기존 id)
        sel, cl = new_row(str(rid), rid)
        return f"INSERT OR REPLACE INTO {t}({cl}) {sel}"
    if op == "INSERT negative rowid":
        sel, cl = new_row("-5", rid)
        return f"INSERT INTO {t}({cl}) {sel}"
    if op == "INSERT multi-row OR REPLACE":                # 1행은 정상 새 행(자동 rowid), 2행이 기존 rowid 교체
        s1, cl = new_row("NULL", other)
        s2, _ = new_row(str(rid), rid)
        s2 = s2.replace(f"{last} || '~n'", f"{last} || '~m'").replace(f"{last} + 100000", f"{last} + 200000")
        return f"INSERT OR REPLACE INTO {t}({cl}) {s1} UNION ALL {s2}"
    if op in ("UPDATE rowid", "UPDATE oid", "UPDATE _rowid_"):
        a = op.split()[1]
        return f"UPDATE {t} SET {a} = {a} + 100000 WHERE rowid={rid}"
    if op == "UPDATE OR REPLACE rowid":
        return f"UPDATE OR REPLACE {t} SET rowid = {other} WHERE rowid={rid}"
    if op == "UPDATE OR REPLACE key":
        sets = ", ".join(f"{c} = (SELECT {c} FROM {t} WHERE rowid={other})" for c in pk)
        return f"UPDATE OR REPLACE {t} SET {sets} WHERE rowid={rid}"
    if op == "UPDATE multi-row":
        return f"UPDATE {t} SET {plain[0]} = {_change(plain[0])}"
    if op == "UPDATE OR REPLACE multi-row":
        return f"UPDATE OR REPLACE {t} SET {last} = (SELECT {last} FROM {t} WHERE rowid={rid})"
    if op == "DELETE":
        return f"DELETE FROM {t} WHERE rowid={rid}"
    if op == "DELETE all":
        return f"DELETE FROM {t}"
    raise AssertionError(op)


OP_CASES = [(t, op) for t in PROTECTED for op in OPS]


@pytest.mark.parametrize("kind", ["product", "raw"])
@pytest.mark.parametrize("table,op", OP_CASES, ids=[f"{t}:{op}" for t, op in OP_CASES])
def test_sql_operation_cannot_change_protected_rows(template, kind, table, op):
    db = sqlite3.connect(template["path"])
    try:
        sql = _sql(db, table, op, template)
        alias = _info(db, table)[3]
    finally:
        db.close()
    err, same = attempt(template, kind, sql)
    assert same, (table, op, sql)                                   # 보호 대상 행·원장·감사 이력 동일
    if op not in NO_ERROR_REQUIRED and not (table == "ab_eligibility" and op in ELIGIBILITY_SAME):
        assert err and PROTECT.search(err), (table, op, err)
    if op == "INSERT OR REPLACE hidden rowid" and not alias:
        assert "row identity" in err, (table, err)                  # 숨은 rowid 교체는 ab0006 rowid 보호가 막음
    if op == "INSERT negative rowid" and table not in ("ab_runs",):     # ab_runs 복사본은 단일 CURRENT 가 먼저 막음
        assert "row identity" in err, (table, err)


def test_auto_generated_ids_still_work_and_new_rows_get_positive_rowids(template, tmp_path):
    path = str(tmp_path / "j.sqlite3")
    src = sqlite3.connect(template["path"])
    dst = sqlite3.connect(path)
    src.backup(dst)
    src.close()
    dst.close()
    os.chmod(path, 0o600)
    db = connect_rw(path)
    try:
        with tx(db):
            event(db, "INFO", "auto_id", "자동 id")
            db.execute("INSERT INTO ab_bars(day, ticker, input_rev, status, provider, fetched_at, purpose) "
                       "VALUES ('2026-12-02','ZZZ',1,'MISSING','test',0,'DAILY')")
        e = db.execute("SELECT rowid, id FROM ab_events WHERE kind='auto_id'").fetchone()
        b = db.execute("SELECT rowid FROM ab_bars WHERE ticker='ZZZ'").fetchone()
        assert e[0] == e[1] > 0 and b[0] > 0
    finally:
        db.close()


# ── 4. 최초 1회 입력: ab_day_inputs 계좌 4필드 · ab_scan_captures 수집 건수 ───────────────────────

ACCT_BAD = {
    "ok_without_snapshot": "account_status='OK', account_snapshot=NULL, account_fetched_at=1",
    "ok_invalid_json": "account_status='OK', account_snapshot='not json', account_fetched_at=1",
    "unavailable_with_snapshot": "account_status='UNAVAILABLE', account_snapshot='{}', account_fetched_at=1",
    "late_without_fetched_at": "account_status='LATE', account_fetched_at=NULL",
    "reason_only": "account_reason='x'",
    "fetched_at_only": "account_fetched_at=1",
    "status_only_ok": "account_status='OK'",
}
ACCT_GOOD = {
    "ok": "account_status='OK', account_snapshot='{\"equity\": 1}', account_fetched_at=1",
    "unavailable": "account_status='UNAVAILABLE', account_reason='x', account_fetched_at=1",
    "late": "account_status='LATE', account_reason='late', account_fetched_at=1",
}


@pytest.mark.parametrize("kind", ["product", "raw"])
@pytest.mark.parametrize("case", sorted(ACCT_BAD))
def test_account_input_rejects_invalid_combinations(template, kind, case):
    err, same = attempt(template, kind, f"UPDATE ab_day_inputs SET {ACCT_BAD[case]} WHERE day=?", (FUTURE_DAY,))
    assert err and "contract" in err and same, (case, err)


@pytest.mark.parametrize("case", sorted(ACCT_GOOD))
def test_account_input_allows_valid_first_store(template, case):
    db = _open("product", template["path"])
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute(f"UPDATE ab_day_inputs SET {ACCT_GOOD[case]} WHERE day=?", (FUTURE_DAY,))
        r = db.execute("SELECT account_status FROM ab_day_inputs WHERE day=?", (FUTURE_DAY,)).fetchone()
        assert r[0] in ("OK", "UNAVAILABLE", "LATE")
        db.execute("ROLLBACK")
    finally:
        db.close()


@pytest.mark.parametrize("kind", ["product", "raw"])
@pytest.mark.parametrize("field,value", [("account_status", "'UNAVAILABLE'"), ("account_reason", "'edited'"),
                                         ("account_snapshot", "'{\"equity\": 999}'"),
                                         ("account_fetched_at", "account_fetched_at + 1")])
def test_account_input_cannot_change_after_first_store(template, kind, field, value):
    db = sqlite3.connect(template["path"])
    try:
        day = db.execute("SELECT day FROM ab_day_inputs WHERE account_status='OK' LIMIT 1").fetchone()[0]
    finally:
        db.close()
    err, same = attempt(template, kind, f"UPDATE ab_day_inputs SET {field} = {value} WHERE day=?", (day,))
    assert err and PROTECT.search(err) and same, (field, err)


@pytest.mark.parametrize("kind", ["product", "raw"])
@pytest.mark.parametrize("values", ["account_status='OK', account_snapshot='{}', account_fetched_at=1",
                                    "decision_status='FROZEN'", "decision_scan_log_id=1"])
def test_day_input_insert_requires_empty_account_and_valid_decision(template, kind, values):
    base = {"day": "'2026-12-03'", "decision_status": "'NO_DECISION_SCAN'", "decision_scan_log_id": "NULL",
            "frozen_at": "0", "frozen_by": "'t'", "signal_set_hash": "NULL", "strategy_version": "'SV1'",
            "config_hash": "'c'", "logic_hash": "'l'"}
    for part in values.split(", "):
        k, v = part.split("=", 1)
        base[k] = v
    sql = f"INSERT INTO ab_day_inputs({', '.join(base)}) VALUES ({', '.join(base.values())})"
    err, same = attempt(template, kind, sql)
    assert err and "contract insert" in err and same, err


def test_store_account_app_path_validates_and_stores_once(template, tmp_path):
    path = str(tmp_path / "j.sqlite3")
    src, dst = sqlite3.connect(template["path"]), sqlite3.connect(path)
    src.backup(dst)
    src.close()
    dst.close()
    os.chmod(path, 0o600)
    db = connect_rw(path)
    try:
        with pytest.raises(IN.AccountInputInvalid):
            IN.store_account(db, FUTURE_DAY, "OK", None, None)              # OK 인데 스냅샷 없음
        with pytest.raises(IN.AccountInputInvalid):
            IN.store_account(db, FUTURE_DAY, "MAYBE", None, None)
        assert IN.day_input(db, FUTURE_DAY)["account_status"] is None
        IN.store_account(db, FUTURE_DAY, "UNAVAILABLE", None, "조회 실패")
        r = dict(IN.day_input(db, FUTURE_DAY))
        assert r["account_status"] == "UNAVAILABLE" and r["account_snapshot"] is None and r["account_fetched_at"]
        IN.store_account(db, FUTURE_DAY, "OK", {"equity": 1.0}, None)        # 재실행: 이미 저장됨 → 그대로
        assert dict(IN.day_input(db, FUTURE_DAY)) == r
    finally:
        db.close()


def test_store_account_verifies_in_same_transaction_and_rolls_back(template, tmp_path):
    """조건부 갱신이 적용되지 않으면(예: 다른 경로가 먼저 일부 필드를 채움) 저장 확인이 실패하고 롤백한다."""
    path = str(tmp_path / "j.sqlite3")
    src, dst = sqlite3.connect(template["path"]), sqlite3.connect(path)
    src.backup(dst)
    src.close()
    dst.close()
    os.chmod(path, 0o600)
    real = connect_rw(path)

    class Proxy:                                    # ab_day_inputs 갱신만 적용되지 않는 연결 (0행)
        def __getattr__(self, name):
            return getattr(real, name)

        def execute(self, sql, *a):
            if sql.startswith("UPDATE ab_day_inputs"):
                return real.execute("UPDATE ab_day_inputs SET day=day WHERE 0")
            return real.execute(sql, *a)
    try:
        before = dict(IN.day_input(real, FUTURE_DAY))
        with pytest.raises(IN.AccountInputInvalid, match="롤백"):
            IN.store_account(Proxy(), FUTURE_DAY, "LATE", None, "late")
        assert dict(IN.day_input(real, FUTURE_DAY)) == before and not real.in_transaction
    finally:
        real.close()


@pytest.mark.parametrize("kind", ["product", "raw"])
@pytest.mark.parametrize("sets", ["rows_captured = rows_captured + 1", "strict_rows = strict_rows + 1",
                                  "rows_captured = 0, strict_rows = 0"])
def test_scan_capture_counts_cannot_change_after_set(template, kind, sets):
    db = sqlite3.connect(template["path"])
    try:
        sid = db.execute("SELECT scan_log_id FROM ab_scan_captures WHERE rows_captured > 0 LIMIT 1").fetchone()[0]
    finally:
        db.close()
    err, same = attempt(template, kind, f"UPDATE ab_scan_captures SET {sets} WHERE scan_log_id={sid}")
    assert err and PROTECT.search(err) and same, err


def test_scan_capture_counts_set_once_only_to_snapshot_counts(template, tmp_path):
    path = str(tmp_path / "j.sqlite3")
    src, dst = sqlite3.connect(template["path"]), sqlite3.connect(path)
    src.backup(dst)
    src.close()
    dst.close()
    db = sqlite3.connect(path, isolation_level=None)
    try:
        cols = [r[1] for r in db.execute("PRAGMA table_info(ab_scan_captures)")]
        sel = ", ".join("scan_log_id + 1000" if c == "scan_log_id" else "0" if c in ("rows_captured", "strict_rows")
                        else c for c in cols)
        db.execute(f"INSERT INTO ab_scan_captures({', '.join(cols)}) SELECT {sel} FROM ab_scan_captures LIMIT 1")
        new = db.execute("SELECT MAX(scan_log_id) FROM ab_scan_captures").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="contract set-once"):
            db.execute(f"UPDATE ab_scan_captures SET rows_captured=5 WHERE scan_log_id={new}")   # 스냅샷 0건
        db.execute(f"UPDATE ab_scan_captures SET rows_captured=0, strict_rows=0 WHERE scan_log_id={new}")  # 변화 없음
    finally:
        db.close()


# ── 5. 상태 전환 (활성 run 에서도 되돌림·종결 행 변경 불가) ─────────────────────────────────────

@pytest.mark.parametrize("kind", ["product", "raw"])
@pytest.mark.parametrize("change", ["CURRENT->BUILDING", "CURRENT->FAILED", "FAILED->CURRENT", "SUPERSEDED->CURRENT"])
def test_run_status_transitions(template, kind, change):
    old, new = change.split("->")
    run = {"CURRENT": template["cur"], "FAILED": template["failed"], "SUPERSEDED": template["old"]}[old]
    err, same = attempt(template, kind, f"UPDATE ab_runs SET status='{new}' WHERE run_id={run}")
    assert err and PROTECT.search(err) and same, err


@pytest.fixture
def pending(shadow, prod):
    """CURRENT run 에 PENDING 주문이 있는 저널 (D1 결정 → D2 목표)."""
    scan(prod, utc(D1, 21, 30), [sig("AAA"), sig("BBB")])
    job(shadow, prod, Bars({}), evening(D1))
    return shadow.journal_path


@pytest.mark.parametrize("kind", ["product", "raw"])
def test_order_position_reservation_transitions(shadow, pending, kind):
    from tests.test_auto_buy_shadow import D2
    db = connect_rw(pending)
    try:
        o1, o2 = [dict(r) for r in db.execute("SELECT * FROM ab_orders WHERE account_id='SV1:P7C:B00' ORDER BY id")]
        with tx(db):                                                     # 정상: 부분 체결 → PARTIAL, 포지션 OPEN
            EN.apply_fill(db, o1["id"], 5, 100.5, D2)
            EN.apply_fill(db, o2["id"], o2["qty"], 100.5, D2)
            pos2 = db.execute("SELECT * FROM ab_positions WHERE order_id=?", (o2["id"],)).fetchone()
            EN.close_position(db, pos2, D2, pos2["stop_price"], "STOP")   # o2: FILLED, 포지션 CLOSED, 예약 CONVERTED
            db.execute("UPDATE ab_positions SET price_status='UNRESOLVED', unresolved_since=? WHERE order_id=?",
                       (D2, o1["id"]))
        assert db.execute("SELECT state FROM ab_orders WHERE id=?", (o1["id"],)).fetchone()[0] == "PARTIAL"
    finally:
        db.close()
    path = pending
    c = _open(kind, path)
    blocked = {
        "partial->pending": f"UPDATE ab_orders SET state='PENDING' WHERE id={o1['id']}",
        "filled_qty_decrease": f"UPDATE ab_orders SET filled_qty=1 WHERE id={o1['id']}",
        "pending_immutable_column": f"UPDATE ab_orders SET limit_price=limit_price+1 WHERE id={o1['id']}",
        "pending_target_day": f"UPDATE ab_orders SET target_day='2026-11-30' WHERE id={o1['id']}",
        "terminal_order": f"UPDATE ab_orders SET details='{{}}' WHERE id={o2['id']}",
        "closed_position_reopen": f"UPDATE ab_positions SET state='OPEN' WHERE order_id={o2['id']}",
        "closed_position_edit": f"UPDATE ab_positions SET pnl=0 WHERE order_id={o2['id']}",
        "unresolved_to_ok": f"UPDATE ab_positions SET price_status='OK' WHERE order_id={o1['id']}",
        "position_entry_day": f"UPDATE ab_positions SET entry_day='2026-01-01' WHERE order_id={o1['id']}",
        "converted_reservation": f"UPDATE ab_reservations SET state='ACTIVE' WHERE order_id={o2['id']}",
        "reservation_sector": f"UPDATE ab_reservations SET sector='X' WHERE order_id={o1['id']}",
        "account_run_id": "UPDATE ab_account_runs SET account_id=account_id||'x' WHERE rowid=1",
    }
    try:
        for name, sql in blocked.items():
            before = dump(c)
            c.execute("BEGIN IMMEDIATE")
            with pytest.raises(sqlite3.IntegrityError) as exc:
                c.execute(sql)
            assert PROTECT.search(str(exc.value)), (name, exc.value)
            assert dump(c) == before, name
            c.execute("ROLLBACK")
        c.execute("BEGIN IMMEDIATE")                                     # 허용: 열린 주문의 허용 목록 컬럼
        c.execute(f"UPDATE ab_orders SET details='{{\"n\": 1}}', updated_at=1 WHERE id={o1['id']}")
        c.execute(f"UPDATE ab_reservations SET updated_at=1 WHERE order_id={o1['id']}")
        c.execute("ROLLBACK")
    finally:
        c.close()


# ── 6. ab_eligibility 충돌 정책 (P2) ────────────────────────────────────────────────

def _elig(db):
    return dict(db.execute("SELECT rowid, * FROM ab_eligibility ORDER BY rowid LIMIT 1").fetchone())


@pytest.mark.parametrize("kind", ["product", "raw"])
@pytest.mark.parametrize("verb", ["INSERT OR IGNORE", "INSERT", "INSERT OR REPLACE", "REPLACE"])
def test_eligibility_same_judgement_is_idempotent(template, kind, verb):
    db = _open(kind, template["path"])
    try:
        e = _elig(db)
        db.execute("BEGIN IMMEDIATE")
        cur = db.execute(f"{verb} INTO ab_eligibility(snapshot_id, pool, strategy_version, eligible, reasons, "
                         f"decision_role, logic_hash, created_at) VALUES (?,?,?,?,?,?,?,?)",
                         (e["snapshot_id"], e["pool"], e["strategy_version"], e["eligible"], e["reasons"],
                          e["decision_role"], e["logic_hash"], e["created_at"] + 999))   # 기록 시각만 다름
        assert cur.rowcount == 0 and dump(db) == template["baseline"]
        db.execute("ROLLBACK")
    finally:
        db.close()


@pytest.mark.parametrize("kind", ["product", "raw"])
@pytest.mark.parametrize("verb", ["INSERT OR IGNORE", "INSERT", "INSERT OR REPLACE", "REPLACE"])
@pytest.mark.parametrize("field", ELIGIBILITY_JUDGEMENT)
def test_eligibility_different_judgement_is_rejected(template, kind, verb, field):
    db = sqlite3.connect(template["path"])
    db.row_factory = sqlite3.Row
    try:
        e = _elig(db)
    finally:
        db.close()
    vals = {k: e[k] for k in ("snapshot_id", "pool", "strategy_version", "eligible", "reasons", "decision_role",
                              "logic_hash", "created_at")}
    vals[field] = (1 - vals[field]) if field == "eligible" else f"{vals[field]}~different"
    sql = (f"{verb} INTO ab_eligibility({', '.join(vals)}) VALUES ({', '.join('?' * len(vals))})")
    err, same = attempt(template, kind, sql, tuple(vals.values()))
    assert err and "eligibility conflict" in err and same, (field, err)


def test_record_eligibility_conflict_rolls_back_and_records_safety_event(template, tmp_path):
    """판정 대상을 고른 뒤 트랜잭션 시작 전에 다른 작업자가 같은 키에 **다른 판단**을 먼저 기록한 경우 (경합).
    INSERT OR IGNORE 여도 조용히 숨기지 않고 거부 → 이번 기록 전체 롤백, 안전 위반 이벤트, ``EligibilityConflict``."""
    path = str(tmp_path / "j.sqlite3")
    src, dst = sqlite3.connect(template["path"]), sqlite3.connect(path)
    src.backup(dst)
    src.close()
    dst.close()
    os.chmod(path, 0o600)
    raw = sqlite3.connect(path, isolation_level=None)
    try:                                         # 판정 기록이 아직 없는 새 스냅샷 (기존 수집·스냅샷 행 복사)
        first = raw.execute("SELECT scan_log_id FROM ab_signal_snapshots ORDER BY id LIMIT 1").fetchone()[0]
        cc = [r[1] for r in raw.execute("PRAGMA table_info(ab_scan_captures)")]
        raw.execute(f"INSERT INTO ab_scan_captures({', '.join(cc)}) SELECT " + ", ".join(
            "scan_log_id + 1000" if c == "scan_log_id" else "0" if c in ("rows_captured", "strict_rows") else c
            for c in cc) + f" FROM ab_scan_captures WHERE scan_log_id={first}")
        sc = [r[1] for r in raw.execute("PRAGMA table_info(ab_signal_snapshots)")]
        raw.execute(f"INSERT INTO ab_signal_snapshots({', '.join(sc)}) SELECT " + ", ".join(
            "NULL" if c == "id" else "scan_log_id + 1000" if c == "scan_log_id" else c for c in sc)
            + f" FROM ab_signal_snapshots WHERE scan_log_id={first}")
        new = [r[0] for r in raw.execute("SELECT id FROM ab_signal_snapshots s WHERE NOT EXISTS (SELECT 1 FROM "
                                         "ab_eligibility e WHERE e.snapshot_id=s.id) ORDER BY id")]
        pool = raw.execute("SELECT pool FROM ab_eligibility ORDER BY pool DESC LIMIT 1").fetchone()[0]
    finally:
        raw.close()
    assert new
    real = connect_rw(path)

    class Racing:                                # 대상 조회 후 BEGIN 직전에 다른 작업자가 다른 판단을 기록
        raced = False

        def __getattr__(self, name):
            return getattr(real, name)

        def execute(self, sql, *a):
            if sql == "BEGIN IMMEDIATE" and not Racing.raced:
                Racing.raced = True
                other = sqlite3.connect(path, isolation_level=None)
                other.execute("INSERT INTO ab_eligibility(snapshot_id, pool, strategy_version, eligible, reasons, "
                              "decision_role, logic_hash, created_at) VALUES (?,?,?,?,?,?,?,?)",
                              (new[-1], pool, S.STRATEGY_VERSION, 1, '["forged"]', "FORGED", "forged", 0))
                other.close()
            return real.execute(sql, *a)
    try:
        before = real.execute("SELECT COUNT(*) FROM ab_eligibility").fetchone()[0]
        with pytest.raises(EN.EligibilityConflict):
            EN.record_eligibility(Racing())
        assert Racing.raced
        assert real.execute("SELECT COUNT(*) FROM ab_eligibility").fetchone()[0] == before + 1   # 경합 행만 (전체 롤백)
        forged = real.execute("SELECT * FROM ab_eligibility WHERE decision_role='FORGED'").fetchone()
        assert forged["reasons"] == '["forged"]' and forged["snapshot_id"] == new[-1]           # 기존 행 유지
        ev = real.execute("SELECT * FROM ab_events WHERE kind='eligibility_conflict'").fetchone()
        assert ev["severity"] == "ERROR" and json.loads(ev["details"])["snapshot_id"] == new[-1]
        assert not real.in_transaction
    finally:
        real.close()


# ── 7. 정상 경로 유지 ─────────────────────────────────────────────────────────

def test_normal_paths_still_work(template, tmp_path):
    path = str(tmp_path / "j.sqlite3")
    src, dst = sqlite3.connect(template["path"]), sqlite3.connect(path)
    src.backup(dst)
    src.close()
    dst.close()
    os.chmod(path, 0o600)
    db = connect_rw(path)
    try:
        with tx(db):
            set_runtime(db, "k", {"a": 1})
            set_runtime(db, "k", {"a": 2})                                 # 런타임 upsert
            db.execute("UPDATE ab_runs SET summary=? WHERE run_id=?", ('{"x": 1}', template["cur"]))   # CURRENT run
            db.execute("UPDATE ab_account_runs SET cash=cash, updated_at=1 WHERE run_id=?", (template["cur"],))
        assert get_runtime(db, "k") == {"a": 2}
        assert EN.record_eligibility(db) == 0                              # 이미 기록 → 0건 (오류 없음)
    finally:
        db.close()
