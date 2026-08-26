"""수동 매수 기록, 보유 재계산, 매도 상태 저장 테스트."""
import asyncio
from datetime import date, datetime, timedelta

import exchange_calendars as xcals
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

    counts = scan_engine._check_holdings(db, us_bench=None)
    holding = db.query(Holding).filter(Holding.ticker == "AAPL").one()
    assert holding.sell_status == expected
    assert counts[expected] == 1
    assert holding.sell_checked_at is not None
    assert holding.current_price is not None


def test_holding_fetch_failure_is_saved(db, monkeypatch):
    from scanner import scan_engine, us_stocks

    _buy(db)

    def fail(_ticker):
        raise RuntimeError("downstream unavailable")

    monkeypatch.setattr(us_stocks, "get_us_ohlcv", fail)
    counts = scan_engine._check_holdings(db)
    holding = db.query(Holding).filter(Holding.ticker == "AAPL").one()
    assert holding.sell_status == "CHECK_FAILED"
    assert counts["CHECK_FAILED"] == 1
    assert "확인하지 못했습니다" in holding.sell_reason


def test_intraday_position_price_is_separate_from_final_strategy_bar(db, monkeypatch):
    from scanner import kr_stocks, scan_engine, weinstein
    from scanner.time_context import ScanContext

    _buy(db, account_id=1, market="KR", ticker="005930", price=100)
    context = ScanContext.create("KR", "2026-08-25T05:00:00Z")  # 14:00 KST
    sessions = xcals.get_calendar("XKRX").sessions_in_range(
        "2025-08-01", "2026-08-25"
    )
    idx = pd.DatetimeIndex(sessions).tz_localize(None)
    values = [120.0] * len(idx)
    values[-1] = 80.0  # 8/25 장중 관측가; 마지막 확정 세션은 8/24
    raw = pd.DataFrame({
        "Open": values,
        "High": [value + 1 for value in values],
        "Low": [value - 1 for value in values],
        "Close": values,
        "Volume": [1_000_000] * len(values),
    }, index=idx)

    monkeypatch.setattr(
        kr_stocks, "get_kr_ohlcv", lambda ticker, **kwargs: raw
    )
    captured = {}

    def fake_check(*args, **kwargs):
        captured["strategy_last"] = float(args[0]["Close"].iloc[-1])
        captured["current_price"] = kwargs.get("current_price")
        return {"severity": "HIGH", "sell_reason": "장중 손절 테스트"}

    monkeypatch.setattr(weinstein, "check_sell_signal", fake_check)
    counts = scan_engine._check_holdings(
        db, scan_contexts={"KR": context}
    )

    holding = db.query(Holding).filter(Holding.ticker == "005930").one()
    assert captured["strategy_last"] == 120.0
    assert captured["current_price"] == 80.0
    assert holding.current_price == 80.0
    assert holding.sell_status == "SELL_REQUIRED"
    assert counts["SELL_REQUIRED"] == 1


def test_missing_intraday_quote_does_not_overwrite_price_as_current(db, monkeypatch):
    from scanner import kr_stocks, scan_engine
    from scanner.time_context import ScanContext

    _buy(db, account_id=1, market="KR", ticker="005930", price=100)
    context = ScanContext.create("KR", "2026-08-25T05:00:00Z")  # 장중
    sessions = xcals.get_calendar("XKRX").sessions_in_range(
        "2025-08-01", "2026-08-24"
    )
    idx = pd.DatetimeIndex(sessions).tz_localize(None)
    raw = pd.DataFrame({
        "Open": [120.0] * len(idx),
        "High": [121.0] * len(idx),
        "Low": [119.0] * len(idx),
        "Close": [120.0] * len(idx),
        "Volume": [1_000_000] * len(idx),
    }, index=idx)
    monkeypatch.setattr(
        kr_stocks, "get_kr_ohlcv", lambda ticker, **kwargs: raw
    )
    before = db.query(Holding).filter(Holding.ticker == "005930").one()
    previous_price = before.current_price
    previous_updated_at = before.price_updated_at

    counts = scan_engine._check_holdings(
        db, scan_contexts={"KR": context}
    )

    holding = db.query(Holding).filter(Holding.ticker == "005930").one()
    assert holding.current_price == previous_price
    assert holding.price_updated_at == previous_updated_at
    assert holding.sell_status == "CHECK_FAILED"
    assert counts["CHECK_FAILED"] == 1
