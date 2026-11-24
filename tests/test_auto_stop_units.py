"""시세 검증·지정가·설정·브로커 파싱·스케줄러 등록."""
from dataclasses import replace
from decimal import Decimal

import pytest

from tests.auto_stop_fakes import MON_10, ts
from trading.auto_stop import settings as S
from trading.auto_stop.brokers import BalanceUnavailable, LiveStopSellBroker, ShadowBroker, _parse_orderbook
from trading.auto_stop.quotes import Quote, QuoteUnavailable, is_jump, limit_price, reference_price, validate
from trading.auto_stop.us_calendar import market_clock


def q(price="50", as_of=MON_10 - 30, bid=None):
    return Quote("T", "ND", Decimal(price), as_of, "X", None if bid is None else Decimal(bid))


def test_limit_price_floors_to_cent():
    assert limit_price(Decimal("44.00"), 50) == Decimal("43.78")
    assert limit_price(Decimal("10.01"), 50) == Decimal("9.95")
    assert limit_price(Decimal("123.45"), 0) == Decimal("123.45")


def test_reference_price_uses_bid_only_when_lower_and_set():
    assert reference_price(q()) == (Decimal("50"), "CUR")
    assert reference_price(q(bid="49.90")) == (Decimal("49.90"), "BID")
    assert reference_price(q(bid="50.10")) == (Decimal("50"), "CUR")


def test_validate_quote_rules():
    st = S.Settings()
    mc = market_clock(MON_10, st)
    assert validate(q(), MON_10, mc, st) is None
    assert "지났" in validate(q(as_of=MON_10 - 200), MON_10, mc, st)
    assert validate(q(as_of=None), MON_10, mc, st)
    assert validate(q(price="0"), MON_10, mc, st)
    assert "당일" in validate(q(as_of=MON_10 - 86400), MON_10, mc, st)
    assert "개장 이전" in validate(q(as_of=ts(2026, 11, 23, 9, 29)), ts(2026, 11, 23, 9, 30, 30),
                                  market_clock(ts(2026, 11, 23, 9, 30, 30), st), st)


def test_jump_threshold():
    st = S.Settings()
    assert not is_jump(None, Decimal("10"), st)
    assert is_jump(Decimal("40"), Decimal("30"), st) and not is_jump(Decimal("40"), Decimal("31"), st)


def test_settings_defaults_off_and_clamped(monkeypatch):
    for k in ("AUTO_STOP_US_MODE", "AUTO_STOP_US_REPLACE_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    st = S.Settings.from_env()
    assert st.mode == "OFF" and st.replace_enabled is False
    assert (st.max_replaces_per_session, st.max_replaces_per_event, st.max_carry_over_sessions) == (3, 6, 1)
    assert (st.session_start_offset_min, st.session_cutoff_min, st.slip_bps, st.lease_sec) == (0, 5, 50, 90)
    monkeypatch.setenv("AUTO_STOP_US_MODE", "yolo")
    monkeypatch.setenv("AUTO_STOP_US_SLIP_BPS", "99999")
    monkeypatch.setenv("AUTO_STOP_US_REPLACE_ENABLED", "true")
    st = S.Settings.from_env()
    assert st.mode == "OFF" and st.slip_bps == 500 and st.replace_enabled


def test_live_gate_requires_every_switch_and_read_contracts(monkeypatch, tmp_path):
    env = {"AUTO_STOP_US_MODE": "LIVE", "KIWOOM_WEB_ENABLED": "true", "KIWOOM_TRADING_ENABLED": "true",
           "ACCOUNT1_ORDER_ENABLED": "true", "SCANNER_ORDER_JOURNAL_PATH": str(tmp_path / "j"),
           "AUTO_STOP_US_VERIFIED_CONTRACTS": "USA20101_QUOTE_TIME,UST21070_SELLABLE,UST21150_STATUS"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert S.live_order_block_reason() is None
    assert "REPLACE" in S.live_cancel_block_reason()
    monkeypatch.setenv("AUTO_STOP_US_REPLACE_ENABLED", "true")
    assert "UST20003_PARTIAL_CANCEL" in S.live_cancel_block_reason()
    for k in env:
        monkeypatch.setenv(k, "" if k in ("SCANNER_ORDER_JOURNAL_PATH", "AUTO_STOP_US_VERIFIED_CONTRACTS") else "false")
        assert S.live_order_block_reason() is not None, k
        monkeypatch.setenv(k, env[k])
    monkeypatch.setenv("AUTO_STOP_US_VERIFIED_CONTRACTS", "USA20101_QUOTE_TIME,UST21070_SELLABLE,BOGUS")
    assert "UST21150_STATUS" in S.live_order_block_reason()
    st = S.Settings.from_env()
    assert "BOGUS" not in st.verified_contracts and st.live_qty_cap() == 1


def test_live_broker_requires_real_profile():
    from types import SimpleNamespace
    with pytest.raises(Exception):
        LiveStopSellBroker(SimpleNamespace(mode="mock", base_url="https://mockapi.kiwoom.com"), token="t",
                           client=object(), evidence=object())


def test_parse_orderbook_and_bid_field(monkeypatch):
    raw = {"stk_cd": "T", "stex_tp": "ND", "cur_prc": "+50.123", "dt": "20261123", "bid_tm": "09:59"}
    quote = _parse_orderbook(raw, "T", "ND")
    assert quote.price == Decimal("50.12") and quote.bid is None and quote.as_of == ts(2026, 11, 23, 9, 59)
    with pytest.raises(QuoteUnavailable):
        _parse_orderbook({**raw, "stex_tp": "NY"}, "T", "ND")
    assert _parse_orderbook({k: v for k, v in raw.items() if k != "bid_tm"}, "T", "ND").as_of is None


class _Client:
    def __init__(self, balance):
        self.balance = balance

    def get_overseas_account_balance(self, token):
        return self.balance


def _broker(balance):
    b = LiveStopSellBroker.__new__(LiveStopSellBroker)
    b.client, b.token, b.orders = _Client(balance), "t", None
    return b


def test_holdings_require_complete_and_unique_rows():
    row = {"stk_cd": "T", "poss_qty": "10", "sell_alowq": "8", "frgn_stk_book_uv": "60.5"}
    assert _broker({"complete": True, "holdings": [row]}).holdings() == {
        "T": {"qty": 10, "sellable": 8, "avg_cost": Decimal("60.5")}}
    with pytest.raises(BalanceUnavailable):
        _broker({"complete": False, "holdings": [row]}).holdings()
    with pytest.raises(BalanceUnavailable):
        _broker({"complete": True, "holdings": [row, row]}).holdings()


def test_read_only_live_broker_cannot_send():
    b = _broker({})
    with pytest.raises(Exception):
        b.sell("T", "ND", 1, Decimal("1"))
    with pytest.raises(Exception):
        b.cancel("T", "ND", "1")


class _R:
    def __init__(self, price="44", as_of=MON_10 - 30):
        self.price, self.as_of = Decimal(price), as_of

    def quote(self, t, e):
        return q(str(self.price), as_of=self.as_of)


def _order(**over):
    o = {"order_number": "V1", "ticker": "T", "exchange": "ND", "requested_qty": 5, "filled_qty": 0,
         "limit_price": "43.78", "state": "SUBMITTED", "quote_as_of": MON_10 - 30, "sent_at": MON_10,
         "attempt_no": 0, "cancel_sent_at": None}
    o.update(over)
    return o


def test_shadow_immediate_fills_on_same_quote():
    assert ShadowBroker(_R(), "IMMEDIATE").status(_order())["state"] == "FILLED"


def test_shadow_next_quote_requires_newer_broker_time():
    r = _R()
    sb = ShadowBroker(r, "NEXT_QUOTE")
    assert sb.status(_order())["state"] == "OPEN"            # 주문 근거와 같은 시세
    r.as_of = MON_10 + 30
    row = sb.status(_order())
    assert row["state"] == "FILLED" and row["virtual_fill_price"] == "43.78"
    r.price = Decimal("43")
    assert sb.status(_order())["state"] == "OPEN"            # 새 시세라도 지정가 미만
    assert sb.status(_order(state="CANCEL_PENDING"))["state"] == "CANCELLED"
    assert sb.day_orders("T") == []


def test_shadow_stress_partial_then_delayed_cancel():
    clock = [MON_10 + 10]
    r = _R(as_of=MON_10 + 120)
    sb = ShadowBroker(r, "STRESS", clock=lambda: clock[0])
    assert sb.status(_order())["state"] == "OPEN"
    clock[0] = MON_10 + 61
    row = sb.status(_order())
    assert row["state"] == "PARTIAL" and row["filled_quantity"] == 2
    pend = _order(state="CANCEL_PENDING", filled_qty=2, cancel_sent_at=MON_10 + 400)
    clock[0] = MON_10 + 430
    assert sb.status(pend)["state"] == "PARTIAL"              # 취소 지연
    clock[0] = MON_10 + 491
    assert sb.status(pend)["state"] == "CANCELLED"
    assert sb.status(_order(attempt_no=1))["state"] == "FILLED"   # 재주문은 NEXT_QUOTE 규칙


def test_shadow_rejects_unknown_model():
    with pytest.raises(ValueError):
        ShadowBroker(_R(), "LIVE")


def test_scheduler_registers_single_interval_job(monkeypatch):
    import scheduler

    class Fake:
        jobs = []

        def add_job(self, func, trigger, **kw):
            self.jobs.append((func, trigger, kw))
    fake = Fake()
    scheduler._add_auto_stop_job(fake)
    func, trigger, kw = fake.jobs[0]
    assert kw["id"] == "auto_stop_us_tick" and kw["max_instances"] == 1 and kw["coalesce"] is True
    assert trigger.interval.total_seconds() == 30


def test_runner_without_journal_does_nothing(monkeypatch):
    from trading.auto_stop import runner
    monkeypatch.setenv("SCANNER_ORDER_JOURNAL_PATH", "")
    assert runner.auto_stop_us_tick() == {"status": "NO_JOURNAL"}


def test_runner_read_blocked_when_web_disabled(monkeypatch):
    from trading.auto_stop import runner
    monkeypatch.setenv("KIWOOM_WEB_ENABLED", "false")
    with pytest.raises(runner.ReadAccessBlocked):
        runner.broker_factory("SHADOW")


def test_readonly_probe_never_imports_order_client():
    import ast
    import pathlib
    tree = ast.parse(pathlib.Path("scripts/auto_stop_readonly_probe.py").read_text())
    names = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
            {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert "trading.kiwoom_orders" not in names and not any("order" in (m or "") and "kiwoom_orders" in (m or "")
                                                           for m in names)


def test_journal_backup_copies_and_refuses_same_folder(tmp_path):
    from scripts.auto_stop_journal_backup import backup
    from trading.auto_stop.store import AutoStopStore
    src = tmp_path / "j.sqlite"
    AutoStopStore(src).runtime_set("x", 1, 0)
    out = backup(str(src), str(tmp_path / "bk"))
    assert AutoStopStore(out).runtime_get("x") == 1
    with pytest.raises(SystemExit):
        backup(str(src), str(tmp_path))
