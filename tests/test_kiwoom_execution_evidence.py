from datetime import datetime
import pytest
import requests
from trading.kiwoom_readonly import KiwoomReadOnlyClient, KiwoomConfig, KiwoomError
from trading.kiwoom_execution_evidence import ExecutionEvidence, KST

class Response:
    def __init__(self, data, headers=None):
        self.data, self.headers = data, headers or {}
    def raise_for_status(self): pass
    def json(self): return self.data

class Session:
    def __init__(self, *responses): self.responses, self.calls = list(responses), []
    def post(self, url, **kw):
        self.calls.append((url, kw))
        r = self.responses.pop(0)
        if isinstance(r, Exception): raise r
        return r

def service(*responses, **kw):
    return ExecutionEvidence(KiwoomReadOnlyClient(KiwoomConfig('mock', 'secret'), Session(*responses)), 'token', **kw)

def response(rows, **headers): return Response({'return_code': 0, 'result_list': rows}, headers)

def row(**kw):
    return dict(ord_no='000000123', crnc_code='USD', stk_cd='SPY', slby_tp_nm='매수',
                ord_qty='10', cntr_qty='10', ord_remnq='0', cncl_qty='0', mdfy_qty='0',
                ord_uv='100.0000', ord_time='22:30:00', ord_stat_nm='체결완료', **kw)

def intent():
    return dict(id='intent', order_number='123', ticker='SPY', side='BUY', quantity=10,
                price='100', created_at=datetime(2026,9,11,22,30,tzinfo=KST).timestamp())

def test_filled_match_and_kst_date():
    s = service(response([row()]))
    assert s.lookup(intent())['state'] == 'FILLED'
    payload = s.client.session.calls[0][1]['json']
    assert payload['ord_dt'] == '20260911' and payload['query_tp'] == '1'

@pytest.mark.parametrize('updates,state', [
    ({'ord_stat_nm':'접수','cntr_qty':'3','ord_remnq':'7'},'PARTIAL'),
    ({'ord_stat_nm':'접수','cntr_qty':'0','ord_remnq':'10'},'OPEN'),
    ({'ord_stat_nm':'취소완료','cntr_qty':'3','cncl_qty':'7'},'CANCELLED'),
    ({'ord_stat_nm':'무효주문','cntr_qty':'0'},'REJECTED'),
    ({'mdfy_qty':'1'},'MODIFIED'),
    ({'ord_stat_nm':'접수'},'UNKNOWN'),
    ({'ord_stat_nm':'체결완료','cntr_qty':'3'},'UNKNOWN'),
])
def test_incomplete_states(updates,state):
    r=row(); r.update(updates)
    assert service(response([r])).lookup(intent())['state'] == state

@pytest.mark.parametrize('field,value', [('ticker','QQQ'),('price','99'),('quantity',9),('side','SELL')])
def test_identity_mismatch(field,value):
    i=intent(); i[field]=value
    assert service(response([row()])).lookup(i) is None

def test_unknown_receipt_never_adopts_manual_order():
    s=service(); i=intent(); i['order_number']=None
    assert s.lookup(i) is None and not s.client.session.calls

@pytest.mark.parametrize('data',[{}, {'result_list':[]}, {'return_code':False,'result_list':[]},
    {'return_code':0}, {'return_code':0,'result_list':[None]}])
def test_malformed_evidence_fails_closed(data):
    with pytest.raises(KiwoomError): service(Response(data)).history('20260911')

def test_all_pages_and_duplicate_guard():
    s=service(response([], **{'cont-yn':'Y','next-key':'next'}),response([row()]))
    assert len(s.history('20260911')) == 1
    assert s.client.session.calls[1][1]['headers']['next-key']=='next'
    with pytest.raises(KiwoomError): service(response([row(),row()])).history('20260911')

@pytest.mark.parametrize('headers',[{'cont-yn':'Y'}, {'cont-yn':'invalid'}, {'cont-yn':'Y','next-key':'next'}])
def test_incomplete_pagination(headers):
    with pytest.raises(KiwoomError): service(response([],**headers), max_pages=1).history('20260911')

def test_timeout_not_empty_success():
    with pytest.raises(requests.Timeout): service(requests.Timeout()).history('20260911')

def test_cash_is_broker_value_no_estimated_proceeds_or_double_deduction():
    s=service(response([{'ord_no':'123'}]), response([{'crnc_code':'USD','fc_ord_alowa':'1,200.25'}]))
    result=s.cash_check('20260911')
    assert result['broker_orderable_cash']=='1200.25'
    assert result['open_order_count']==1
    assert result['cash_includes_reservations'] is None
    assert not result['validated_for_execution']

@pytest.mark.parametrize('rows',[[],[{'crnc_code':'USD'}],
    [{'crnc_code':'USD','fc_ord_alowa':'NaN'}],
    [{'crnc_code':'USD','fc_ord_alowa':'-1'}],
    [{'crnc_code':'USD','fc_ord_alowa':'1'}]*2])
def test_invalid_cash(rows):
    with pytest.raises(KiwoomError): service(response([]),response(rows)).cash_check('20260911')

def test_invalid_date_before_network():
    s=service()
    with pytest.raises(KiwoomError): s.history('20260230')
    assert not s.client.session.calls

def test_evidence_api_is_registered_before_dynamic_cycle_route(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from web import asset_allocation_api as api
    app = FastAPI()
    app.include_router(api.router)
    test_client = TestClient(app)
    class Fake:
        def history(self, day): return []
        def cash_check(self, day): return {'validated_for_execution':False}
    monkeypatch.setattr(api, '_execution_evidence', lambda: Fake())
    r=test_client.get('/api/asset-allocation/rebalance/evidence?order_day=20260911')
    assert r.status_code==200 and r.json()['orders']==[]
    assert test_client.get('/api/asset-allocation/rebalance/evidence?order_day=bad').status_code==422


def capacity(**updates):
    data=dict(return_code=0, crnc_code='USD', ord_alowa='1000', min_ord_alowa='900', min_ord_alowq='8',
              ord_alowq_50='999', krw_ord_alowq_100='888')
    data.update(updates)
    return Response(data)

def test_price_specific_cash_capacity_excludes_margin_and_krw():
    s=service(capacity())
    r=s.buy_capacity('SPY','NY','100')
    assert r['max_quantity_with_reserve']==8 and r['budget_with_reserve']=='891.00'
    url, request=s.client.session.calls[0]
    assert url.endswith('/api/us/ordr') and request['headers']['api-id']=='ust31490'
    assert request['json']=={'stex_tp':'NY','stk_cd':'SPY','uv':'100'}
    assert not r['execution_enabled']

@pytest.mark.parametrize('updates',[
    {'crnc_code':'KRW'}, {'min_ord_alowq':None}, {'min_ord_alowa':'NaN'}, {'ord_alowa':'-1'},
    {'min_ord_alowq':'1.5'}, {'return_code':False}])
def test_invalid_capacity(updates):
    with pytest.raises(KiwoomError): service(capacity(**updates)).buy_capacity('SPY','NY','100')

def test_capacity_cash_drop_and_zero():
    assert service(capacity(ord_alowa='99')).buy_capacity('SPY','NY','100')['max_quantity_with_reserve']==0
    assert service(capacity(min_ord_alowq='0')).buy_capacity('SPY','NY','100')['max_quantity_with_reserve']==0

@pytest.mark.parametrize('price',['0','NaN','1.001','-1'])
def test_capacity_invalid_price_before_http(price):
    s=service()
    with pytest.raises(KiwoomError): s.buy_capacity('SPY','NY',price)
    assert not s.client.session.calls
