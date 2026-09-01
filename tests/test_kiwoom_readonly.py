import pytest
import requests

from trading.kiwoom_readonly import (
    BALANCE_API_ID,
    DOMESTIC_DEPOSIT_API_ID,
    KiwoomConfig,
    KiwoomError,
    KiwoomReadOnlyClient,
    MOCK_BASE_URL,
    OVERSEAS_BALANCE_API_ID,
    OVERSEAS_CURRENCY_API_ID,
    OVERSEAS_DEPOSIT_API_ID,
    OVERSEAS_VALUATION_API_ID,
    OVERSEAS_QUOTE_API_ID,
    REAL_BASE_URL,
    load_profile_configs,
)


class _Response:
    def __init__(self, data, *, status_code=200, headers=None):
        self._data = data
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def test_config_defaults_to_mock(monkeypatch):
    monkeypatch.setenv("KIWOOM_APP_KEY", "key")
    monkeypatch.setenv("KIWOOM_APP_SECRET", "secret")
    monkeypatch.delenv("KIWOOM_MODE", raising=False)

    config = KiwoomConfig.from_env()

    assert config.mode == "mock"
    assert config.base_url == MOCK_BASE_URL


def test_real_mode_uses_real_endpoint(monkeypatch):
    monkeypatch.setenv("KIWOOM_APP_KEY", "key")
    monkeypatch.setenv("KIWOOM_APP_SECRET", "secret")
    monkeypatch.setenv("KIWOOM_MODE", "real")

    assert KiwoomConfig.from_env().base_url == REAL_BASE_URL


def test_missing_credentials_are_rejected(monkeypatch):
    monkeypatch.delenv("KIWOOM_APP_KEY", raising=False)
    monkeypatch.delenv("KIWOOM_APP_SECRET", raising=False)

    with pytest.raises(KiwoomError, match="환경변수"):
        KiwoomConfig.from_env()


def test_issue_token_uses_client_credentials_without_logging_secret():
    session = _Session(
        [_Response({"return_code": 0, "token": "access", "expires_dt": "20260901000000"})]
    )
    client = KiwoomReadOnlyClient(KiwoomConfig("key", "secret"), session)

    result = client.issue_token()

    assert result["token"] == "access"
    url, request = session.calls[0]
    assert url == MOCK_BASE_URL + "/oauth2/token"
    assert request["json"] == {
        "grant_type": "client_credentials",
        "appkey": "key",
        "secretkey": "secret",
    }


def test_overseas_quote_uses_fixed_read_only_api():
    session = _Session([_Response({"return_code": 0, "cur_prc": "123.4500"})])
    client = KiwoomReadOnlyClient(KiwoomConfig("key", "secret"), session)

    result = client.get_overseas_quote("token", exchange="ND", ticker="QQQ")

    assert result["quote"]["cur_prc"] == "123.4500"
    url, request = session.calls[0]
    assert url == MOCK_BASE_URL + "/api/us/mrkcond"
    assert request["headers"]["api-id"] == OVERSEAS_QUOTE_API_ID
    assert request["json"] == {"stex_tp": "ND", "stk_cd": "QQQ"}


def test_balance_uses_fixed_read_only_api_and_follows_continuation(monkeypatch):
    monkeypatch.setattr("trading.kiwoom_readonly.time.sleep", lambda _: None)
    session = _Session(
        [
            _Response(
                {
                    "return_code": 0,
                    "tot_evlt_amt": "10000",
                    "acnt_evlt_remn_indv_tot": [{"stk_cd": "005930"}],
                },
                headers={"cont-yn": "Y", "next-key": "next"},
            ),
            _Response(
                {
                    "return_code": 0,
                    "acnt_evlt_remn_indv_tot": [{"stk_cd": "000660"}],
                }
            ),
        ]
    )
    client = KiwoomReadOnlyClient(KiwoomConfig("key", "secret"), session)

    result = client.get_account_balance("token")

    assert result["pages"] == 2
    assert [item["stk_cd"] for item in result["holdings"]] == ["005930", "000660"]
    assert result["summary"]["tot_evlt_amt"] == "10000"
    first_url, first_request = session.calls[0]
    assert first_url == MOCK_BASE_URL + "/api/dostk/acnt"
    assert first_request["headers"]["api-id"] == BALANCE_API_ID
    assert first_request["headers"]["authorization"] == "Bearer token"
    assert "next-key" not in first_request["headers"]
    second_headers = session.calls[1][1]["headers"]
    assert second_headers["cont-yn"] == "Y"
    assert second_headers["next-key"] == "next"


def test_overseas_balance_uses_us_read_only_api_and_follows_continuation(monkeypatch):
    monkeypatch.setattr("trading.kiwoom_readonly.time.sleep", lambda _: None)
    session = _Session(
        [
            _Response(
                {
                    "return_code": 0,
                    "tot_evlt_amt": "100.00",
                    "result_list": [{"stk_cd": "AAPL"}],
                },
                headers={"cont-yn": "Y", "next-key": "us-next"},
            ),
            _Response({"return_code": 0, "result_list": [{"stk_cd": "MSFT"}]}),
        ]
    )
    client = KiwoomReadOnlyClient(KiwoomConfig("key", "secret"), session)

    result = client.get_overseas_account_balance("token")

    assert result["api_id"] == OVERSEAS_BALANCE_API_ID
    assert result["pages"] == 2
    assert [item["stk_cd"] for item in result["holdings"]] == ["AAPL", "MSFT"]
    first_url, first_request = session.calls[0]
    assert first_url == MOCK_BASE_URL + "/api/us/acnt"
    assert first_request["headers"]["api-id"] == "ust21070"
    assert first_request["json"] == {"stex_tp": "", "stk_cd": ""}
    assert session.calls[1][1]["headers"]["next-key"] == "us-next"


def test_cash_and_valuation_methods_use_read_only_account_trs():
    session = _Session([
        _Response({"return_code": 0, "entr": "500", "ord_alow_amt": "400"}),
        _Response({"return_code": 0, "result_list": [{"crnc_code": "USD", "fc_entra": "10.25"}]}),
        _Response({"return_code": 0, "aset_evlt_amt": "10000", "result_list": [{"crnc_code": "USD", "evlt_amt": "20"}]}),
        _Response({"return_code": 0, "result_list": [{"crnc_code": "USD", "pl_amt": "2"}]}),
    ])
    client = KiwoomReadOnlyClient(KiwoomConfig("key", "secret"), session)

    domestic = client.get_domestic_deposit("token")
    overseas_cash = client.get_overseas_deposit("token")
    overseas_assets = client.get_overseas_currency_valuation("token")
    overseas_profit = client.get_overseas_valuation("token")

    assert domestic["summary"]["entr"] == "500"
    assert overseas_cash["items"][0]["fc_entra"] == "10.25"
    assert overseas_assets["summary"]["aset_evlt_amt"] == "10000"
    assert overseas_profit["items"][0]["pl_amt"] == "2"
    assert [call[1]["headers"]["api-id"] for call in session.calls] == [
        DOMESTIC_DEPOSIT_API_ID,
        OVERSEAS_DEPOSIT_API_ID,
        OVERSEAS_CURRENCY_API_ID,
        OVERSEAS_VALUATION_API_ID,
    ]
    assert session.calls[0][1]["json"] == {"qry_tp": "3"}
    assert session.calls[2][1]["json"] == {"cmsn_incl_tp": "1", "exrt_tp": "1"}


def test_api_error_does_not_expose_credentials():
    session = _Session([_Response({"return_code": 101, "return_msg": "invalid key"})])
    client = KiwoomReadOnlyClient(KiwoomConfig("top-secret-key", "secret"), session)

    with pytest.raises(KiwoomError, match="invalid key") as caught:
        client.issue_token()

    assert "top-secret-key" not in str(caught.value)


@pytest.mark.parametrize("query_type,exchange", [("3", "KRX"), ("1", "ALL")])
def test_balance_rejects_unknown_query_options(query_type, exchange):
    client = KiwoomReadOnlyClient(KiwoomConfig("key", "secret"), _Session([]))

    with pytest.raises(KiwoomError):
        client.get_account_balance("token", query_type=query_type, exchange=exchange)


def test_load_multiple_account_profiles(tmp_path):
    profiles_file = tmp_path / "profiles.json"
    profiles_file.write_text(
        '{"mode":"real","profiles":{'
        '"account1":{"app_key":"key1","app_secret":"secret1"},'
        '"account2":{"app_key":"key2","app_secret":"secret2"}}}',
        encoding="utf-8",
    )
    profiles_file.chmod(0o600)

    profiles = load_profile_configs(profiles_file)

    assert list(profiles) == ["account1", "account2"]
    assert profiles["account1"] == KiwoomConfig("key1", "secret1", "real", 15.0)


def test_profile_file_rejects_open_permissions(tmp_path):
    profiles_file = tmp_path / "profiles.json"
    profiles_file.write_text(
        '{"mode":"real","profiles":{"account1":'
        '{"app_key":"key","app_secret":"secret"}}}',
        encoding="utf-8",
    )
    profiles_file.chmod(0o644)

    with pytest.raises(KiwoomError, match="600"):
        load_profile_configs(profiles_file)
