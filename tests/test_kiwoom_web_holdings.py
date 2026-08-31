from trading.kiwoom_readonly import KiwoomConfig, KiwoomError
from web import kiwoom_holdings


def test_map_kiwoom_holding_matches_web_schema():
    row = kiwoom_holdings.map_kiwoom_holding(
        "account1",
        {
            "stk_cd": "A005930",
            "stk_nm": "삼성전자",
            "rmnd_qty": "10",
            "pur_pric": "70000",
            "cur_prc": "+75000",
            "evlt_amt": "750000",
            "evltv_prft": "50000",
            "prft_rt": "7.14",
        },
        updated_at="2026-08-31T12:00:00",
    )

    assert row["id"] == "kiwoom:account1:KR:005930"
    assert row["ticker"] == "005930"
    assert row["quantity"] == 10
    assert row["avg_price"] == 70000
    assert row["current_price"] == 75000
    assert row["eval_amount"] == 750000
    assert row["profit_loss"] == 50000
    assert row["source"] == "kiwoom"
    assert row["read_only"] is True
    assert "app_key" not in row


def test_account_profiles_use_portfolio_names():
    domestic = kiwoom_holdings.map_kiwoom_holding(
        "account2", {"stk_cd": "005930", "rmnd_qty": "1"}
    )
    overseas = kiwoom_holdings.map_kiwoom_overseas_holding(
        "account2", {"stk_cd": "SPY", "poss_qty": "1"}
    )

    assert domestic["account_name"] == "자유투자 · account2"
    assert overseas["account_name"] == "자유투자 · account2"
    assert kiwoom_holdings.map_kiwoom_holding(
        "account1", {"stk_cd": "005930", "rmnd_qty": "1"}
    )["account_name"] == "퀀트투자 · account1"
    assert kiwoom_holdings.map_kiwoom_holding(
        "account4", {"stk_cd": "005930", "rmnd_qty": "1"}
    )["account_name"] == "ISA · account4"


def test_account_summary_maps_cash_and_profit_by_market():
    summary = kiwoom_holdings._account_summary(
        "account1",
        {"summary": {"tot_pur_amt": "1000", "tot_evlt_amt": "1200", "tot_evlt_pl": "200", "tot_prft_rt": "20", "prsm_dpst_aset_amt": "1700"}},
        {"summary": {"entr": "500", "pymn_alow_amt": "450", "ord_alow_amt": "430", "d2_entra": "480"}},
        {"items": [{"crnc_code": "USD", "fc_entra": "100.25", "fc_pymn_alowa": "90", "fc_ord_alowa": "95"}]},
        {"summary": {"aset_evlt_amt": "200000"}, "items": [{"crnc_code": "USD", "evlt_amt": "300", "crnc_rt": "1400", "chg_entr": "140350", "chg_evlt_amt": "420000"}]},
        {"items": [{"crnc_code": "USD", "pl_amt": "20", "pl_rt": "7.14", "chg_profit_amt": "28000"}]},
        updated_at="2026-08-31T12:00:00Z",
    )

    assert summary["display_name"] == "퀀트투자"
    assert summary["domestic"]["cash"] == 500
    assert summary["domestic"]["profit_loss"] == 200
    assert summary["overseas"]["cash"] == 100.25
    assert summary["overseas"]["evaluation_amount"] == 300
    assert summary["overseas"]["profit_loss"] == 20
    assert summary["read_only"] is True


def test_empty_overseas_valuation_is_treated_as_zero_balance():
    report = kiwoom_holdings._safe_overseas_call(
        lambda: (_ for _ in ()).throw(KiwoomError("조회 실패 (20): 조회내역이 없습니다."))
    )

    assert report == {"summary": {}, "items": [], "holdings": []}


def test_map_kiwoom_overseas_holding_matches_web_schema():
    row = kiwoom_holdings.map_kiwoom_overseas_holding(
        "account1",
        {
            "stk_cd": "aapl",
            "frgn_stk_nm": "Apple Inc",
            "crnc_code": "USD",
            "stex_nm": "NASDAQ",
            "poss_qty": "2",
            "frgn_stk_book_uv": "200.50",
            "now_pric": "210.25",
            "evlt_amt": "420.50",
            "pl_amt": "19.50",
            "pl_rt": "4.86",
            "evlt_amt_krw": "567675",
            "pl_amt_krw": "26325",
            "exch_rate": "1350",
        },
        updated_at="2026-08-31T12:00:00",
    )

    assert row["id"] == "kiwoom:account1:US:AAPL"
    assert row["ticker"] == "AAPL"
    assert row["market"] == "US"
    assert row["currency"] == "USD"
    assert row["quantity"] == 2
    assert row["avg_price"] == 200.5
    assert row["eval_amount"] == 420.5
    assert row["eval_amount_krw"] == 567675
    assert row["source"] == "kiwoom"
    assert row["read_only"] is True


def test_get_kiwoom_holdings_combines_profiles_and_reuses_cache(monkeypatch):
    kiwoom_holdings.clear_kiwoom_cache()
    monkeypatch.setattr(
        kiwoom_holdings,
        "load_profile_configs",
        lambda: {
            "account1": KiwoomConfig("key1", "secret1", "real"),
            "account2": KiwoomConfig("key2", "secret2", "real"),
        },
    )
    calls = []

    class Client:
        def __init__(self, config):
            self.config = config

        def issue_token(self):
            calls.append((self.config.app_key, "token"))
            return {"token": f"token-{self.config.app_key}"}

        def get_account_balance(self, token):
            calls.append((self.config.app_key, "balance"))
            suffix = "005930" if self.config.app_key == "key1" else "000660"
            return {"holdings": [{
                "stk_cd": suffix,
                "stk_nm": suffix,
                "rmnd_qty": "1",
                "pur_pric": "100",
                "cur_prc": "110",
                "evlt_amt": "110",
                "evltv_prft": "10",
                "prft_rt": "10",
            }]}

        def get_overseas_account_balance(self, token):
            calls.append((self.config.app_key, "overseas_balance"))
            ticker = "AAPL" if self.config.app_key == "key1" else "MSFT"
            return {"holdings": [{
                "stk_cd": ticker,
                "frgn_stk_nm": ticker,
                "crnc_code": "USD",
                "poss_qty": "1",
                "frgn_stk_book_uv": "100",
                "now_pric": "110",
                "evlt_amt": "110",
                "pl_amt": "10",
                "pl_rt": "10",
            }]}

    first = kiwoom_holdings.get_kiwoom_holdings(client_factory=Client)
    second = kiwoom_holdings.get_kiwoom_holdings(client_factory=Client)

    assert [(row["account_profile"], row["market"]) for row in first] == [
        ("account1", "KR"), ("account1", "US"),
        ("account2", "KR"), ("account2", "US"),
    ]
    assert second == first
    assert calls == [
        ("key1", "token"), ("key1", "balance"), ("key1", "overseas_balance"),
        ("key2", "token"), ("key2", "balance"), ("key2", "overseas_balance"),
    ]


def test_domestic_only_profile_does_not_hide_domestic_holdings(monkeypatch):
    kiwoom_holdings.clear_kiwoom_cache()
    monkeypatch.setattr(
        kiwoom_holdings,
        "load_profile_configs",
        lambda: {"account4": KiwoomConfig("key4", "secret4", "real")},
    )

    class Client:
        def __init__(self, config):
            self.config = config

        def issue_token(self):
            return {"token": "token"}

        def get_account_balance(self, token):
            return {"holdings": [{
                "stk_cd": "005930", "stk_nm": "삼성전자", "rmnd_qty": "1",
                "pur_pric": "100", "cur_prc": "110", "evlt_amt": "110",
                "evltv_prft": "10", "prft_rt": "10",
            }]}

        def get_overseas_account_balance(self, token):
            raise KiwoomError("미국주식 원장잔고 조회 실패: 508540")

    rows = kiwoom_holdings.get_kiwoom_holdings(force=True, client_factory=Client)

    assert [(row["account_profile"], row["market"]) for row in rows] == [
        ("account4", "KR")
    ]
