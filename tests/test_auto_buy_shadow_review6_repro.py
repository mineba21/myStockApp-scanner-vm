"""PR #14 재리뷰(head c27d9d0) 결함 재현 테스트 — c27d9d0 와 현재 코드 모두에 있는 API 만 쓴다.
각 테스트는 수정 후 기대 동작을 단언하므로 c27d9d0 에서 실패해야 한다."""
import json
import sqlite3

import pytest
from yfinance.exceptions import YFRateLimitError

from tests.test_auto_buy_shadow import D2, job, prod, shadow  # noqa: F401
from tests.test_auto_buy_shadow_integrity import _two_orders, final, one
from tests.test_auto_buy_shadow_review4 import A2, chart_payload
from tests.test_auto_buy_shadow_review5_http import R, http, ok
from trading.auto_buy.shadow import market_data as MD
from trading.auto_buy.shadow.store import connect_rw, event, tx


def _minute(ticker, day, quote):
    p = chart_payload(ticker, day, quote)
    p["chart"]["result"][0]["meta"]["dataGranularity"] = "1m"
    return p


def test_repro_minute_granularity_parsed_as_daily_bar():
    bar, ev = MD.parse_chart(_minute("AAA", D2, (100.0, 100.0, 100.0, 100.0, 0)), "AAA", D2)
    assert bar is None                                                   # 기대: 1d 가 아니면 일봉으로 쓰지 않음


def test_repro_minute_volume_zero_creates_zero_volume_expired(shadow, prod, monkeypatch):
    _two_orders(shadow, prod)
    monkeypatch.setattr(MD, "_http_get", http({("AAA", D2): [ok("AAA", D2, A2 + (5e5,))],
                                               ("BBB", D2): [R(_minute("BBB", D2, (100.2,) * 4 + (0,)))]}))
    job(shadow, prod, MD.yfinance_bars, final(D2))
    o = one(shadow, "SELECT * FROM ab_orders WHERE account_id='SV1:P7C:B00' AND ticker='BBB'")
    assert o["state"] == "FILL_UNKNOWN" and "ZERO_VOLUME_EXPIRED" not in (o["details"] or "")


@pytest.mark.parametrize("kind", ["product", "raw"])
def test_repro_insert_or_replace_overwrites_audit_event(shadow, kind):
    db = connect_rw(shadow.journal_path)
    try:
        with tx(db):
            event(db, "ERROR", "original", "원래 감사 이벤트")
    finally:
        db.close()
    before = one(shadow, "SELECT * FROM ab_events WHERE kind='original'")
    db = connect_rw(shadow.journal_path) if kind == "product" else sqlite3.connect(shadow.journal_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):                      # 기대: 기존 감사 이벤트 교체 거부
            db.execute("INSERT OR REPLACE INTO ab_events(id, at, severity, kind, message, details) "
                       "VALUES (?, 0, 'INFO', 'forged', 'x', '{}')", (before["id"],))
    finally:
        db.close()
    assert one(shadow, "SELECT * FROM ab_events WHERE id=?", before["id"]) == before


def test_repro_yf_rate_limit_error_treated_as_generic_and_continues():
    calls = []
    table = {("AAA", D2): [YFRateLimitError()], ("BBB", D2): [ok("BBB", D2, (100.2, 101.0, 99.5, 100.6, 10))]}
    got = MD.ChartFetcher(http(table, calls))(["AAA", "BBB"], D2)
    assert not got["AAA"].code.startswith("FETCH_ERROR")                 # 기대: 호출 제한으로 식별
    assert [c[0] for c in calls] == ["AAA"]                              # 기대: 다른 종목 요청 없음
    assert json.dumps(got["BBB"].evidence, default=str).count("RATE_LIMITED")
