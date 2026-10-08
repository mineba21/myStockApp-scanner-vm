"""Shadow integration against synthetic holdings only; shared SQLite/PG fixture."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from threading import Barrier

import pandas as pd
import pytest
from sqlalchemy import event, text, inspect

from tests.test_agent_events import database, runtime_database_url, observation, NOW
from agent_runtime.schemas import RuntimeSettings, HoldingStatus, DataQuality
from agent_runtime.shadow import ShadowStore
from agent_runtime.holding_capture import HoldingCapture, create_capture, episode_anchor
from database.models import Account, Holding, Transaction, ScanLog, AgentObservation, AgentEvent, AgentDelivery, AgentScanCapture
from scanner import scan_engine, us_stocks, weinstein


@pytest.fixture
def enabled(monkeypatch):
    import config
    monkeypatch.setattr(config, 'AGENT_RUNTIME_ENABLED', True)
    monkeypatch.setattr(config, 'AGENT_DELIVERY_ENABLED', True)  # B must still force shadow.


def runtime(database):
    return ShadowStore(database[1], RuntimeSettings(enabled=True, delivery_enabled=True))


def persist(store, item, *, complete=True):
    store.begin_capture(item.scan_sequence, [dict(id=item.id, episode_id=item.episode_id,
                                               holding_id=item.holding_id)], now=NOW)
    store.record_observation(item)
    if complete:
        store.finish_capture(item.scan_sequence, now=NOW+timedelta(hours=2))


def item(number, status=HoldingStatus.HOLD, **kwargs):
    return observation(number, status=status, payload=dict(
        reason='synthetic', severity='MEDIUM' if status == HoldingStatus.REVIEW else None,
        comparison={'predicted_sell_due': status == HoldingStatus.REVIEW, 'matches': True}), **kwargs)


def daily():
    idx = pd.date_range('2025-01-01', periods=220, freq='B')
    return pd.DataFrame(dict(Open=100., High=101., Low=99., Close=100., Volume=1000.), index=idx)


def seed(database, *, missing_stop=False):
    with database[1]() as session:
        session.add(Account(id=1, name='synthetic', account_type='US_STOCK', currency='USD'))
        session.flush()
        session.add(Holding(id=1, account_id=1, ticker='TEST', name='synthetic', market='US',
                            quantity=1., avg_price=100., entry_price=100.,
                            current_stop_loss=None if missing_stop else 90.))
        session.commit()


def evaluation(database, monkeypatch, number, severity=None, *, fail=False):
    monkeypatch.setattr(us_stocks, 'get_us_ohlcv', lambda ticker: None if fail else daily())
    monkeypatch.setattr(weinstein, 'to_weekly_ohlcv', lambda df: df)
    monkeypatch.setattr(weinstein, 'check_sell_signal', lambda *a, **kw: None if severity is None else
                        {'severity': severity, 'sell_reason': 'synthetic'})
    with database[1]() as session:
        session.add(ScanLog(id=number, market='US', status='RUNNING'))
        session.commit()
        capture = HoldingCapture(number, database[1])
        signals = scan_engine._check_holdings(session, us_bench=daily()['Close'], capture=capture)
        assert session.query(AgentObservation).count() == number-1  # not written in scanner transaction
        session.get(ScanLog, number).status = 'DONE'
        session.commit()
    capture.finish()
    return signals, capture


def test_full_hold_suppressed_failure_and_recovery(database, enabled, monkeypatch):
    seed(database)
    for n, severity, fail in [(1, None, False), (2, 'MEDIUM', False), (3, 'MEDIUM', False),
                              (4, None, True), (5, 'MEDIUM', False), (6, None, False)]:
        evaluation(database, monkeypatch, n, severity, fail=fail)
    with database[1]() as session:
        rows = session.query(AgentObservation).order_by(AgentObservation.scan_sequence).all()
        assert [r.status for r in rows] == ['HOLD','REVIEW','REVIEW',None,'REVIEW','HOLD']
        assert rows[3].data_quality == 'UNAVAILABLE'
        assert json.loads(rows[2].payload_json)['comparison']['legacy_sell_due'] is False
        assert all(json.loads(r.payload_json)['comparison']['matches'] for r in rows)
        assert all(json.loads(r.payload_json)['bar_finality']=='UNKNOWN' for r in rows)
        assert session.query(AgentDelivery).count() == 0
        events = session.query(AgentEvent).all()
        assert sum(e.event_type=='DATA_QUALITY_CHANGED' for e in events) == 2
        assert sum(e.event_type=='HOLDING_STATE_CHANGED' for e in events) == 2
    assert runtime(database).report()['mismatches'] == 0


@pytest.mark.parametrize('severity', [None, 'LOW', 'MEDIUM', 'HIGH'])
@pytest.mark.parametrize('missing_stop', [False, True])
def test_same_signal_and_holding_state_with_capture_on_or_off(database, enabled, monkeypatch, severity, missing_stop):
    seed(database, missing_stop=missing_stop)
    monkeypatch.setattr(us_stocks, 'get_us_ohlcv', lambda ticker: daily())
    monkeypatch.setattr(weinstein, 'to_weekly_ohlcv', lambda df: df)
    monkeypatch.setattr(weinstein, 'check_sell_signal', lambda *a, **kw: None if severity is None else
                        dict(severity=severity, sell_reason='synthetic'))
    # Fix timestamps, so exact results and legacy state can be compared.
    class Clock(datetime):
        @classmethod
        def utcnow(cls): return NOW.replace(tzinfo=None)
    monkeypatch.setattr(scan_engine, 'datetime', Clock)
    states, signals = [], []
    for on in (False, True):
        with database[1]() as session:
            capture = HoldingCapture(1, database[1]) if on else None
            signals.append(scan_engine._check_holdings(session, us_bench=daily()['Close'], capture=capture))
            h = session.get(Holding, 1)
            states.append({c.name:getattr(h,c.name) for c in h.__table__.columns})
            session.rollback()
        if capture:
            capture.finish()
    assert signals[0] == signals[1] and states[0] == states[1]
    assert runtime(database).report()['mismatches'] == 0


def test_episode_ledger_reentry_additions_and_account_separation(database, enabled, monkeypatch):
    seed(database)
    with database[1]() as session:
        session.add(Account(id=2, name='other'))
        session.add(Holding(id=2, account_id=2, ticker='TEST', name='other', market='US', quantity=1., avg_price=100.))
        session.add(Transaction(id=1, account_id=1, tx_type='BUY', trade_date='2025-01-01', ticker='TEST', market='US', quantity=1., price=100., amount=100.))
        session.commit()
        def capture(n):
            c=HoldingCapture(n,database[1]); c.prepare(session,session.query(Holding).all()); return c.inputs
        first=capture(1)
        session.add(Transaction(id=2, account_id=1, tx_type='BUY', trade_date='2025-01-02', ticker='TEST', market='US', quantity=1., price=110., amount=110.))
        session.commit()
        added=capture(2)
        assert first[1]['episode_id']==added[1]['episode_id']
        assert first[1]['input_version']!=added[1]['input_version']
        assert first[1]['episode_id']!=first[2]['episode_id']
        session.add_all([
            Transaction(id=3, account_id=1, tx_type='SELL', trade_date='2025-01-03', ticker='TEST', market='US', quantity=2., price=90., amount=180.),
            Transaction(id=4, account_id=1, tx_type='BUY', trade_date='2025-01-04', ticker='TEST', market='US', quantity=1., price=80., amount=80.)])
        session.commit()
        assert capture(3)[1]['episode_id']!=first[1]['episode_id']


def test_manual_holding_observed_absence_resets_episode(database, enabled):
    seed(database)
    with database[1]() as session:
        c=HoldingCapture(1,database[1]); c.prepare(session,session.query(Holding).all())
        empty=HoldingCapture(2,database[1]); empty.prepare(session,[])
        new=HoldingCapture(3,database[1]); new.prepare(session,session.query(Holding).all())
        assert c.inputs[1]['episode_id'] != new.inputs[1]['episode_id']


def test_replay_persists_each_transition_restart_and_idempotence(database):
    store=runtime(database)
    for n,status in enumerate([HoldingStatus.HOLD,HoldingStatus.REVIEW,HoldingStatus.HOLD,HoldingStatus.REVIEW],1):
        persist(store,item(n,status))
    assert store.replay(limit=2)['processed']==2
    assert runtime(database).replay()['processed']==2
    assert runtime(database).replay()['processed']==0
    events=store.list_events(observation().episode_id)
    assert sum(e['event_type']=='HOLDING_STATE_CHANGED' for e in events)==3
    assert store.list_deliveries()==[]


@pytest.mark.parametrize('field,value,cause', [('strategy_version','new','STRATEGY_CHANGED'),('input_version','new','INPUT_CHANGED')])
def test_changed_baseline_is_not_called_market_move(database,field,value,cause):
    store=runtime(database)
    persist(store,item(1)); persist(store,item(2,HoldingStatus.REVIEW,**{field:value}))
    store.replay()
    events=store.list_events(observation().episode_id)
    assert any(e['event_type']=='BASELINE_RESET' and cause in json.loads(e['payload_json'])['causes'] for e in events)
    assert not any(e['event_type']=='HOLDING_STATE_CHANGED' for e in events)


def test_failed_replay_does_not_skip_intermediate_observation(database, monkeypatch):
    store=runtime(database)
    for n,status in enumerate([HoldingStatus.HOLD,HoldingStatus.REVIEW,HoldingStatus.HOLD],1): persist(store,item(n,status))
    original=store.apply_observation
    def fail(identity,**kwargs):
        if identity==observation(2).id: raise RuntimeError('synthetic')
        return original(identity,**kwargs)
    monkeypatch.setattr(store,'apply_observation',fail)
    assert store.replay()['failed']==1
    assert store.get_state(observation().episode_id)['scan_sequence']==1
    assert runtime(database).replay()['processed']==2
    assert sum(e['event_type']=='HOLDING_STATE_CHANGED' for e in store.list_events(observation().episode_id))==2


def test_gap_blocks_then_recovered_observation_preserves_middle_transition(database):
    store=runtime(database)
    persist(store,item(1))
    middle=item(2,HoldingStatus.REVIEW)
    store.begin_capture(2,[dict(id=middle.id,episode_id=middle.episode_id,holding_id=1)],now=NOW)
    store.finish_capture(2,now=NOW)
    persist(store,item(3))
    assert store.replay()['degraded']
    assert store.get_state(middle.episode_id)['scan_sequence']==1
    store.record_observation(middle); store.finish_capture(2,now=NOW)
    assert store.replay()['processed']==2
    assert store.report()['gaps']==[]


def test_unrecoverable_gap_requires_explicit_reset_and_retains_evidence(database):
    store=runtime(database)
    persist(store,item(1))
    store.begin_capture(2,[dict(id=observation(2).id,episode_id=observation().episode_id,holding_id=1)],now=NOW)
    store.finish_capture(2,now=NOW)
    persist(store,item(3))
    store.replay()
    with pytest.raises(ValueError): store.reset_gap(2,'',now=NOW)
    store.reset_gap(2,'source evidence unavailable; restart baseline',now=NOW)
    assert store.replay()['processed']==1
    assert store.report()['gaps'][0]['missing']==[observation(2).id]
    events=store.list_events(observation().episode_id)
    reset=[e for e in events if e['event_type']=='BASELINE_RESET'][0]
    assert json.loads(reset['payload_json'])['gap_scan_ids']==[2]
    assert json.loads(reset['payload_json'])['intermediate_changes_may_be_missing'] is True


def test_late_input_is_audited_without_rewinding_state(database):
    store=runtime(database)
    persist(store,item(2,HoldingStatus.REVIEW)); store.replay()
    persist(store,item(1)); store.replay()
    assert store.get_state(observation().episode_id)['scan_sequence']==2
    assert store.report()['stale_audit']==1


def test_old_data_in_new_scan_is_audited(database):
    store=runtime(database)
    persist(store,item(1)); store.replay()
    persist(store,item(2,data_as_of=NOW-timedelta(days=1)))
    assert store.replay()['stale']==1
    assert store.get_state(observation().episode_id)['scan_sequence']==1
    assert store.report()['stale_audit']==1


def test_parallel_replay_does_not_duplicate_facts(database):
    store=runtime(database)
    persist(store,item(1,HoldingStatus.REVIEW))
    gate=Barrier(2)
    def run(_): gate.wait(timeout=10); return runtime(database).replay()
    with ThreadPoolExecutor(max_workers=2) as pool: list(pool.map(run,range(2)))
    assert len(store.list_events(observation().episode_id))==1
    assert store.list_deliveries()==[]


def test_capture_failure_does_not_break_holdings(database, enabled, monkeypatch, caplog):
    seed(database)
    monkeypatch.setattr(us_stocks,'get_us_ohlcv',lambda ticker:daily())
    monkeypatch.setattr(weinstein,'check_sell_signal',lambda *a,**kw:dict(severity='HIGH',sell_reason='synthetic'))
    capture=HoldingCapture(1,database[1])
    monkeypatch.setattr(capture.store,'begin_capture',lambda *a,**kw: (_ for _ in ()).throw(RuntimeError('do not log secret')))
    with database[1]() as session:
        assert scan_engine._check_holdings(session,capture=capture)[0]['severity']=='HIGH'
        session.commit()
    assert 'DEGRADED' in caplog.text and 'do not log secret' not in caplog.text


def test_runtime_off_does_not_construct_store(monkeypatch):
    import config
    monkeypatch.setattr(config,'AGENT_RUNTIME_ENABLED',False)
    assert create_capture(1,lambda:pytest.fail('must not open DB')) is None


def test_running_scan_cannot_be_reset(database):
    store=runtime(database)
    store.begin_capture(1,[],now=NOW)
    with database[1]() as session:
        session.add(ScanLog(id=1,market='US',status='RUNNING'));session.commit()
    with pytest.raises(ValueError,match='running'): store.reset_gap(1,'ack',now=NOW)
    with pytest.raises(ValueError,match='DONE'): store.finish_capture(1,now=NOW)


def test_process_crash_after_observation_commit_recovers_completion_marker(database):
    store=runtime(database)
    persist(store,item(1,HoldingStatus.REVIEW),complete=False)
    with database[1]() as session:
        session.add(ScanLog(id=1,market='US',status='DONE'));session.commit()
    assert runtime(database).replay()['processed']==1
    assert runtime(database).report()['gaps']==[]


def test_process_crash_before_observation_commit_is_detected_as_gap(database):
    store=runtime(database)
    current=item(1)
    store.begin_capture(1,[dict(id=current.id,episode_id=current.episode_id,holding_id=1)],now=NOW)
    with database[1]() as session:
        session.add(ScanLog(id=1,market='US',status='DONE'));session.commit()
    assert store.replay()['degraded']
    assert store.report()['gaps'][0]['status']=='GAP'


def test_shadow_migration_preserves_a_records_and_only_downgrades_inventory(runtime_database_url,monkeypatch):
    from pathlib import Path
    from alembic import command
    from alembic.config import Config
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from database.models import Base
    from agent_runtime.event_engine import RuntimeStore
    from tests.test_agent_events import apply
    import config
    monkeypatch.setattr(config,'DATABASE_DIRECT_URL',runtime_database_url)
    cfg=Config(str(Path(__file__).parents[1]/'alembic.ini'))
    cfg.set_main_option('script_location',str(Path(__file__).parents[1]/'alembic'))
    cfg.attributes['configure_logger']=False
    command.upgrade(cfg,'a41e7c9d2b60')
    engine= create_engine(runtime_database_url)
    try:
        store=RuntimeStore(sessionmaker(bind=engine),RuntimeSettings(enabled=True,delivery_enabled=True))
        original=apply(store)
        command.upgrade(cfg,'head');command.upgrade(cfg,'head')
        with engine.connect() as conn:
            assert compare_metadata(MigrationContext.configure(conn),Base.metadata)==[]
            assert {c['name'] for c in inspect(conn).get_check_constraints('agent_scan_captures')}=={'ck_agent_capture_status'}
        assert apply(store).event_ids==original.event_ids
        command.downgrade(cfg,'a41e7c9d2b60')
        assert apply(store).event_ids==original.event_ids
        assert len(store.list_deliveries())==1
        with engine.connect() as conn: assert 'agent_scan_captures' not in inspect(conn).get_table_names()
        command.upgrade(cfg,'head')
    finally: engine.dispose()


def test_run_scan_commits_legacy_then_shadow_without_changing_notifications(database,enabled,monkeypatch):
    import database.models as models
    import scanner.market_analysis as market_analysis
    seed(database)
    monkeypatch.setattr(models,'SessionLocal',database[1])
    monkeypatch.setattr(market_analysis,'get_market_stages',lambda:{})
    monkeypatch.setattr(market_analysis,'get_benchmark_close',lambda market:daily()['Close'])
    monkeypatch.setattr(scan_engine,'_scan_us',lambda *a,**kw:([],0))
    monkeypatch.setattr(scan_engine,'_check_watchlist',lambda *a,**kw:[])
    monkeypatch.setattr(scan_engine,'_prepare_alert_candidates',lambda db,s:(s,[]))
    monkeypatch.setattr(scan_engine,'_finalize_funnel',lambda *a,**kw:None)
    monkeypatch.setattr(us_stocks,'get_us_ohlcv',lambda ticker:daily())
    monkeypatch.setattr(weinstein,'check_sell_signal',lambda *a,**kw:dict(severity='HIGH',sell_reason='synthetic'))
    notified=[]
    def fake_notify(buy,sell,holdings,*a):
        with database[1]() as session:
            assert session.query(AgentObservation).count()==0
        notified.extend(holdings)
    monkeypatch.setattr(scan_engine,'_notify',fake_notify)
    assert scan_engine.run_scan('US')['status']=='done'
    with database[1]() as session:
        assert session.query(ScanLog).one().status=='DONE'
        assert session.query(AgentObservation).count()==1
        assert session.query(AgentDelivery).count()==0
    assert len(notified)==1


def test_postcommit_storage_failure_leaves_scan_done_and_gap(database,enabled,monkeypatch):
    seed(database)
    monkeypatch.setattr(us_stocks,'get_us_ohlcv',lambda ticker:daily())
    monkeypatch.setattr(weinstein,'check_sell_signal',lambda *a,**kw:None)
    capture=HoldingCapture(1,database[1])
    with database[1]() as session:
        session.add(ScanLog(id=1,status='RUNNING'));session.commit()
        scan_engine._check_holdings(session,capture=capture)
        session.get(ScanLog,1).status='DONE';session.commit()
    monkeypatch.setattr(capture.store,'record_observation',lambda *a,**kw:(_ for _ in ()).throw(RuntimeError('synthetic')))
    capture.finish()
    with database[1]() as session:
        assert session.get(ScanLog,1).status=='DONE'
        assert session.get(Holding,1).sell_status=='HOLD'
    assert runtime(database).report()['gaps'][0]['status']=='GAP'


def test_recovery_separates_quality_from_changed_last_valid_judgment(database):
    store=runtime(database)
    persist(store,item(1))
    persist(store,item(2,status=None,data_quality=DataQuality.UNAVAILABLE,data_as_of=None))
    store.replay()
    assert store.get_state(observation().episode_id)['last_valid_status']=='HOLD'
    persist(store,item(3,HoldingStatus.REVIEW));store.replay()
    events=store.list_events(observation().episode_id)
    changed=[e for e in events if e['event_type']=='HOLDING_STATE_CHANGED']
    assert len(changed)==1
    evidence=json.loads(changed[0]['payload_json'])
    assert evidence['last_valid_status']=='HOLD'
    assert evidence['comparison_spans_unavailable_data'] is True
    assert sum(e['event_type']=='DATA_QUALITY_CHANGED' for e in events)==2


def test_registered_earlier_capture_blocks_later_completion(database):
    store=runtime(database)
    first=item(1,HoldingStatus.REVIEW)
    store.begin_capture(1,[dict(id=first.id,episode_id=first.episode_id,holding_id=1)],now=NOW)
    persist(store,item(2))
    assert store.replay()['blocked']==1
    assert store.get_state(first.episode_id) is None
    store.record_observation(first);store.finish_capture(1,now=NOW)
    assert store.replay()['processed']==2
    assert sum(e['event_type']=='HOLDING_STATE_CHANGED' for e in store.list_events(first.episode_id))==1


def test_additional_query_db_error_does_not_poison_scanner_transaction(database,enabled,monkeypatch):
    seed(database)
    monkeypatch.setattr(us_stocks,'get_us_ohlcv',lambda ticker:daily())
    monkeypatch.setattr(weinstein,'check_sell_signal',lambda *a,**kw:None)
    def fail(conn,cursor,statement,parameters,context,many):
        if statement.startswith('SELECT transactions.'):
            conn.exec_driver_sql('SELECT * FROM synthetic_missing_table_for_failure_test')
    event.listen(database[0],'before_cursor_execute',fail)
    try:
        with database[1]() as session:
            scan_engine._check_holdings(session,capture=HoldingCapture(1,database[1]))
            session.commit()
            assert session.get(Holding,1).sell_status=='HOLD'
    finally: event.remove(database[0],'before_cursor_execute',fail)


def test_missing_benchmark_is_degraded_without_overriding_judgment(database,enabled,monkeypatch):
    seed(database)
    monkeypatch.setattr(us_stocks,'get_us_ohlcv',lambda ticker:daily())
    monkeypatch.setattr(weinstein,'check_sell_signal',lambda *a,**kw:dict(severity='LOW',sell_reason='synthetic'))
    with database[1]() as session:
        c=HoldingCapture(1,database[1])
        signals=scan_engine._check_holdings(session,capture=c)
        session.commit()
    c.finish()
    with database[1]() as session:
        row=session.query(AgentObservation).one()
        assert row.status=='CAUTION' and row.data_quality=='DEGRADED'
        assert json.loads(row.payload_json)['comparison']['matches']
    assert signals[0]['severity']=='LOW'


def test_pending_replay_is_degraded_until_caught_up(database):
    store=runtime(database)
    persist(store,item(1));persist(store,item(2))
    result=store.replay(limit=1)
    assert result['pending']==1 and result['degraded']
    assert store.report()['pending']==1
    assert store.replay()['degraded'] is False
    assert store.report()['degraded'] is False


def test_all_shadow_operations_are_noop_when_disabled():
    store=ShadowStore(lambda:pytest.fail('disabled database access'),RuntimeSettings())
    store.begin_capture(1,[],now=NOW)
    store.finish_capture(1,now=NOW)
    store.reset_gap(1,'reason',now=NOW)
    store.recover_captures()
    assert store.report()=={'enabled':False}
    assert store.replay()=={'enabled':False}
