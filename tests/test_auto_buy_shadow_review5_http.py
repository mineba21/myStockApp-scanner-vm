"""PR #14 Codex 재리뷰(head 9a315db) 반영 (1) — HTTP 오류 응답·호출량 안전장치. 수집 → 주문 → 일별 상태 → 복구.

원칙: 검증되지 않은 시장 데이터로 가상 체결하지 않는다 / 확인 불가능한 상태를 정상으로 확정하지 않는다 /
수집 실패를 정상 미체결 만료로 처리하지 않는다. HTTP 계층(``market_data._http_get``)에 응답·예외를 주입한다.
실제 네트워크·운영 DB·주문 없음.
"""
import json

import pytest

from tests.test_auto_buy_shadow import D2, D3, Bars, job, prod, scan, shadow, utc  # noqa: F401
from tests.test_auto_buy_shadow_integrity import _two_orders, final, one, q
from tests.test_auto_buy_shadow_review4 import A2, A3, B2, B3, C3, chart_payload
from trading.auto_buy.shadow import market_data as MD
from trading.auto_buy.shadow.runner import run_shadow_job
from trading.auto_buy.shadow.vfills import Bar, BarUnavailable


class R:
    def __init__(self, body=b"", status=200, headers=None, no_status=False):
        self.content = body if isinstance(body, bytes) else json.dumps(body).encode()
        if not no_status:
            self.status_code = status
        self.headers = headers or {}


def http(table, calls=None):
    """table = {(ticker, day): [응답 또는 예외, ...]} — 시도마다 다음 항목 (마지막 항목 반복). 없으면 404."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    seen = {}

    def get(url, params, timeout=15):
        t = url.rstrip("/").rsplit("/", 1)[-1]
        day = datetime.fromtimestamp(params["period1"], ZoneInfo("America/New_York")).date().isoformat()
        if calls is not None:
            calls.append((t, day, timeout))
        seq = table.get((t, day))
        if seq is None:
            return R({"chart": {"result": None, "error": {"code": "Not Found"}}}, 404)
        i = seen.get((t, day), 0)
        seen[(t, day)] = i + 1
        item = seq[min(i, len(seq) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item
    return get


def ok(t, day, quote):
    return R(chart_payload(t, day, quote))


class Timeout(Exception):
    pass


Timeout.__name__ = "ReadTimeout"
ZERO_A2 = (100.5, 100.5, 100.5, 100.5, 0)


# ── 단위: fetch_chart / ChartFetcher ───────────────────────────────────

@pytest.mark.parametrize("resp,code,retryable", [
    (R(chart_payload("AAA", D2, ZERO_A2), 503), "HTTP_503", True),           # 오류 + 정상 형식 JSON + volume:0
    (R(chart_payload("AAA", D2, A2 + (10,)), 429), "HTTP_429", True),
    (R(b"denied", 401), "HTTP_401", False),
    (R(b"denied", 403), "HTTP_403", False),
    (R(b"oops", 500), "HTTP_500", True),
    (R(chart_payload("AAA", D2, A2 + (10,)), no_status=True), "HTTP_STATUS_UNKNOWN", False),   # 상태 확인 불가
    (R(b"{not json", 200), "INVALID_JSON", False),
    (R(b"", 200), "EMPTY_BODY", False),
])
def test_fetch_chart_uses_body_only_for_http_200(resp, code, retryable):
    res = MD.fetch_chart("AAA", D2, http({("AAA", D2): [resp]}))
    assert res["payload"] is None and res["error"] == code and res["retryable"] is retryable
    if resp.content:
        assert len(res["sha256"]) == 64                                     # 실패 근거: 응답 해시


@pytest.mark.parametrize("exc,code", [(Timeout("t"), "TIMEOUT"), (ConnectionError("c"), "CONNECTION_ERROR"),
                                      (ValueError("x"), "FETCH_ERROR:ValueError")])
def test_fetch_chart_transport_errors(exc, code):
    res = MD.fetch_chart("AAA", D2, http({("AAA", D2): [exc]}))
    assert res["payload"] is None and res["error"] == code


def test_http_error_body_volume_zero_is_never_a_confirmed_zero():
    calls = []
    got = MD.ChartFetcher(http({("AAA", D2): [R(chart_payload("AAA", D2, ZERO_A2), 503)]}, calls))(["AAA"], D2)
    b = got["AAA"]
    assert isinstance(b, BarUnavailable) and not isinstance(b, Bar) and b.code == "HTTP_503"
    assert b.evidence["http_status"] == 503 and len(b.evidence["response_sha256"]) == 64
    assert b.evidence["attempts"] == 2 and len(calls) == 2                  # 재시도 1회 (제한)


@pytest.mark.parametrize("payload,code", [
    ({"chart": {"result": None, "error": {"code": "Not Found", "description": "delisted"}}}, "CHART_ERROR"),
    ({"chart": {"result": [], "error": None}}, "NO_RESULT"),
    (chart_payload("ZZZ", D2, A2 + (10,)), "SYMBOL_MISMATCH"),
    (chart_payload("AAA", D3, A2 + (10,)), "DAY_NOT_IN_RESPONSE"),
    (chart_payload("AAA", D2, (None, 103, 99, 102, 10)), "OHLC_NULL_OR_INVALID"),
])
def test_http_200_content_is_validated_separately(payload, code):
    calls = []
    b = MD.ChartFetcher(http({("AAA", D2): [R(payload)]}, calls))(["AAA"], D2)["AAA"]
    assert isinstance(b, BarUnavailable) and b.code == code and len(calls) == 1     # 내용 오류는 재시도하지 않음


def test_retry_then_success_and_timeout_passed(monkeypatch):
    calls, waits = [], []
    f = MD.ChartFetcher(http({("AAA", D2): [R(b"busy", 503, {"Retry-After": "3"}), ok("AAA", D2, A2 + (10,))]}, calls),
                        sleep=waits.append)
    b = f(["AAA"], D2)["AAA"]
    assert isinstance(b, Bar) and b.volume == 10 and b.evidence["attempts"] == 2 and b.evidence["http_status"] == 200
    assert waits == [3.0] and all(c[2] == MD.REQUEST_TIMEOUT_SEC for c in calls)    # Retry-After 존중, timeout 지정


def test_long_retry_after_stops_collection_without_waiting():
    calls, waits = [], []
    f = MD.ChartFetcher(http({("AAA", D2): [R(b"slow down", 429, {"Retry-After": "600"})],
                              ("BBB", D2): [ok("BBB", D2, B2 + (10,))]}, calls), sleep=waits.append)
    got = f(["AAA", "BBB"], D2)
    assert got["AAA"].code == "HTTP_429" and got["BBB"].code == "RATE_LIMITED" and waits == []
    assert [c[0] for c in calls] == ["AAA"] and f.stats()["halted"] == "RATE_LIMITED"   # 제한 중 추가 요청 없음


def test_request_budget_and_dedupe():
    calls = []
    table = {(t, D2): [ok(t, D2, A2 + (10,))] for t in ("AAA", "BBB", "CCC")}
    f = MD.ChartFetcher(http(table, calls), budget=2)
    first = f(["AAA", "BBB", "CCC"], D2)
    again = f(["AAA", "CCC"], D2)                                            # 같은 job 안 재요청 → 재사용
    assert isinstance(first["AAA"], Bar) and isinstance(first["BBB"], Bar)
    assert first["CCC"].code == "BUDGET_EXHAUSTED" and again["CCC"].code == "BUDGET_EXHAUSTED"
    assert len(calls) == 2 and f.stats()["requests"] == 2


def test_consecutive_failures_open_circuit():
    calls = []
    tickers = [f"T{i}" for i in range(8)]
    f = MD.ChartFetcher(http({(t, D2): [ConnectionError("down")] for t in tickers}, calls))
    got = f(tickers, D2)
    n = MD.CIRCUIT_CONSECUTIVE_FAILURES
    assert all(got[t].code == "CONNECTION_ERROR" for t in sorted(tickers)[:n])
    assert all(got[t].code == "CIRCUIT_OPEN" for t in sorted(tickers)[n:])
    assert len(calls) == n * MD.MAX_ATTEMPTS                                 # 중단 뒤 추가 요청 없음


# ── 수집 → 주문 → 일별 상태 → 복구 (실제 공급자 경로) ────────────────────

FAILURES = {
    "http503_json_vol0": [R(chart_payload("BBB", D2, (100.2, 100.2, 100.2, 100.2, 0)), 503)],
    "http429_json": [R(chart_payload("BBB", D2, B2 + (10,)), 429)],
    "http401": [R(b"unauthorized", 401)],
    "http403": [R(b"forbidden", 403)],
    "http500": [R(b"error", 500)],
    "timeout": [Timeout("read timed out")],
    "connection": [ConnectionError("reset")],
    "status_unknown": [R(chart_payload("BBB", D2, B2 + (10,)), no_status=True)],
    "http200_bad_json": [R(b"<html>not json</html>", 200)],
    "http200_chart_error": [R({"chart": {"result": None, "error": {"code": "Internal", "description": "x"}}})],
}


def _provider(monkeypatch, table):
    monkeypatch.setattr(MD, "_http_get", http(table))
    return MD.yfinance_bars


@pytest.mark.parametrize("case", sorted(FAILURES))
def test_collection_failure_is_recoverable_missing_not_unfilled(shadow, prod, monkeypatch, case):
    _two_orders(shadow, prod)
    table = {("AAA", D2): [ok("AAA", D2, A2 + (5e5,))], ("BBB", D2): FAILURES[case]}
    out = job(shadow, prod, _provider(monkeypatch, table), utc(D2, 22, 30))
    assert out["days"] == [] and "1/2" in out["waiting"]                    # 마감 전: 기다림 (결측 확정 안 함)
    job(shadow, prod, _provider(monkeypatch, table), final(D2))
    bb = one(shadow, "SELECT * FROM ab_bars WHERE ticker='BBB' AND day=?", D2)
    assert bb["status"] == "MISSING" and bb["volume_status"] == "UNKNOWN" and bb["volume"] is None
    assert bb["open"] is None                                               # 오류 응답 본문의 가격·거래량을 쓰지 않음
    ev = json.loads(bb["source_evidence"])
    assert ev["fetch_failed"] and ev["error"] and ev["ticker"] == "BBB"
    o = {r["ticker"]: r for r in q(shadow, "SELECT * FROM ab_orders WHERE account_id='SV1:P7C:B00'")}
    assert o["BBB"]["state"] == "FILL_UNKNOWN"                              # 미체결 만료(UNFILLED·ZERO_VOLUME) 아님
    assert o["AAA"]["state"] == "FILLED"
    assert one(shadow, "SELECT status FROM ab_days WHERE day=?", D2)["status"] == "DATA_INCOMPLETE"
    # 정상 응답 확보 → 기존 복구·재계산
    scan(prod, utc(D3, 21, 30), [])
    good = {("AAA", D2): [ok("AAA", D2, A2 + (5e5,))], ("BBB", D2): [ok("BBB", D2, B2 + (1500,))],
            ("AAA", D3): [ok("AAA", D3, A3 + (6e5,))], ("BBB", D3): [ok("BBB", D3, B3 + (7e5,))],
            ("CCC", D3): [ok("CCC", D3, C3 + (1e5,))]}
    out = job(shadow, prod, _provider(monkeypatch, good), utc(D3, 22, 30))
    assert out["replay"] and out["days"] == [D3]
    run = out["replay"]["run"]
    assert one(shadow, "SELECT state FROM ab_orders WHERE run_id=? AND account_id='SV1:P7C:B00' AND ticker='BBB'",
               run)["state"] == "FILLED"
    assert one(shadow, "SELECT status FROM ab_days WHERE run_id=? AND day=?", run, D2)["status"] == "COMPLETE"


def test_http200_raw_null_and_zero_still_follow_volume_policy(shadow, prod, monkeypatch):
    _two_orders(shadow, prod)
    table = {("AAA", D2): [ok("AAA", D2, (100.5, 100.5, 100.5, 100.5, 0))], ("BBB", D2): [ok("BBB", D2, B2 + (None,))]}
    job(shadow, prod, _provider(monkeypatch, table), final(D2))
    o = {r["ticker"]: r for r in q(shadow, "SELECT * FROM ab_orders WHERE account_id='SV1:P7C:B00'")}
    assert o["AAA"]["state"] == "UNFILLED" and json.loads(o["AAA"]["details"])["reason"] == "ZERO_VOLUME_EXPIRED"
    assert o["BBB"]["state"] == "FILL_UNKNOWN"
    aa = one(shadow, "SELECT * FROM ab_bars WHERE ticker='AAA' AND day=?", D2)
    assert (aa["volume_status"], json.loads(aa["source_evidence"])["http_status"]) == ("ZERO", 200)


def test_budget_exhaustion_never_confirms_a_normal_day(shadow, prod, monkeypatch):
    _two_orders(shadow, prod)
    monkeypatch.setattr(MD, "JOB_REQUEST_BUDGET", 1)
    monkeypatch.setattr(MD, "_http_get", http({("AAA", D2): [ok("AAA", D2, A2 + (5e5,))],
                                               ("BBB", D2): [ok("BBB", D2, B2 + (9e5,))]}))
    out = run_shadow_job(now=final(D2), settings=shadow, account_provider=lambda _l: None, session_factory=prod,
                         scan_running=lambda: False)                         # 운영 기본 공급자 (job 단위 ChartFetcher)
    assert out["fetch"]["requests"] == 1 and out["fetch"]["halted"] == "BUDGET_EXHAUSTED"
    bb = one(shadow, "SELECT * FROM ab_bars WHERE ticker='BBB' AND day=?", D2)
    assert bb["status"] == "MISSING" and json.loads(bb["source_evidence"])["error"] == "BUDGET_EXHAUSTED"
    assert one(shadow, "SELECT status FROM ab_days WHERE day=?", D2)["status"] == "DATA_INCOMPLETE"


def test_scanner_running_defers_yahoo_calls(shadow, prod, monkeypatch):
    _two_orders(shadow, prod)
    scan(prod, utc(D2, 21, 30), [])
    job(shadow, prod, Bars({}), utc(D2, 22, 30))                            # D2 결정 확정, 일봉은 마감 전 대기
    assert one(shadow, "SELECT decision_status FROM ab_day_inputs WHERE day=?", D2)["decision_status"] == "FROZEN"
    calls = []
    monkeypatch.setattr(MD, "_http_get", http({("AAA", D2): [ok("AAA", D2, A2 + (5e5,))]}, calls))
    out = run_shadow_job(now=final(D2), settings=shadow, account_provider=lambda _l: None, session_factory=prod,
                         scan_running=lambda: True)
    assert calls == [] and "Scanner 실행 중" in out["waiting"] and out["days"] == []
    assert one(shadow, "SELECT COUNT(*) n FROM ab_bars WHERE day=?", D2)["n"] == 0         # 결측으로 확정하지 않음


# ── 전체 경로 장애 주입: 수집 → 가상 주문 → 원장 → 일별 확정 → 복구 → S5 보고 ─────────

def test_full_path_fault_injection_to_s5_report_without_real_order_path(shadow, prod, monkeypatch):
    from trading import kiwoom_orders as KO
    from trading.auto_buy.shadow import report as RP

    def forbidden(*a, **k):
        raise AssertionError("SHADOW 가 실주문 경로를 호출함")
    for name in ("buy_us_limit", "sell_us_limit", "cancel_us_order", "buy_kr_limit", "sell_kr_limit", "_post"):
        monkeypatch.setattr(KO.KiwoomOrderClient, name, forbidden)
    _two_orders(shadow, prod)
    bad = {("AAA", D2): [ok("AAA", D2, A2 + (5e5,))],
           ("BBB", D2): [R(chart_payload("BBB", D2, (100.2, 100.2, 100.2, 100.2, 0)), 503)]}
    job(shadow, prod, _provider(monkeypatch, bad), final(D2))
    run1 = one(shadow, "SELECT run_id FROM ab_runs WHERE status='CURRENT'")["run_id"]
    fills = q(shadow, "SELECT o.ticker FROM ab_fill_ledger f JOIN ab_orders o ON o.id=f.order_id "
                      "WHERE o.run_id=? AND o.account_id='SV1:P7C:B00'", run1)
    assert [f["ticker"] for f in fills] == ["AAA"]                       # 미확인 BBB 는 원장에 체결 없음
    s5 = RP.summary(shadow)["s5"]
    b = s5["profiles"]["P7C"]["safety_breakdown"]
    assert b["current_unresolved"]["data_incomplete_days"] == 1 and b["current_unresolved"]["fill_unknown_orders"] >= 1
    scan(prod, utc(D3, 21, 30), [])
    good = {("AAA", D2): [ok("AAA", D2, A2 + (5e5,))], ("BBB", D2): [ok("BBB", D2, B2 + (1500,))],
            ("AAA", D3): [ok("AAA", D3, A3 + (6e5,))], ("BBB", D3): [ok("BBB", D3, B3 + (7e5,))],
            ("CCC", D3): [ok("CCC", D3, C3 + (1e5,))]}
    out = job(shadow, prod, _provider(monkeypatch, good), utc(D3, 22, 30))
    run2 = out["replay"]["run"]
    assert one(shadow, "SELECT status FROM ab_runs WHERE run_id=?", run1)["status"] == "SUPERSEDED"
    fills = {f["ticker"] for f in q(shadow, "SELECT o.ticker FROM ab_fill_ledger f JOIN ab_orders o ON o.id=f.order_id "
                                            "WHERE o.run_id=? AND o.account_id='SV1:P7C:B00'", run2)}
    assert {"AAA", "BBB"} <= fills
    s5 = RP.summary(shadow)["s5"]
    p = s5["profiles"]["P7C"]
    b = p["safety_breakdown"]
    assert b["current_unresolved"]["data_incomplete_days"] == 0 and b["current_unresolved"]["fill_unknown_orders"] == 0
    assert b["data_incomplete_days"] == 1 and b["recovered"]["data_incomplete_days"] == 1   # 복구돼도 이력 유지
    assert not p["safety_pass"]
    assert D2 in {x["day"] for x in b["continuity"]["violation_days"]}
    assert s5["verdict"] == "NOT_RECOMMENDED" and s5["b01_status"] == "NEED_MORE_DATA"
