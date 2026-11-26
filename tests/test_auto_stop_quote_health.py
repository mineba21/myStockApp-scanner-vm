"""시세 장애 격리·복구 (2026-11-25 SHADOW 첫날 FAIL 대응). FakeBroker + 가짜 시계 + 임시 SQLite. 실주문 없음.

CINF(id 1), EVRG(id 2) 두 종목으로 첫날 구성을 흉내 낸다.
"""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tests.auto_stop_fakes import Harness, NoOrderReader, ts
from trading.auto_stop import quote_health as QH
from trading.auto_stop import service
from trading.auto_stop.brokers import LiveStopSellBroker, ShadowBroker
from trading.auto_stop.quotes import QuoteUnavailable
from trading.kiwoom_readonly import REAL_BASE_URL, KiwoomError

SHADOW_ENV = {"KIWOOM_WEB_ENABLED": "true", "AUTO_STOP_US_MODE": "SHADOW"}


def unknown():
    return QuoteUnavailable("시세 조회 실패")             # 원인 정보 없음 → UNKNOWN (첫날 로그와 같은 형태)


def auth():
    return QuoteUnavailable("인증 실패", info={"category": "auth", "http_status": 401})


def two(h, *, mode="LIVE", stop="45.00"):
    for t in ("CINF", "EVRG"):
        h.broker.holding[t] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
    return h.add(mode=mode, ticker="CINF", stop=stop), h.add(mode=mode, ticker="EVRG", stop=stop)


def rec(h, pid, mode="LIVE"):
    return h.store.runtime_get(f"quote_health:{mode}", {})[str(pid)]


def stat(h, metric, key=None, day="2026-11-23", mode="LIVE"):
    return sum(r["value"] for r in h.store.stats(day, mode) if r["metric"] == metric and (key is None or r["key"] == key))


def run(h, n, step=30):
    for _ in range(n):
        h.tick()
        h.clock.advance(step)


def sells_of(h, ticker):
    return [s for s in h.broker.sells if s["ticker"] == ticker]


@pytest.fixture
def h(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


# ── A/B: 한 종목만 실패 ──────────────────────────────────────────
@pytest.mark.parametrize("bad,good", [("CINF", "EVRG"), ("EVRG", "CINF")])
def test_single_ticker_failure_is_isolated(h, bad, good):
    ids = dict(zip(("CINF", "EVRG"), two(h)))
    h.broker.quote_exc_by[bad] = unknown()
    run(h, 6)
    assert rec(h, ids[bad])["state"] == QH.FAULT
    assert rec(h, ids[good])["state"] == QH.OK
    assert h.engine._hold_reasons() == []                    # 다른 종목 성공이 공통 카운터를 끊는다
    assert stat(h, "quote_ok", good) == 6
    h.broker.prices = {bad: "40", good: "44"}
    h.tick()
    assert len(sells_of(h, good)) == 1 and sells_of(h, bad) == []
    assert h.pos(ids[bad])["state"] == "ACTIVE"               # 장애 종목은 무효 시세로 트리거하지 않는다


# ── C: 두 종목 모두 실패 → 전체 보류, 모든 종목 회복 조회 ─────────────
def both_fail(h):
    ids = two(h)
    h.broker.quote_exc_by = {"CINF": unknown(), "EVRG": unknown()}
    run(h, 3)                                                 # 공통 카운터 6 → 전체 보류
    assert "quote_system" in h.engine._hold_reasons()
    return ids


def test_both_fail_global_hold_probes_every_ticker(h):
    c, e = both_fail(h)
    h.broker.prices = {"CINF": "40", "EVRG": "40"}
    before = len(h.broker.quote_log)
    run(h, 4)
    probed = [t for t, _ in h.broker.quote_log[before:]]
    assert probed.count("CINF") == 4 and probed.count("EVRG") == 4      # 첫 포지션 고정 조회가 아니다
    assert h.broker.sells == [] and h.store.events() == []
    assert rec(h, c)["state"] == QH.FAULT and rec(h, e)["state"] == QH.FAULT
    # 전체 보류에 들어간 tick 에서 EVRG 는 이미 회복 조회로 바뀌었으므로 1회 더 많다 (같은 tick 임계값 처리)
    assert stat(h, "recovery_fail", "CINF") == 4 and stat(h, "recovery_fail", "EVRG") == 5


# ── D: 두 번째 종목만 먼저 정상화 ─────────────────────────────────
def test_second_ticker_recovers_first_and_resumes_alone(h):
    c, e = both_fail(h)
    h.broker.quote_exc_by["EVRG"] = None
    run(h, 2, step=60)                                        # 서로 다른 시세 시각 2회
    assert rec(h, e)["state"] == QH.OK and rec(h, c)["state"] == QH.FAULT
    assert "quote_system" not in h.engine._hold_reasons()    # 장애 종목 1/2 → 광범위 아님
    h.broker.prices = {"CINF": "40", "EVRG": "44"}
    run(h, 1)
    assert len(sells_of(h, "EVRG")) == 1 and sells_of(h, "CINF") == []
    before = stat(h, "recovery_attempt", "CINF")
    run(h, 2, step=60)
    assert stat(h, "recovery_attempt", "CINF") == before + 2              # 장애 종목도 계속 회복 확인


# ── E: 두 종목 모두 정상화 ────────────────────────────────────────
def test_both_recover(h):
    c, e = both_fail(h)
    h.broker.quote_exc_by = {}
    run(h, 2, step=60)
    assert rec(h, c)["state"] == rec(h, e)["state"] == QH.OK
    assert h.engine._hold_reasons() == []
    assert h.store.runtime_get("api_errors")["quote"] == 0
    ok = stat(h, "quote_ok")
    run(h, 1)
    assert stat(h, "quote_ok") == ok + 2


# ── F: 시스템 공통 장애 (인증) ────────────────────────────────────
def test_system_auth_failure_needs_clean_rounds_not_one_ticker(h):
    c, e = two(h)
    h.broker.quote_exc_by = {"CINF": auth(), "EVRG": auth()}
    run(h, 3)
    assert "quote_system" in h.engine._hold_reasons()
    h.broker.quote_exc_by = {"CINF": auth()}                 # EVRG 만 정상 응답, CINF 는 여전히 인증 오류
    h.broker.prices = {"EVRG": "44"}
    run(h, 4, step=60)
    assert rec(h, e)["state"] == QH.OK                        # 종목 자체는 회복 확인됨
    assert "quote_system" in h.engine._hold_reasons()        # SYSTEM 오류가 남아 있으면 전체 보류 유지
    assert h.broker.sells == [] and h.store.events() == []
    h.broker.quote_exc_by = {}
    h.broker.prices = {}
    run(h, 2, step=60)
    assert h.engine._hold_reasons() == []
    audits = h.store.audits("quote_hold_on", 0, h.clock() + 1)
    assert audits and audits[0]["details"]["reason"] == "auth"


def test_broad_symbol_failure_holds_globally(h):
    c, e = two(h)
    h.broker.age_by = {"CINF": 500, "EVRG": 500}            # 응답은 오지만 둘 다 오래된 시세
    run(h, 6)
    assert h.store.runtime_get("api_errors", {}).get("quote", 0) == 0   # 종목 단위 오류는 공통 카운터에 안 셈
    assert h.engine._hold_reasons() == ["quote_broad"]       # 감시 종목 전부 장애 → 광범위 조회 실패


# ── 복구 기준: stale·동일 시세 ───────────────────────────────────
def fault_one(h, **kw):
    pid = h.add()
    h.broker.quote_exc = unknown()
    run(h, 5)
    h.broker.quote_exc = None
    assert rec(h, pid)["state"] == QH.FAULT
    return pid


def test_stale_quote_does_not_recover(h):
    pid = fault_one(h)
    h.broker.quote_age = 200
    run(h, 4, step=60)
    assert rec(h, pid)["state"] == QH.FAULT
    assert stat(h, "recovery_invalid", "T") == 4 and stat(h, "recovery_ok", "T") == 0
    h.broker.quote_age = 30
    run(h, 2, step=60)
    assert rec(h, pid)["state"] == QH.OK


def test_repeated_same_quote_time_does_not_recover(h):
    pid = fault_one(h)
    h.broker.fixed_as_of = {"T": h.clock() - 30}
    run(h, 3, step=10)                                        # 120초 안: 유효하지만 같은 시각 반복
    assert rec(h, pid)["state"] == QH.RECOVERING
    assert stat(h, "recovery_stale", "T") == 2
    h.broker.fixed_as_of = {}
    run(h, 1, step=60)
    assert rec(h, pid)["state"] == QH.OK


# ── 같은 tick 도중 임계값 도달 ────────────────────────────────────
def test_global_threshold_mid_tick_blocks_remaining_tickers(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, max_consecutive_errors=1)
    c, e = two(h)
    h.broker.quote_exc_by = {"CINF": unknown()}
    h.broker.prices = {"EVRG": "44"}                          # 유효 시세지만 같은 tick 에서 전체 보류 진입 직후
    h.tick()
    assert h.store.events() == [] and h.broker.sells == []
    assert stat(h, "held_trigger", "EVRG") == 1 and stat(h, "recovery_attempt", "EVRG") == 1


# ── 정상 시세 수신 전 신규 주문 차단 ─────────────────────────────
def test_exiting_position_waits_for_valid_quote_before_order(h):
    pid = h.add()
    h.broker.price = Decimal("44")
    h.broker.exc_seq = [None, unknown()]                      # 감시 시세는 정상(트리거), 주문 직전 시세는 실패
    h.tick()
    assert h.pos(pid)["state"] == "EXITING" and h.broker.sells == []
    h.broker.quote_age = 200                                  # 무효 시세: 임계값 전이라도 주문에 쓰지 않는다
    run(h, 2)
    assert h.broker.sells == []
    run(h, 2)                                                 # 주문 직전 시세 연속 5회 실패 → 종목 장애
    assert rec(h, pid)["state"] == QH.FAULT
    calls = h.broker.quote_calls
    h.tick()
    assert stat(h, "order_held_symbol", "T") >= 1             # 장애 중에는 _send 가 시세도 부르지 않는다
    assert h.broker.quote_calls == calls + 1                  # 회복 조회 1회만 (EXITING 장애 종목)
    h.broker.quote_age = 30
    run(h, 2, step=60)
    assert rec(h, pid)["state"] == QH.OK and len(h.broker.sells) == 1
    assert h.event(pid)["state"] == "OPEN"                    # 기존 손절 이벤트는 유지된다


# ── 60초 감시 주기 (FakeClock: 30초 tick + 응답 지연) ──────────────
def starts(h, ticker):
    return [t for tk, t in h.broker.quote_log if tk == ticker]


def gaps(xs):
    return [round(b - a, 1) for a, b in zip(xs, xs[1:])]


@pytest.mark.parametrize("latency", [[0], [2], [5, 8], [2, 5, 8, 12]])
def test_watch_cycle_stays_60s_with_response_delays(tmp_path, monkeypatch, latency):
    h = Harness(tmp_path, monkeypatch, price_interval_sec=60)
    t0 = ts(2026, 11, 23, 10, 0)
    two(h)
    h.broker.latency = latency
    for k in range(40):                                       # 20분, 30초 tick
        h.clock.set(max(h.clock(), t0 + 30 * k))
        h.tick()
    for t in ("CINF", "EVRG"):
        g = gaps(starts(h, t))
        assert len(starts(h, t)) == 20                        # 슬롯당 1회, 누적 밀림 없음
        assert max(g) < 60 + max(latency) + 1 and min(g) > 60 - max(latency) - 1
        assert all(x < 90 for x in g)                         # 이전 방식의 90초 간격이 생기지 않는다


def test_watch_cycle_with_jittered_ticks_and_no_catch_up_burst(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, price_interval_sec=60)
    t0 = ts(2026, 11, 23, 10, 0)
    h.add()
    for k in range(40):
        h.clock.set(t0 + 30.8 * k)                            # 첫날 최대 간격 30.8초
        h.tick()
    n = len(starts(h, "T"))
    assert abs(n - int(30.8 * 39 // 60)) <= 1 and max(gaps(starts(h, "T"))) < 90
    h.clock.advance(300)                                      # 5분 동안 tick 없음 (놓친 슬롯)
    h.tick()
    h.tick()                                                  # 같은 시각 재실행: 만회 조회 없음
    assert len(starts(h, "T")) == n + 1
    r = rec(h, 1)
    assert r["next_due_at"] > h.clock()


def test_failed_watch_retries_within_slot_at_most_once_per_tick(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, price_interval_sec=60)
    h.add()
    h.broker.exc_seq = [unknown()]
    h.tick(); h.clock.advance(30); h.tick(); h.clock.advance(30); h.tick()
    assert [round(t - h.broker.quote_log[0][1]) for _, t in h.broker.quote_log] == [0, 30, 60]
    assert stat(h, "quote_ok", "T") == 2 and stat(h, "slot_failed", "T") == 0


# ── 예외 경로의 429 집계, HTTP 요청 수 ────────────────────────────
def test_http_429_counted_when_quote_raises(h):
    h.add()

    def limited(ticker, exchange):
        h.broker.http_429 += 4
        raise QuoteUnavailable("429", info={"category": "rate_limit", "http_429_in_call": 4})
    h.broker.quote = limited
    h.tick()
    assert stat(h, "http_429") == 4
    assert stat(h, "quote_error", "watch:rate_limit") == 1


class _Resp:
    def __init__(self, status):
        self.status_code = status


class _Client:
    """조회 클라이언트 대역: session.post 로 HTTP 를 흉내 내고 키움 오류를 그대로 올린다."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.session = SimpleNamespace(post=lambda *a, **k: self._post(*a, **k))

    def _post(self, *a, **k):
        return _Resp(self.next_status)

    def get_overseas_orderbook(self, token, *, exchange, ticker):
        out = self.outcomes.pop(0)
        self.next_status = out if isinstance(out, int) else 200
        self.session.post("u", headers={"api-id": "usa20101"})
        if isinstance(out, int):
            exc = KiwoomError(f"미국주식 호가 조회 HTTP 오류: {out}")
            exc.kind, exc.http_status = "http", out
            raise exc
        if isinstance(out, Exception):
            raise out
        return {"quote": out}


def _broker(outcomes):
    cfg = SimpleNamespace(mode="real", base_url=REAL_BASE_URL, timeout_seconds=15)
    b = LiveStopSellBroker(cfg, client=_Client(outcomes), token="t", evidence=object(), clock=lambda: 1000.0)
    b.sleep = lambda s: None
    return b


def test_live_broker_rate_limit_exhaustion_keeps_429_and_retry_counts():
    b = _broker([429, 429, 429, 429])
    with pytest.raises(QuoteUnavailable) as e:
        b.quote("CINF", "NY")
    info = QH.classify(e.value)
    assert info["category"] == "rate_limit" and info["scope"] == QH.SYSTEM
    assert info["attempts"] == 4 and info["retries"] == 3 and info["http_429_in_call"] == 4
    assert b.pop_429() == 4 and b.pop_http() == {"usa20101": 4}


def test_live_broker_error_details_are_classified_without_secrets():
    import requests
    raw = {"stk_cd": "CINX", "stex_tp": "NY", "cur_prc": "120.00", "dt": "20261123", "bid_tm": "10:00"}
    b = _broker([raw])
    with pytest.raises(QuoteUnavailable) as e:
        b.quote("CINF", "NY")
    info = QH.classify(e.value)
    assert (info["category"], info["scope"], info["response_ticker"]) == ("symbol_mismatch", QH.SYMBOL, "CINX")
    b = _broker([requests.Timeout("read timed out")])
    with pytest.raises(QuoteUnavailable) as e:
        b.quote("CINF", "NY")
    assert QH.classify(e.value)["category"] == "timeout"
    api = KiwoomError("미국주식 호가 조회 실패 (1501): 처리 실패 계좌 12345678901 Bearer abcdefghijklmnopqrstuvwxyz0123")
    api.kind, api.http_status, api.return_code = "api", 200, "1501"
    b = _broker([api])
    with pytest.raises(QuoteUnavailable) as e:
        b.quote("CINF", "NY")
    info = QH.classify(e.value)
    assert info["category"] == "kiwoom_api_error" and info["scope"] == QH.UNKNOWN and info["return_code"] == "1501"
    assert "12345678901" not in info["cause_message"] and "abcdefghijklmnop" not in info["cause_message"]
    raw_ok = {"stk_cd": "CINF", "stex_tp": "NY", "cur_prc": "120.00", "dt": "20261123", "bid_tm": "10:00"}
    b = _broker([429, raw_ok])
    assert b.quote("CINF", "NY").price == Decimal("120.00")
    assert b.last_quote_diag["attempts"] == 2 and b.last_quote_diag["retries"] == 1


def test_classify_http_statuses_and_validation_scope():
    def k(status):
        exc = KiwoomError("x")
        exc.kind, exc.http_status = "http", status
        return exc
    assert QH.classify(k(401))["category"] == "auth"
    assert QH.classify(k(503))["scope"] == QH.SYSTEM
    assert QH.classify(k(404))["scope"] == QH.UNKNOWN
    assert QH.classify(ValueError("boom"))["scope"] == QH.UNKNOWN
    assert QH.validation_error("stale", "old")["scope"] == QH.SYMBOL
    tok = KiwoomError("미국주식 호가 조회 실패 (8005): Token이 유효하지 않습니다")
    tok.kind, tok.http_status, tok.return_code = "api", 200, "8005"
    assert (QH.classify(tok)["category"], QH.classify(tok)["scope"]) == ("auth", QH.SYSTEM)


def test_parse_response_tags_kiwoom_errors():
    from trading.kiwoom_readonly import KiwoomReadOnlyClient

    class R:
        def __init__(self, status, data):
            self.status_code, self._data = status, data

        def raise_for_status(self):
            import requests
            if self.status_code >= 400:
                raise requests.HTTPError("x")

        def json(self):
            return self._data
    with pytest.raises(KiwoomError) as e:
        KiwoomReadOnlyClient._parse_response(R(429, {}), "op")
    assert (e.value.kind, e.value.http_status) == ("http", 429) and "HTTP 오류: 429" in str(e.value)
    with pytest.raises(KiwoomError) as e:
        KiwoomReadOnlyClient._parse_response(R(200, {"return_code": 3, "return_msg": "m"}), "op")
    assert (e.value.kind, e.value.return_code) == ("api", "3")


def test_engine_error_record_is_structured(h):
    h.add()
    h.broker.quote_exc = QuoteUnavailable("x", info={"category": "kiwoom_api_error", "return_code": "1501",
                                                     "http_status": 200, "retries": 0})
    h.tick()
    r = h.store.runtime_get("quote_errors_recent")[-1]
    for key in ("ticker", "request_exchange", "purpose", "started_at", "finished_at", "duration_sec",
                "http_status", "return_code", "category", "scope", "message"):
        assert key in r
    assert r["purpose"] == "watch" and r["return_code"] == "1501"
    assert rec(h, 1)["first_error"]["return_code"] == "1501"


# ── 통계 분리와 일일 보고 ─────────────────────────────────────────
def test_report_separates_watch_and_recovery_and_counts_all_errors(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, env=SHADOW_ENV, mode="SHADOW", price_interval_sec=60)
    h.clock.set(ts(2026, 11, 23, 9, 30))
    two(h, mode="SHADOW")
    run(h, 4)                                                 # 정상 감시
    h.broker.quote_exc_by = {"CINF": unknown(), "EVRG": unknown()}
    run(h, 16)                                                # 일반 감시 실패 → 전체 보류 → 회복 조회 실패
    h.broker.quote_exc_by = {"CINF": unknown()}
    run(h, 10)
    rep = service.daily_report(h.store, "2026-11-23", "SHADOW", h.st, now=h.clock())
    w, r = rep["watch"], rep["recovery"]
    assert r["attempts"] > 0 and r["fail"] > 0 and r["ok"] > 0
    assert rep["api"]["errors"] == w["api_errors"] + r["fail"]          # 회복 조회 실패도 오류 합계에 포함
    assert rep["api"]["by_category"]["quote_recovery"] == r["fail"]
    assert w["ok"] == stat(h, "quote_ok", mode="SHADOW")
    assert w["missed"] == w["expected"] - w["ok"]                      # 누락 합계는 축소하지 않는다
    assert w["missed"] == w["held_slots"] + w["failed_slots"] + w["schedule_missed"]
    assert w["held_slots"] > 0 and w["attempts"] > 0
    assert rep["holds"]["global_seconds"] > 0 and rep["holds"]["periods"]
    cinf = [p for p in w["positions"] if p["ticker"] == "CINF"][0]
    evrg = [p for p in w["positions"] if p["ticker"] == "EVRG"][0]
    assert cinf["health_state"] == QH.FAULT and evrg["health_state"] == QH.OK
    assert [f["kind"] for f in evrg["faults"]] == ["fault", "recovered"]
    assert cinf["first_error"]["purpose"] == "watch"
    text = service.report_text(rep)
    assert "회복 조회" in text and "전체 보류" in text and "CINF" in text


# ── 재시작·기존 카운터 이행 ───────────────────────────────────────
def test_restart_keeps_fault_and_requires_two_confirmations(h):
    pid = fault_one(h)
    h.engine = h.new_engine("w2")                             # 프로세스 재시작
    h.tick()
    assert rec(h, pid)["state"] == QH.RECOVERING
    h.clock.advance(60)
    h.tick()
    assert rec(h, pid)["state"] == QH.OK


def test_legacy_common_counter_is_migrated_without_zeroing(h):
    c, e = two(h)
    h.store.runtime_set("api_errors", {"quote": 6}, h.clock())
    h.store.runtime_set("consecutive_api_errors", 6, h.clock())
    h.broker.prices = {"CINF": "44", "EVRG": "44"}
    h.tick()
    hold = h.store.runtime_get("quote_global_hold")
    assert hold["reason"] == "legacy_common_counter" and hold["legacy_count"] == 6
    assert h.store.runtime_get("api_errors")["quote"] == 6                # 근거 없이 0으로 만들지 않는다
    assert rec(h, c)["state"] == QH.RECOVERING and rec(h, c)["legacy_global_count"] == 6
    assert h.store.audits("quote_health_migrate", 0, h.clock() + 1)
    assert h.broker.sells == [] and h.store.events() == []
    h.clock.advance(60); h.tick()
    assert h.store.runtime_get("quote_global_hold") is None
    assert h.store.audits("quote_hold_off", 0, h.clock() + 1)[0]["details"]["counter_before"] == 6
    assert h.broker.sells == []                               # 회복 확정 tick 의 시세로는 주문하지 않는다
    h.clock.advance(60); h.tick()
    assert len(h.broker.sells) == 2


def test_kill_switch_survives_quote_recovery(h):
    pid = fault_one(h)
    h.store.set_kill_switch(h.clock(), True, actor="t", reason="점검")
    h.broker.price = Decimal("44")
    run(h, 3, step=60)
    assert rec(h, pid)["state"] == QH.OK and h.store.kill_switch()
    assert h.broker.sells == []


# ── 첫날 장애 패턴 재현 (SHADOW, 가상 주문만) ──────────────────────
def test_shadow_first_day_pattern_recovers_healthy_ticker(tmp_path, monkeypatch):
    holder = {}
    h = Harness(tmp_path, monkeypatch, env=SHADOW_ENV, mode="SHADOW", price_interval_sec=60,
                factory=lambda mode: ShadowBroker(holder["r"], "NEXT_QUOTE", h.clock))
    holder["r"] = h.broker = NoOrderReader(h.clock)          # 실주문·실취소 호출 시 즉시 실패
    h.clock.set(ts(2026, 11, 25, 9, 30))
    c, e = two(h, mode="SHADOW")
    day = "2026-11-25"
    run(h, 30)                                                # 15분 정상
    # 두 종목 모두 원인 불명 오류 → 공통 카운터 5 → 전체 보류 (첫날 00:46:57 KST)
    h.broker.quote_exc_by = {"CINF": unknown(), "EVRG": unknown()}
    run(h, 10)
    assert "quote_system" in h.engine._hold_reasons()
    # 이후 CINF(첫 포지션)는 계속 실패, EVRG 는 정상화. 이전 코드는 CINF 만 반복 조회해 장 종료까지 보류됐다.
    h.broker.quote_exc_by = {"CINF": unknown()}
    run(h, 8)
    assert h.engine._hold_reasons() == []
    assert rec(h, e, "SHADOW")["state"] == QH.OK and rec(h, c, "SHADOW")["state"] == QH.FAULT
    ok_before = stat(h, "quote_ok", "EVRG", day, "SHADOW")
    run(h, 10)
    assert stat(h, "quote_ok", "EVRG", day, "SHADOW") >= ok_before + 4   # EVRG 정상 감시 재개
    h.broker.prices = {"EVRG": "44"}
    run(h, 4)
    orders = h.orders()
    assert [o["ticker"] for o in orders] == ["EVRG"] and orders[0]["mode"] == "SHADOW"
    assert orders[0]["order_number"].startswith("V")         # 가상 주문
    rep = service.daily_report(h.store, day, "SHADOW", h.st, now=h.clock())
    per = {p["ticker"]: p for p in rep["watch"]["positions"]}
    assert per["EVRG"]["recovery"]["attempts"] > 0 and per["CINF"]["recovery"]["attempts"] > 0
    assert rep["api"]["errors"] >= rep["watch"]["api_errors"] + rep["recovery"]["fail"]
    assert rep["holds"]["global_seconds"] > 0
