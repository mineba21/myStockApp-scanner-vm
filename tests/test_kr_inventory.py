import pytest
import requests
from scanner import kr_stocks, scan_engine
from scanner.errors import DataFetchError

GOOD={'block1':[{'short_code':'00104K','codeName':'삼성전자','marketCode':'STK'}]}

class Response:
    status_code=200
    content=b'synthetic'
    def __init__(self, body): self.body=body
    def json(self):
        if isinstance(self.body, Exception): raise self.body
        return self.body

@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(kr_stocks.time,'sleep',lambda _:None)
    monkeypatch.setattr(kr_stocks,'_get_kr_cap_price',lambda:None)

def test_https_post_preserves_parameters(monkeypatch, setup):
    def post(url, **kw):
        assert url.startswith('https://') and kw['headers']['Referer'].startswith('https://')
        assert kw['allow_redirects'] is False and kw['data']['bld'].endswith('finder_stkisu')
        return Response(GOOD)
    monkeypatch.setattr(requests,'post',post)
    assert kr_stocks.get_all_kr_tickers()[0]['ticker']=='00104K'

@pytest.mark.parametrize('body',[{}, {'block1':[]}, {'block1':[None]}, ValueError('html')])
def test_invalid_response_exhausts_then_raises(monkeypatch,setup,body):
    calls=[]
    monkeypatch.setattr(requests,'post',lambda *a,**kw:(calls.append(1) or Response(body)))
    with pytest.raises(DataFetchError): kr_stocks.get_all_kr_tickers()
    assert len(calls)==3

def test_timeout_recovers(monkeypatch,setup):
    calls=[]
    def post(*a,**kw):
        calls.append(1)
        if len(calls)==1: raise requests.Timeout()
        return Response(GOOD)
    monkeypatch.setattr(requests,'post',post)
    assert len(kr_stocks.get_all_kr_tickers())==1 and len(calls)==2

def test_empty_inventory_cannot_be_success(monkeypatch):
    monkeypatch.setattr(kr_stocks,'get_all_kr_tickers',lambda **kw:[])
    with pytest.raises(DataFetchError): scan_engine._scan_kr(None)
