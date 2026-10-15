import asyncio
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace

import pytest
import requests

from trading.allocation_rebalance import Rebalance, RebalanceBlocked, UnavailableBroker, buy_plan
from trading.kiwoom_orders import KiwoomOrderClient, OrderUnknown, OrderRejected
from trading.kiwoom_readonly import KiwoomConfig


class Broker:
    """Only mock cash that the broker explicitly makes orderable can be spent."""
    def __init__(self):
        self.holdings = {'SPY': 4, 'OTHER': 10}
        self.cash = Decimal('10')
        self.sent = []
        self.evidence = {}
        self.pending = []
        self.failure = None
        self.path = None
        self.cancelled = []

    def snapshot(self):
        return {'account_profile': 'account2', 'currency': 'USD', 'complete': True,
                'cash_includes_reservations': True, 'as_of': time.time(),
                'holdings': dict(self.holdings), 'open_orders': list(self.pending),
                'orderable_cash': str(self.cash)}

    def quotes(self, tickers):
        return {t: {'limit_price': '100.00', 'as_of': time.time()} for t in tickers}

    def submit(self, order):
        # A separate connection sees committed intent before any side effect.
        journal = Rebalance(self.path, self).status(order['cycle_id'])
        assert next(o for o in journal['orders'] if o['id'] == order['id'])['state'] == 'SENDING'
        self.sent.append(order)
        if self.failure:
            raise self.failure
        if order['side'] == 'BUY':
            self.cash -= Decimal(order['price']) * order['quantity']
        return {'order_number': str(len(self.sent)), 'status': 'submitted'}

    def lookup(self, order):
        return self.evidence.get(order['id'])

    def cancel(self, order):
        self.cancelled.append(order)
        return {'order_number': '9' + str(len(self.cancelled)), 'status': 'submitted'}

    def fill(self, orders):
        for o in orders:
            self.evidence[o['id']] = {'matched_intent_id': o['id'], 'state': 'FILLED',
                                      'filled_quantity': o['quantity']}
            self.holdings[o['ticker']] = self.holdings.get(o['ticker'], 0) + (-o['quantity'] if o['side'] == 'SELL' else o['quantity'])
        self.pending = []


@pytest.fixture
def flow(tmp_path):
    broker = Broker()
    path = tmp_path / 'journal.sqlite'
    broker.path = path
    service = Rebalance(path, broker, reserve_bps=100)
    cycle = service.create('easy-2026-08-31', {'SPY': .5, 'QQQ': .5}, ['SPY', 'QQQ'])
    return service, broker, cycle['id']


def seen(cycle):
    """The plan the browser displays and sends back with the execute click."""
    p = cycle['payload']
    return {'sells': [{'ticker': o['ticker'], 'quantity': o['quantity']} for o in p['preview']],
            'targets': p['planning']['targets'],
            'current': {t: q for t, q in p['original_quantities'].items() if Decimal(q)}}


def sold(flow):
    service, broker, cid = flow
    sell = service.confirm(cid, 'SELL')
    broker.fill(sell['orders'])
    assert service.reconcile(cid)['state'] == 'SELL_CONFIRMED'
    return service, broker, cid


def test_excess_sale_fill_cash_refresh_buys_only_deficits_preserves_other_strategy(flow):
    service, broker, cid = flow
    submitted = service.confirm(cid, 'SELL')
    assert [(o['ticker'], o['quantity']) for o in broker.sent] == [('SPY', 2)]
    assert submitted['state'] == 'SELL_PENDING'
    with pytest.raises(RebalanceBlocked):
        service.preview_buys(cid)
    # Receipt alone neither zeroes holdings nor makes sale proceeds orderable.
    assert service.reconcile(cid)['blockers']
    broker.fill(submitted['orders'])
    service.reconcile(cid)
    assert service.preview_buys(cid)['payload']['preview'] == []  # only $10 available
    broker.cash = Decimal('1000')
    preview = service.preview_buys(cid)
    assert preview['payload']['budget']['budget'] == '990.00'
    assert sum(o['quantity'] * Decimal(o['limit_price']) for o in preview['payload']['preview']) == 800
    result = service.confirm(cid, 'BUY')
    buys = [o for o in result['orders'] if o['side'] == 'BUY']
    assert len(buys) == 2  # second order not charged the first reservation twice
    broker.fill(buys)
    assert service.reconcile(cid)['state'] == 'COMPLETE'
    assert broker.holdings['OTHER'] == 10
    assert broker.holdings['SPY'] == 5  # retained 2 + bought 3, budget-capped deficit
    assert service.create('easy-2026-08-31', {'SPY': 1}, ['SPY'])['id'] == cid
    service.confirm(cid, 'BUY')
    assert len(broker.sent) == 3


def test_one_user_authorization_advances_sell_then_buy_without_second_confirmation(flow):
    service, broker, cid = flow

    selling = service.execute_authorized_cycle(cid, seen(service.status(cid)))
    assert selling['state'] == 'SELL_PENDING'
    assert selling['payload']['authorization_scope'] == 'FULL_CYCLE_V1'
    assert selling['blockers']
    restarted = Rebalance(service.path, broker, reserve_bps=100)
    assert restarted.execute_authorized_cycle(cid)['state'] == 'SELL_PENDING'
    assert len(broker.sent) == 1

    broker.fill([o for o in selling['orders'] if o['side'] == 'SELL'])
    broker.cash = Decimal('1000')
    buying = service.advance_authorized_cycle(cid)
    assert buying['state'] == 'BUY_PENDING'
    assert {o['side'] for o in buying['orders']} == {'SELL', 'BUY'}

    broker.fill([o for o in buying['orders'] if o['side'] == 'BUY'])
    assert service.advance_authorized_cycle(cid)['state'] == 'COMPLETE'
    sent = len(broker.sent)
    assert service.execute_authorized_cycle(cid)['state'] == 'COMPLETE'
    assert len(broker.sent) == sent


def test_allocation_mock_adapter_rejects_real_account_before_network():
    from trading.kiwoom_allocation_mock_broker import KiwoomAllocationMockBroker
    from trading.kiwoom_readonly import KiwoomError

    with pytest.raises(KiwoomError, match='모의투자 설정만'):
        KiwoomAllocationMockBroker(KiwoomConfig('key', 'secret', 'real'))


def test_one_button_stops_after_broker_rejection(flow):
    service, broker, cid = flow
    broker.failure = OrderRejected("market closed")

    result = service.execute_authorized_cycle(cid, seen(service.status(cid)))

    assert result['state'] == 'SELL_PENDING'
    assert result['orders'][0]['state'] == 'REJECTED'
    assert any('주문 거절' in blocker for blocker in result['blockers'])
    assert len(broker.sent) == 1


@pytest.mark.parametrize('state,filled', [('OPEN', 0), ('PARTIAL', 1), ('CANCELED', 1), ('REJECTED', 0)])
def test_incomplete_sales_never_advance(flow, state, filled):
    service, broker, cid = flow
    order = service.confirm(cid, 'SELL')['orders'][0]
    broker.evidence[order['id']] = {'matched_intent_id': order['id'], 'state': state, 'filled_quantity': filled}
    broker.holdings['SPY'] = 4 - filled
    broker.pending = [{'order_number': '1'}] if state in {'OPEN', 'PARTIAL'} else []
    result = service.reconcile(cid)
    assert result['state'] == 'SELL_PENDING'
    assert any(state in p for p in result['blockers'])
    with pytest.raises(RebalanceBlocked):
        service.preview_buys(cid)


def test_timeout_restart_new_preview_and_retry_do_not_duplicate(flow):
    service, broker, cid = flow
    broker.failure = requests.Timeout('response lost')
    assert service.confirm(cid, 'SELL')['orders'][0]['state'] == 'UNKNOWN'
    restarted = Rebalance(service.path, broker)
    assert restarted.confirm(cid, 'SELL')['orders'][0]['state'] == 'UNKNOWN'
    assert restarted.create('easy-2026-08-31', {'SPY': 1}, ['SPY'])['id'] == cid
    with pytest.raises(RebalanceBlocked):
        restarted.create('original-2026-08-31', {'SPY': 1}, ['SPY'])
    assert len(broker.sent) == 1
    assert restarted.reconcile(cid)['blockers']
    # A later unique broker-history match can resolve an unknown receipt.
    broker.fill(restarted.status(cid)['orders'])
    assert restarted.reconcile(cid)['state'] == 'SELL_CONFIRMED'
    script = 'import sys; from trading.allocation_rebalance import Rebalance,UnavailableBroker; print(Rebalance(sys.argv[1],UnavailableBroker()).status(sys.argv[2])["state"])'
    import sys
    output = subprocess.check_output([sys.executable, '-c', script, service.path, cid], text=True)
    assert output.strip() == 'SELL_CONFIRMED'


def test_simultaneous_confirms_only_one_dispatcher(flow):
    service, broker, cid = flow
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: Rebalance(service.path, broker).confirm(cid, 'SELL'), range(2)))
    assert len(broker.sent) == 1


def test_process_dies_after_transmission_leaves_sending_intent(flow):
    service, broker, cid = flow
    broker.failure = SystemExit('crash')
    with pytest.raises(SystemExit):
        service.confirm(cid, 'SELL')
    restarted = Rebalance(service.path, broker)
    assert restarted.status(cid)['orders'][0]['state'] == 'SENDING'
    restarted.confirm(cid, 'SELL')
    assert len(broker.sent) == 1
    assert restarted.reconcile(cid)['state'] == 'SELL_PENDING'


def test_unavailable_adapter_blocks_before_any_orders(tmp_path):
    with pytest.raises(RebalanceBlocked, match='ETF 주문 차단'):
        Rebalance(tmp_path / 'journal', UnavailableBroker()).create('month', {'SPY': 1}, ['SPY'])


def test_net_cash_semantics_and_incomplete_snapshot_must_be_known(flow):
    service, broker, _ = flow
    original = broker.snapshot
    for override in ({'complete': False}, {'cash_includes_reservations': False}, {'as_of': 0}, {'orderable_cash': None}):
        broker.snapshot = lambda override=override: {**original(), **override}
        with pytest.raises(RebalanceBlocked):
            service.snapshot()


def test_residual_position_after_reported_fill_still_blocks(flow):
    service, broker, cid = flow
    order = service.confirm(cid, 'SELL')['orders'][0]
    broker.evidence[order['id']] = {'matched_intent_id': order['id'], 'state': 'FILLED', 'filled_quantity': 2}
    result = service.reconcile(cid)
    assert 'SPY: 보유 4주 / 유지 예정 2주 — 조정 매도 확인 필요' in result['blockers']


@pytest.mark.parametrize('payload', [{}, {'ord_no': '1'}, {'return_code': 0},
    {'return_code': 0, 'ord_no': ''}, {'return_code': '', 'ord_no': '1'}, {'return_code': 0, 'ord_no': '000'},
    {'return_code': False, 'ord_no': '1'}, {'return_code': 0, 'ord_no': {}}, []])
@pytest.mark.parametrize('side', ['buy_us_limit', 'sell_us_limit'])
def test_invalid_receipts_are_unknown_not_success(payload, side):
    response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)
    session = SimpleNamespace(post=lambda *a, **k: response)
    client = KiwoomOrderClient(KiwoomConfig('key', 'secret', 'mock'), session)
    with pytest.raises(OrderUnknown):
        getattr(client, side)('token', exchange='NY', ticker='SPY', quantity=1, price=100)


def test_low_level_timeout_and_explicit_rejection_differ():
    def timeout(*a, **k):
        raise requests.Timeout()
    client = KiwoomOrderClient(KiwoomConfig('key', 'secret', 'mock'), SimpleNamespace(post=timeout))
    with pytest.raises(OrderUnknown):
        client.buy_us_limit('token', exchange='NY', ticker='SPY', quantity=1, price=100)
    client.session.post = lambda *a, **k: SimpleNamespace(raise_for_status=lambda: None,
        json=lambda: {'return_code': 12, 'return_msg': 'rejected'})
    with pytest.raises(OrderRejected):
        client.buy_us_limit('token', exchange='NY', ticker='SPY', quantity=1, price=100)


@pytest.mark.parametrize('allocations', [{'SPY': 1.1}, {'SPY': -.1}, {'SPY': float('nan')}, {}, {'SPY': 0}])
def test_bad_weights_rejected(allocations):
    with pytest.raises(RebalanceBlocked):
        buy_plan(1000, allocations, {'SPY': {'as_of': time.time(), 'limit_price': '10'}})


@pytest.mark.parametrize('quote', [{}, {'as_of': 0, 'limit_price': 10},
    {'as_of': time.time(), 'limit_price': 'NaN'}, {'as_of': time.time(), 'limit_price': '10.001'}])
def test_missing_stale_or_invalid_quotes_block_all_buys(quote):
    with pytest.raises(RebalanceBlocked):
        buy_plan(1000, {'SPY': 1}, {'SPY': quote})


def test_total_budget_many_etfs_preserves_unallocated_cash():
    quotes = {t: {'as_of': time.time(), 'limit_price': '33.33'} for t in ['SPY', 'QQQ', 'BIL']}
    orders, budget = buy_plan('101', {'SPY': .3, 'QQQ': .3, 'BIL': .3}, quotes)
    assert sum(Decimal(o['limit_price']) * o['quantity'] for o in orders) <= Decimal(budget['budget'])


def test_cash_decrease_before_confirm_requires_new_preview(flow):
    service, broker, cid = sold(flow)
    broker.cash = Decimal('1000')
    service.preview_buys(cid)
    broker.cash = Decimal('500')
    with pytest.raises(RebalanceBlocked, match='현금 감소'):
        service.confirm(cid, 'BUY')
    assert len(broker.sent) == 1


def test_legacy_routes_cannot_bypass_reconciliation(monkeypatch):
    from web import kiwoom_order_api as api
    from fastapi import HTTPException
    monkeypatch.setenv('KIWOOM_TRADING_ENABLED', 'true')
    for method, cls in [(api.execute_buy, api.BuyExecuteRequest), (api.execute_sell, api.SellExecuteRequest)]:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(method(cls(preview_id='x' * 24, confirmation_ticker='SPY')))
        assert exc.value.status_code == 503
        assert '미체결' in exc.value.detail


def test_refresh_same_sell_preview_cannot_restart_a_sent_cycle(flow):
    service, broker, cid = flow
    assert service.refresh_sell_preview(cid)['id'] == cid
    service.confirm(cid, 'SELL')
    with pytest.raises(RebalanceBlocked):
        service.refresh_sell_preview(cid)
    assert len(broker.sent) == 1


def test_repeated_preview_uses_fresh_holdings_cash_and_quotes(flow, monkeypatch):
    service, broker, cid = flow
    before = service.status(cid)['payload']
    broker.holdings = {'SPY': 2, 'QQQ': 2, 'OTHER': 10}
    broker.cash = Decimal('400')
    monkeypatch.setattr(broker, 'quotes', lambda tickers: {
        t: {'limit_price': '200.00', 'as_of': time.time()} for t in tickers})
    refreshed = service.create('easy-2026-08-31', {'SPY': .5, 'QQQ': .5}, ['SPY', 'QQQ'])
    assert refreshed['id'] == cid
    assert refreshed['payload']['original_quantities'] == {'SPY': '2', 'QQQ': '2'}
    assert refreshed['payload']['preview'] == []
    assert refreshed['payload']['planning'] != before['planning']
    assert refreshed['orders'] == broker.sent == []


def test_refresh_failure_does_not_return_stale_preview(flow):
    service, broker, cid = flow
    before = service.status(cid)
    broker.pending = [{'ticker': 'SPY'}]
    with pytest.raises(RebalanceBlocked):
        service.create('easy-2026-08-31', {'SPY': .5, 'QQQ': .5}, ['SPY', 'QQQ'])
    assert service.status(cid) == before
    assert broker.sent == []


def test_legacy_authorized_preview_without_intents_is_recoverable(flow):
    """Old code saved authorization before an expired confirm and locked the
    month. No intent row means nothing was sent, so a refresh may replace it."""
    service, broker, cid = flow
    payload = service.status(cid)['payload']
    payload['execution_authorized'] = True
    payload['expires_at'] = time.time() - 1
    with service.db() as db:
        db.execute('UPDATE allocation_cycles SET payload=? WHERE id=?', (json.dumps(payload), cid))
    refreshed = service.create('easy-2026-08-31', {'SPY': 1}, ['SPY'])
    assert refreshed['id'] == cid
    assert refreshed['state'] == 'SELL_PREVIEW'
    assert 'execution_authorized' not in refreshed['payload']
    assert refreshed['payload']['expires_at'] > time.time()
    assert broker.sent == []
    result = service.execute_authorized_cycle(cid, seen(refreshed))
    assert result['state'] == 'SELL_PENDING'
    assert len(broker.sent) == 1


def test_concurrent_authorization_blocks_refresh(flow, monkeypatch):
    service, broker, cid = flow
    snapshot = broker.snapshot

    def authorize_during_snapshot():
        payload = service.status(cid)['payload']
        payload['execution_authorized'] = True
        with service.db() as db:
            db.execute('UPDATE allocation_cycles SET payload=? WHERE id=?', (json.dumps(payload), cid))
        return snapshot()

    monkeypatch.setattr(broker, 'snapshot', authorize_during_snapshot)
    with pytest.raises(RebalanceBlocked, match='다른 요청'):
        service.refresh_sell_preview(cid)
    assert service.status(cid)['payload']['execution_authorized'] is True
    assert broker.sent == []


def test_scope_required_and_cash_reservations_prevent_start(tmp_path):
    broker = Broker()
    service = Rebalance(tmp_path / 'j', broker)
    with pytest.raises(RebalanceBlocked):
        service.create('k', {'SPY': 1}, [])
    broker.pending = [{'side': 'BUY', 'ticker': 'OTHER'}]
    with pytest.raises(RebalanceBlocked, match='미체결'):
        service.create('k', {'SPY': 1}, ['SPY'])


def test_budget_change_between_buy_submissions_never_overspends(flow):
    service, broker, cid = sold(flow)
    broker.cash = Decimal('1000')
    service.preview_buys(cid)
    original = broker.submit
    def submit(order):
        result = original(order)
        broker.cash = Decimal('0')
        return result
    broker.submit = submit
    result = service.confirm(cid, 'BUY')
    assert len([o for o in broker.sent if o['side'] == 'BUY']) == 1
    assert len([o for o in result['orders'] if o['state'] == 'PREPARED']) == 1
    service.confirm(cid, 'BUY')
    assert len(broker.sent) == 2  # one sell, one buy


def test_live_reconcile_with_existing_journal_explains_missing_adapter(flow):
    service, broker, cid = flow
    service.confirm(cid, 'SELL')
    with pytest.raises(RebalanceBlocked, match='ETF 주문 차단'):
        Rebalance(service.path, UnavailableBroker()).reconcile(cid)


def test_wrong_order_history_match_does_not_clear_unknown(flow):
    service, broker, cid = flow
    order = service.confirm(cid, 'SELL')['orders'][0]
    broker.evidence[order['id']] = {'matched_intent_id': 'different-intent',
                                  'state': 'FILLED', 'filled_quantity': 2}
    broker.holdings['SPY'] = 0
    assert service.reconcile(cid)['state'] == 'SELL_PENDING'


def test_quote_original_timestamp_controls_preview_expiry(flow):
    service, broker, cid = sold(flow)
    broker.cash = 1000
    source_time = time.time() - 55
    broker.quotes = lambda tickers: {t: {'limit_price': 100, 'as_of': source_time} for t in tickers}
    preview = service.preview_buys(cid)
    assert preview['payload']['expires_at'] == source_time + 60


def test_same_targets_and_weights_send_no_orders(tmp_path):
    broker = Broker()
    broker.holdings = {'SPY': 5, 'QQQ': 5, 'OTHER': 99}
    broker.cash = 0
    broker.path = tmp_path / 'journal'
    service = Rebalance(broker.path, broker)
    cycle = service.create('same', {'SPY': .5, 'QQQ': .5}, ['SPY', 'QQQ'])
    assert cycle['payload']['preview'] == []
    service.confirm(cycle['id'], 'SELL')
    assert service.reconcile(cycle['id'])['state'] == 'SELL_CONFIRMED'
    assert service.preview_buys(cycle['id'])['payload']['preview'] == []
    service.confirm(cycle['id'], 'BUY')
    assert service.reconcile(cycle['id'])['state'] == 'COMPLETE'
    assert broker.sent == []
    assert broker.holdings == {'SPY': 5, 'QQQ': 5, 'OTHER': 99}


def test_changed_weights_sell_excess_and_buy_deficit(tmp_path):
    broker = Broker()
    broker.holdings = {'SPY': 8, 'QQQ': 2, 'OTHER': 99}
    broker.cash = 0
    broker.path = tmp_path / 'journal'
    service = Rebalance(broker.path, broker, reserve_bps=0)
    cycle = service.create('changed', {'SPY': .6, 'QQQ': .4}, ['SPY', 'QQQ'])
    cid = cycle['id']
    assert [(o['ticker'], o['quantity']) for o in cycle['payload']['preview']] == [('SPY', 2)]
    result = service.confirm(cid, 'SELL')
    broker.fill(result['orders'])
    broker.cash = 200  # only the broker snapshot makes proceeds available
    service.reconcile(cid)
    preview = service.preview_buys(cid)
    assert [(o['ticker'], o['quantity']) for o in preview['payload']['preview']] == [('QQQ', 2)]
    result = service.confirm(cid, 'BUY')
    broker.fill([o for o in result['orders'] if o['side'] == 'BUY'])
    assert service.reconcile(cid)['state'] == 'COMPLETE'
    assert broker.holdings == {'SPY': 6, 'QQQ': 4, 'OTHER': 99}


def test_removed_etf_sold_new_etf_bought_existing_etf_retained(tmp_path):
    broker = Broker()
    broker.holdings = {'SPY': 5, 'BIL': 5, 'OTHER': 99}
    broker.cash = 0
    broker.path = tmp_path / 'j'
    service = Rebalance(broker.path, broker, reserve_bps=0)
    cycle = service.create('replacement', {'SPY': .5, 'QQQ': .5}, ['SPY', 'QQQ', 'BIL'])
    cid = cycle['id']
    assert [(o['ticker'], o['quantity']) for o in cycle['payload']['preview']] == [('BIL', 5)]
    broker.fill(service.confirm(cid, 'SELL')['orders'])
    broker.cash = 500
    service.reconcile(cid)
    preview = service.preview_buys(cid)
    assert [(o['ticker'], o['quantity']) for o in preview['payload']['preview']] == [('QQQ', 5)]
    assert broker.holdings['SPY'] == 5


def test_additional_cash_only_buys_shortfall_and_preserves_cash_weight(tmp_path):
    from trading.allocation_rebalance import differential_plan
    broker = Broker()
    sells, buys, info = differential_plan(400, {'SPY': .4, 'QQQ': .4},
                                          {'SPY': 3, 'QQQ': 3}, broker.quotes(['SPY', 'QQQ']), 0)
    assert sells == []
    assert [(o['ticker'], o['quantity']) for o in buys] == [('QQQ', 1), ('SPY', 1)]
    assert info['estimated_cost'] == '200.00'


def test_old_full_liquidation_plan_cannot_be_executed(flow):
    service, broker, cid = flow
    current = service.status(cid)
    del current['payload']['mode']
    with service.db() as db:
        db.execute('UPDATE allocation_cycles SET payload=? WHERE id=?', (json.dumps(current['payload']), cid))
    with pytest.raises(RebalanceBlocked, match='이전 전량매도'):
        service.confirm(cid, 'SELL')
    assert broker.sent == []


def test_retained_position_change_blocks_buy(flow):
    service, broker, cid = sold(flow)
    broker.cash = 1000
    service.preview_buys(cid)
    broker.holdings['SPY'] += 1
    with pytest.raises(RebalanceBlocked):
        service.confirm(cid, 'BUY')
    assert len(broker.sent) == 1


def test_click_after_preview_expiry_replans_instead_of_locking_the_month(flow, monkeypatch):
    """Kiwoom quote times are minute-precision, so a preview may expire before
    the user finishes reading. The click must re-plan, not lock the cycle."""
    service, broker, cid = flow
    shown = service.status(cid)
    payload = shown['payload']
    payload['expires_at'] = time.time() - 30
    with service.db() as db:
        db.execute('UPDATE allocation_cycles SET payload=? WHERE id=?', (json.dumps(payload), cid))

    result = service.execute_authorized_cycle(cid, seen(shown))

    assert result['state'] == 'SELL_PENDING'
    assert result['payload']['execution_authorized'] is True
    assert result['payload']['expires_at'] > time.time()
    assert [(o['ticker'], o['quantity']) for o in broker.sent] == [('SPY', 2)]


def test_changed_plan_is_shown_again_and_never_executed_by_the_same_click(flow):
    service, broker, cid = flow
    shown = service.status(cid)
    broker.holdings['SPY'] = 6  # another fill changed account2 after the preview

    result = service.execute_authorized_cycle(cid, seen(shown))

    assert result['plan_changed'] is True
    assert result['state'] == 'SELL_PREVIEW'
    assert 'execution_authorized' not in result['payload']
    assert result['payload']['original_quantities']['SPY'] == '6'
    assert broker.sent == []
    again = service.execute_authorized_cycle(cid, seen(result))
    assert again['state'] == 'SELL_PENDING'
    assert [(o['ticker'], o['quantity']) for o in broker.sent] == [('SPY', 3)]  # 50% of $610


def test_failure_before_first_order_does_not_persist_authorization(flow):
    service, broker, cid = flow
    shown = service.status(cid)
    broker.pending = [{'ticker': 'SPY'}]
    with pytest.raises(RebalanceBlocked, match='미체결'):
        service.execute_authorized_cycle(cid, seen(shown))
    current = service.status(cid)
    assert current['state'] == 'SELL_PREVIEW'
    assert 'execution_authorized' not in current['payload']
    broker.pending = []
    assert service.refresh_sell_preview(cid)['state'] == 'SELL_PREVIEW'
    assert broker.sent == []


def _authorized_buy_preview(flow, cash='1000'):
    service, broker, cid = flow
    selling = service.execute_authorized_cycle(cid, seen(service.status(cid)))
    broker.fill([o for o in selling['orders'] if o['side'] == 'SELL'])
    broker.cash = Decimal(cash)
    assert service.reconcile(cid)['state'] == 'SELL_CONFIRMED'
    preview = service.preview_buys(cid)
    payload = preview['payload']
    payload['expires_at'] = time.time() - 1  # buy plan expired before dispatch
    with service.db() as db:
        db.execute('UPDATE allocation_cycles SET payload=? WHERE id=?', (json.dumps(payload), cid))
    return service, broker, cid


def test_expired_buy_plan_sends_at_approved_limit_when_fresh_quote_is_no_worse(flow):
    service, broker, cid = _authorized_buy_preview(flow)
    broker.quotes = lambda tickers: {t: {'limit_price': '99.50', 'as_of': time.time()} for t in tickers}

    result = service.advance_authorized_cycle(cid)

    assert result['state'] == 'BUY_PENDING'
    buys = [o for o in broker.sent if o['side'] == 'BUY']
    assert buys and {o['price'] for o in buys} == {'100.00'}
    assert not [o for o in result['orders'] if o['state'] == 'PREPARED']


def test_expired_buy_plan_stops_when_price_rose_then_resumes_without_duplicates(flow):
    service, broker, cid = _authorized_buy_preview(flow)
    sells = len(broker.sent)
    broker.quotes = lambda tickers: {t: {'limit_price': '101.00', 'as_of': time.time()} for t in tickers}

    stopped = service.advance_authorized_cycle(cid)

    assert stopped['state'] == 'BUY_PENDING'
    assert stopped['blockers']
    assert len(broker.sent) == sells
    prepared = [o for o in stopped['orders'] if o['side'] == 'BUY' and o['state'] == 'PREPARED']
    assert prepared

    broker.quotes = lambda tickers: {t: {'limit_price': '100.00', 'as_of': time.time()} for t in tickers}
    resumed = service.advance_authorized_cycle(cid)
    sent_buys = [o for o in broker.sent if o['side'] == 'BUY']
    assert len(sent_buys) == len(prepared)
    assert len({o['id'] for o in sent_buys}) == len(sent_buys)
    broker.fill([o for o in resumed['orders'] if o['side'] == 'BUY'])
    assert service.advance_authorized_cycle(cid)['state'] == 'COMPLETE'
    assert len([o for o in broker.sent if o['side'] == 'BUY']) == len(prepared)


def test_unsent_intents_never_resume_after_rejection_or_unknown(flow):
    service, broker, cid = _authorized_buy_preview(flow)
    broker.failure = OrderRejected('rejected')
    first = service.advance_authorized_cycle(cid)
    buys = [o for o in first['orders'] if o['side'] == 'BUY']
    assert [o['state'] for o in buys][0] == 'REJECTED'
    sent = len(broker.sent)
    broker.failure = None
    service.advance_authorized_cycle(cid)
    assert len(broker.sent) == sent



def test_orders_use_side_limits_while_targets_use_valuation_price():
    from trading.allocation_rebalance import differential_plan
    now = time.time()
    quotes = {t: {'limit_price': '100.00', 'buy_limit': '100.02', 'sell_limit': '99.98', 'as_of': now}
              for t in ('SPY', 'QQQ')}
    sells, buys, info = differential_plan(400, {'SPY': '.3', 'QQQ': '.7'}, {'SPY': 6, 'QQQ': 4}, quotes, 0)
    assert info['equity'] == '1400.00'  # valued at limit_price, not bid/ask
    assert info['targets'] == {'SPY': 4, 'QQQ': 9}
    assert sells == [{'ticker': 'SPY', 'side': 'SELL', 'quantity': 2, 'limit_price': '99.98'}]
    assert [(o['ticker'], o['limit_price']) for o in buys] == [('QQQ', '100.02')]


def _open_sell(flow):
    service, broker, cid = flow
    selling = service.execute_authorized_cycle(cid, seen(service.status(cid)))
    order = next(o for o in selling['orders'] if o['side'] == 'SELL')
    broker.evidence[order['id']] = {'matched_intent_id': order['id'], 'state': 'OPEN', 'filled_quantity': 0}
    broker.pending = [{'order_number': order['order_number']}]
    return service, broker, cid, order


def test_close_out_cancels_open_sell_then_replans_from_actual_holdings(flow):
    service, broker, cid, order = _open_sell(flow)
    sent = len(broker.sent)

    requested = service.close_out(cid)
    assert [o['id'] for o in broker.cancelled] == [order['id']]
    assert requested['orders'][0]['state'] == 'CANCEL_REQUESTED'
    assert requested['payload']['closing_out'] is True
    assert any('취소 확인 대기' in b for b in requested['blockers'])
    # Still waiting: the button may be pressed again without a second cancel.
    assert service.close_out(cid)['blockers']
    assert len(broker.cancelled) == 1

    # Broker confirms: 1 of 2 shares filled before the cancel.
    broker.evidence[order['id']] = {'matched_intent_id': order['id'], 'state': 'CANCELLED', 'filled_quantity': 1}
    broker.holdings['SPY'] = 3
    broker.cash = Decimal('110')
    broker.pending = []
    successor = service.advance_authorized_cycle(cid)

    assert successor['replanned'] is True
    assert successor['id'] != cid
    assert successor['state'] == 'SELL_PREVIEW'
    assert successor['cycle_key'] == 'easy-2026-08-31#r1'
    assert 'execution_authorized' not in successor['payload']
    assert successor['payload']['replaces'] == cid
    assert successor['payload']['original_quantities']['SPY'] == '3'
    old = service.status(cid)
    assert old['state'] == 'SUPERSEDED' and old['active_account'] is None
    assert old['payload']['superseded_by'] == successor['id']
    assert old['orders'][0]['state'] == 'CANCELLED'
    assert service.active()['id'] == successor['id']
    assert service.create('easy-2026-08-31', {'SPY': .5, 'QQQ': .5}, ['SPY', 'QQQ'])['id'] == successor['id']
    assert len(broker.sent) == sent  # close-out never sends a new order

    again = service.execute_authorized_cycle(successor['id'], seen(successor))
    assert again['state'] in {'SELL_PENDING', 'SELL_CONFIRMED', 'BUY_PREVIEW', 'BUY_PENDING'}


def test_close_out_abandons_unsent_buys_and_rejected_orders_then_replans(flow):
    service, broker, cid = _authorized_buy_preview(flow)
    broker.quotes = lambda tickers: {t: {'limit_price': '101.00', 'as_of': time.time()} for t in tickers}
    stopped = service.advance_authorized_cycle(cid)
    assert any(o['state'] == 'PREPARED' for o in stopped['orders'])
    sent = len(broker.sent)

    broker.quotes = lambda tickers: {t: {'limit_price': '100.00', 'as_of': time.time()} for t in tickers}
    result = service.close_out(cid)

    assert result['replanned'] is True
    assert {o['state'] for o in service.status(cid)['orders'] if o['side'] == 'BUY'} == {'ABANDONED'}
    assert len(broker.sent) == sent
    assert broker.cancelled == []


def test_close_out_never_replans_past_an_unmatched_unknown_order(flow):
    service, broker, cid = flow
    broker.failure = requests.Timeout('lost')
    service.execute_authorized_cycle(cid, seen(service.status(cid)))
    broker.failure = None
    result = service.close_out(cid)
    assert result['state'] == 'SELL_PENDING'
    assert any('접수 여부 불명' in b for b in result['blockers'])
    assert broker.cancelled == []
    assert service.active()['id'] == cid


def test_close_out_requires_a_pending_cycle(flow):
    service, broker, cid = flow
    with pytest.raises(RebalanceBlocked, match='체결 대기'):
        service.close_out(cid)


def test_repeated_replans_number_successors_and_keep_one_active(flow):
    service, broker, cid, order = _open_sell(flow)
    broker.evidence[order['id']] = {'matched_intent_id': order['id'], 'state': 'CANCELLED', 'filled_quantity': 0}
    broker.pending = []
    first = service.close_out(cid)
    second_cycle = service.execute_authorized_cycle(first['id'], seen(first))
    second_order = next(o for o in second_cycle['orders'] if o['side'] == 'SELL')
    broker.evidence[second_order['id']] = {'matched_intent_id': second_order['id'],
                                           'state': 'CANCELLED', 'filled_quantity': 0}
    second = service.close_out(first['id'])
    assert second['cycle_key'] == 'easy-2026-08-31#r2'
    with service.db() as db:
        active = db.execute("SELECT COUNT(*) FROM allocation_cycles WHERE active_account='account2'").fetchone()[0]
    assert active == 1


# ── 리뷰 2026-10-14: 동시성·취소 재전송 회귀 ─────────────────────────


def _two_prepared_buys(flow):
    service, broker, cid = _authorized_buy_preview(flow, cash='1000')
    broker.quotes = lambda tickers: {t: {'limit_price': '101.00', 'as_of': time.time()} for t in tickers}
    stopped = service.advance_authorized_cycle(cid)
    prepared = [o for o in stopped['orders'] if o['side'] == 'BUY' and o['state'] == 'PREPARED']
    assert len(prepared) == 2
    broker.quotes = lambda tickers: {t: {'limit_price': '100.00', 'as_of': time.time()} for t in tickers}
    return service, broker, cid


def test_close_out_never_replans_when_an_unsent_intent_was_claimed_concurrently(flow, monkeypatch):
    """[P1] A resume claims PREPARED -> SENDING just before close-out's flag transaction.

    After that transaction no claim can succeed (see the next test), so this is
    the only remaining window: the in-flight order must block the re-plan.
    """
    service, broker, cid = _two_prepared_buys(flow)
    real_status = service.status
    calls = {'n': 0}

    def racing_status(cycle_id):
        view = real_status(cycle_id)
        calls['n'] += 1
        if calls['n'] == 1:  # close-out's first read: another dispatcher claims first
            with service.db() as db:
                db.execute("UPDATE allocation_intents SET state='SENDING' WHERE cycle_id=? AND side='BUY'"
                           " AND state='PREPARED'", (cycle_id,))
        return view

    monkeypatch.setattr(service, 'status', racing_status)
    result = service.close_out(cid)
    monkeypatch.setattr(service, 'status', real_status)

    assert not result.get('replanned')
    assert any('접수 여부 불명' in b for b in result['blockers'])
    current = service.status(cid)
    assert {o['state'] for o in current['orders'] if o['side'] == 'BUY'} == {'SENDING'}
    assert current['state'] == 'BUY_PENDING'
    assert service.active()['id'] == cid


def test_concurrent_dispatchers_never_send_past_an_in_flight_order(flow):
    """[P1] B must not send order 2 while A's order 1 awaits its (later rejected) reply."""
    import threading
    service, broker, cid = _two_prepared_buys(flow)
    started, release = threading.Event(), threading.Event()
    original = broker.submit

    def slow_rejecting_submit(order):
        if not started.is_set():
            original(order)
            started.set()
            release.wait(5)
            raise OrderRejected('rejected after a slow reply')
        return original(order)

    broker.submit = slow_rejecting_submit
    payload = service.status(cid)['payload']
    a = threading.Thread(target=lambda: Rebalance(service.path, broker)._dispatch(cid, 'BUY', payload))
    a.start()
    assert started.wait(5)
    # B already passed its resumable pre-check before A claimed order 1.
    Rebalance(service.path, broker)._dispatch(cid, 'BUY', payload)
    release.set()
    a.join(5)

    buys = [o for o in broker.sent if o['side'] == 'BUY']
    assert len(buys) == 1
    states = sorted(o['state'] for o in service.status(cid)['orders'] if o['side'] == 'BUY')
    assert states == ['PREPARED', 'REJECTED']


def test_cancel_with_lost_reply_is_never_resent_automatically(flow):
    """[P2] A cancel whose reply is lost stays unknown until reconciled; no second cancel."""
    service, broker, cid, order = _open_sell(flow)

    def lost(order):
        broker.cancelled.append(order)
        raise requests.Timeout('cancel reply lost')

    broker.cancel = lost
    first = service.close_out(cid)
    assert len(broker.cancelled) == 1
    assert any('불명' in b for b in first['blockers'])
    second = service.close_out(cid)
    third = service.advance_authorized_cycle(cid)
    assert len(broker.cancelled) == 1
    assert any('불명' in b for b in second['blockers'] + third['blockers'])
    # Once broker history shows the cancel, the close-out completes.
    broker.evidence[order['id']] = {'matched_intent_id': order['id'], 'state': 'CANCELLED', 'filled_quantity': 0}
    broker.pending = []
    assert service.close_out(cid)['replanned'] is True
    assert len(broker.cancelled) == 1


def test_dispatcher_cannot_send_after_close_out_started(flow):
    """[P1 2nd review] While close-out reconciles order 1, a running dispatcher
    must not claim and send order 2."""
    service, broker, cid = _two_prepared_buys(flow)
    first, second = [o for o in service.status(cid)['orders'] if o['side'] == 'BUY']
    with service.db() as db:  # order 1 already went out before the close-out
        db.execute("UPDATE allocation_intents SET state='SUBMITTED', order_number='7', sent_at=? WHERE id=?",
                   (time.time(), first['id']))
    broker.evidence[first['id']] = {'matched_intent_id': first['id'], 'state': 'OPEN', 'filled_quantity': 0}
    payload = service.status(cid)['payload']
    sent_before = len(broker.sent)
    original_lookup = broker.lookup

    def lookup_while_dispatcher_runs(order):
        # The existing dispatcher resumes exactly while close-out reads order 1.
        Rebalance(service.path, broker)._dispatch(cid, 'BUY', payload)
        return original_lookup(order)

    broker.lookup = lookup_while_dispatcher_runs
    service.close_out(cid)

    assert len(broker.sent) == sent_before
    states = {o['id']: o['state'] for o in service.status(cid)['orders']}
    assert states[second['id']] == 'ABANDONED'
    assert states[first['id']] == 'CANCEL_REQUESTED'


def test_existing_journal_gains_send_and_closing_columns(tmp_path):
    """A journal created before sent_at/closing_out is migrated in place."""
    import sqlite3
    path = tmp_path / 'old.sqlite'
    db = sqlite3.connect(path)
    db.executescript('''
        CREATE TABLE allocation_cycles (
            id TEXT PRIMARY KEY, cycle_key TEXT NOT NULL UNIQUE,
            active_account TEXT UNIQUE, state TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE allocation_intents (
            id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL, side TEXT NOT NULL,
            ticker TEXT NOT NULL, quantity INTEGER NOT NULL, price TEXT NOT NULL,
            state TEXT NOT NULL, order_number TEXT, created_at REAL NOT NULL,
            UNIQUE(cycle_id, side, ticker));
    ''')
    db.execute("INSERT INTO allocation_cycles VALUES ('a', 'k', 'account2', 'BUY_PENDING', ?)",
               (json.dumps({'mode': 'DELTA_V1', 'closing_out': True}),))
    db.execute("INSERT INTO allocation_cycles VALUES ('b', 'k2', NULL, 'COMPLETE', ?)",
               (json.dumps({'mode': 'DELTA_V1'}),))
    db.commit()
    db.close()

    service = Rebalance(path, Broker())
    with service.db() as conn:
        flags = dict(conn.execute('SELECT id, closing_out FROM allocation_cycles'))
        intent_columns = {r['name'] for r in conn.execute('PRAGMA table_info(allocation_intents)')}
    assert flags == {'a': 1, 'b': 0}
    assert 'sent_at' in intent_columns
    Rebalance(path, Broker())  # idempotent on restart
