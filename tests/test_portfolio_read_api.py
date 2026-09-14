from fastapi.testclient import TestClient


TOKEN = "portfolio-read-test-" + "x" * 32


def _summary(profile, name, market):
    result = {
        "account_profile": profile,
        "account_name": name,
        "updated_at": "2026-09-15T00:00:00Z",
        "domestic": {},
        "overseas": {},
    }
    if market == "US":
        result["overseas"] = {
            "cash": 100, "withdrawable_cash": 90, "orderable_cash": 80,
            "evaluation_amount": 1000, "profit_loss": 100,
            "profit_loss_pct": 11.11, "exchange_rate": 1400,
            "cash_krw": 140000, "evaluation_amount_krw": 1400000,
            "profit_loss_krw": 140000, "estimated_assets_krw": 1540000,
        }
    else:
        result["domestic"] = {
            "cash": 200000, "withdrawable_cash": 190000,
            "orderable_cash": 180000, "d2_cash": 195000,
            "purchase_amount": 800000, "evaluation_amount": 900000,
            "profit_loss": 100000, "profit_loss_pct": 12.5,
            "estimated_assets": 1100000,
        }
    return result


def _holding(profile, name, market, ticker):
    return {
        "id": "secret-internal-id", "account_profile": profile,
        "account_name": name, "market": market,
        "currency": "USD" if market == "US" else "KRW",
        "ticker": ticker, "name": ticker, "quantity": 2,
        "avg_price": 100, "current_price": 110, "eval_amount": 220,
        "profit_loss": 20, "profit_loss_pct": 10,
        "eval_amount_krw": 308000, "profit_loss_krw": 28000,
        "price_updated_at": "2026-09-15T00:00:00Z",
        "source": "kiwoom", "memo": "must-not-leak", "read_only": True,
    }


def test_portfolio_projection_and_filters(monkeypatch):
    import web.app as webapp

    monkeypatch.setattr(webapp, "PORTFOLIO_READ_TOKEN", TOKEN)
    monkeypatch.setattr(
        "web.portfolio_read_api.get_kiwoom_account_summaries",
        lambda: [
            _summary("account1", "자유투자", "US"),
            _summary("account2", "퀀트투자", "US"),
            _summary("account4", "ISA", "KR"),
        ],
    )
    monkeypatch.setattr(
        "web.portfolio_read_api.get_kiwoom_holdings",
        lambda: [
            _holding("account1", "자유투자", "US", "AAPL"),
            _holding("account2", "퀀트투자", "US", "SPY"),
            _holding("account4", "ISA", "KR", "005930"),
            {**_holding("account1", "수동", "US", "FAKE"), "source": "manual"},
        ],
    )
    client = TestClient(webapp.app, headers={"Authorization": "Bearer " + TOKEN})

    overview = client.get("/api/portfolio-read/overview")
    assert overview.status_code == 200
    assert overview.headers["cache-control"] == "no-store"
    assert [row["account_profile"] for row in overview.json()["accounts"]] == [
        "account1", "account2", "account4"
    ]
    assert [row["markets"][0]["market"] for row in overview.json()["accounts"]] == [
        "US", "US", "KR"
    ]

    all_rows = client.get("/api/portfolio-read/holdings").json()["items"]
    assert [row["ticker"] for row in all_rows] == ["AAPL", "SPY", "005930"]
    selected = client.get("/api/portfolio-read/holdings?account=account2").json()["items"]
    assert [row["ticker"] for row in selected] == ["SPY"]
    assert not {"id", "memo", "account_number", "sell_status"} & selected[0].keys()
    assert "must-not-leak" not in str(selected)


def test_portfolio_credential_is_isolated(monkeypatch):
    import web.app as webapp

    monkeypatch.setattr(webapp, "PORTFOLIO_READ_TOKEN", TOKEN)
    monkeypatch.setattr(webapp, "SCANNER_READ_TOKEN", "scanner-" + "s" * 40)
    monkeypatch.setattr(webapp, "SITES_API_KEY", "application-" + "a" * 40)
    client = TestClient(webapp.app)

    for supplied in ("", "bad", webapp.SCANNER_READ_TOKEN, webapp.SITES_API_KEY):
        response = client.get(
            "/api/portfolio-read/overview",
            headers={"Authorization": "Bearer " + supplied},
        )
        assert response.status_code == 401
    headers = {"Authorization": "Bearer " + TOKEN}
    assert client.post("/api/portfolio-read/overview", headers=headers).status_code == 403
    assert client.get("/api/portfolio-read/unknown", headers=headers).status_code == 403
    assert client.get("/api/scanner-read/status", headers=headers).status_code == 401
    assert client.get("/api/accounts", headers=headers).status_code == 401
    assert client.post("/api/asset-allocation/rebalance/preview", headers=headers).status_code == 401


def test_portfolio_token_collisions_fail_closed(monkeypatch):
    import web.app as webapp

    client = TestClient(webapp.app)
    monkeypatch.setattr(webapp, "PORTFOLIO_READ_TOKEN", TOKEN)
    monkeypatch.setattr(webapp, "SCANNER_READ_TOKEN", TOKEN)
    assert client.get(
        "/api/portfolio-read/overview",
        headers={"Authorization": "Bearer " + TOKEN},
    ).status_code == 503


def test_portfolio_broker_failure_is_generic(monkeypatch):
    import web.app as webapp

    monkeypatch.setattr(webapp, "PORTFOLIO_READ_TOKEN", TOKEN)
    monkeypatch.setattr(
        "web.portfolio_read_api.get_kiwoom_holdings",
        lambda: (_ for _ in ()).throw(RuntimeError("secret brokerage response")),
    )
    response = TestClient(webapp.app).get(
        "/api/portfolio-read/holdings",
        headers={"Authorization": "Bearer " + TOKEN},
    )
    assert response.status_code == 502
    assert "secret brokerage response" not in response.text
