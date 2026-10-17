import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from risk_guard import (LIVE_ENV_VAR, AccountSnapshot, BrokerResult, GuardConfig, OrderIntent,
                        RiskGuard, Verdict)

UTC = timezone.utc
# 2026-10-14(수) 15:00 UTC = 뉴욕 11:00 (정규장)
OPEN = datetime(2026, 10, 14, 15, 0, tzinfo=UTC)
SATURDAY = datetime(2026, 10, 17, 15, 0, tzinfo=UTC)


def cfg(tmp_path, **kw) -> GuardConfig:
    base = dict(
        kill_switch_path=str(tmp_path / "KILL"),
        ledger_path=str(tmp_path / "ledger.json"),
        audit_log_path=str(tmp_path / "audit.jsonl"),
    )
    base.update(kw)
    return GuardConfig(**base)


def guard(tmp_path, broker=None, **kw) -> RiskGuard:
    return RiskGuard(cfg(tmp_path, **kw), broker=broker, clock=lambda: OPEN)


def acct(**kw) -> AccountSnapshot:
    base = dict(equity=20000.0, cash=10000.0, positions={})
    base.update(kw)
    return AccountSnapshot(**base)


def buy(symbol="AAPL", qty=5, limit=100.0, ref=100.0, stop=92.0, **kw) -> OrderIntent:
    return OrderIntent(symbol=symbol, side="BUY", qty=qty, order_type="LIMIT",
                       reference_price=ref, limit_price=limit, stop_price=stop, **kw)


def sell(symbol="AAPL", qty=5, limit=100.0, ref=100.0) -> OrderIntent:
    return OrderIntent(symbol=symbol, side="SELL", qty=qty, order_type="LIMIT",
                       reference_price=ref, limit_price=limit)


def failed(decision) -> set[str]:
    return {c.name for c in decision.failed_checks}


# 기본 설정: capital_cap 5000, 1R = 50달러, 진입 100 / 손절 92 → R 기준 최대 6주

def test_approve_within_limits(tmp_path):
    d = guard(tmp_path).evaluate(buy(qty=5), acct())
    assert d.verdict is Verdict.APPROVE and d.approved_qty == 5


def test_resize_to_risk_budget(tmp_path):
    d = guard(tmp_path).evaluate(buy(qty=50), acct())
    assert d.verdict is Verdict.RESIZE and d.approved_qty == 6
    assert "50→6주" in d.summary()


def test_reject_when_resize_not_allowed(tmp_path):
    d = guard(tmp_path, allow_resize=False).evaluate(buy(qty=50), acct())
    assert d.verdict is Verdict.REJECT and "resize" in failed(d)


def test_capital_cap_limits_sizing_even_with_big_account(tmp_path):
    d = guard(tmp_path).evaluate(buy(qty=100), acct(equity=1_000_000, cash=1_000_000))
    assert d.approved_qty == 6  # 계좌가 커도 에이전트 자금 5000 기준


def test_position_notional_cap_counts_existing_holding(tmp_path):
    # 종목 한도 1000달러 중 900달러어치 보유 → 1주만 추가 가능
    d = guard(tmp_path).evaluate(buy(qty=5), acct(positions={"AAPL": 9}))
    assert d.verdict is Verdict.RESIZE and d.approved_qty == 1


def test_kill_switch_blocks(tmp_path):
    g = guard(tmp_path)
    g.kill_switch.activate("테스트")
    d = g.evaluate(buy(), acct())
    assert d.verdict is Verdict.REJECT and "kill_switch" in failed(d)


@pytest.mark.parametrize("stop,expected", [(None, "stop"), (101.0, "stop"), (80.0, "stop")])
def test_stop_rules(tmp_path, stop, expected):
    d = guard(tmp_path).evaluate(buy(stop=stop), acct())
    assert d.verdict is Verdict.REJECT and expected in failed(d)


def test_market_closed_weekend_and_holiday(tmp_path):
    g = guard(tmp_path)
    assert "market_hours" in failed(g.evaluate(buy(), acct(), now=SATURDAY))
    g2 = guard(tmp_path, market_holidays=(OPEN.date(),))
    assert "market_hours" in failed(g2.evaluate(buy(), acct()))


def test_market_not_enabled(tmp_path):
    d = guard(tmp_path).evaluate(buy(symbol="005930", market="KR"), acct())
    assert "market_enabled" in failed(d)


def test_market_order_rejected_by_default(tmp_path):
    intent = OrderIntent(symbol="AAPL", side="BUY", qty=1, order_type="MARKET",
                         reference_price=100.0, stop_price=92.0)
    assert "order_type" in failed(guard(tmp_path).evaluate(intent, acct()))


def test_fat_finger_price(tmp_path):
    d = guard(tmp_path).evaluate(buy(limit=110.0, stop=101.0), acct())
    assert "price_deviation" in failed(d)


def test_blocklist_and_allowlist(tmp_path):
    assert "symbol_lists" in failed(
        guard(tmp_path, symbol_blocklist=("AAPL",)).evaluate(buy(), acct()))
    assert "symbol_lists" in failed(
        guard(tmp_path, symbol_allowlist=("SPY",)).evaluate(buy(), acct()))


def test_bad_intent_fails_closed(tmp_path):
    d = guard(tmp_path).evaluate(buy(qty=0, ref=0.0), acct())
    assert d.verdict is Verdict.REJECT and "intent_sanity" in failed(d)


def test_string_side_is_normalized_and_invalid_side_raises():
    assert buy().side.value == "BUY"
    with pytest.raises(ValueError):
        OrderIntent(symbol="AAPL", side="HOLD", qty=1, order_type="LIMIT",
                    reference_price=1.0, limit_price=1.0)


def test_open_positions_limit(tmp_path):
    positions = {s: 1 for s in ["A", "B", "C", "D", "E"]}
    d = guard(tmp_path).evaluate(buy(), acct(positions=positions))
    assert "open_positions" in failed(d)


def test_sell_more_than_held_rejected_and_sell_allowed(tmp_path):
    g = guard(tmp_path)
    assert "sell_holdings" in failed(g.evaluate(sell(qty=10), acct(positions={"AAPL": 3})))
    d = g.evaluate(sell(qty=3), acct(positions={"AAPL": 3}))
    assert d.verdict is Verdict.APPROVE


def test_execute_requires_human_approval(tmp_path):
    g = guard(tmp_path)
    d = g.evaluate(buy(), acct())
    assert g.execute(d, acct()).status == "REFUSED"
    assert g.execute(d, acct(), approved_by="dean").status == "DRY_RUN"


def test_execute_cannot_run_twice(tmp_path):
    g = guard(tmp_path, require_human_approval=False)
    d = g.evaluate(buy(), acct())
    assert g.execute(d, acct()).placed
    second = g.execute(d, acct())
    assert second.status == "REFUSED" and "이미 실행" in second.message


def test_duplicate_order_blocked_within_window(tmp_path):
    g = guard(tmp_path, require_human_approval=False)
    assert g.execute(g.evaluate(buy(qty=1), acct()), acct()).placed
    later = OPEN + timedelta(minutes=10)
    assert "duplicate" in failed(g.evaluate(buy(qty=1), acct(), now=later))
    much_later = OPEN + timedelta(minutes=61)
    assert "duplicate" not in failed(g.evaluate(buy(qty=1), acct(), now=much_later))


def test_daily_order_and_new_position_limits(tmp_path):
    g = guard(tmp_path, require_human_approval=False, max_new_positions_per_day=2,
              duplicate_window_minutes=0)
    for i, sym in enumerate(["AAA", "BBB"]):
        t = OPEN + timedelta(minutes=i)
        assert g.execute(g.evaluate(buy(symbol=sym, qty=1), acct(), now=t), acct(), now=t).placed
    d = g.evaluate(buy(symbol="CCC", qty=1), acct(), now=OPEN + timedelta(minutes=5))
    assert "new_positions_today" in failed(d)

    g2 = guard(tmp_path / "b", require_human_approval=False, max_orders_per_day=1,
               duplicate_window_minutes=0)
    assert g2.execute(g2.evaluate(buy(qty=1), acct()), acct()).placed
    assert "daily_orders" in failed(g2.evaluate(buy(symbol="MSFT", qty=1), acct()))


def test_daily_buy_notional_cap(tmp_path):
    g = guard(tmp_path, require_human_approval=False, max_buy_notional_per_day=250,
              duplicate_window_minutes=0)
    assert g.execute(g.evaluate(buy(qty=2), acct()), acct()).placed  # 200달러 사용
    d = g.evaluate(buy(symbol="MSFT", qty=3), acct())
    # 남은 50달러로는 100달러 주식을 1주도 못 삼
    assert d.verdict is Verdict.REJECT and "position_sizing" in failed(d)


def test_ledger_persists_and_resets_next_day(tmp_path):
    g = guard(tmp_path, require_human_approval=False, max_orders_per_day=1)
    assert g.execute(g.evaluate(buy(qty=1), acct()), acct()).placed
    reloaded = guard(tmp_path, max_orders_per_day=1)  # 새 프로세스라고 가정
    assert "daily_orders" in failed(reloaded.evaluate(buy(symbol="MSFT", qty=1), acct()))
    tomorrow = OPEN + timedelta(days=1)
    assert "daily_orders" not in failed(reloaded.evaluate(buy(symbol="MSFT", qty=1), acct(), now=tomorrow))


def test_decision_ttl(tmp_path):
    g = guard(tmp_path)
    d = g.evaluate(buy(), acct())
    res = g.execute(d, acct(), approved_by="dean", now=OPEN + timedelta(minutes=16))
    assert res.status == "REFUSED" and "유효시간" in res.message


def test_recheck_catches_kill_switch_after_evaluation(tmp_path):
    g = guard(tmp_path)
    d = g.evaluate(buy(), acct())
    g.kill_switch.activate("장중 급락")
    res = g.execute(d, acct(), approved_by="dean")
    assert res.status == "REFUSED" and "kill_switch" in res.message


class SpyBroker:
    name = "spy"

    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def place_order(self, intent, qty, client_order_id):
        self.calls.append((intent.symbol, qty, client_order_id))
        if self.fail:
            raise ConnectionError("timeout")
        return BrokerResult(True, "B-1", "accepted")


def test_live_needs_both_config_and_env(tmp_path, monkeypatch):
    spy = SpyBroker()
    g = guard(tmp_path, broker=spy, live_trading=True, require_human_approval=False)
    monkeypatch.delenv(LIVE_ENV_VAR, raising=False)
    assert g.execute(g.evaluate(buy(qty=1), acct()), acct()).status == "DRY_RUN"
    assert spy.calls == []

    monkeypatch.setenv(LIVE_ENV_VAR, "1")
    later = OPEN + timedelta(minutes=61)
    res = g.execute(g.evaluate(buy(qty=1), acct(), now=later), acct(), now=later)
    assert res.status == "SUBMITTED" and len(spy.calls) == 1

    g_off = guard(tmp_path / "off", broker=spy, live_trading=False, require_human_approval=False)
    assert g_off.execute(g_off.evaluate(buy(qty=1), acct()), acct()).status == "DRY_RUN"


def test_broker_error_is_recorded_to_block_resend(tmp_path, monkeypatch):
    monkeypatch.setenv(LIVE_ENV_VAR, "1")
    g = guard(tmp_path, broker=SpyBroker(fail=True), live_trading=True,
              require_human_approval=False)
    d = g.evaluate(buy(qty=1), acct())
    assert g.execute(d, acct()).status == "ERROR"
    assert "duplicate" in failed(g.evaluate(buy(qty=1), acct()))


def test_audit_log_written(tmp_path):
    g = guard(tmp_path)
    d = g.evaluate(buy(), acct())
    g.execute(d, acct(), approved_by="dean")
    events = [json.loads(line)["event"] for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert events == ["evaluate", "recheck", "execute"]


def test_config_from_toml_and_validation(tmp_path):
    p = tmp_path / "g.toml"
    p.write_text('[guard]\ncapital_cap = 3000\nsymbol_blocklist = ["soxl"]\n'
                 'market_holidays = ["2026-12-25"]\n', encoding="utf-8")
    c = GuardConfig.from_toml(p)
    assert c.capital_cap == 3000 and c.symbol_blocklist == ("SOXL",)
    assert str(c.market_holidays[0]) == "2026-12-25"

    p.write_text("[guard]\ncapital_capp = 3000\n", encoding="utf-8")
    with pytest.raises(ValueError, match="알 수 없는"):
        GuardConfig.from_toml(p)
    with pytest.raises(ValueError):
        GuardConfig(risk_per_trade_pct=5)


def test_example_config_loads():
    from pathlib import Path
    GuardConfig.from_toml(Path(__file__).parent.parent / "risk_guard" / "guard.example.toml")


def test_naive_datetime_rejected(tmp_path):
    with pytest.raises(ValueError):
        guard(tmp_path).evaluate(buy(), acct(), now=datetime(2026, 10, 14, 11, 0))


def test_preview_matches_evaluate_without_writing_files(tmp_path):
    g = guard(tmp_path)
    p = g.preview(buy(qty=40), acct())
    e = g.evaluate(buy(qty=40), acct())
    assert (p.verdict, p.approved_qty) == (e.verdict, e.approved_qty)
    events = [json.loads(line)["event"] for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert events == ["evaluate"]  # preview는 기록하지 않음
    assert not (tmp_path / "ledger.json").exists()


def test_empty_audit_path_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    g = RiskGuard(cfg(tmp_path, audit_log_path=""), clock=lambda: OPEN)
    g.evaluate(buy(), acct())
    assert list(tmp_path.iterdir()) == []
