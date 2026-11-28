"""auto_buy_us 저널 스키마 abu0001 과 전용 Migration. 네트워크·운영 DB 없음."""
import glob
import os
import sqlite3
from pathlib import Path

import pytest

from trading.auto_buy_us.schema import (EXPECTED_SCHEMA_VERSION, OWNED_PREFIX, VERSION_TABLE, schema_status,
                                        structure_diff, upgrade_journal)

TABLES = {"abu_signal_days", "abu_signals", "abu_decisions", "abu_orders", "abu_order_events", "abu_fills",
          "abu_positions", "abu_sleeves", "abu_quotes", "abu_account_snapshots", "abu_daily_perf",
          "abu_system_events", "abu_api_calls", "abu_runtime"}


@pytest.fixture
def jp(tmp_path):
    p = str(tmp_path / "j.sqlite")
    upgrade_journal(p, allow_create=True)
    return p


def conn(p):
    db = sqlite3.connect(p)
    db.execute("PRAGMA foreign_keys=ON")
    return db


def test_constants():
    assert (EXPECTED_SCHEMA_VERSION, VERSION_TABLE, OWNED_PREFIX) == ("abu0001", "abu_alembic_version", "abu_")


def test_status_missing_file_does_not_create(tmp_path):
    p = tmp_path / "j.sqlite"
    st = schema_status(str(p))
    assert st["schema_ready"] is False and st["schema_version"] is None and st["schema_error"] and not p.exists()
    assert schema_status(None)["schema_ready"] is False


def test_upgrade_creates_expected_tables(tmp_path):
    p = str(tmp_path / "j.sqlite")
    assert upgrade_journal(p, allow_create=True) == "abu0001"
    st = schema_status(p)
    assert st["schema_ready"] and st["schema_version"] == "abu0001" and st["schema_error"] is None
    db = sqlite3.connect(p)
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert TABLES <= names and VERSION_TABLE in names
    assert structure_diff(db) == []
    db.close()


def test_upgrade_requires_allow_create(tmp_path):
    with pytest.raises(FileNotFoundError):
        upgrade_journal(str(tmp_path / "x.sqlite"))


def test_upgrade_rerun_idempotent(jp):
    assert upgrade_journal(jp) == "abu0001"


def test_status_detects_tampered_structure(jp):
    db = sqlite3.connect(jp)
    db.execute("DROP TRIGGER abu_fills_no_update")
    db.commit()
    db.close()
    st = schema_status(jp)
    assert not st["schema_ready"] and "abu_fills_no_update" in st["schema_error"]


def sig(db, n=1, ticker="AAPL"):
    db.execute("INSERT INTO abu_signals(session_day, scan_log_id, ticker, signal_key, is_first_detection, raw_json,"
               " strategy_version, captured_at) VALUES('2026-11-27', ?, ?, 'k', 1, '{}', 'RT1', 1.0)", (n, ticker))


def order(db, state="WORKING", ticker="AAPL", side="BUY", strategy="B00"):
    cur = db.execute("INSERT INTO abu_orders(strategy, side, kind, ticker, exchange, qty, limit_price, state, executor,"
                     " created_at, session_day) VALUES(?, ?, 'ENTRY', ?, 'NA', 1, '10', ?, 'SHADOW', 1.0, '2026-11-30')",
                     (strategy, side, ticker, state))
    return cur.lastrowid


@pytest.mark.parametrize("table", ["abu_signals", "abu_decisions", "abu_order_events", "abu_fills", "abu_quotes",
                                   "abu_signal_days", "abu_daily_perf", "abu_system_events"])
def test_immutable_tables_reject_update_delete(jp, table):
    db = conn(jp)
    sig(db)
    db.execute("INSERT INTO abu_signal_days VALUES('2026-11-27','CAPTURED',1,1.0)")
    db.execute("INSERT INTO abu_decisions(strategy, session_day, signal_id, ticker, exchange, verdict, reasons,"
               " strategy_version, created_at) VALUES('B00','2026-11-30',1,'AAPL','NA','PLANNED','[]','RT1',1.0)")
    oid = order(db, "PLANNED")
    db.execute("INSERT INTO abu_order_events VALUES(?,1,NULL,'PLANNED',1.0,'{}')", (oid,))
    db.execute("INSERT INTO abu_fills VALUES(?,1,1,'10',1.0,NULL,'CUR_V1','x')", (oid,))
    db.execute("INSERT INTO abu_quotes(ticker, exchange, requested_at, received_at, valid) VALUES('AAPL','NA',1,1,1)")
    db.execute("INSERT INTO abu_daily_perf VALUES('B00','2026-11-27','1','1','0',0,0,0,'COMPLETE',1.0)")
    db.execute("INSERT INTO abu_system_events(at, level, kind) VALUES(1.0,'INFO','k')")
    db.commit()
    col = db.execute(f"PRAGMA table_info({table})").fetchall()[0][1]
    with pytest.raises(sqlite3.IntegrityError, match="abu immutable"):
        db.execute(f"UPDATE {table} SET {col}={col}")
    with pytest.raises(sqlite3.IntegrityError, match="abu immutable"):
        db.execute(f"DELETE FROM {table}")


def test_system_events_allows_only_notified_flip(jp):
    db = conn(jp)
    db.execute("INSERT INTO abu_system_events(at, level, kind, details_json) VALUES(1.0,'WARN','k','{}')")
    db.commit()
    with pytest.raises(sqlite3.IntegrityError, match="abu immutable"):
        db.execute("UPDATE abu_system_events SET kind='z'")
    with pytest.raises(sqlite3.IntegrityError, match="abu immutable"):
        db.execute("UPDATE abu_system_events SET notified=1, kind='z'")
    db.execute("UPDATE abu_system_events SET notified=1")
    db.commit()
    with pytest.raises(sqlite3.IntegrityError, match="abu immutable"):
        db.execute("UPDATE abu_system_events SET notified=0")
    with pytest.raises(sqlite3.IntegrityError, match="abu immutable"):
        db.execute("DELETE FROM abu_system_events")


def test_signals_unique_scan_ticker(jp):
    db = conn(jp)
    sig(db)
    with pytest.raises(sqlite3.IntegrityError):
        sig(db)


@pytest.mark.parametrize("state", ["FILLED", "EXPIRED", "EXPIRED_UNOBSERVED", "NOT_STARTED", "SKIPPED_OPEN",
                                   "BLOCKED", "CANCELLED"])
def test_terminal_order_rejects_update(jp, state):
    db = conn(jp)
    oid = order(db, state)
    with pytest.raises(sqlite3.IntegrityError, match="abu immutable"):
        db.execute("UPDATE abu_orders SET qty=2 WHERE id=?", (oid,))


def test_open_order_updates_allowed_and_bad_state_rejected(jp):
    db = conn(jp)
    oid = order(db, "PLANNED")
    db.execute("UPDATE abu_orders SET state='WORKING' WHERE id=?", (oid,))
    db.execute("UPDATE abu_orders SET state='FILLED', ended_at=2.0 WHERE id=?", (oid,))
    with pytest.raises(sqlite3.IntegrityError):
        order(db, "BOGUS", ticker="MSFT")


def test_one_open_order_per_strategy_ticker_side(jp):
    db = conn(jp)
    order(db, "WORKING")
    with pytest.raises(sqlite3.IntegrityError):
        order(db, "PLANNED")
    order(db, "WORKING", side="SELL")            # 방향 다르면 허용
    order(db, "WORKING", strategy="B01")         # 전략 다르면 허용
    order(db, "FILLED")                          # 종료 주문은 제한 없음


def test_one_live_position_per_strategy_ticker(jp):
    db = conn(jp)
    ins = ("INSERT INTO abu_positions(strategy, ticker, exchange, qty, entry_price, initial_stop, state, opened_at,"
           " opened_day) VALUES('B00','AAPL','NA',1,'10','9',?,1.0,'2026-11-30')")
    db.execute(ins, ("OPEN",))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(ins, ("EXITING",))
    db.execute("UPDATE abu_positions SET state='CLOSED'")
    db.execute(ins, ("OPEN",))


def test_foreign_keys(jp):
    db = conn(jp)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO abu_fills VALUES(99,1,1,'10',1.0,NULL,'m','b')")


def test_runtime_store_never_runs_ddl():
    root = Path(__file__).resolve().parent.parent / "trading" / "auto_buy_us"
    files = [f for f in glob.glob(str(root / "*.py")) if os.path.basename(f) != "schema.py"]
    assert files
    for f in files:
        src = Path(f).read_text()
        assert "CREATE TABLE" not in src.upper() and "upgrade_journal" not in src, f


def test_s01_journal_untouched(tmp_path):
    from trading.auto_stop.schema import upgrade_journal as s01_upgrade
    s01 = str(tmp_path / "s01.sqlite")
    s01_upgrade(s01, allow_create=True)
    abu = str(tmp_path / "abu.sqlite")
    upgrade_journal(abu, allow_create=True)
    db = sqlite3.connect(s01)
    assert not [r for r in db.execute("SELECT name FROM sqlite_master WHERE name GLOB 'abu_*'")]
    db.close()
    db = sqlite3.connect(abu)
    assert not [r for r in db.execute("SELECT name FROM sqlite_master WHERE name GLOB 'auto_stop_*'")]
    db.close()


def test_upgrade_in_shared_file_keeps_other_objects(tmp_path):
    p = str(tmp_path / "s.sqlite")
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE scanner_buy_orders(id TEXT PRIMARY KEY)")
    db.execute("INSERT INTO scanner_buy_orders VALUES('o1')")
    db.commit()
    db.close()
    upgrade_journal(p)
    db = sqlite3.connect(p)
    assert db.execute("SELECT id FROM scanner_buy_orders").fetchall() == [("o1",)]


# ── 배포 스크립트 ───────────────────────────────────────────────
def run_script(*args):
    from scripts.auto_buy_us_journal_migrate import main
    return main(list(args))


def test_migrate_script_check_mode_no_change(tmp_path, jp, capsys):
    before = (os.stat(jp).st_mtime_ns, Path(jp).read_bytes())
    assert run_script("--journal", jp, "--backup-dir", str(tmp_path / "bk"), "--check") == 0
    assert (os.stat(jp).st_mtime_ns, Path(jp).read_bytes()) == before and not (tmp_path / "bk").exists()
    # 파일 없음 + --check: 만들지 않는다
    miss = tmp_path / "none.sqlite"
    assert run_script("--journal", str(miss), "--backup-dir", str(tmp_path / "bk"), "--check", "--allow-create") == 0
    assert not miss.exists()


def test_migrate_script_missing_journal_exit2(tmp_path):
    assert run_script("--journal", str(tmp_path / "n.sqlite"), "--backup-dir", str(tmp_path / "bk")) == 2
    assert run_script("--journal", "", "--backup-dir", str(tmp_path / "bk")) == 2


def test_migrate_script_create_then_backup_on_rerun(tmp_path):
    p = str(tmp_path / "n.sqlite")
    assert run_script("--journal", p, "--backup-dir", str(tmp_path / "bk"), "--allow-create") == 0
    assert schema_status(p)["schema_ready"]
    assert run_script("--journal", p, "--backup-dir", str(tmp_path / "bk")) == 0
    assert list((tmp_path / "bk").glob("*.sqlite3"))


def test_migrate_script_verify_failure_exit4(tmp_path, monkeypatch):
    import scripts.auto_buy_us_journal_migrate as m
    real = m.expected_objects
    monkeypatch.setattr(m, "expected_objects", lambda: {**real(), "abu_ghost": ("table", "x")})
    p = str(tmp_path / "n.sqlite")
    assert run_script("--journal", p, "--backup-dir", str(tmp_path / "bk"), "--allow-create") == 4


def test_migrate_script_failure_exit3_rolls_back(tmp_path, monkeypatch):
    import scripts.auto_buy_us_journal_migrate as m
    p = tmp_path / "n.sqlite"
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE abu_runtime(key TEXT)")      # 다른 정의의 기존 객체 → 중단
    db.commit()
    db.close()
    before = Path(p).read_bytes()
    assert run_script("--journal", str(p), "--backup-dir", str(tmp_path / "bk")) == 3
    db = sqlite3.connect(p)
    assert VERSION_TABLE not in {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
