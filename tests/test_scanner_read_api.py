from datetime import datetime, timedelta
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from database.models import Base, ScanResult, ScanLog, get_db

TOKEN = 'read-only-test-token-' + 'x' * 32

@pytest.fixture
def setup(monkeypatch):
    import web.app as webapp
    engine = create_engine('sqlite://', connect_args={'check_same_thread':False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[ScanResult.__table__, ScanLog.__table__])
    factory=sessionmaker(bind=engine)
    def db():
        session=factory()
        try: yield session
        finally: session.close()
    monkeypatch.setattr(webapp,'SCANNER_READ_TOKEN',TOKEN)
    monkeypatch.setattr(webapp,'SITES_API_KEY','full-app-key')
    def forbidden(*args, **kwargs): raise AssertionError('Brokerage must never be called')
    monkeypatch.setattr(webapp,'get_kiwoom_holdings',forbidden)
    monkeypatch.setattr(webapp,'get_kiwoom_account_summaries',forbidden)
    monkeypatch.setattr(webapp,'KIWOOM_WEB_ENABLED',True)
    webapp.app.dependency_overrides[get_db]=db
    with factory() as session:
        now=datetime.utcnow()-timedelta(minutes=1)
        session.add_all([
            ScanResult(id=1,market='US',ticker='SPY',signal_type='BREAKOUT',signal_date='2026-09-11',scan_time=now,
                       strict_filter_passed=True,entry_warnings='["early warning"]',equity_snapshot=9876543,suggested_qty=77),
            ScanResult(id=2,market='KR',ticker='005930',signal_type='REBOUND',scan_time=now,strict_filter_passed=None),
            ScanResult(id=3,market='US',ticker='FAIL',signal_type='BREAKOUT',scan_time=now,strict_filter_passed=False),
            ScanLog(market='US',status='ERROR',started_at=now,error_msg='secret internal traceback')])
        session.commit()
    client=TestClient(webapp.app, headers={'Authorization':'Bearer '+TOKEN})
    yield client, factory
    webapp.app.dependency_overrides.pop(get_db,None)
    engine.dispose()


def test_read_projection_no_account_data_or_live_calls(setup):
    client,_=setup
    response=client.get('/api/scanner-read/signals')
    assert response.status_code==200 and response.headers['cache-control']=='no-store'
    rows=response.json()['items']
    assert [r['id'] for r in rows]==[1,2]
    assert rows[0]['entry_warnings']==['early warning']
    assert rows[1]['strict_assessment']=='legacy_unassessed'
    for row in rows:
        assert not {'equity_snapshot','suggested_qty','notified','cash_balance'} & row.keys()
    assert '9876543' not in response.text
    assert client.get('/api/scanner-read/signals/3').status_code==404


def test_auth_scope_blocks_orders_mutations_and_application_key(setup):
    client,_=setup
    for method,path in [('get','/api/asset-allocation/rebalance/capabilities'),('get','/api/results'),
        ('delete','/api/results/1'),('post','/api/scan'),('post','/api/asset-allocation/rebalance/preview')]:
        assert getattr(client,method)(path).status_code==401
    assert client.post('/api/scanner-read/signals').status_code==403
    assert client.get('/api/scanner-read/unknown').status_code==403
    for auth in ['', 'Bearer bad', 'Bearer full-app-key']:
        assert client.get('/api/scanner-read/status',headers={'Authorization':auth}).status_code==401


def test_no_key_or_reused_key_fails_closed(setup,monkeypatch):
    import web.app as webapp
    client,_=setup
    monkeypatch.setattr(webapp,'SCANNER_READ_TOKEN','')
    assert client.get('/api/scanner-read/status').status_code==503
    monkeypatch.setattr(webapp,'SCANNER_READ_TOKEN','full-app-key')
    assert client.get('/api/results',headers={'Authorization':'Bearer full-app-key'}).status_code==503


def test_pagination_equal_timestamps_and_rescan_updates(setup):
    client,factory=setup
    a=client.get('/api/scanner-read/signals?limit=1').json()
    assert a['has_more'] and a['items'][0]['id']==1
    b=client.get('/api/scanner-read/signals',params={**a['next_cursor'],'through':a['through'],'limit':1}).json()
    assert not b['has_more'] and b['items'][0]['id']==2
    assert client.get('/api/scanner-read/signals',params=b['next_cursor']).json()['items']==[]
    with factory() as db:
        db.query(ScanResult).filter_by(id=1).update({'scan_time':datetime.utcnow()})
        db.commit()
    update=client.get('/api/scanner-read/signals',params=b['next_cursor']).json()['items'][0]
    assert update['id']==1 and update['event_key']!=a['items'][0]['event_key']
    assert update['signal_key']==a['items'][0]['signal_key']


def test_limits_status_and_empty_results(setup):
    client,_=setup
    status=client.get('/api/scanner-read/status')
    assert 'secret internal traceback' not in status.text
    assert status.json()['scans'][0]['status']=='NO_DATA'
    for params in ({'limit':101},{'limit':0},{'market':'BAD'},{'after':'bad'},{'after':'2026-01-01'}, {'after_id':-1}):
        assert client.get('/api/scanner-read/signals',params=params).status_code==422
    assert client.get('/api/scanner-read/signals?market=KR&signal_type=BREAKOUT').json()['items']==[]
