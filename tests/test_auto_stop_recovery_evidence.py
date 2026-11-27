"""Codex 2차 리뷰 P2: 시스템 장애 복구 증거. 응답 수신 ≠ 유효 시세 회복 ≠ 전체 장애 해제. 실주문 없음.

조건: 60초 감시, 30초 tick, 429 백오프 (Codex 재현과 같음).
"""
from decimal import Decimal

import pytest

from tests.auto_stop_fakes import Harness, ts
from trading.auto_stop.quotes import Quote, QuoteUnavailable

T0 = ts(2026, 11, 23, 10, 0)


def auth():
    return QuoteUnavailable("인증 실패", info={"category": "auth", "http_status": 401})


def rl():
    return QuoteUnavailable("429", info={"category": "rate_limit", "http_status": 429})


def unknown():
    return QuoteUnavailable("시세 조회 실패")


class Grid:
    def __init__(self, h):
        self.h, self.k = h, 0

    def run(self, n):
        for _ in range(n):
            self.h.clock.set(max(self.h.clock(), T0 + 30 * self.k))
            self.h.tick()
            self.k += 1


@pytest.fixture
def h(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch, price_interval_sec=60)


def setup(h, tickers=("CINF", "EVRG")):
    ids = {}
    for t in tickers:
        h.broker.holding[t] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
        ids[t] = h.add(ticker=t)
    return ids, Grid(h)


def inc(h):
    return h.store.runtime_get("quote_global_hold")


def held(h):
    return bool(inc(h))


def enter(h, g, errors, n=8):
    h.broker.quote_exc_by = dict(errors)
    g.run(n)
    assert held(h)


def stat(h, metric, key=None):
    return sum(r["value"] for r in h.store.stats("2026-11-23", "LIVE")
               if r["metric"] == metric and (key is None or r["key"] == key))


def responding(h, make):
    """quote 를 응답은 오지만 무효인 시세로 바꾼다."""
    real = h.broker.quote

    def q(ticker, exchange):
        real(ticker, exchange)
        return make(ticker, exchange)
    h.broker.quote = q
    return real


def test_stale_quotes_after_auth_error_are_not_recovery_evidence(h):
    ids, g = setup(h)
    enter(h, g, {"CINF": auth(), "EVRG": auth()})
    h.broker.quote_exc_by = {}
    h.broker.quote_age = 500                                      # 응답은 오지만 오래된 시세
    g.run(12)
    assert held(h), "오래된 시세 반복으로 장애가 해제되면 안 된다"
    a = inc(h)["affected"]
    assert all(not x["evidence_ticks"] and x.get("responses", 0) >= 2 for x in a.values())
    assert stat(h, "incident_response") >= 4 and stat(h, "incident_evidence") == 0
    h.broker.quote_age = 30
    g.run(6)
    assert not held(h)


def test_same_quote_time_repeat_is_not_evidence(h):
    ids, g = setup(h, ("CINF",))
    enter(h, g, {"CINF": auth()})
    h.broker.quote_exc_by = {}
    h.broker.fixed_as_of = {"CINF": h.clock() + 30}               # 같은 시세 시각 반복 (120초 안)
    g.run(3)
    assert held(h) and len(inc(h)["affected"][str(ids["CINF"])]["evidence_ticks"]) <= 1


@pytest.mark.parametrize("kind", ["symbol", "exchange", "price", "no_time", "prev_day"])
def test_invalid_responses_after_system_error_are_response_only(h, kind):
    ids, g = setup(h)
    enter(h, g, {"CINF": rl(), "EVRG": auth()})
    h.broker.quote_exc_by = {}
    make = {
        "symbol": lambda t, e: Quote("OTHER", e, Decimal("50"), h.clock() - 30, "F"),
        "exchange": lambda t, e: Quote(t, "NY", Decimal("50"), h.clock() - 30, "F"),
        "price": lambda t, e: Quote(t, e, Decimal("0"), h.clock() - 30, "F"),
        "no_time": lambda t, e: Quote(t, e, Decimal("50"), None, "F"),
        "prev_day": lambda t, e: Quote(t, e, Decimal("50"), h.clock() - 86400, "F"),
    }[kind]
    real = responding(h, make)
    g.run(16)
    assert held(h)
    assert all(not x["evidence_ticks"] for x in inc(h)["affected"].values())
    h.broker.quote = real
    g.run(16)
    assert not held(h)


def test_valid_quote_and_unknown_error_in_same_tick_is_not_a_clean_round(h):
    ids, g = setup(h)
    enter(h, g, {"CINF": unknown(), "EVRG": unknown()})
    h.broker.quote_exc_by = {"CINF": unknown()}
    g.run(8)
    assert inc(h)["rounds_ok"] == 0 and held(h)


def test_new_error_on_other_target_resets_evidence_and_rounds_atomically(h):
    ids, g = setup(h)
    enter(h, g, {"CINF": auth(), "EVRG": auth()})
    h.broker.quote_exc_by = {}
    for _ in range(6):                                            # 첫 정상 라운드가 쌓일 때까지 (해제 전)
        g.run(1)
        if inc(h)["rounds_ok"] >= 1:
            break
    before = inc(h)
    assert before is not None and before["rounds_ok"] >= 1
    # 다음 회복 조회 1회에서만 EVRG 가 UNKNOWN 실패
    real = h.broker.quote
    state = {"n": 0}

    def once(ticker, exchange):
        if ticker == "EVRG" and state["n"] == 0:
            state["n"] = 1
            raise unknown()
        return real(ticker, exchange)
    h.broker.quote = once
    while state["n"] == 0:
        g.run(1)
    after = inc(h)
    assert after["rounds_ok"] == 0                                 # 같은 트랜잭션에서 공통 라운드 초기화
    assert after["affected"][str(ids["EVRG"])]["evidence_ticks"] == []
    assert after.get("last_error", {}).get("category") == "quote_unavailable"


def test_recovered_target_failing_again_restarts(h):
    ids, g = setup(h)
    enter(h, g, {"CINF": auth(), "EVRG": auth()})
    h.broker.quote_exc_by = {"EVRG": auth()}
    g.run(6)
    assert inc(h)["affected"][str(ids["CINF"])]["resolved"]
    h.broker.quote_exc_by = {"CINF": auth(), "EVRG": auth()}
    g.run(2)
    assert not inc(h)["affected"][str(ids["CINF"])]["resolved"]
    assert not inc(h)["affected"][str(ids["CINF"])]["evidence_ticks"]


def test_paused_and_disabled_targets_need_valid_quotes_not_responses(h):
    ids, g = setup(h)
    enter(h, g, {"CINF": auth(), "EVRG": auth()})
    h.store.update_position(ids["CINF"], h.clock(), auto_paused=1)
    h.store.update_position(ids["EVRG"], h.clock(), state="DISABLED", auto_paused=1)
    h.broker.quote_exc_by = {}
    real = responding(h, lambda t, e: Quote(t, e, Decimal("50"), h.clock() - 500, "F"))
    g.run(12)
    assert held(h) and set(inc(h)["affected"]) == {str(ids["CINF"]), str(ids["EVRG"])}
    h.broker.quote = real
    g.run(12)
    assert not held(h)


def test_release_only_after_every_target_has_valid_quotes(h):
    ids, g = setup(h, ("CINF", "EVRG", "AEE"))
    enter(h, g, {"CINF": auth(), "EVRG": auth(), "AEE": auth()})
    h.broker.quote_exc_by = {"AEE": auth()}
    g.run(8)
    assert held(h)
    h.broker.quote_exc_by = {}
    g.run(8)
    assert not held(h)
    off = h.store.audits("quote_hold_off", 0, h.clock() + 1)[-1]["details"]
    assert all(len(v["evidence_ticks"]) >= 2 for v in off["affected"].values())
    assert stat(h, "incident_release") == 1
