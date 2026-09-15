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

    selling = service.execute_authorized_cycle(cid)
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

    result = service.execute_authorized_cycle(cid)

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
