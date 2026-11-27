"""PR #14 Codex 재리뷰(head c27d9d0) 반영 (1) — 일봉 응답 계약(P1)·yfinance 호출 제한 예외(P2).

원칙: 일봉 계약(1d·종목·시간대·거래일·배열)을 확인할 수 없는 응답은 정상 일봉으로 쓰지 않는다 → 그 응답의 volume:0 으로
ZERO_VOLUME_EXPIRED 를 만들지 않고 FILL_UNKNOWN·DATA_INCOMPLETE → 정상 일봉 확보 후 replay 복구.
HTTP 계층(``market_data._http_get``)에 응답·예외를 주입한다. 실제 네트워크·운영 DB·주문 없음.
"""
import copy
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from yfinance.exceptions import YFRateLimitError

from tests.test_auto_buy_shadow import D2, D3, job, prod, scan, shadow, utc  # noqa: F401
from tests.test_auto_buy_shadow_integrity import _two_orders, final, one, q
from tests.test_auto_buy_shadow_review4 import A2, A3, B2, B3, C3, chart_payload
from tests.test_auto_buy_shadow_review5_http import R, http, ok
from trading.auto_buy.shadow import market_data as MD
from trading.auto_buy.shadow.runner import run_shadow_job
from trading.auto_buy.shadow.store import connect_rw, get_runtime
from trading.auto_buy.shadow.vfills import Bar

NY = ZoneInfo("America/New_York")
ZERO = (100.2, 100.2, 100.2, 100.2, 0)


def _ts(day, hh=9, mm=30):
    return int(datetime.fromisoformat(day).replace(hour=hh, minute=mm, tzinfo=NY).timestamp())


def variant(base, *, meta=None, drop_meta=(), ts=None, quote=None):
    p = copy.deepcopy(base)
    res = p["chart"]["result"][0]
    res["meta"].update(meta or {})
    for k in drop_meta:
        res["meta"].pop(k, None)
    if ts is not None:
        res["timestamp"] = ts
    if quote is not None:
        res["indicators"]["quote"][0].update(quote)
    return p


def intraday(ticker, day, gran, n=3, step_min=5, vol=0):
    """실제 분봉 형식에 가까운 응답 (같은 날 여러 봉)."""
    base = chart_payload(ticker, day, (100.0, 100.0, 100.0, 100.0, vol), meta={"dataGranularity": gran})
    res = base["chart"]["result"][0]
    res["timestamp"] = [_ts(day, 9, 30 + i * step_min) for i in range(n)]
    for k in ("open", "high", "low", "close"):
        res["indicators"]["quote"][0][k] = [100.0] * n
    res["indicators"]["quote"][0]["volume"] = [vol] * n
    res["indicators"]["adjclose"][0]["adjclose"] = [100.0] * n
    return base


GOOD = chart_payload("AAA", D2, (100.0, 103.0, 99.0, 102.0, 5000))


# ── 일봉 응답 계약 (단위) ───────────────────────────────────────────────

@pytest.mark.parametrize("payload,code", [
    (variant(GOOD, meta={"dataGranularity": "1m"}), "GRANULARITY_NOT_DAILY"),
    (variant(GOOD, meta={"dataGranularity": "5m"}), "GRANULARITY_NOT_DAILY"),
    (variant(GOOD, meta={"dataGranularity": "1h"}), "GRANULARITY_NOT_DAILY"),
    (variant(GOOD, meta={"dataGranularity": "1wk"}), "GRANULARITY_NOT_DAILY"),
    (variant(GOOD, meta={"dataGranularity": "1D "}), "GRANULARITY_NOT_DAILY"),
    (intraday("AAA", D2, "5m"), "GRANULARITY_NOT_DAILY"),
    (variant(GOOD, drop_meta=("dataGranularity",)), "GRANULARITY_MISSING"),
    (variant(GOOD, meta={"dataGranularity": None}), "GRANULARITY_MISSING"),
    (variant(GOOD, meta={"dataGranularity": 1}), "GRANULARITY_INVALID_TYPE"),
    (variant(GOOD, meta={"dataGranularity": ["1d"]}), "GRANULARITY_INVALID_TYPE"),
    (variant(GOOD, drop_meta=("symbol",)), "SYMBOL_MISSING"),
    (variant(GOOD, meta={"symbol": ""}), "SYMBOL_MISSING"),
    (variant(GOOD, meta={"symbol": "ZZZ"}), "SYMBOL_MISMATCH"),
    (variant(GOOD, drop_meta=("exchangeTimezoneName",)), "TZ_MISSING"),
    (variant(GOOD, meta={"exchangeTimezoneName": "Asia/Seoul"}), "TZ_MISMATCH"),
    (variant(GOOD, ts=[float(_ts(D2))]), "TIMESTAMP_INVALID"),
    (variant(GOOD, ts=[str(_ts(D2))]), "TIMESTAMP_INVALID"),
    (variant(GOOD, ts=[_ts(D3)]), "TIMESTAMP_OUT_OF_RANGE"),                      # 다른 거래일
    (variant(GOOD, ts=[_ts(D2, 20, 0)]), "BAR_TIME_OUTSIDE_SESSION"),             # 장 마감 후 시각
    (variant(GOOD, ts=[_ts(D2, 9, 30), _ts(D2, 9, 0)], quote={k: [1.0, 1.0] for k in
                                                               ("open", "high", "low", "close", "volume")}),
     "TIMESTAMP_NOT_INCREASING"),
    (variant(GOOD, quote={"high": [98.0]}), "OHLC_INCONSISTENT"),
    (variant(GOOD, quote={"low": [102.5]}), "OHLC_INCONSISTENT"),
    (variant(GOOD, quote={"close": [102.0, 1.0]}), "CLOSE_ARRAY_PARTIAL"),
    ({"chart": {"result": [{"meta": "x", "timestamp": [_ts(D2)]}], "error": None}}, "META_MISSING"),
])
def test_daily_contract_rejects_unverifiable_responses(payload, code):
    bar, ev = MD.parse_chart(payload, "AAA", D2, "h" * 64)
    assert bar is None and ev["issues"][0] == code and ev["response_sha256"] == "h" * 64


def test_holiday_request_is_not_a_trading_session():
    p = chart_payload("AAA", "2026-11-26", (100.0, 103.0, 99.0, 102.0, 10))            # 추수감사절 휴장
    bar, ev = MD.parse_chart(p, "AAA", "2026-11-26")
    assert bar is None and ev["issues"][0] == "DAY_NOT_TRADING_SESSION"


def test_normal_daily_and_early_close_still_pass():
    bar, ev = MD.parse_chart(GOOD, "AAA", D2)
    assert isinstance(bar, Bar) and bar.volume == 5000 and ev["data_granularity"] == "1d"
    early = chart_payload("AAA", "2026-11-27", (100.0, 103.0, 99.0, 102.0, 10))        # 조기폐장일 09:30 봉
    assert isinstance(MD.parse_chart(early, "AAA", "2026-11-27")[0], Bar)
    late = variant(early, ts=[_ts("2026-11-27", 14, 0)])                               # 조기폐장 13:00 이후 시각
    assert MD.parse_chart(late, "AAA", "2026-11-27")[1]["issues"][0] == "BAR_TIME_OUTSIDE_SESSION"


def test_raw_null_and_zero_distinction_kept_for_valid_daily():
    z, _ = MD.parse_chart(chart_payload("AAA", D2, (100.0, 100.0, 100.0, 100.0, 0)), "AAA", D2)
    n, ev = MD.parse_chart(chart_payload("AAA", D2, (100.0, 100.0, 100.0, 100.0, None)), "AAA", D2)
    assert z.volume == 0 and z.volume_state == "ZERO"
    assert n.volume is None and "VOLUME_NULL" in ev["issues"]


def test_contract_failure_evidence_is_kept_through_fetcher():
    calls = []
    b = MD.ChartFetcher(http({("AAA", D2): [R(variant(GOOD, meta={"dataGranularity": "1m"}))]}, calls))(["AAA"], D2)["AAA"]
    assert b.code == "GRANULARITY_NOT_DAILY" and b.evidence["data_granularity"] == "1m"
    assert b.evidence["http_status"] == 200 and len(b.evidence["response_sha256"]) == 64 and len(calls) == 1


# ── 수집 → 파싱 → 저널 → 주문 → 거래일 확정 → 복구 ─────────────────────

WRONG = {
    "1m": variant(chart_payload("BBB", D2, ZERO), meta={"dataGranularity": "1m"}),
    "5m_multi": intraday("BBB", D2, "5m"),
    "1h": variant(chart_payload("BBB", D2, ZERO), meta={"dataGranularity": "1h"}),
    "1wk": variant(chart_payload("BBB", D2, ZERO), meta={"dataGranularity": "1wk"}),
    "missing": variant(chart_payload("BBB", D2, ZERO), drop_meta=("dataGranularity",)),
    "bad_type": variant(chart_payload("BBB", D2, ZERO), meta={"dataGranularity": 86400}),
    "symbol_missing": variant(chart_payload("BBB", D2, ZERO), drop_meta=("symbol",)),
    "tz_wrong": variant(chart_payload("BBB", D2, ZERO), meta={"exchangeTimezoneName": "Europe/London"}),
}


def _prov(monkeypatch, table):
    monkeypatch.setattr(MD, "_http_get", http(table))
    return MD.yfinance_bars


@pytest.mark.parametrize("case", sorted(WRONG))
def test_wrong_contract_volume_zero_never_expires_and_recovers(shadow, prod, monkeypatch, case):
    _two_orders(shadow, prod)
    table = {("AAA", D2): [ok("AAA", D2, A2 + (5e5,))], ("BBB", D2): [R(WRONG[case])]}
    job(shadow, prod, _prov(monkeypatch, table), final(D2))
    bb = one(shadow, "SELECT * FROM ab_bars WHERE ticker='BBB' AND day=?", D2)
    assert bb["status"] == "MISSING" and bb["volume_status"] == "UNKNOWN" and bb["volume"] is None
    ev = json.loads(bb["source_evidence"])
    assert ev["fetch_failed"] and ev["error"] and len(ev["response_sha256"]) == 64
    o = one(shadow, "SELECT * FROM ab_orders WHERE account_id='SV1:P7C:B00' AND ticker='BBB'")
    assert o["state"] == "FILL_UNKNOWN" and "ZERO_VOLUME" not in (o["details"] or "")
    assert not q(shadow, "SELECT 1 FROM ab_orders WHERE ticker='BBB' AND details LIKE '%ZERO_VOLUME_EXPIRED%'")
    assert one(shadow, "SELECT status FROM ab_days WHERE day=?", D2)["status"] == "DATA_INCOMPLETE"
    scan(prod, utc(D3, 21, 30), [])
    good = {("AAA", D2): [ok("AAA", D2, A2 + (5e5,))], ("BBB", D2): [ok("BBB", D2, B2 + (1500,))],
            ("AAA", D3): [ok("AAA", D3, A3 + (6e5,))], ("BBB", D3): [ok("BBB", D3, B3 + (7e5,))],
            ("CCC", D3): [ok("CCC", D3, C3 + (1e5,))]}
    out = job(shadow, prod, _prov(monkeypatch, good), utc(D3, 22, 30))
    run = out["replay"]["run"]
    assert one(shadow, "SELECT state FROM ab_orders WHERE run_id=? AND account_id='SV1:P7C:B00' AND ticker='BBB'",
               run)["state"] == "FILLED"
    assert one(shadow, "SELECT status FROM ab_days WHERE run_id=? AND day=?", run, D2)["status"] == "COMPLETE"


# ── yfinance YFRateLimitError (실제 예외 클래스) ─────────────────────────

def test_yf_rate_limit_error_is_identified_not_generic_fetch_error():
    res = MD.fetch_chart("AAA", D2, http({("AAA", D2): [YFRateLimitError()]}))
    assert res["error"] == "YF_RATE_LIMITED" and res["rate_limited"] and not res["error"].startswith("FETCH_ERROR")


def test_yf_rate_limit_stops_job_immediately_without_other_tickers():
    calls = []
    table = {("AAA", D2): [YFRateLimitError()], ("BBB", D2): [ok("BBB", D2, B2 + (10,))],
             ("CCC", D2): [ok("CCC", D2, (50.0, 51.0, 49.0, 50.5, 10))]}
    f = MD.ChartFetcher(http(table, calls))
    got = f(["AAA", "BBB", "CCC"], D2)
    assert [c[0] for c in calls] == ["AAA"]                              # 재시도·다른 종목 요청 없음
    assert got["AAA"].code == "YF_RATE_LIMITED" and got["AAA"].evidence["halted"] == "RATE_LIMITED"
    assert got["AAA"].evidence["exception"] == "YFRateLimitError"
    assert got["BBB"].code == got["CCC"].code == "RATE_LIMITED"
    assert f.stats()["halted"] == "RATE_LIMITED" and f.stats()["requests"] == 1
    assert f(["BBB"], D3)["BBB"].code == "RATE_LIMITED" and len(calls) == 1    # 같은 job 의 다른 거래일도 중단


def _default_job(shadow, prod, now):
    return run_shadow_job(now=now, settings=shadow, account_provider=lambda _l: None, session_factory=prod,
                          scan_running=lambda: False)


def test_yf_rate_limit_in_job_leaves_day_incomplete_and_recoverable(shadow, prod, monkeypatch):
    _two_orders(shadow, prod)
    calls = []
    monkeypatch.setattr(MD, "_http_get", http({("AAA", D2): [YFRateLimitError()],
                                               ("BBB", D2): [ok("BBB", D2, B2 + (9e5,))]}, calls))
    out = _default_job(shadow, prod, final(D2))
    assert len(calls) == 1 and out["fetch"]["halted"] == "RATE_LIMITED"
    rows = {r["ticker"]: r for r in q(shadow, "SELECT * FROM ab_bars WHERE day=?", D2)}
    assert {t: json.loads(r["source_evidence"])["error"] for t, r in rows.items()} == \
        {"AAA": "YF_RATE_LIMITED", "BBB": "RATE_LIMITED"}
    assert all(r["status"] == "MISSING" for r in rows.values())
    assert one(shadow, "SELECT status FROM ab_days WHERE day=?", D2)["status"] == "DATA_INCOMPLETE"   # COMPLETE 아님
    assert {o["state"] for o in q(shadow, "SELECT state FROM ab_orders WHERE account_id='SV1:P7C:B00' "
                                          "AND ticker IN ('AAA','BBB')")} == {"FILL_UNKNOWN"}


def test_repeated_rate_limit_across_jobs_warns_once_then_resets(shadow, prod, monkeypatch):
    _two_orders(shadow, prod)
    calls = []
    monkeypatch.setattr(MD, "_http_get", http({("AAA", D2): [YFRateLimitError()],
                                               ("BBB", D2): [YFRateLimitError()]}, calls))
    base = final(D2)
    from datetime import timedelta
    for i in range(4):                                                   # 후속 job 도 제한 → job 당 1회 시도
        n0 = len(calls)
        _default_job(shadow, prod, base + timedelta(minutes=20 * i))
        assert len(calls) - n0 == 1
    db = connect_rw(shadow.journal_path)
    try:
        assert get_runtime(db, "yahoo_rate_limit")["consecutive_jobs"] == 4
    finally:
        db.close()
    assert one(shadow, "SELECT COUNT(*) n FROM ab_events WHERE kind='yahoo_rate_limited_repeated'")["n"] == 1
    monkeypatch.setattr(MD, "_http_get", http({("AAA", D2): [ok("AAA", D2, A2 + (5e5,))],
                                               ("BBB", D2): [ok("BBB", D2, B2 + (9e5,))]}))
    _default_job(shadow, prod, base + timedelta(minutes=100))
    db = connect_rw(shadow.journal_path)
    try:
        assert get_runtime(db, "yahoo_rate_limit")["consecutive_jobs"] == 0
    finally:
        db.close()


def test_http_429_retry_after_and_503_policies_unchanged():
    waits = []
    f = MD.ChartFetcher(http({("AAA", D2): [R(b"busy", 503, {"Retry-After": "2"}), ok("AAA", D2, A2 + (10,))]}),
                        sleep=waits.append)
    assert isinstance(f(["AAA"], D2)["AAA"], Bar) and waits == [2.0]
    g = MD.ChartFetcher(http({("AAA", D2): [R(b"slow", 429)]}), sleep=lambda s: None)
    assert g(["AAA", "BBB"], D2)["BBB"].code == "RATE_LIMITED"
