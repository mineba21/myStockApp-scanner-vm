from decimal import Decimal

import pytest

from tests.auto_stop_fakes import MON_10
from trading.auto_buy_us.settings import RISK, SECTOR_CAP_PCT, SLEEVE_PCT, STRATEGIES, Settings

ENVS = ("AUTO_BUY_US_RT_MODE", "AUTO_BUY_US_RT_JOURNAL_PATH", "AUTO_BUY_US_RT_KILL",
        "AUTO_BUY_US_RT_EXTRA_CLOSED_DATES", "AUTO_BUY_US_RT_EXTRA_EARLY_CLOSE_DATES",
        "SCANNER_ORDER_JOURNAL_PATH")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ENVS:
        monkeypatch.delenv(k, raising=False)


def test_default_mode_off():
    s = Settings.from_env()
    assert s.mode == "OFF" and s.mode_block is None and s.kill is False


def test_live_rejected(monkeypatch):
    monkeypatch.setenv("AUTO_BUY_US_RT_MODE", "LIVE")
    s = Settings.from_env()
    assert s.mode == "OFF" and "LIVE" in s.mode_block


def test_unknown_mode_rejected(monkeypatch):
    monkeypatch.setenv("AUTO_BUY_US_RT_MODE", "shadoww")
    s = Settings.from_env()
    assert s.mode == "OFF" and isinstance(s.mode_block, str)


def test_shadow_mode(monkeypatch):
    monkeypatch.setenv("AUTO_BUY_US_RT_MODE", "shadow")
    s = Settings.from_env()
    assert s.mode == "SHADOW" and s.mode_block is None


def test_journal_must_differ_from_s01(monkeypatch, tmp_path):
    p = str(tmp_path / "j.sqlite")
    monkeypatch.setenv("AUTO_BUY_US_RT_MODE", "SHADOW")
    monkeypatch.setenv("AUTO_BUY_US_RT_JOURNAL_PATH", p)
    monkeypatch.setenv("SCANNER_ORDER_JOURNAL_PATH", str(tmp_path / "." / "j.sqlite"))
    s = Settings.from_env()
    assert s.mode == "OFF" and "S01" in s.mode_block
    monkeypatch.setenv("SCANNER_ORDER_JOURNAL_PATH", str(tmp_path / "other.sqlite"))
    assert Settings.from_env().mode == "SHADOW"


def test_extra_dates_parsed(monkeypatch):
    monkeypatch.setenv("AUTO_BUY_US_RT_MODE", "SHADOW")
    monkeypatch.setenv("AUTO_BUY_US_RT_EXTRA_CLOSED_DATES", "2026-12-24,2026-12-26")
    s = Settings.from_env()
    assert s.extra_closed_dates == frozenset({"2026-12-24", "2026-12-26"}) and s.mode == "SHADOW"
    monkeypatch.setenv("AUTO_BUY_US_RT_EXTRA_EARLY_CLOSE_DATES", "2026-13-01")
    s = Settings.from_env()
    assert s.mode == "OFF" and "2026-13-01" in s.mode_block


def test_constants_match_spec():
    assert STRATEGIES["B00N"]["topn"] == 2 and STRATEGIES["B00"]["topn"] is None
    assert RISK["RISK_PCT"] == 0.75 and SECTOR_CAP_PCT == 35.0 and SLEEVE_PCT == Decimal("0.30")


def test_settings_compatible_with_s01_clock():
    from trading.auto_stop.us_calendar import market_clock
    assert market_clock(MON_10, Settings.from_env()).is_open
