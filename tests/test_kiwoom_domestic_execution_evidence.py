from types import SimpleNamespace

import pytest

from trading.kiwoom_domestic_execution_evidence import DomesticExecutionEvidence
from trading.kiwoom_readonly import KiwoomConfig, KiwoomError


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if len(response) == 2:
            data, headers = response
            status_code = 200
        else:
            data, headers, status_code = response
        return SimpleNamespace(json=lambda: data, headers=headers, status_code=status_code)


class Client:
    def __init__(self, responses):
        self.config = KiwoomConfig("key", "secret", "mock")
        self.session = Session(responses)

    def _parse_response(self, response, operation):
        return response.json()


def open_row(**changes):
    row = {"ord_no": "0000123", "stk_cd": "005930", "io_tp_nm": "+매수",
           "ord_qty": "1", "cntr_qty": "0", "oso_qty": "1", "ord_pric": "70000"}
    return {**row, **changes}


def history_row(**changes):
    row = {"ord_no": "0000124", "ori_ord": "0000123", "stk_cd": "A005930",
           "io_tp_nm": "현금매수", "ord_qty": "1", "cntr_qty": "0",
           "ord_remnq": "0", "ord_uv": "70000", "acpt_tp": "확인", "mdfy_cncl": "취소"}
    return {**row, **changes}


def test_snapshot_uses_orderable_cash_and_all_open_pages():
    client = Client([
        ({"return_code": 0, "ord_alow_amt": "000100000"}, {}),
        ({"return_code": 0, "oso": [open_row()]}, {"cont-yn": "Y", "next-key": "n"}),
        ({"return_code": 0, "oso": [open_row(ord_no="0000125")]}, {"cont-yn": "N"}),
    ])
    evidence = DomesticExecutionEvidence(client, "token")

    result = evidence.snapshot()

    assert result["broker_orderable_cash"] == "100000"
    assert result["open_order_count"] == 2
    assert client.session.calls[2][1]["headers"]["next-key"] == "n"


def test_lookup_open_matches_full_identity():
    client = Client([({"return_code": 0, "oso": [open_row()]}, {})])
    evidence = DomesticExecutionEvidence(client, "token")
    intent = {"order_number": "123", "ticker": "005930", "side": "BUY",
              "quantity": 1, "price": 70000}
    assert evidence.lookup_open(intent)["state"] == "OPEN"


def test_cancel_requires_original_absent_and_cancel_history_evidence():
    client = Client([
        ({"return_code": 0, "oso": []}, {}),
        ({"return_code": 0, "acnt_ord_cntr_prps_dtl": [history_row()]}, {}),
    ])
    evidence = DomesticExecutionEvidence(client, "token")
    intent = {"order_number": "0000123", "cancel_order_number": "0000124"}
    assert evidence.lookup_cancel(intent)["state"] == "CANCELLED"


def test_cancel_is_not_inferred_only_from_open_order_disappearance():
    client = Client([
        ({"return_code": 0, "oso": []}, {}),
        ({"return_code": 0, "acnt_ord_cntr_prps_dtl": []}, {}),
    ])
    evidence = DomesticExecutionEvidence(client, "token")
    assert evidence.lookup_cancel({"order_number": "123", "cancel_order_number": "124"}) is None


def test_filled_order_requires_full_identity_and_completed_quantity():
    row = history_row(ord_no="0000123", ori_ord="0000000", cntr_qty="1",
                      ord_remnq="0", mdfy_cncl="일반")
    evidence = DomesticExecutionEvidence(
        Client([({"return_code": 0, "acnt_ord_cntr_prps_dtl": [row]}, {})]), "token"
    )
    intent = {"order_number": "123", "ticker": "005930", "side": "BUY", "quantity": 1}
    assert evidence.lookup_filled(intent)["state"] == "FILLED"


def test_missing_success_code_and_malformed_quantity_fail_closed():
    evidence = DomesticExecutionEvidence(Client([({"oso": []}, {})]), "token")
    with pytest.raises(KiwoomError):
        evidence.open_orders()
    with pytest.raises(KiwoomError):
        DomesticExecutionEvidence._normalize_open(open_row(oso_qty="x"))


def test_read_only_lookup_retries_rate_limit_without_resending_an_order():
    client = Client([
        ({"return_code": -1}, {}, 429),
        ({"return_code": 0, "oso": [open_row()]}, {}, 200),
    ])
    delays = []
    evidence = DomesticExecutionEvidence(
        client, "token", rate_limit_delays=(2,), sleeper=delays.append,
    )

    assert evidence.open_orders()[0]["order_number"] == "0000123"
    assert delays == [2]
    assert len(client.session.calls) == 2
