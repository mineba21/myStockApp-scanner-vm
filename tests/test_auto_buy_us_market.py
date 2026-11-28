"""auto_buy_us limiter + market: SHADOW 호출 예산과 키움 조회 전용 어댑터 (가짜 전송만)."""
from __future__ import annotations

import pathlib
from decimal import Decimal

import pytest

from tests.auto_buy_us_fakes import (INVALID_TOKEN, NOT_FOUND, FakeClock, FakeKiwoomAdapter,
                                     kst_ts, ny_ts, orderbook)

MON_1002 = ny_ts(2026, 11, 23, 10, 2)          # 월요일 정규장
PEAK = kst_ts(2026, 11, 24, 9, 30)             # KST 09:30 = 뉴욕 11/23 19:30


@pytest.fixture
def settings():
    from trading.auto_buy_us.settings import Settings
    return Settings(mode="SHADOW", mode_block=None, journal_path="", kill=False)


@pytest.fixture(autouse=True)
def _no_orderbook_fields(monkeypatch):
    monkeypatch.delenv("KIWOOM_US_ORDERBOOK_ASK_FIELD", raising=False)
    monkeypatch.delenv("KIWOOM_US_ORDERBOOK_BID_FIELD", raising=False)


class Rig:
    def __init__(self, settings, t=MON_1002, script=None):
        from trading.auto_buy_us.limiter import CallBudget, ThrottledSession
        from trading.auto_buy_us.market import KiwoomMarketData
        from trading.auto_stop.brokers import LiveStopSellBroker
        from trading.kiwoom_readonly import KiwoomConfig, KiwoomReadOnlyClient

        self.clock = FakeClock(t)
        self.budget = CallBudget(settings, clock=self.clock, sleep=self.clock.sleep)
        self.session = ThrottledSession(self.budget)
        self.fake = FakeKiwoomAdapter(self.clock, script).mount(self.session)
        config = KiwoomConfig("k", "s", "real")
        client = KiwoomReadOnlyClient(config, session=self.session)
        self.reader = LiveStopSellBroker(config, client=client, token="T", allow_orders=False)
        self.market = KiwoomMarketData(self.reader, self.budget, settings, clock=self.clock)
        self.budget.start_tick()


@pytest.fixture
def fake(settings):
    return lambda **kw: Rig(settings, **kw)


# --- limiter ---------------------------------------------------------------

def test_global_spacing_one_per_sec(fake):
    rig = fake(script={"usa20101": lambda b: (200, orderbook("AAPL", "ND", "100", MON_1002)),
                       "ust21110": (200, {"return_code": 0, "result_list": []}),
                       "ust21120": (200, {"return_code": 0, "result_list": []})})
    for api_id in ("usa20101", "ust21110", "ust21120"):
        rig.session.post("https://api.kiwoom.com/x", headers={"api-id": api_id}, json={})
    times = [t for t, _, _ in rig.fake.calls]
    assert len(times) == 3
    assert all(b - a >= 1.0 for a, b in zip(times, times[1:]))


def test_per_tr_spacing(fake, settings):
    from trading.auto_buy_us.limiter import CallBudget
    clock = FakeClock(MON_1002)
    budget = CallBudget(settings, clock=clock, sleep=clock.sleep)
    budget.start_tick()
    seen = []
    for api_id in ("A", "A", "B", "A"):
        budget.acquire(api_id)
        seen.append((clock(), api_id))
    assert seen[1][0] - seen[0][0] >= 1.0           # 같은 TR 연속
    assert seen[2][0] - seen[1][0] >= 1.0           # 다른 TR 도 전체 간격 1초
    assert seen[3][0] - seen[1][0] >= 1.0


def test_per_tr_spacing_stricter_than_global(settings):
    from dataclasses import replace

    from trading.auto_buy_us.limiter import CallBudget
    clock = FakeClock(MON_1002)
    budget = CallBudget(replace(settings, calls_per_sec=10.0, per_tr_per_sec=1.0),
                        clock=clock, sleep=clock.sleep)
    budget.start_tick()
    budget.acquire("A")
    t0 = clock()
    budget.acquire("B")
    assert clock() - t0 == pytest.approx(0.1)
    budget.acquire("A")
    assert clock() - t0 >= 1.0


def test_tick_cap_20(fake, settings):
    from trading.auto_buy_us.limiter import BudgetExhausted, CallBudget
    clock = FakeClock(MON_1002)
    budget = CallBudget(settings, clock=clock, sleep=clock.sleep)
    budget.start_tick()
    for _ in range(20):
        budget.acquire("usa20101")
    with pytest.raises(BudgetExhausted):
        budget.acquire("usa20101")
    assert budget.drain_stats()[("usa20101", "budget_skip")] == 1
    assert budget.drain_stats() == {}              # drain 은 비운다
    budget.start_tick()
    budget.acquire("usa20101")                     # 새 tick 에는 다시 허용


def test_peak_hours_zero_calls(fake):
    from trading.auto_buy_us.limiter import BudgetExhausted
    rig = fake(t=PEAK, script={"usa20101": lambda b: (200, orderbook("AAPL", "ND", "1", PEAK))})
    with pytest.raises(BudgetExhausted):
        rig.budget.acquire("usa20101")
    with pytest.raises(BudgetExhausted):
        rig.market.quote("AAPL", "ND")
    assert rig.fake.count() == 0
    assert rig.budget.drain_stats()[("usa20101", "budget_skip")] == 2
    rig.clock.set(kst_ts(2026, 11, 24, 10, 0))     # 10:00 부터는 허용
    rig.budget.acquire("usa20101")


def test_429_stops_and_cools_down(fake):
    from trading.auto_buy_us.limiter import BudgetExhausted, RateLimited
    rig = fake(script={"usa20101": [(429, {"return_code": -1}),
                                    (200, orderbook("AAPL", "ND", "100", MON_1002))]})
    with pytest.raises(RateLimited):
        rig.session.post("https://api.kiwoom.com/x", headers={"api-id": "usa20101"}, json={})
    assert rig.budget.drain_stats() == {("usa20101", "http_429"): 1}
    hit = rig.clock()
    rig.clock.set(hit + 59)
    with pytest.raises(BudgetExhausted):
        rig.budget.acquire("ust21110")
    rig.clock.set(hit + 61)
    rig.budget.acquire("ust21110")


def test_429_not_retried_by_s01_helper(fake):
    from trading.auto_buy_us.limiter import RateLimited
    rig = fake(script={"usa20101": (429, {"return_code": -1})})
    with pytest.raises(RateLimited):
        rig.reader.quote("AAPL", "ND")
    assert rig.fake.count() == 1
    rig.clock.advance(120)
    with pytest.raises(RateLimited):
        rig.market.quote("AAPL", "ND")
    assert rig.fake.count() == 2                   # S01 의 2/4/8초 429 재시도 없음


def test_timeout_counted_and_quote_invalid(fake):
    rig = fake(script={"usa20101": "TIMEOUT"})
    ev = rig.market.quote("AAPL", "ND")
    assert ev.valid is False and ev.price is None and ev.invalid_reason
    assert rig.budget.drain_stats() == {("usa20101", "timeout"): 1}


def test_ok_and_err_outcomes(fake):
    rig = fake(script={"usa20101": [(200, orderbook("AAPL", "ND", "100", MON_1002)),
                                    (500, {"return_code": -1})]})
    rig.market.quote("AAPL", "ND")
    rig.market.quote("AAPL", "ND")
    assert rig.budget.drain_stats() == {("usa20101", "ok"): 1, ("usa20101", "err"): 1}


def test_oauth_call_is_budgeted(fake):
    rig = fake(script={"oauth": (200, {"return_code": 0, "token": "N"}),
                       "usa20101": lambda b: (200, orderbook("AAPL", "ND", "100", MON_1002))})
    rig.reader.client.issue_token()
    rig.market.quote("AAPL", "ND")
    (t1, a1, _), (t2, a2, _) = rig.fake.calls
    assert (a1, a2) == ("oauth", "usa20101") and t2 - t1 >= 1.0
    assert rig.budget.drain_stats()[("oauth", "ok")] == 1


# --- market: quote ---------------------------------------------------------

def test_quote_valid(fake):
    as_of = ny_ts(2026, 11, 23, 10, 1)
    rig = fake(script={"usa20101": lambda b: (200, orderbook("AAPL", "ND", "+187.456", as_of))})
    ev = rig.market.quote("AAPL", "ND")
    assert ev.valid is True and ev.invalid_reason is None
    assert ev.price == Decimal("187.46") and ev.as_of == as_of
    assert ev.ticker == "AAPL" and ev.exchange == "ND" and ev.source == "KIWOOM_USA20101"
    assert ev.requested_at == MON_1002 and ev.received_at >= ev.requested_at


def test_quote_stale_invalid(fake):
    as_of = MON_1002 - 180
    rig = fake(script={"usa20101": lambda b: (200, orderbook("AAPL", "ND", "100", as_of))})
    ev = rig.market.quote("AAPL", "ND")
    assert ev.valid is False and "초 지났습니다" in ev.invalid_reason
    assert ev.price == Decimal("100.00") and ev.as_of == as_of


def test_quote_premarket_invalid(fake):
    at = ny_ts(2026, 11, 23, 9, 20)
    rig = fake(t=at + 60, script={"usa20101": lambda b: (200, orderbook("AAPL", "ND", "100", at))})
    ev = rig.market.quote("AAPL", "ND")
    assert ev.valid is False and "개장 이전" in ev.invalid_reason


def test_invalid_token_response_is_invalid_quote(fake):
    rig = fake(script={"usa20101": (200, INVALID_TOKEN)})
    ev = rig.market.quote("AAPL", "ND")
    assert ev.valid is False and ev.price is None and ev.as_of is None
    assert rig.market.token_invalid is True
    assert rig.fake.count() == 1


# --- market: exchange ------------------------------------------------------

def _by_exchange(answers):
    def answer(body):
        ex = body.get("stex_tp")
        if ex in answers:
            return 200, orderbook(body["stk_cd"], ex, answers[ex], MON_1002)
        return 200, NOT_FOUND
    return answer


def test_ambiguous_exchange_unresolved(fake):
    rig = fake(script={"usa20101": _by_exchange({"ND": "10", "NY": "10"})})
    assert rig.market.resolve_exchange("DUAL") is None


def test_single_exchange_resolved(fake):
    rig = fake(script={"usa20101": _by_exchange({"NY": "10"})})
    assert rig.market.resolve_exchange("IBM") == "NY"
    assert rig.fake.count("usa20101") == 3


def test_resolve_exchange_propagates_budget(fake):
    from trading.auto_buy_us.limiter import BudgetExhausted
    rig = fake(t=PEAK, script={"usa20101": _by_exchange({"NY": "10"})})
    with pytest.raises(BudgetExhausted):
        rig.market.resolve_exchange("IBM")


# --- market: account snapshot ----------------------------------------------

def _account_script(currency_items, deposit_items=None, holdings=None):
    deposit_items = deposit_items if deposit_items is not None else [
        {"crnc_code": "KRW", "fc_entra": "5", "fc_ord_alowa": "5"},
        {"crnc_code": "USD", "fc_entra": "1,000.50", "fc_ord_alowa": "900.25"}]
    return {"ust21110": (200, {"return_code": 0, "result_list": deposit_items}),
            "ust21120": (200, {"return_code": 0, "result_list": currency_items}),
            "ust21070": (200, {"return_code": 0, "result_list": holdings if holdings is not None else [
                {"stk_cd": "AAPL", "poss_qty": "3", "sell_alowq": "3", "frgn_stk_book_uv": "10"}]})}


def test_account_snapshot_complete(fake):
    rig = fake(script=_account_script([{"crnc_code": "USD", "evlt_amt": "-5000"}]))
    snap = rig.market.account_snapshot()
    assert snap.complete is True and snap.reason is None
    assert snap.net_asset_usd == Decimal("6000.50")
    assert snap.orderable_cash_usd == Decimal("900.25")
    assert snap.holdings_count == 1


def test_account_snapshot_incomplete_not_zero(fake):
    rig = fake(script=_account_script([{"crnc_code": "KRW", "evlt_amt": "1"}]))
    snap = rig.market.account_snapshot()
    assert snap.complete is False and snap.reason
    assert snap.net_asset_usd is None


def test_account_snapshot_kiwoom_error(fake):
    script = _account_script([{"crnc_code": "USD", "evlt_amt": "1"}])
    script["ust21110"] = (200, INVALID_TOKEN)
    rig = fake(script=script)
    snap = rig.market.account_snapshot()
    assert snap.complete is False and "8005" in snap.reason
    assert snap.net_asset_usd is None and snap.orderable_cash_usd is None and snap.holdings_count is None
    assert rig.market.token_invalid is True


def test_account_snapshot_balance_unavailable(fake):
    script = _account_script([{"crnc_code": "USD", "evlt_amt": "1"}])
    script["ust21070"] = (500, {"return_code": -1})
    rig = fake(script=script)
    snap = rig.market.account_snapshot()
    assert snap.complete is False and snap.holdings_count is None


# --- 격리 -------------------------------------------------------------------

def test_no_order_client_import():
    root = pathlib.Path(__file__).resolve().parents[1] / "trading" / "auto_buy_us"
    src = list(root.rglob("*.py"))
    assert src
    assert not any("kiwoom_orders" in p.read_text(encoding="utf-8") for p in src)


def test_rate_errors_not_kiwoom_errors():
    from trading.auto_buy_us.limiter import BudgetExhausted, RateLimited
    from trading.kiwoom_readonly import KiwoomError
    assert not issubclass(RateLimited, KiwoomError)
    assert not issubclass(BudgetExhausted, KiwoomError)


@pytest.fixture
def live_env(monkeypatch, tmp_path):
    from trading.kiwoom_readonly import KiwoomConfig
    monkeypatch.setenv("KIWOOM_WEB_ENABLED", "true")
    monkeypatch.setenv("SCANNER_ORDER_JOURNAL_PATH", str(tmp_path / "s01.sqlite"))
    monkeypatch.setattr(KiwoomConfig, "from_profile", classmethod(lambda cls, p: KiwoomConfig("k", "s", "real")))
    calls = []

    def fake_token(profile, config, client, *, force=False):
        calls.append((profile, force))
        return "T"
    monkeypatch.setattr("web.kiwoom_holdings._get_token", fake_token)
    return calls


def test_build_blocked_by_read_access(settings, monkeypatch):
    from trading.auto_buy_us.limiter import CallBudget
    from trading.auto_buy_us.market import build_market_data
    monkeypatch.delenv("KIWOOM_WEB_ENABLED", raising=False)
    with pytest.raises(RuntimeError, match="KIWOOM_WEB_ENABLED"):
        build_market_data(settings, CallBudget(settings))


def test_build_passes_force_token(settings, live_env):
    from trading.auto_buy_us.limiter import CallBudget, ThrottledSession
    from trading.auto_buy_us.market import build_market_data
    md = build_market_data(settings, CallBudget(settings), force_token=True)
    assert live_env == [("account1", True)]
    assert isinstance(md.reader.client.session, ThrottledSession)
    assert md.reader.orders is None


def test_does_not_patch_s01_session(settings, live_env):
    from trading.auto_buy_us.limiter import CallBudget, ThrottledSession
    from trading.auto_buy_us.market import build_market_data
    from trading.auto_stop import runner

    s01 = runner.live_reader()
    s01_session = s01.client.session
    s01_post = s01_session.post
    a = build_market_data(settings, CallBudget(settings))
    b = build_market_data(settings, CallBudget(settings))
    sessions = {id(s01_session), id(a.reader.client.session), id(b.reader.client.session)}
    assert len(sessions) == 3
    assert not isinstance(s01_session, ThrottledSession)
    assert s01_session.post is s01_post             # S01 세션 post 를 다시 감싸지 않는다
    assert a.reader.client is not s01.client
