"""수동 매수 기록, 보유 재계산, 매도 상태 저장 테스트."""
import asyncio
from datetime import date, datetime, timedelta

import pandas as pd
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import Account, Base, Holding, Transaction


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    session.add_all([
        Account(id=1, name="국내", account_type="KR_STOCK", currency="KRW"),
        Account(id=2, name="해외", account_type="US_STOCK", currency="USD"),
    ])
    session.commit()
    try:
        yield session
    finally:
        session.close()


def _buy(db, *, account_id=2, market="US", ticker="AAPL", quantity=1.0, price=100.0,
         trade_date=None):
    from web.app import TxCreate, create_transaction

    body = TxCreate(
        account_id=account_id,
        tx_type="BUY",
        trade_date=trade_date or date.today().isoformat(),
        ticker=ticker,
        name=ticker,
        market=market,
        quantity=quantity,
        price=price,
    )
    return asyncio.run(create_transaction(body, db))


def _daily(n=220):
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    values = [100 + i * 0.1 for i in range(n)]
    return pd.DataFrame({
        "Open": values,
        "High": [v + 1 for v in values],
        "Low": [v - 1 for v in values],
        "Close": values,
        "Volume": [1_000_000] * n,
    }, index=idx)


def test_us_fractional_buys_compute_weighted_average(db):
    _buy(db, quantity=1.5, price=100)
    _buy(db, quantity=0.5, price=120)

    holding = db.query(Holding).filter(Holding.ticker == "AAPL").one()
    txs = db.query(Transaction).filter(Transaction.ticker == "AAPL").all()
    assert holding.quantity == 2.0
    assert holding.avg_price == 105.0
    assert holding.sell_status == "PENDING"
    assert [tx.amount for tx in txs] == [150.0, 60.0]


def test_kr_buy_requires_integer_quantity_and_krw_account(db):
    with pytest.raises(HTTPException, match="정수"):
        _buy(db, account_id=1, market="KR", ticker="005930", quantity=1.5, price=70000)

    with pytest.raises(HTTPException, match="KRW"):
        _buy(db, account_id=2, market="KR", ticker="005930", quantity=1, price=70000)


def test_future_buy_date_is_rejected(db):
    from web.app import KST
    future = (datetime.now(KST).date() + timedelta(days=1)).isoformat()
    with pytest.raises(HTTPException, match="미래"):
        _buy(db, trade_date=future)


def test_delete_buy_recalculates_or_removes_holding(db):
    from web.app import delete_transaction

    _buy(db, quantity=1, price=100)
    _buy(db, quantity=1, price=120)
    txs = db.query(Transaction).order_by(Transaction.id).all()

    asyncio.run(delete_transaction(txs[1].id, db))
    holding = db.query(Holding).filter(Holding.ticker == "AAPL").one()
    assert holding.quantity == 1
    assert holding.avg_price == 100

    asyncio.run(delete_transaction(txs[0].id, db))
    assert db.query(Holding).filter(
        Holding.ticker == "AAPL", Holding.is_active == True,
    ).count() == 0


@pytest.mark.parametrize(
    ("severity", "expected"),
    [("HIGH", "SELL_REQUIRED"), ("MEDIUM", "REVIEW"),
     ("LOW", "CAUTION"), (None, "HOLD")],
)
def test_holding_sell_status_mapping(db, monkeypatch, severity, expected):
    from scanner import scan_engine, us_stocks, weinstein

    _buy(db)
    monkeypatch.setattr(us_stocks, "get_us_ohlcv", lambda ticker: _daily())
    monkeypatch.setattr(weinstein, "to_weekly_ohlcv", lambda frame: frame)
    monkeypatch.setattr(
        weinstein,
        "check_sell_signal",
        lambda *args, **kwargs: None if severity is None else {
            "severity": severity,
            "sell_reason": f"{severity} 테스트",
        },
    )

    signals = scan_engine._check_holdings(db, us_bench=None)
    holding = db.query(Holding).filter(Holding.ticker == "AAPL").one()
    assert holding.sell_status == expected
    sell_signals = [s for s in signals if s.get("signal_type") == "SELL"]
    assert len(sell_signals) == (0 if severity is None else 1)
    assert holding.sell_checked_at is not None
    assert holding.current_price is not None


def test_holding_fetch_failure_is_saved(db, monkeypatch):
    from scanner import scan_engine, us_stocks

    _buy(db)

    def fail(_ticker):
        raise RuntimeError("downstream unavailable")

    monkeypatch.setattr(us_stocks, "get_us_ohlcv", fail)
    signals = scan_engine._check_holdings(db)
    holding = db.query(Holding).filter(Holding.ticker == "AAPL").one()
    assert holding.sell_status == "CHECK_FAILED"
    assert signals == []
    assert "확인하지 못했습니다" in holding.sell_reason


def test_holding_patch_validates_stop_and_calculates_r(db):
    from web.app import HoldingRiskUpdate, update_holding_risk

    _buy(db, price=100)
    holding = db.query(Holding).filter(Holding.ticker == "AAPL").one()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(update_holding_risk(
            holding.id,
            HoldingRiskUpdate(entry_price=100, initial_stop_loss=100,
                              current_stop_loss=95),
            db,
        ))
    assert exc.value.status_code == 400

    result = asyncio.run(update_holding_risk(
        holding.id,
        HoldingRiskUpdate(entry_price=100, initial_stop_loss=90,
                          current_stop_loss=92),
        db,
    ))
    assert result["entry_price"] == 100
    assert result["initial_r"] == 10
    assert result["current_stop_loss"] == 92


def test_stop_loss_is_passed_and_holding_signal_reaches_notify(db, monkeypatch):
    from scanner import scan_engine, us_stocks, weinstein

    _buy(db, quantity=12, price=100)
    holding = db.query(Holding).filter(Holding.ticker == "AAPL").one()
    holding.entry_price = 100
    holding.initial_stop_loss = 95
    holding.current_stop_loss = 96
    holding.initial_r = 5
    db.flush()

    daily = _daily()
    daily.loc[daily.index[-1], "Close"] = 94
    monkeypatch.setattr(us_stocks, "get_us_ohlcv", lambda ticker: daily)
    monkeypatch.setattr(weinstein, "to_weekly_ohlcv", lambda frame: frame)
    captured = {}

    def fake_check(*args, **kwargs):
        captured["stop_loss"] = kwargs.get("stop_loss")
        return {
            "ticker": "AAPL", "name": "Apple", "market": "US",
            "signal_type": "SELL", "severity": "HIGH", "price": 94,
            "sell_reason": "손절가 이탈", "profit_pct": -6,
        }

    monkeypatch.setattr(weinstein, "check_sell_signal", fake_check)
    signals = scan_engine._check_holdings(db)
    assert captured["stop_loss"] == 96
    assert signals[0]["unrealized_r"] == -1.2

    messages = []
    scan_engine._notify([], [], signals, messages.append)
    assert len(messages) == 1
    assert "[\ubcf4\uc720]" in messages[0]
    assert "전량 청산 12주" in messages[0]


def test_none_stop_still_allows_other_sell_rules(db, monkeypatch):
    from scanner import scan_engine, us_stocks, weinstein

    _buy(db)
    monkeypatch.setattr(us_stocks, "get_us_ohlcv", lambda ticker: _daily())
    monkeypatch.setattr(weinstein, "to_weekly_ohlcv", lambda frame: frame)
    captured = {}

    def fake_check(*args, **kwargs):
        captured["stop_loss"] = kwargs.get("stop_loss")
        return {
            "ticker": "AAPL", "name": "Apple", "market": "US",
            "signal_type": "SELL", "severity": "MEDIUM", "price": 121.9,
            "sell_reason": "Mansfield RS 하락 전환", "profit_pct": 21.9,
        }

    monkeypatch.setattr(weinstein, "check_sell_signal", fake_check)
    signals = scan_engine._check_holdings(db)
    assert captured["stop_loss"] is None
    assert any(s.get("severity") == "MEDIUM" for s in signals)


def test_holding_alert_transition_suppression_and_reset(db):
    from scanner.scan_engine import _holding_alert_due, _clear_holding_alert_state

    _buy(db)
    holding = db.query(Holding).filter(Holding.ticker == "AAPL").one()
    now = datetime.utcnow()
    holding.last_alert_severity = "MEDIUM"
    holding.last_alert_reason = "same"
    holding.last_alert_at = now

    assert _holding_alert_due(holding, "MEDIUM", "same", now + timedelta(hours=23), 24) is False
    assert _holding_alert_due(holding, "MEDIUM", "same", now + timedelta(hours=24), 24) is True
    assert _holding_alert_due(holding, "HIGH", "same", now + timedelta(hours=1), 24) is True
    assert _holding_alert_due(holding, "MEDIUM", "changed", now + timedelta(hours=1), 24) is True
    assert _holding_alert_due(holding, "LOW", "same", now + timedelta(hours=1), 24) is False
    assert holding.last_alert_severity is None

    holding.last_alert_severity = "HIGH"
    holding.last_alert_reason = "old"
    holding.last_alert_at = now
    _clear_holding_alert_state(holding)
    assert holding.last_alert_severity is None
    assert holding.last_alert_reason is None
    assert holding.last_alert_at is None


def test_check_holdings_suppresses_repeat_then_resets_on_recovery(db, monkeypatch):
    from scanner import scan_engine, us_stocks, weinstein

    _buy(db)
    holding = db.query(Holding).filter(Holding.ticker == "AAPL").one()
    holding.entry_price = 100
    holding.initial_stop_loss = 90
    holding.current_stop_loss = 90
    holding.initial_r = 10
    db.flush()
    monkeypatch.setattr(us_stocks, "get_us_ohlcv", lambda ticker: _daily())
    monkeypatch.setattr(weinstein, "to_weekly_ohlcv", lambda frame: frame)

    state = {"severity": "MEDIUM", "reason": "RS 하락"}

    def fake_check(*args, **kwargs):
        if state["severity"] is None:
            return None
        return {
            "ticker": "AAPL", "name": "Apple", "market": "US",
            "signal_type": "SELL", "severity": state["severity"],
            "sell_reason": state["reason"], "price": 121.9,
            "profit_pct": 21.9,
        }

    monkeypatch.setattr(weinstein, "check_sell_signal", fake_check)
    assert len(scan_engine._check_holdings(db)) == 1
    assert scan_engine._check_holdings(db) == []

    state.update(severity="HIGH", reason="손절가 이탈")
    assert len(scan_engine._check_holdings(db)) == 1

    state.update(severity=None, reason="")
    assert scan_engine._check_holdings(db) == []
    assert holding.last_alert_severity is None
    assert holding.last_alert_reason is None
    assert holding.last_alert_at is None


def test_missing_stops_are_grouped_into_one_notification():
    from scanner.scan_engine import _format_holding_alerts

    message = _format_holding_alerts([{
        "signal_type": "HOLDING_STOP_MISSING",
        "holdings": [
            {"ticker": "AAPL", "name": "Apple", "market": "US"},
            {"ticker": "005930", "name": "삼성전자", "market": "KR"},
        ],
    }])
    assert "손절가 미등록 2건" in message
    assert message.count("손절가 미등록 2건") == 1
    assert "AAPL Apple / 005930 삼성전자" in message


def test_unrealized_r_is_none_without_initial_r(db):
    from web.app import _holding_to_dict

    _buy(db)
    holding = db.query(Holding).filter(Holding.ticker == "AAPL").one()
    holding.entry_price = 100
    holding.current_price = 110
    holding.initial_r = None
    assert _holding_to_dict(holding, db)["unrealized_r"] is None
