"""PR #14 Codex 최종 재리뷰 반영 회귀 테스트 (1) — 원본 거래량 수집 경로.

Yahoo v8 chart **원본 JSON 응답**을 HTTP 계층(``market_data._http_get``)에 주입해 실제 조회·파싱 → 저장(ab_bars) →
엔진 → 일 완료 판정·복구·재계산까지 수집 경로 전체를 검증한다. 엔진에 Bar 를 직접 넣는 테스트는 review3 에 있다.
실제 네트워크·운영 DB·주문 없음 (conftest 가 실제 Yahoo 호출을 막는다).
"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from tests.test_auto_buy_shadow import D1, D2, D3, job, scan, shadow, prod, sig, utc  # noqa: F401
from tests.test_auto_buy_shadow_integrity import _two_orders, final, one, q
from trading.auto_buy.shadow import market_data as MD
from trading.auto_buy.shadow.market_data import bars_from_download, parse_chart, yfinance_bars

NY = ZoneInfo("America/New_York")
ABSENT = object()            # volume 필드 자체가 없음


def chart_payload(ticker, day, quote, *, adj=None, meta=None, drop=(), short=()):
    """Yahoo v8 chart 원본 응답 (일봉 1개). quote = (o, h, l, c, v) — v 는 None(null)·ABSENT·숫자."""
    o, h, lo, c, v = quote
    d = datetime.fromisoformat(day).replace(hour=9, minute=30, tzinfo=NY)
    q_ = {"open": [o], "high": [h], "low": [lo], "close": [c], "volume": [v]}
    if v is ABSENT:
        del q_["volume"]
    for k in drop:
        q_.pop(k, None)
    for k in short:
        q_[k] = []
    res = {"meta": {"symbol": ticker, "exchangeTimezoneName": "America/New_York", "currency": "USD",
                    "dataGranularity": "1d", **(meta or {})},
           "timestamp": [int(d.timestamp())],
           "indicators": {"quote": [q_], "adjclose": [{"adjclose": [c if adj is None else adj]}]}}
    return {"chart": {"result": [res], "error": None}}


class Resp:
    def __init__(self, payload, status=200):
        self.content = json.dumps(payload).encode()
        self.status_code = status


def chart_http(table, calls=None):
    """table = {ticker: {day: quote | payload dict | ('HTTP', status)}} → http_get(url, params)."""
    def get(url, params, timeout=15):
        t = url.rstrip("/").rsplit("/", 1)[-1]
        day = datetime.fromtimestamp(params["period1"], NY).date().isoformat()
        if calls is not None:
            calls.append((t, day))
        spec = table.get(t, {}).get(day)
        if spec is None:
            return Resp({"chart": {"result": None, "error": {"code": "Not Found", "description": "No data found"}}},
                        404)
        if isinstance(spec, tuple) and spec and spec[0] == "HTTP":
            return Resp({}, spec[1])
        if isinstance(spec, dict):
            return Resp(spec)
        return Resp(chart_payload(t, day, spec))
    return get


def raw_provider(monkeypatch, table, calls=None):
    """운영 기본 공급자(yfinance_bars)를 그대로 쓰고 HTTP 계층만 원본 응답으로 바꾼다."""
    monkeypatch.setattr(MD, "_http_get", chart_http(table, calls))
    return MD.yfinance_bars


A2, B2 = (100.5, 103, 99, 102), (100.2, 102, 99.5, 101)
A3, B3, C3 = (102, 104, 101, 103), (101, 102, 100, 101.5), (50.2, 51, 49.8, 50.5)


# ── 파서 단위 ──────────────────────────────────────────────────────

@pytest.mark.parametrize("v,state,volume,issue", [
    (1500, "POSITIVE", 1500.0, None),
    (0, "ZERO", 0.0, None),                       # 원본에서 확인된 0
    (None, "UNKNOWN", None, "VOLUME_NULL"),        # 원본 null — yfinance DataFrame 이면 0 이 됐을 값
    (ABSENT, "UNKNOWN", None, "VOLUME_FIELD_ABSENT"),
    (-5, "UNKNOWN", None, "VOLUME_INVALID"),
    ("12", "UNKNOWN", None, "VOLUME_INVALID"),
])
def test_parse_chart_keeps_raw_null_and_zero_distinct(v, state, volume, issue):
    bar, ev = parse_chart(chart_payload("AAA", D2, (10, 11, 9.5, 10.5, v)), "AAA", D2, "abc")
    assert bar is not None and (bar.open, bar.high, bar.low, bar.close) == (10, 11, 9.5, 10.5)
    assert bar.volume_state == state and bar.volume == volume and bar.volume_source == "yahoo_chart_raw"
    assert ev["response_sha256"] == "abc" and ev["raw"]["volume"] == (None if v is ABSENT else v)
    assert (issue in ev["issues"]) if issue else not [i for i in ev["issues"] if i.startswith("VOLUME")]


def test_parse_chart_applies_auto_adjust_like_yfinance_from_same_response():
    bar, ev = parse_chart(chart_payload("AAA", D2, (20, 22, 19, 21, 900), adj=10.5), "AAA", D2)
    assert (bar.open, bar.high, bar.low, bar.close) == pytest.approx((10, 11, 9.5, 10.5))   # 비율 0.5
    assert ev["adjust_ratio"] == pytest.approx(0.5) and bar.volume == 900                   # 거래량은 원본 그대로


@pytest.mark.parametrize("kw,issue", [
    ({"short": ("close",)}, "CLOSE_ARRAY_PARTIAL"),          # 원본 응답 일부 누락
    ({"drop": ("open",)}, "OPEN_ARRAY_PARTIAL"),
    ({"meta": {"symbol": "ZZZ"}}, "SYMBOL_MISMATCH"),
])
def test_parse_chart_rejects_partial_or_foreign_response(kw, issue):
    bar, ev = parse_chart(chart_payload("AAA", D2, (10, 11, 9.5, 10.5, 100), **kw), "AAA", D2)
    assert bar is None and issue in ev["issues"]


def test_parse_chart_volume_array_partial_is_unknown_not_zero():
    bar, ev = parse_chart(chart_payload("AAA", D2, (10, 11, 9.5, 10.5, 100), short=("volume",)), "AAA", D2)
    assert bar.volume is None and bar.volume_state == "UNKNOWN" and "VOLUME_ARRAY_PARTIAL" in ev["issues"]


def test_other_day_null_ohlc_and_http_errors_give_no_bar():
    assert parse_chart(chart_payload("AAA", D3, (10, 11, 9.5, 10.5, 1)), "AAA", D2)[0] is None
    assert parse_chart(chart_payload("AAA", D2, (None, 11, 9.5, 10.5, 1)), "AAA", D2)[0] is None
    got = yfinance_bars(["AAA", "BBB", "CCC"], D2,
                        http_get=chart_http({"AAA": {D2: ("HTTP", 500)}, "BBB": {D2: A2 + (10,)}}))
    assert got["AAA"].code == "HTTP_500" and not got["AAA"].valid() and got["BBB"].volume == 10
    assert got["CCC"].code == "HTTP_404"                     # 오류 응답 본문(chart.error JSON)은 읽지 않는다


def test_dataframe_zero_is_not_confirmed_zero():
    """yfinance 변환 DataFrame 의 0 (원본 null 이었을 수 있음) 은 확정 0 이 아니다."""
    idx = pd.DatetimeIndex([D2])
    df = pd.DataFrame({"Open": [10.0], "High": [11.0], "Low": [9.5], "Close": [10.5], "Volume": [0]}, index=idx)
    b = bars_from_download(pd.concat({"AAA": df}, axis=1), ["AAA"], D2)["AAA"]
    assert b.volume == 0 and b.volume_state == "UNKNOWN" and not b.no_volume


def test_each_ticker_reads_ohlc_and_volume_from_its_own_single_response():
    calls = []
    got = yfinance_bars(["BBB", "AAA", "AAA"], D2,
                        http_get=chart_http({"AAA": {D2: A2 + (5e5,)}, "BBB": {D2: B2 + (None,)}}, calls))
    assert sorted(calls) == [("AAA", D2), ("BBB", D2)]                 # 종목당 원본 응답 1건 (중복 요청 없음)
    assert got["AAA"].volume == 5e5 and got["BBB"].volume is None
    assert got["AAA"].evidence["raw"]["close"] == 102 and got["BBB"].evidence["raw"]["close"] == 101


# ── 수집 경로 전체 (HTTP 원본 → ab_bars → 엔진) ─────────────────────────

def test_raw_zero_volume_through_collection_path_expires_order(shadow, prod, monkeypatch):
    scan(prod, utc(D1, 21, 30), [sig("AAA")])
    job(shadow, prod, raw_provider(monkeypatch, {}), utc(D1, 22, 30))
    prov = raw_provider(monkeypatch, {"AAA": {D2: (100, 100, 100, 100, 0)}})
    assert job(shadow, prod, prov, utc(D2, 22, 30))["days"] == []           # 마감 전 0 은 잠정 (대기)
    job(shadow, prod, prov, final(D2))
    b = one(shadow, "SELECT * FROM ab_bars WHERE ticker='AAA' AND day=?", D2)
    assert (b["status"], b["volume_status"], b["volume"], b["volume_source"]) == ("OK", "ZERO", 0, "yahoo_chart_raw")
    ev = json.loads(b["source_evidence"])
    assert ev["raw"]["volume"] == 0 and ev["volume_field"] == "PRESENT" and len(ev["response_sha256"]) == 64
    assert b["provider"] == "yahoo_chart_raw_v8_auto_adjust"
    orders = q(shadow, "SELECT * FROM ab_orders WHERE ticker='AAA'")
    assert orders and all(o["state"] == "UNFILLED" and json.loads(o["details"])["reason"] == "ZERO_VOLUME_EXPIRED"
                          for o in orders)
    assert one(shadow, "SELECT status FROM ab_days WHERE day=?", D2)["status"] == "COMPLETE"


@pytest.mark.parametrize("vol,issue", [(None, "VOLUME_NULL"), (ABSENT, "VOLUME_FIELD_ABSENT")])
def test_raw_null_or_absent_volume_is_unknown_then_recovered_and_replayed(shadow, prod, monkeypatch, vol, issue):
    """복수 종목 중 한 종목만 거래량 결측: 그 종목만 미확인, 다른 종목은 정상 체결. 다음 날 원본이 확인되면 재계산."""
    _two_orders(shadow, prod)
    prov = raw_provider(monkeypatch, {"AAA": {D2: A2 + (5e5,)}, "BBB": {D2: B2 + (vol,)}})
    out = job(shadow, prod, prov, utc(D2, 22, 30))
    assert out["days"] == [] and "1/2" in out["waiting"]                 # 마감 전: 미확인은 다시 받는다
    job(shadow, prod, prov, final(D2))
    bb = one(shadow, "SELECT * FROM ab_bars WHERE ticker='BBB' AND day=?", D2)
    assert (bb["status"], bb["volume_status"], bb["volume"]) == ("INVALID", "UNKNOWN", None)
    assert issue in json.loads(bb["source_evidence"])["issues"] and bb["open"] == 100.2
    ab = one(shadow, "SELECT * FROM ab_bars WHERE ticker='AAA' AND day=?", D2)
    assert (ab["status"], ab["volume_status"]) == ("OK", "POSITIVE")
    o = {r["ticker"]: r for r in q(shadow, "SELECT * FROM ab_orders WHERE account_id='SV1:P7C:B00'")}
    assert o["BBB"]["state"] == "FILL_UNKNOWN" and o["AAA"]["state"] == "FILLED"   # 0 으로 보지 않음 (만료 아님)
    day = one(shadow, "SELECT * FROM ab_days WHERE day=?", D2)
    assert day["status"] == "DATA_INCOMPLETE" and "VOLUME_UNKNOWN:1" in json.loads(day["notes"])
    # 다음 날: 같은 거래일 원본에 거래량이 확인되면 결측 복구 → 재계산
    scan(prod, utc(D3, 21, 30), [])
    prov = raw_provider(monkeypatch, {"AAA": {D2: A2 + (5e5,), D3: A3 + (6e5,)},
                                      "BBB": {D2: B2 + (1500,), D3: B3 + (7e5,)}, "CCC": {D3: C3 + (1e5,)}})
    out = job(shadow, prod, prov, utc(D3, 22, 30))
    assert out["replay"] and out["days"] == [D3]
    rec = one(shadow, "SELECT * FROM ab_bars WHERE ticker='BBB' AND day=? ORDER BY input_rev DESC LIMIT 1", D2)
    assert (rec["status"], rec["volume_status"], rec["purpose"], rec["volume"]) == ("OK", "POSITIVE", "RECOVERY", 1500)
    new = one(shadow, "SELECT * FROM ab_orders WHERE run_id=? AND account_id='SV1:P7C:B00' AND ticker='BBB'",
              out["replay"]["run"])
    assert new["state"] == "FILLED"
    assert one(shadow, "SELECT status FROM ab_days WHERE run_id=? AND day=?", out["replay"]["run"], D2)["status"] \
        == "COMPLETE"


def test_partial_raw_response_is_missing_not_zero(shadow, prod, monkeypatch):
    _two_orders(shadow, prod)
    partial = chart_payload("BBB", D2, B2 + (900,), short=("close",))          # 원본 응답 일부 누락
    prov = raw_provider(monkeypatch, {"AAA": {D2: A2 + (5e5,)}, "BBB": {D2: partial}})
    job(shadow, prod, prov, final(D2))
    bb = one(shadow, "SELECT * FROM ab_bars WHERE ticker='BBB' AND day=?", D2)
    assert bb["status"] == "MISSING" and bb["volume_status"] == "UNKNOWN"
    assert one(shadow, "SELECT state FROM ab_orders WHERE account_id='SV1:P7C:B00' AND ticker='BBB'")["state"] \
        == "FILL_UNKNOWN"


def test_dataframe_provider_zero_is_stored_unknown_and_not_expired(shadow, prod):
    """출처가 변환 DataFrame 인 0 은 확정 0 으로 저장하지 않는다 (마감 후 INVALID/UNKNOWN → FILL_UNKNOWN)."""
    scan(prod, utc(D1, 21, 30), [sig("AAA")])
    idx = pd.DatetimeIndex([D2])
    df = pd.DataFrame({"Open": [100.0], "High": [100.0], "Low": [100.0], "Close": [100.0], "Volume": [0]}, index=idx)
    frame = pd.concat({"AAA": df}, axis=1)
    prov = lambda tickers, day: bars_from_download(frame, tickers, day)  # noqa: E731
    job(shadow, prod, prov, utc(D1, 22, 30))
    job(shadow, prod, prov, final(D2))
    b = one(shadow, "SELECT * FROM ab_bars WHERE ticker='AAA' AND day=?", D2)
    assert (b["status"], b["volume_status"], b["volume_source"]) == ("INVALID", "UNKNOWN", "yfinance_dataframe")
    assert json.loads(b["source_evidence"])["reported_volume"] == 0
    assert {r["state"] for r in q(shadow, "SELECT state FROM ab_orders WHERE ticker='AAA'")} == {"FILL_UNKNOWN"}


def test_legacy_unsourced_zero_row_is_reverified_by_recovery(shadow, prod, monkeypatch):
    """ab0002 시절(출처 기록 없음)의 OK/ZERO 행은 미확정으로 보고 복구 기간 안에서 원본으로 다시 확인한다."""
    import sqlite3
    import time as _t
    from trading.auto_buy.shadow import inputs as IN
    db = sqlite3.connect(shadow.journal_path)
    db.execute("INSERT INTO ab_bars(day, ticker, input_rev, status, open, high, low, close, provider, fetched_at, "
               "purpose, volume, volume_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (D2, "AAA", 1, "OK", 10, 10, 10, 10, "yfinance_daily_auto_adjust", _t.time(), "DAILY", 0, "ZERO"))
    db.commit()
    db.row_factory = sqlite3.Row
    assert not IN.bar_complete(db.execute("SELECT * FROM ab_bars").fetchone())
    db.close()
    from trading.auto_buy.shadow.store import connect_rw
    w = connect_rw(shadow.journal_path)
    try:
        got = IN.recover_missing(w, raw_provider(monkeypatch, {"AAA": {D2: (10, 10.5, 9.8, 10.2, 3000)}}),
                                 final(D3).replace(tzinfo=ZoneInfo("UTC")), shadow)
    finally:
        w.close()
    assert got == [(D2, "AAA")]
    assert one(shadow, "SELECT volume, volume_source FROM ab_bars WHERE day=? AND input_rev=2", D2) == \
        {"volume": 3000, "volume_source": "yahoo_chart_raw"}
