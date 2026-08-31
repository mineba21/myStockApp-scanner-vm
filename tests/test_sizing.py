"""Step 5 — R-multiple 포지션 사이징, heat, 자산 스냅샷 테스트."""

import asyncio
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import AccountEquity, Base, Holding, ScanResult
from scanner.sizing import calculate_open_risk, calculate_position


BASE_CFG = {
    "RISK_PCT": 1.0,
    "MAX_POSITION_PCT": 20.0,
    "MAX_TOTAL_HEAT_PCT": 6.0,
    "MIN_R_PCT": 3.0,
    "MAX_R_PCT": 15.0,
}


def _position(**overrides):
    args = dict(
        entry=100.0, stop=90.0, equity=100_000.0, cash=100_000.0,
        open_risk_sum=0.0, market="US", cfg=BASE_CFG,
    )
    args.update(overrides)
    return calculate_position(**args)


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


class TestCalculatePosition:
    def test_market_specific_config_override_is_used(self, monkeypatch):
        import config
        from scanner.scan_engine import _sizing_config

        monkeypatch.setattr(config, "US_RISK_PCT", 0.5, raising=False)
        monkeypatch.setattr(config, "KR_RISK_PCT", 1.5, raising=False)
        assert _sizing_config("US")["RISK_PCT"] == 0.5
        assert _sizing_config("KR")["RISK_PCT"] == 1.5

    def test_s1_risk_budget_is_default_constraint(self):
        result = _position()
        assert result["qty"] == 100
        assert result["r_per_share"] == 10.0
        assert result["risk_amount"] == 1_000.0
        assert result["position_pct"] == 10.0
        assert result["risk_pct_actual"] == 1.0
        assert result["constrained_by"] == "risk"
        assert result["currency"] == "USD"

    def test_s2_position_cap_reduces_tight_stop(self):
        cfg = {**BASE_CFG, "MIN_R_PCT": 1.0}
        result = _position(stop=98.0, cfg=cfg)
        assert result["calculated_qty"] == 500
        assert result["qty"] == 200
        assert result["constrained_by"] == "position_cap"

    def test_s3_heat_reduces_remaining_risk_budget(self):
        result = _position(open_risk_sum=5_500.0)
        assert result["qty"] == 50
        assert result["risk_amount"] == 500.0
        assert result["constrained_by"] == "heat"

    @pytest.mark.parametrize(
        ("stop", "reason"),
        [(98.0, "r_pct_below_min"), (84.0, "r_pct_above_max")],
    )
    def test_s4_r_range_rejects_outside_inclusive_bounds(self, stop, reason):
        result = _position(stop=stop)
        assert result["qty"] == 0
        assert result["reasons"] == [reason]

    @pytest.mark.parametrize("stop", [97.0, 85.0])
    def test_s4_boundary_values_are_allowed(self, stop):
        assert _position(stop=stop)["qty"] > 0

    def test_cash_is_applied_after_risk_position_and_heat(self):
        result = _position(cash=550.0)
        assert result["qty"] == 5
        assert result["constrained_by"] == "cash"

    def test_two_caps_follow_priority_and_later_tighter_cap_wins(self):
        cfg = {**BASE_CFG, "MIN_R_PCT": 1.0}
        heat_limited = _position(stop=98.0, open_risk_sum=5_900.0, cfg=cfg)
        assert heat_limited["qty"] == 50
        assert heat_limited["constrained_by"] == "heat"

        cash_limited = _position(
            stop=98.0, open_risk_sum=5_900.0, cash=2_000.0, cfg=cfg)
        assert cash_limited["qty"] == 20
        assert cash_limited["constrained_by"] == "cash"

    def test_kr_quantity_is_always_integer_floor(self):
        result = _position(
            entry=257_000.0, stop=231_300.0, equity=100_000_000.0,
            cash=100_000_000.0, market="KR",
        )
        assert result["qty"] == 38
        assert isinstance(result["qty"], int)
        assert result["currency"] == "KRW"


class TestOpenRisk:
    def test_missing_stop_excluded_and_breached_stop_is_zero(self):
        holdings = [
            {"market": "US", "quantity": 10, "current_price": 120,
             "current_stop_loss": 100},
            {"market": "US", "quantity": 5, "current_price": 80,
             "current_stop_loss": None},
            {"market": "US", "quantity": 3, "current_price": 90,
             "current_stop_loss": 95},
            {"market": "KR", "quantity": 100, "current_price": 1000,
             "current_stop_loss": 900},
        ]
        result = calculate_open_risk(holdings, "US")
        assert result["open_risk_sum"] == 200.0
        assert result["missing_stop_count"] == 1
        assert result["breached_stop_count"] == 1
        assert "손절가 미등록 1건" in result["warnings"]
        assert "손절선 이탈 보유 1건" in result["warnings"]


class TestFormattingAndNotification:
    def test_currency_format_is_market_native(self):
        from scanner.scan_engine import _format_currency

        assert _format_currency(1234.5, "USD") == "$1,234.50"
        assert _format_currency(1234567.4, "KRW") == "1,234,567원"

    def test_missing_equity_keeps_signal_notification(self, monkeypatch):
        from scanner.scan_engine import _notify

        monkeypatch.setattr("scanner.scan_engine._sector_summary", lambda market: "")
        messages = []
        signal = {
            "market": "US", "ticker": "SLB", "name": "SLB",
            "signal_type": "BREAKOUT", "price": 34.2, "strict_price": 34.2,
            "stop_loss": 30.72, "pivot_price": 33.5, "cur_ext_pct": 2.1,
            "cur_stop_pct": 10.2, "signal_date": "2026-08-28",
            "volume_ratio": 1.82, "entry_warnings": [], "_grade": "A",
            "_sizing_status": "equity_missing", "sizing": None,
        }
        _notify([signal], [], messages.append)
        assert len(messages) == 1
        assert "SLB" in messages[0]
        assert "자산 미등록" in messages[0]


class TestEquityApiAndPersistence:
    def test_equity_is_append_only_and_currency_is_automatic(self, db):
        from web.app import EquityCreate, create_equity, get_equity

        first = asyncio.run(create_equity(
            EquityCreate(market="us", total_equity=100_000, cash_balance=50_000,
                         note="first"), db))
        second = asyncio.run(create_equity(
            EquityCreate(market="US", total_equity=110_000, cash_balance=40_000,
                         note="second"), db))

        assert db.query(AccountEquity).count() == 2
        assert first["currency"] == second["currency"] == "USD"
        latest = asyncio.run(get_equity(db))
        assert latest["US"]["id"] == second["id"]
        assert latest["US"]["total_equity"] == 110_000
        assert latest["KR"] is None

    def test_currency_input_is_rejected(self):
        from web.app import EquityCreate

        with pytest.raises(ValidationError):
            EquityCreate(market="US", total_equity=100_000,
                         cash_balance=50_000, currency="KRW")

    def test_stale_after_more_than_30_days(self, db):
        from web.app import _equity_to_dict

        row = AccountEquity(
            market="KR", currency="KRW", total_equity=100_000_000,
            cash_balance=50_000_000,
            recorded_at=datetime.utcnow() - timedelta(days=31),
        )
        db.add(row)
        db.commit()
        assert _equity_to_dict(row)["is_stale"] is True

    def test_equity_snapshot_matches_latest_calculation_row(self, db, monkeypatch):
        from scanner.scan_engine import _attach_position_sizing, _save

        db.add(AccountEquity(
            market="US", currency="USD", total_equity=100_000,
            cash_balance=100_000,
        ))
        db.commit()
        monkeypatch.setattr(
            "scanner.scan_engine._sizing_config", lambda market: BASE_CFG)
        signal = {
            "market": "US", "ticker": "SLB", "name": "SLB",
            "signal_type": "BREAKOUT", "price": 34.2, "strict_price": 34.2,
            "stop_loss": 30.72, "signal_date": "2026-08-28",
            "ma150": 30.0, "volume_ratio": 1.82,
        }
        _attach_position_sizing(db, [signal])
        _save(db, signal)

        row = db.query(ScanResult).filter(ScanResult.ticker == "SLB").one()
        assert signal["equity_snapshot"] == 100_000
        assert row.equity_snapshot == 100_000
        assert row.suggested_qty == signal["sizing"]["qty"]

    def test_missing_equity_persists_null_sizing_without_dropping_signal(self, db):
        from scanner.scan_engine import _attach_position_sizing, _save

        signal = {
            "market": "US", "ticker": "NONE", "name": "No Equity",
            "signal_type": "BREAKOUT", "price": 100.0, "strict_price": 100.0,
            "stop_loss": 90.0, "signal_date": "2026-08-28",
            "ma150": 80.0, "volume_ratio": 2.0,
        }
        _attach_position_sizing(db, [signal])
        _save(db, signal)

        row = db.query(ScanResult).filter(ScanResult.ticker == "NONE").one()
        assert signal["suggested_qty"] is None
        assert row.suggested_qty is None
        assert row.equity_snapshot is None
