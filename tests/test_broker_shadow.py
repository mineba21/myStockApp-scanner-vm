"""Broker B integration: synthetic balances and quotes, no live accounts."""
import json
from datetime import datetime, timezone

import pytest

from tests.test_agent_events import database, runtime_database_url
from tests.test_agent_shadow import enabled, daily, seed
from agent_runtime import broker_capture
from agent_runtime.holding_capture import HoldingCapture
from database.models import AgentObservation, AgentDelivery, Holding, ScanLog
from scanner import scan_engine, us_stocks, weinstein, market_analysis
from web import kiwoom_sell_analysis


def row(ticker='TXN', profile='account1', market='US', quantity=12):
    return dict(source='kiwoom', account_profile=profile, market=market,
                ticker=ticker, name=ticker, quantity=quantity, avg_price=100.)


@pytest.fixture
def feeds(monkeypatch):
    monkeypatch.setenv('KIWOOM_WEB_ENABLED', 'true')
    monkeypatch.setattr(broker_capture, 'load_snapshot', lambda: [row()])
    monkeypatch.setattr(us_stocks, 'get_us_ohlcv', lambda ticker: daily())
    monkeypatch.setattr(weinstein, 'to_weekly_ohlcv', lambda df: df)
    monkeypatch.setattr(weinstein, 'check_sell_signal', lambda *a, **kw: None)
    monkeypatch.setattr(market_analysis, 'get_benchmark_close', lambda market: daily()['Close'])


def run(database, number):
    with database[1]() as session:
        session.add(ScanLog(id=number, market='US', status='RUNNING'))
        session.commit()
        capture = HoldingCapture(number, database[1])
        signals = scan_engine._check_holdings(session, us_bench=daily()['Close'], capture=capture)
        session.get(ScanLog, number).status = 'DONE'
        session.commit()
    capture.finish()
    with database[1]() as session:
        rows = session.query(AgentObservation).filter_by(scan_sequence=number).all()
        session.expunge_all()
        assert session.query(AgentDelivery).count() == 0
    return signals, rows, capture.store.report()


def test_real_source_without_manual_holdings_and_no_delivery(database, enabled, feeds):
    signals, rows, report = run(database, 1)
    assert signals == []
    assert len(rows) == 1 and rows[0].ticker == 'TXN' and rows[0].status == 'HOLD'
    assert rows[0].data_quality == 'AVAILABLE'
    payload = json.loads(rows[0].payload_json)
    assert payload['source'] == 'kiwoom'
    assert payload['comparison'] == dict(eligible=False, matches=None,
                                       reason='BROKER_HAS_NO_LEGACY_NOTIFICATION_SELECTION')
    assert report['broker_observations'] == report['comparison_unavailable'] == 1
    assert report['gaps'] == [] and report['pending'] == 0
    with database[1]() as session:
        assert session.query(Holding).count() == 0


def test_manual_selection_unchanged_and_broker_warning_never_sent(database, enabled, feeds, monkeypatch):
    seed(database)
    monkeypatch.setattr(weinstein, 'check_sell_signal', lambda *a, **kw:
                        dict(severity='HIGH', sell_reason='synthetic'))
    signals, rows, report = run(database, 1)
    assert len(signals) == 1 and signals[0]['ticker'] == 'TEST'
    assert {r.ticker for r in rows} == {'TEST', 'TXN'}
    assert report['mismatches'] == 0
    assert report['comparison_unavailable'] == 1


def test_scope_identity_fractional_and_reentry(database, enabled, feeds, monkeypatch):
    current = [row(quantity=.125)]
    monkeypatch.setattr(broker_capture, 'load_snapshot', lambda: current)
    _, first, _ = run(database, 1)
    current[0]['quantity'] = 2.5
    _, added, _ = run(database, 2)
    assert first[0].episode_id == added[0].episode_id
    assert first[0].input_version != added[0].input_version
    current.clear()
    _, empty, _ = run(database, 3)
    assert empty == []
    current.append(row())
    _, reentry, _ = run(database, 4)
    assert reentry[0].episode_id != first[0].episode_id
    assert reentry[0].holding_id == first[0].holding_id


def test_snapshot_failure_is_durable_gap_and_not_absence(database, enabled, feeds, monkeypatch):
    _, first, _ = run(database, 1)
    def fail(): raise RuntimeError('synthetic')
    monkeypatch.setattr(broker_capture, 'load_snapshot', fail)
    _, failed, report = run(database, 2)
    assert failed == [] and report['degraded']
    assert report['gaps'][0]['scan_id'] == 2 and report['gaps'][0]['missing']
    monkeypatch.setattr(broker_capture, 'load_snapshot', lambda: [row()])
    _, recovered, report = run(database, 3)
    assert recovered[0].episode_id == first[0].episode_id
    assert report['pending'] == 1  # no silent gap reset


def test_quote_failure_is_observed_and_recovery_preserves_episode(database, enabled, feeds, monkeypatch):
    monkeypatch.setattr(us_stocks, 'get_us_ohlcv', lambda ticker: None)
    _, failed, report = run(database, 1)
    assert failed[0].status is None and failed[0].data_quality == 'UNAVAILABLE'
    assert report['gaps'] == []
    monkeypatch.setattr(us_stocks, 'get_us_ohlcv', lambda ticker: daily())
    _, recovered, _ = run(database, 2)
    assert recovered[0].episode_id == failed[0].episode_id
    assert recovered[0].status == 'HOLD'


@pytest.mark.parametrize('rows', [[row(profile='account2')], [row(), row()], [row(quantity=float('nan'))]])
def test_invalid_inventory_never_reports_empty_success(database, enabled, feeds, monkeypatch, rows):
    monkeypatch.setattr(broker_capture, 'load_snapshot', lambda: rows)
    _, observations, report = run(database, 1)
    assert observations == [] and report['degraded'] and report['gaps']


def test_disabled_broker_never_fetches(database, enabled, feeds, monkeypatch):
    monkeypatch.setenv('KIWOOM_WEB_ENABLED', 'false')
    monkeypatch.setattr(broker_capture, 'load_snapshot', lambda: pytest.fail('network while disabled'))
    _, rows, report = run(database, 1)
    assert rows == [] and not report['degraded']


def test_snapshot_requires_both_target_profiles_and_only_balance_calls(monkeypatch):
    from trading import kiwoom_readonly as api
    from web import kiwoom_holdings as web
    calls = []
    monkeypatch.setattr(api, 'load_profile_configs', lambda: {'account1': 'us', 'account4': 'kr'})
    monkeypatch.setattr(web, '_get_token', lambda *a, **kw: 'synthetic-token')
    class Client:
        def __init__(self, config): self.config = config
        def get_overseas_account_balance(self, token):
            calls.append((self.config, 'US'))
            return {'complete': True, 'holdings': [dict(stk_cd='TXN', poss_qty='0.125', frgn_stk_book_uv='100')]}
        def get_account_balance(self, token):
            calls.append((self.config, 'KR'))
            return {'complete': True, 'holdings': []}
    monkeypatch.setattr(api, 'KiwoomReadOnlyClient', Client)
    rows = broker_capture.load_snapshot()
    assert calls == [('us', 'US'), ('kr', 'KR')]
    assert rows[0]['quantity'] == .125
    monkeypatch.setattr(api, 'load_profile_configs', lambda: {'account1': 'us'})
    with pytest.raises(ValueError): broker_capture.load_snapshot()


def test_readonly_broker_error_is_not_empty_balance(monkeypatch):
    from trading import kiwoom_readonly as api
    from web import kiwoom_holdings as web
    monkeypatch.setattr(api, 'load_profile_configs', lambda: {'account1': 'us', 'account4': 'kr'})
    monkeypatch.setattr(web, '_get_token', lambda *a, **kw: 'synthetic-token')
    class Client:
        def __init__(self, config): pass
        def get_overseas_account_balance(self, token): raise api.KiwoomError('조회내역이 없습니다')
    monkeypatch.setattr(api, 'KiwoomReadOnlyClient', Client)
    with pytest.raises(api.KiwoomError): broker_capture.load_snapshot()


def test_kr_and_us_snapshot_no_etf_and_missing_benchmark(database, enabled, feeds, monkeypatch):
    from scanner import kr_stocks
    monkeypatch.setattr(broker_capture, 'load_snapshot', lambda: [row(), row('005930', 'account4', 'KR', .5)])
    monkeypatch.setattr(kr_stocks, 'get_kr_ohlcv', lambda ticker: daily())
    monkeypatch.setattr(market_analysis, 'get_benchmark_close', lambda market: None)
    _, rows, report = run(database, 1)
    assert {(r.account_id, r.market, r.ticker) for r in rows} == {(1, 'US', 'TXN'), (4, 'KR', '005930')}
    assert all(r.data_quality == 'DEGRADED' for r in rows)
    assert report['broker_observations'] == 2


def test_broker_save_failure_is_gap(database, enabled, feeds, monkeypatch):
    from agent_runtime.shadow import ShadowStore
    monkeypatch.setattr(ShadowStore, 'record_observation', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('synthetic')))
    _, rows, report = run(database, 1)
    assert rows == [] and report['gaps'][0]['missing'] and report['degraded']


def test_gap_reset_is_explicit_and_retains_evidence(database, enabled, feeds, monkeypatch):
    from agent_runtime.shadow import ShadowStore
    from agent_runtime.schemas import RuntimeSettings
    monkeypatch.setattr(broker_capture, 'load_snapshot', lambda: (_ for _ in ()).throw(RuntimeError('synthetic')))
    run(database, 1)
    monkeypatch.setattr(broker_capture, 'load_snapshot', lambda: [row()])
    _, _, report = run(database, 2)
    assert report['pending'] == 1
    store = ShadowStore(database[1], RuntimeSettings(enabled=True))
    store.reset_gap(1, 'synthetic test acknowledgment', now=datetime.now(timezone.utc))
    store.replay()
    report = store.report()
    assert report['pending'] == 0 and report['gaps'][0]['reset']
    assert report['gaps'][0]['missing']


@pytest.mark.parametrize('report', [
    {'complete': False, 'holdings': []},
    {'complete': True, 'holdings': [{'stk_cd': 'TXN', 'poss_qty': 'invalid'}]},
    {'complete': True, 'holdings': [{'stk_cd': 'TXN', 'poss_qty': '2', 'frgn_stk_book_uv': ''}]},
])
def test_partial_or_malformed_broker_balance_cannot_be_absence(monkeypatch, report):
    from trading import kiwoom_readonly as api
    from web import kiwoom_holdings as web
    monkeypatch.setattr(api, 'load_profile_configs', lambda: {'account1': 'us', 'account4': 'kr'})
    monkeypatch.setattr(web, '_get_token', lambda *a, **kw: 'synthetic-token')
    class Client:
        def __init__(self, config): pass
        def get_overseas_account_balance(self, token): return report
    monkeypatch.setattr(api, 'KiwoomReadOnlyClient', Client)
    with pytest.raises(ValueError): broker_capture.load_snapshot()
