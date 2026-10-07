"""Step A: no real scanner, notifications, credentials, or production database."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
import os
from uuid import uuid4
from pathlib import Path
from threading import Barrier

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from agent_runtime.event_engine import (
    ConcurrencyConflict, IdempotencyConflict, RuntimeStore, StaleObservation,
)
from agent_runtime.schemas import (
    DataQuality, DeliveryTarget, ErrorCode, EventInput, EventType,
    HoldingStatus, Observation, RuntimeSettings, Severity,
)
from database.models import (
    AgentDelivery, AgentEvent, AgentObservation, AgentRuntimeState, Base,
)

NOW = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)


@pytest.fixture
def runtime_database_url(tmp_path):
    """Opt-in PostgreSQL uses only a private local socket and disposable schema."""
    raw = os.environ.get("RUNTIME_TEST_POSTGRES_URL")
    if not raw:
        yield f"sqlite:///{tmp_path / 'runtime.db'}"
        return
    url = make_url(raw)
    socket = url.query.get("host", "")
    assert url.get_backend_name() == "postgresql"
    assert not url.host and url.database == "runtime_foundation_test"
    assert socket.startswith("/private/tmp/scanner-runtime-pg-")
    assert set(url.query) == {"host"}
    schema = "runtime_test_" + uuid4().hex
    admin = create_engine(url)
    try:
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        yield url.update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(hide_password=False)
    finally:
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def database(runtime_database_url):
    sqlite = runtime_database_url.startswith("sqlite:")
    engine = create_engine(runtime_database_url,
                           connect_args={"check_same_thread": False, "timeout": 10} if sqlite else {})
    if sqlite:
        @event.listens_for(engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    yield engine, sessions
    engine.dispose()


def store(database, **options):
    settings = dict(enabled=True, delivery_enabled=True)
    settings.update(options)
    return RuntimeStore(database[1], RuntimeSettings(**settings))


def observation(number=1, **overrides):
    values = dict(id=f"scan-{number}-holding-1", episode_id="position-1-generation-1",
                  account_id=1, holding_id=1, market="US", ticker="TEST",
                  scan_sequence=number, observed_at=NOW + timedelta(minutes=number),
                  data_as_of=NOW, strategy_version="e2bf7be", input_version="risk-1",
                  status=HoldingStatus.REVIEW, data_quality=DataQuality.AVAILABLE,
                  payload={"reason": "합성 RS 경고", "metrics": [1.2, None, True]})
    values.update(overrides)
    return Observation(**values)


def fact(key="alert", **overrides):
    values = dict(key=key, event_type=EventType.HOLDING_ALERT_DUE,
                  severity=Severity.MEDIUM, payload={"reason": "합성 경고"},
                  targets=(DeliveryTarget("telegram", "holding-alerts"),))
    values.update(overrides)
    return EventInput(**values)


def apply(runtime, item=None, version=0, events=None):
    item = item or observation()
    runtime.record_observation(item)
    return runtime.apply_observation(item.id, expected_version=version,
                                     events=(fact(),) if events is None else events,
                                     now=NOW + timedelta(hours=1))


def count(database, model):
    with database[1]() as session:
        return session.query(model).count()


def test_disabled_has_no_database_io():
    def forbidden():
        pytest.fail("disabled runtime opened a session")
    runtime = RuntimeStore(forbidden, RuntimeSettings())
    assert runtime.record_observation(observation()) is None
    assert runtime.apply_observation("missing", expected_version=0, now=NOW) is None
    assert runtime.get_state("episode") is None
    assert runtime.list_events("episode") == []
    assert runtime.list_deliveries() == []
    assert runtime.claim_due(now=NOW) == []
    assert not runtime.mark_sent("id", "token", now=NOW)
    assert not runtime.mark_failed("id", "token", now=NOW)


def test_config_flags_default_off(monkeypatch):
    import config
    import importlib
    monkeypatch.delenv("AGENT_RUNTIME_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_DELIVERY_ENABLED", raising=False)
    importlib.reload(config)
    assert RuntimeSettings.from_config() == RuntimeSettings()


def test_shadow_does_not_backfill_on_enable(database):
    runtime = store(database, delivery_enabled=False)
    first = apply(runtime)
    assert count(database, AgentEvent) == 1
    assert runtime.list_deliveries() == []
    enabled = store(database)
    second = apply(enabled)
    assert second.replayed and first.event_ids == second.event_ids
    assert enabled.list_deliveries() == []


def test_input_and_application_retries_survive_restarting_store(database):
    first = apply(store(database))
    restarted = store(database)
    replay = apply(restarted)
    assert replay.replayed and replay.event_ids == first.event_ids
    assert replay.version == 1
    assert count(database, AgentObservation) == 1
    assert count(database, AgentEvent) == 1
    assert count(database, AgentDelivery) == 1
    with database[1]() as session:
        row = session.get(AgentObservation, observation().id)
        assert json.loads(row.payload_json) == observation().payload
        assert row.data_as_of == NOW.replace(tzinfo=None)


def test_json_key_order_and_timezone_do_not_change_identity(database):
    runtime = store(database)
    item = observation(payload={"b": 2, "a": 1})
    runtime.record_observation(item)
    other = replace(item, payload={"a": 1, "b": 2},
                    observed_at=item.observed_at.astimezone(timezone(timedelta(hours=9))))
    assert runtime.record_observation(other) == item.id


@pytest.mark.parametrize("change", [dict(payload={"changed": True}), dict(status=HoldingStatus.HOLD),
                                    dict(ticker="OTHER"), dict(strategy_version="new")])
def test_reusing_identity_with_different_input_is_rejected(database, change):
    runtime = store(database)
    runtime.record_observation(observation())
    with pytest.raises(IdempotencyConflict):
        runtime.record_observation(observation(**change))
    assert count(database, AgentObservation) == 1


def test_cursor_identity_cannot_be_replaced_by_new_id(database):
    runtime = store(database)
    runtime.record_observation(observation())
    with pytest.raises(IdempotencyConflict):
        runtime.record_observation(observation(id="different"))


@pytest.mark.parametrize("payload", [{"bad": float('nan')}, {"bad": float('inf')},
                                     {1: "not a string key"}, {"bad": object()},
                                     {"bad": (1, 2)}, []])
def test_malformed_json_is_rejected_before_writes(database, payload):
    runtime = store(database)
    with pytest.raises(ValueError):
        runtime.record_observation(observation(payload=payload))
    assert count(database, AgentObservation) == 0


@pytest.mark.parametrize("change", [dict(scan_sequence=0), dict(revision=-1), dict(account_id=True),
                                    dict(observed_at=NOW.replace(tzinfo=None)),
                                    dict(data_as_of=NOW + timedelta(days=1)), dict(market="JP"),
                                    dict(status=None), dict(data_as_of=None),
                                    dict(data_quality=DataQuality.UNAVAILABLE)])
def test_observation_contract_validation(database, change):
    with pytest.raises(ValueError):
        store(database).record_observation(observation(**change))


def test_state_transitions_keep_each_occurrence_and_failure_quality(database):
    runtime = store(database)
    apply(runtime, observation(status=HoldingStatus.HOLD), events=())
    apply(runtime, observation(2, status=HoldingStatus.REVIEW), version=1)
    apply(runtime, observation(3, status=HoldingStatus.HOLD), version=2)
    apply(runtime, observation(4, status=HoldingStatus.REVIEW), version=3)
    assert [row['sequence'] for row in runtime.list_events(observation().episode_id)] == [1, 2, 3]
    failed = observation(5, status=None, data_as_of=None, data_quality=DataQuality.UNAVAILABLE)
    apply(runtime, failed, version=4, events=(fact(event_type=EventType.DATA_QUALITY_CHANGED),))
    state = runtime.get_state(failed.episode_id)
    assert state['current_status'] is None
    assert state['last_valid_status'] == "REVIEW"
    assert state['data_quality'] == "UNAVAILABLE"
    apply(runtime, observation(6, status=HoldingStatus.HOLD), version=5)
    assert runtime.get_state(failed.episode_id)['last_valid_status'] == "HOLD"
    assert len(runtime.list_events(failed.episode_id, after_sequence=3)) == 2


def test_account_and_episode_isolation(database):
    runtime = store(database)
    apply(runtime)
    other = observation(id="other", episode_id="position-2-generation-1", account_id=2, holding_id=2)
    apply(runtime, other)
    assert count(database, AgentRuntimeState) == 2
    with pytest.raises(ValueError, match="identity"):
        apply(runtime, observation(2, holding_id=3), version=1)


def test_stale_cursor_and_stale_data_do_not_overwrite_state(database):
    runtime = store(database)
    apply(runtime, observation(2))
    with pytest.raises(StaleObservation):
        apply(runtime, observation(), version=1)
    with pytest.raises(StaleObservation):
        apply(runtime, observation(3, data_as_of=NOW-timedelta(days=1)), version=1)
    assert runtime.get_state(observation().episode_id)['version'] == 1
    assert count(database, AgentEvent) == 1


def test_conflicting_version_and_invalid_events_leave_state_unchanged(database):
    runtime = store(database)
    apply(runtime)
    with pytest.raises(ConcurrencyConflict):
        apply(runtime, observation(2), version=0)
    with pytest.raises(ValueError, match="duplicate event"):
        apply(runtime, observation(2), version=1, events=(fact(), fact()))
    assert runtime.get_state(observation().episode_id)['version'] == 1
    assert count(database, AgentEvent) == 1


def test_outbox_failure_rolls_back_state_and_events(database):
    engine, _ = database
    runtime = store(database)
    def fail_outbox(conn, cursor, statement, parameters, context, many):
        if statement.startswith('INSERT INTO agent_deliveries'):
            raise RuntimeError("simulated outbox storage failure")
    event.listen(engine, 'before_cursor_execute', fail_outbox)
    try:
        with pytest.raises(RuntimeError, match="outbox"):
            apply(runtime)
    finally:
        event.remove(engine, 'before_cursor_execute', fail_outbox)
    assert count(database, AgentObservation) == 1
    assert count(database, AgentRuntimeState) == 0
    assert count(database, AgentEvent) == 0
    assert count(database, AgentDelivery) == 0
    assert apply(runtime).version == 1


def test_parallel_observation_inserts_are_idempotent(database):
    barrier = Barrier(2)
    def write(_):
        runtime = store(database)
        barrier.wait(timeout=10)
        return runtime.record_observation(observation())
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(write, range(2))) == [observation().id]*2
    assert count(database, AgentObservation) == 1


def test_parallel_state_updates_have_one_winner(database):
    engine, _ = database
    runtime = store(database)
    apply(runtime)
    runtime.record_observation(observation(2))
    barrier = Barrier(2)
    def align(conn, cursor, statement, parameters, context, many):
        if statement.startswith('UPDATE agent_runtime_states'):
            barrier.wait(timeout=10)
    event.listen(engine, 'before_cursor_execute', align)
    def write(_):
        try:
            return store(database).apply_observation(observation(2).id, expected_version=1,
                                                     events=(fact(),), now=NOW+timedelta(hours=1))
        except ConcurrencyConflict:
            return None
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(write, range(2)))
    finally:
        event.remove(engine, 'before_cursor_execute', align)
    assert sum(outcome is not None for outcome in outcomes) == 1
    assert count(database, AgentEvent) == 2
    assert count(database, AgentDelivery) == 2


def test_parallel_delivery_claims_do_not_share_a_lease(database):
    runtime = store(database)
    apply(runtime)
    barrier = Barrier(2)
    def claim(_):
        barrier.wait(timeout=10)
        return store(database).claim_due(now=NOW+timedelta(hours=1))
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = [c for result in pool.map(claim, range(2)) for c in result]
    assert len(claims) == 1
    assert claims[0].attempts == 1


def test_failure_retries_after_restart_and_success_is_terminal(database):
    runtime = store(database)
    apply(runtime)
    now = NOW+timedelta(hours=1)
    first = runtime.claim_due(now=now)[0]
    assert runtime.mark_failed(first.id, first.token, now=now, error_code=ErrorCode.TIMEOUT)
    assert runtime.claim_due(now=now+timedelta(seconds=29)) == []
    restarted = store(database)
    next_time = now+timedelta(seconds=30)
    second = restarted.claim_due(now=next_time)[0]
    assert second.id == first.id and second.attempts == 2 and second.token != first.token
    assert not restarted.mark_sent(first.id, first.token, now=next_time)
    assert restarted.mark_sent(second.id, second.token, now=next_time)
    assert restarted.claim_due(now=now+timedelta(days=1)) == []
    assert restarted.list_deliveries(status="SENT")[0]['error_code'] is None


def test_expired_lease_recovery_and_stale_ack_are_fenced(database):
    runtime = store(database)
    apply(runtime)
    now = NOW+timedelta(hours=1)
    old = runtime.claim_due(now=now)[0]
    expired = now+timedelta(seconds=60)
    assert not runtime.mark_sent(old.id, old.token, now=expired)
    fresh = store(database).claim_due(now=expired)[0]
    assert fresh.token != old.token
    assert not runtime.mark_failed(old.id, old.token, now=expired)
    assert runtime.mark_sent(fresh.id, fresh.token, now=expired)


def test_retry_backoff_cap_and_exhaustion(database):
    runtime = store(database, max_attempts=3, retry_seconds=10, retry_max_seconds=15)
    apply(runtime)
    now = NOW+timedelta(hours=1)
    for delay in (10, 15, 15):
        claim = runtime.claim_due(now=now)[0]
        assert runtime.mark_failed(claim.id, claim.token, now=now)
        row = runtime.list_deliveries()[0]
        assert row['next_attempt_at'] == (now+timedelta(seconds=delay)).replace(tzinfo=None)
        now += timedelta(seconds=delay)
    assert runtime.list_deliveries()[0]['status'] == 'FAILED'
    assert runtime.claim_due(now=now+timedelta(days=1)) == []


def test_crashed_final_attempt_becomes_failed(database):
    runtime = store(database, max_attempts=1)
    apply(runtime)
    now = NOW+timedelta(hours=1)
    runtime.claim_due(now=now)
    assert runtime.claim_due(now=now+timedelta(seconds=60)) == []
    row = runtime.list_deliveries()[0]
    assert row['status'] == 'FAILED' and row['error_code'] == 'ATTEMPTS_EXHAUSTED'


def test_delivery_flag_off_prevents_claim_and_ack(database):
    runtime = store(database)
    apply(runtime)
    now = NOW+timedelta(hours=1)
    claim = runtime.claim_due(now=now)[0]
    off = store(database, delivery_enabled=False)
    assert off.claim_due(now=now) == []
    assert not off.mark_sent(claim.id, claim.token, now=now)
    assert not off.mark_failed(claim.id, claim.token, now=now)
    assert off.list_deliveries()[0]['status'] == 'SENDING'


def test_error_codes_and_route_keys_do_not_accept_provider_secrets(database):
    runtime = store(database)
    with pytest.raises(ValueError):
        apply(runtime, events=(fact(targets=(DeliveryTarget('telegram', 'https://secret'),)),))
    apply(runtime)
    now = NOW+timedelta(hours=1)
    claim = runtime.claim_due(now=now)[0]
    with pytest.raises(ValueError):
        runtime.mark_failed(claim.id, claim.token, now=now, error_code='arbitrary provider text')
    assert runtime.list_deliveries()[0]['status'] == 'SENDING'


def test_db_enforces_delivery_uniqueness_and_status(database):
    runtime = store(database)
    apply(runtime)
    with database[1]() as session:
        original = session.query(AgentDelivery).one()
        values = {c.name: getattr(original, c.name) for c in original.__table__.columns}
    values['id'] = 'another'
    with database[1]() as session:
        session.add(AgentDelivery(**values))
        with pytest.raises(IntegrityError):
            session.commit()
    values['destination_key'] = 'another-route'
    values['status'] = 'BOGUS'
    with database[1]() as session:
        session.add(AgentDelivery(**values))
        with pytest.raises(IntegrityError):
            session.commit()


def test_migration_preserves_legacy_rows_and_matches_model(runtime_database_url, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    import config
    url = runtime_database_url
    monkeypatch.setattr(config, 'DATABASE_DIRECT_URL', url)
    cfg = Config(str(Path(__file__).parents[1] / 'alembic.ini'))
    cfg.set_main_option('script_location', str(Path(__file__).parents[1] / 'alembic'))
    cfg.attributes['configure_logger'] = False
    command.upgrade(cfg, '7b21d9c4e6a0')
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO accounts (id, name) VALUES (99, 'preserve-me')"))
    command.upgrade(cfg, 'head')
    command.upgrade(cfg, 'head')
    with engine.connect() as conn:
        assert conn.execute(text('SELECT name FROM accounts WHERE id=99')).scalar() == 'preserve-me'
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []
        insp = inspect(conn)
        for table in [AgentObservation.__table__, AgentRuntimeState.__table__,
                      AgentEvent.__table__, AgentDelivery.__table__]:
            expected = {c.name for c in table.constraints if c.__class__.__name__ == 'CheckConstraint'}
            assert {c['name'] for c in insp.get_check_constraints(table.name)} == expected
    command.downgrade(cfg, '7b21d9c4e6a0')
    with engine.connect() as conn:
        assert conn.execute(text('SELECT name FROM accounts WHERE id=99')).scalar() == 'preserve-me'
        assert not any(n.startswith('agent_') for n in inspect(conn).get_table_names())
    command.upgrade(cfg, 'head')
    engine.dispose()


def test_postgresql_migration_compiles_offline_without_connecting(monkeypatch):
    from alembic import command
    from alembic.config import Config
    import config
    monkeypatch.setattr(config, 'DATABASE_DIRECT_URL', 'postgresql://invalid/isolated_compile_only')
    output = StringIO()
    cfg = Config(str(Path(__file__).parents[1] / 'alembic.ini'), output_buffer=output)
    cfg.set_main_option('script_location', str(Path(__file__).parents[1] / 'alembic'))
    cfg.attributes['configure_logger'] = False
    command.upgrade(cfg, '7b21d9c4e6a0:a41e7c9d2b60', sql=True)
    sql = output.getvalue()
    assert sql.count('CREATE TABLE agent_') == 4
    assert 'uq_agent_delivery_route' in sql
    assert 'DROP TABLE' not in sql and 'ALTER TABLE holdings' not in sql
