"""P1: 런타임 DDL 제거와 저널 전용 Migration. 실계좌·운영 DB·네트워크 호출 없음."""
import os
import sqlite3
from decimal import Decimal

import pytest

from tests.auto_stop_fakes import FakeBroker, Harness, migrated_journal
from trading.auto_stop import runner, schema
from trading.auto_stop.store import AutoStopStore

SCANNER_DDL = '''CREATE TABLE scanner_buy_orders (
    id TEXT PRIMARY KEY, account_profile TEXT NOT NULL, market TEXT NOT NULL, currency TEXT NOT NULL,
    scan_result_id INTEGER, ticker TEXT NOT NULL, name TEXT, exchange TEXT NOT NULL,
    quantity INTEGER NOT NULL, price TEXT NOT NULL, state TEXT NOT NULL, order_number TEXT,
    broker_state TEXT, created_at REAL NOT NULL, expires_at REAL NOT NULL, sent_at REAL,
    updated_at REAL NOT NULL, details TEXT NOT NULL)'''


def scanner_journal(path, state="FILLED"):
    db = sqlite3.connect(path)
    db.execute(SCANNER_DDL)
    db.execute("CREATE UNIQUE INDEX scanner_buy_in_flight ON scanner_buy_orders(account_profile, ticker) "
               "WHERE state IN ('SENDING', 'UNKNOWN')")
    db.execute("INSERT INTO scanner_buy_orders VALUES('o1','account1','US','USD',1,'AAPL',NULL,'ND',2,'190.00',"
               "?, '123', NULL, 1, 2, 1, 1, '{}')", (state,))
    db.commit()
    db.close()
    return str(path)


def objects(path):
    db = sqlite3.connect(path)
    try:
        return sorted(db.execute("SELECT type, name, sql FROM sqlite_master"))
    finally:
        db.close()


def legacy_journal(path):
    """이전 자체 DDL 방식(AutoStopStore 생성자)이 만들던 저널."""
    from trading.auto_stop.journal_migrations.ddl_v1 import DDL_V1
    scanner_journal(path)
    db = sqlite3.connect(path)
    for stmt in DDL_V1:
        db.execute(stmt)
    db.execute("INSERT INTO auto_stop_runtime(key, value, updated_at) VALUES('heartbeat_at', '1', 1)")
    db.commit()
    db.close()
    return str(path)


@pytest.fixture
def env(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(runner, "notifier", lambda sev, text: sent.append((sev, text)) or True)
    monkeypatch.setattr(runner, "_SCHEMA_NOTICES", {})

    def no_broker(mode):
        raise AssertionError("스키마 미준비인데 브로커를 만들었습니다")
    monkeypatch.setattr(runner, "broker_factory", no_broker)

    def set_env(mode, journal):
        monkeypatch.setenv("AUTO_STOP_US_MODE", mode)
        monkeypatch.setenv("SCANNER_ORDER_JOURNAL_PATH", str(journal))
        monkeypatch.setenv("KIWOOM_WEB_ENABLED", "true")
    set_env.sent = sent
    return set_env


# ── 런타임 DDL 없음 ────────────────────────────────────────────
def test_off_scheduler_tick_runs_no_ddl(env, tmp_path):
    import scheduler as sched
    j = scanner_journal(tmp_path / "j.sqlite3")
    before = objects(j)
    env("OFF", j)
    assert runner.auto_stop_us_tick()["status"] == "SCHEMA_NOT_READY"
    sched._run_auto_stop()                                    # 스케줄러 작업 진입점 그대로
    assert objects(j) == before
    assert env.sent == []                                     # OFF + 스키마 없음: 조용히 건너뜀


def test_runtime_never_creates_missing_journal(env, tmp_path):
    j = tmp_path / "missing.sqlite3"
    env("SHADOW", j)
    assert runner.auto_stop_us_tick()["status"] == "SCHEMA_NOT_READY"
    assert runner.store_or_none() is None
    assert schema.schema_status(str(j))["journal_exists"] is False
    with pytest.raises(sqlite3.OperationalError):
        AutoStopStore(j).runtime_get("x")
    assert not os.path.exists(j)


def test_store_constructor_runs_no_ddl(tmp_path):
    j = scanner_journal(tmp_path / "j.sqlite3")
    before = objects(j)
    AutoStopStore(j)
    assert objects(j) == before


def _client(monkeypatch, tmp_path, journal, mode="OFF"):
    from fastapi.testclient import TestClient
    from web import app as app_module
    monkeypatch.setattr(app_module, "start_scheduler", lambda: None, raising=False)
    monkeypatch.setattr(app_module, "stop_scheduler", lambda: None, raising=False)
    monkeypatch.setattr(app_module, "SITES_API_KEY", "k" * 32)
    monkeypatch.setenv("SCANNER_ORDER_JOURNAL_PATH", str(journal))
    monkeypatch.setenv("AUTO_STOP_US_MODE", mode)
    c = TestClient(app_module.app)
    c.headers["Authorization"] = "Bearer " + "k" * 32
    return c


@pytest.mark.parametrize("make", ["scanner_only", "missing"])
def test_off_health_api_runs_no_ddl(monkeypatch, tmp_path, make):
    j = tmp_path / "j.sqlite3"
    if make == "scanner_only":
        scanner_journal(j)
    before = objects(j) if j.exists() else None
    c = _client(monkeypatch, tmp_path, j)
    body = c.get("/api/auto-stop/health").json()
    assert body["mode"] == "OFF" and body["schema_ready"] is False and body["migration_required"] is True
    assert body["expected_schema_version"] == schema.EXPECTED_SCHEMA_VERSION and body["schema_version"] is None
    assert body["journal_exists"] is (make == "scanner_only") and body["schema_error"]
    assert "스키마" in body["live_blocked_reason"] or "LIVE 아님" in body["live_blocked_reason"]
    assert c.get("/api/auto-stop/positions").status_code == 503       # store 를 열지 않는다
    if before is None:
        assert not j.exists()
    else:
        assert objects(j) == before


def test_health_reports_ready_after_migration(monkeypatch, tmp_path):
    j = migrated_journal(tmp_path / "j.sqlite3")
    body = _client(monkeypatch, tmp_path, j).get("/api/auto-stop/health").json()
    assert body["schema_ready"] is True and body["schema_version"] == "as0001"
    assert body["migration_required"] is False and body["schema_error"] is None


# ── Migration ──────────────────────────────────────────────────
def migrate(journal, tmp_path, **kw):
    from scripts.auto_stop_journal_migrate import run
    return run(str(journal), str(tmp_path / "backups"), **kw)


def test_migrate_scanner_only_journal_preserves_data(tmp_path):
    from scripts.auto_stop_journal_migrate import snapshot
    j = scanner_journal(tmp_path / "j.sqlite3")
    before = snapshot(j)
    rep = migrate(j, tmp_path)
    after = snapshot(j)
    assert rep["result"] == "OK" and rep["schema_version"] == "as0001"
    assert after["tables"]["scanner_buy_orders"] == before["tables"]["scanner_buy_orders"]
    assert {o["name"]: o["sql"] for o in before["objects"]}.items() <= \
        {o["name"]: o["sql"] for o in after["objects"]}.items()
    assert set(schema.expected_objects()) <= set(rep["added_objects"])
    assert os.path.isfile(rep["backup"]) and rep["scanner_journal"] == "compatible"
    assert schema.schema_status(j)["schema_ready"]


def test_migrate_new_journal_requires_explicit_create(tmp_path):
    from scripts.auto_stop_journal_migrate import Abort
    j = tmp_path / "new.sqlite3"
    with pytest.raises(Abort) as exc:
        migrate(j, tmp_path)
    assert exc.value.code == 2 and not j.exists()
    rep = migrate(j, tmp_path, allow_create=True)
    assert rep["result"] == "OK" and schema.schema_status(str(j))["schema_ready"]


def test_migrate_rerun_is_noop(tmp_path):
    from scripts.auto_stop_journal_migrate import snapshot
    j = scanner_journal(tmp_path / "j.sqlite3")
    migrate(j, tmp_path)
    AutoStopStore(j).runtime_set("k", 1, 0)
    first = snapshot(j)
    rep = migrate(j, tmp_path)
    assert rep["result"] == "OK" and rep["added_objects"] == []
    assert snapshot(j)["tables"] == first["tables"]


def test_migrate_legacy_self_ddl_journal_verifies_structure(tmp_path):
    j = legacy_journal(tmp_path / "j.sqlite3")
    st = schema.schema_status(j)
    assert not st["schema_ready"] and st["schema_version"] is None and st["reconcile_compatible"]
    rep = migrate(j, tmp_path)
    assert rep["result"] == "OK" and rep["legacy_structure_diff"] == [] and rep["added_objects"] == [
        schema.VERSION_TABLE]
    assert AutoStopStore(j).runtime_get("heartbeat_at") == 1                 # 기존 자동 손절 데이터 보존


def test_migrate_aborts_on_schema_mismatch_and_keeps_original(tmp_path):
    from scripts.auto_stop_journal_migrate import Abort
    j = scanner_journal(tmp_path / "j.sqlite3")
    db = sqlite3.connect(j)
    db.execute("CREATE TABLE auto_stop_runtime (key TEXT PRIMARY KEY, value TEXT)")    # 다른 정의
    db.commit()
    db.close()
    before = objects(j)
    with pytest.raises(Abort) as exc:
        migrate(j, tmp_path)
    assert exc.value.code == 3 and "다릅니다" in str(exc.value)
    assert objects(j) == before                                    # 롤백: 버전 표·신규 객체 없음
    assert not schema.schema_status(j)["schema_ready"]


def test_migration_failure_rolls_back_fresh_ddl(tmp_path, monkeypatch):
    from trading.auto_stop.journal_migrations import ddl_v1
    j = scanner_journal(tmp_path / "j.sqlite3")
    before = objects(j)
    monkeypatch.setattr(ddl_v1, "DDL_V1", ddl_v1.DDL_V1 + ("CREATE TABLE broken (",))
    import importlib, sys
    sys.modules.pop("as0001_auto_stop_initial", None)
    importlib.invalidate_caches()
    with pytest.raises(Exception, match="incomplete input|syntax error"):
        schema.upgrade_journal(j)
    assert objects(j) == before                                    # 앞선 CREATE 문까지 모두 롤백


def test_migrate_refuses_unresolved_orders_without_flag(tmp_path):
    from scripts.auto_stop_journal_migrate import Abort
    j = scanner_journal(tmp_path / "j.sqlite3", state="UNKNOWN")
    with pytest.raises(Abort) as exc:
        migrate(j, tmp_path)
    assert exc.value.code == 2 and "미확정" in str(exc.value)
    assert not schema.schema_status(j)["schema_ready"]
    assert migrate(j, tmp_path, allow_unresolved=True)["result"] == "OK"


def test_migrate_check_only_writes_nothing(tmp_path):
    j = scanner_journal(tmp_path / "j.sqlite3")
    before = objects(j)
    assert migrate(j, tmp_path, check=True)["result"] == "CHECK_ONLY"
    assert objects(j) == before and not (tmp_path / "backups").exists()


def test_scanner_buy_journal_still_works_after_migration(tmp_path):
    from trading.scanner_buy_orders import ScannerBuyOrders
    j = scanner_journal(tmp_path / "j.sqlite3")
    migrate(j, tmp_path)
    before = objects(j)
    orders = ScannerBuyOrders(j, broker_factory=lambda market: None)
    with orders.db() as db:
        assert db.execute("SELECT state FROM scanner_buy_orders WHERE id='o1'").fetchone()[0] == "FILLED"
    assert objects(j) == before


# ── 모드별 차단 ────────────────────────────────────────────────
def test_shadow_without_schema_blocks_and_warns(env, tmp_path):
    j = scanner_journal(tmp_path / "j.sqlite3")
    env("SHADOW", j)
    assert runner.auto_stop_us_tick()["status"] == "SCHEMA_NOT_READY"
    assert runner.auto_stop_us_tick()["status"] == "SCHEMA_NOT_READY"
    assert [s for s, _ in env.sent] == ["WARN"]                          # 하루 1회
    assert "Migration" in env.sent[0][1] or "migrate" in env.sent[0][1]


def test_live_without_schema_blocks_everything(env, tmp_path, monkeypatch):
    from trading.auto_stop import settings as S
    j = scanner_journal(tmp_path / "j.sqlite3")
    env("LIVE", j)
    for k in ("KIWOOM_TRADING_ENABLED", "ACCOUNT1_ORDER_ENABLED"):
        monkeypatch.setenv(k, "true")
    monkeypatch.setenv("AUTO_STOP_US_VERIFIED_CONTRACTS", ",".join(S.CONTRACTS))
    assert runner.auto_stop_us_tick()["status"] == "SCHEMA_NOT_READY"
    assert [s for s, _ in env.sent] == ["URGENT"]
    assert "스키마" in S.live_order_block_reason()
    assert "스키마" in S.live_cancel_block_reason()


def trigger(h, price="44.00"):
    h.broker.price = Decimal(price)
    h.tick()


def test_schema_version_mismatch_blocks_new_orders_but_keeps_reconcile(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    pid = h.add()
    trigger(h)                                                   # LIVE 원주문 접수
    assert len(h.broker.sells) == 1
    db = sqlite3.connect(h.st.journal_path)
    db.execute(f"UPDATE {schema.VERSION_TABLE} SET version_num='as9999'")
    db.commit()
    db.close()
    sent = []
    go, block, st = runner.schema_gate(h.st, notify=lambda sev, text: sent.append(sev))
    assert go and block and st["reconcile_compatible"] and st["live_unresolved_orders"] == 1
    assert sent == ["URGENT"]
    h.engine.schema_block = block
    h.broker.fill("1000", 10)
    h.clock.advance(60)
    h.tick()
    assert h.orders()[0]["state"] == "FILLED" and h.pos(pid)["state"] == "SOLD"   # 대조는 계속
    other = h.add(ticker="T")                                    # 새 포지션이 손절가를 깨도
    h.broker.holding["T"].update(qty=10, sellable=10)
    trigger(h)
    assert len(h.broker.sells) == 1 and h.pos(other)["state"] == "ACTIVE"         # 신규 주문 없음


def test_broken_schema_with_auto_stop_records_sends_urgent_manual_check(env, tmp_path):
    j = scanner_journal(tmp_path / "j.sqlite3")
    db = sqlite3.connect(j)
    db.execute("CREATE TABLE auto_stop_orders (id INTEGER PRIMARY KEY, mode TEXT, state TEXT)")
    db.execute("INSERT INTO auto_stop_orders VALUES (1, 'LIVE', 'SUBMITTED')")
    db.commit()
    db.close()
    env("OFF", j)
    assert runner.auto_stop_us_tick()["status"] == "SCHEMA_NOT_READY"
    assert env.sent and env.sent[0][0] == "URGENT" and "키움 앱" in env.sent[0][1]


def test_off_keeps_reconciling_existing_live_order(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    pid = h.add()
    trigger(h)
    h.set(mode="OFF")
    h.broker.fill("1000", 10)
    h.clock.advance(60)
    h.tick()
    assert h.orders()[0]["state"] == "FILLED" and h.pos(pid)["state"] == "SOLD"
    assert len(h.broker.sells) == 1


def test_runner_tick_with_ready_schema_builds_engine(env, tmp_path, monkeypatch):
    j = migrated_journal(tmp_path / "j.sqlite3")
    env("OFF", j)
    monkeypatch.setattr(runner, "broker_factory", lambda mode: FakeBroker(lambda: 0))
    out = runner.auto_stop_us_tick()
    assert out["status"] == "OK" and "schema_block" not in out
