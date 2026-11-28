"""PR #14 재리뷰(head 36e5d2f) 결함 재현 테스트 — 36e5d2f 와 현재 코드 모두에 있는 API 만 쓴다.
각 테스트는 수정 후 기대 동작을 단언하므로 36e5d2f 에서 동작 단언으로 실패해야 한다 (모듈·API 부재가 아님)."""
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tests.test_auto_buy_shadow import ACCT, D1, D2, Bars, prod, scan, shadow, sig, utc  # noqa: F401
from tests.test_auto_buy_shadow_integrity import BAR_A2, BAR_B2, _two_orders, evening, final, job, one, q
from tests.test_auto_buy_shadow_review4 import A2, chart_payload
from tests.test_auto_buy_shadow_review5_http import R, http, ok
from tests.test_auto_buy_shadow_review6_contract import variant
from trading.auto_buy import settings as S
from trading.auto_buy.shadow import market_data as MD
from trading.auto_buy.shadow.runner import _record_rate_limit, run_shadow_job
from trading.auto_buy.shadow.store import connect_rw, get_runtime, set_runtime, tx

NY = ZoneInfo("America/New_York")


def _conn(kind, path):
    return connect_rw(path) if kind == "product" else sqlite3.connect(path, isolation_level=None)


@pytest.fixture
def journal(shadow, prod):
    _two_orders(shadow, prod)
    job(shadow, prod, Bars({D2: {"AAA": BAR_A2, "BBB": BAR_B2}}), final(D2))
    return shadow.journal_path


# (1) P1 동결 입력: ab_day_inputs 의 동결 필드·최초 저장 후 계좌 필드 수정
@pytest.mark.parametrize("kind", ["product", "raw"])
@pytest.mark.parametrize("sets", ["frozen_by='edited'", "strategy_version='SV9'", "config_hash='edited'",
                                  "logic_hash='edited'", "account_reason='edited'",
                                  "account_fetched_at=account_fetched_at+1"])
def test_repro_day_input_frozen_fields_can_be_modified(journal, kind, sets):
    before = one(type("S", (), {"journal_path": journal}), "SELECT * FROM ab_day_inputs WHERE day=?", D2)
    db = _conn(kind, journal)
    try:
        with pytest.raises(sqlite3.IntegrityError):                      # 기대: 동결 필드·계좌 입력 재수정 거부
            db.execute(f"UPDATE ab_day_inputs SET {sets} WHERE day=?", (D2,))
    finally:
        db.close()
    assert one(type("S", (), {"journal_path": journal}), "SELECT * FROM ab_day_inputs WHERE day=?", D2) == before


# (2) P1 숨은 rowid: 선언된 복합 PK 는 새 값, rowid 는 기존 행 값으로 충돌 교체
@pytest.mark.parametrize("kind", ["product", "raw"])
def test_repro_hidden_rowid_replace_deletes_existing_bar(journal, kind):
    sh = type("S", (), {"journal_path": journal})
    rows = q(sh, "SELECT rowid, * FROM ab_bars ORDER BY rowid")
    assert len(rows) >= 2
    db = _conn(kind, journal)
    try:
        with pytest.raises(sqlite3.IntegrityError):                      # 기대: 기존 rowid 충돌 교체 거부
            db.execute("INSERT OR REPLACE INTO ab_bars(rowid, day, ticker, input_rev, status, provider, fetched_at, "
                       "purpose) VALUES (?,?,?,?,?,?,?,?)",
                       (rows[0]["rowid"], "2026-12-31", "FORGED", 1, "OK", "forged", 0, "DAILY"))
        with pytest.raises(sqlite3.IntegrityError):                      # 기대: 다른 행 rowid 가져가기 거부
            db.execute("UPDATE OR REPLACE ab_bars SET rowid=? WHERE rowid=?", (rows[0]["rowid"], rows[1]["rowid"]))
    finally:
        db.close()
    assert q(sh, "SELECT rowid, * FROM ab_bars ORDER BY rowid") == rows


# (3) P1 거래소 세션: 설정상 13:00 ET 폐장일의 15:00 ET + volume:0 응답
def test_repro_user_early_close_ignored_by_parser(shadow, prod, monkeypatch):
    monkeypatch.setenv("AUTO_BUY_US_EXTRA_EARLY_CLOSE_DATES", D2)
    s = S.AutoBuySettings.from_env()

    def run(now, bars=None):
        return run_shadow_job(now=now, settings=s, bars_provider=bars, account_provider=lambda _l: ACCT,
                              session_factory=prod, scan_running=lambda: False)
    scan(prod, utc(D1, 21, 30), [sig("AAA"), sig("BBB")])
    run(evening(D1), Bars({}))
    scan(prod, utc(D2, 21, 30), [])
    late = variant(chart_payload("BBB", D2, (100.2, 100.2, 100.2, 100.2, 0)),
                   ts=[int(datetime.fromisoformat(D2).replace(hour=15, tzinfo=NY).timestamp())])
    monkeypatch.setattr(MD, "_http_get", http({("AAA", D2): [ok("AAA", D2, A2 + (5e5,))], ("BBB", D2): [R(late)]}))
    run(final(D2))
    o = one(s, "SELECT * FROM ab_orders WHERE account_id='SV1:P7C:B00' AND ticker='BBB'")
    assert o["state"] == "FILL_UNKNOWN" and "ZERO_VOLUME_EXPIRED" not in (o["details"] or "")   # 기대
    assert one(s, "SELECT status FROM ab_days WHERE day=?", D2)["status"] == "DATA_INCOMPLETE"


# (4) P2 ab_eligibility: 같은 키에 다른 판단을 INSERT OR IGNORE → 조용히 숨김
@pytest.mark.parametrize("kind", ["product", "raw"])
def test_repro_conflicting_eligibility_silently_ignored(journal, kind):
    sh = type("S", (), {"journal_path": journal})
    e = one(sh, "SELECT * FROM ab_eligibility ORDER BY rowid LIMIT 1")
    db = _conn(kind, journal)
    try:
        with pytest.raises(sqlite3.IntegrityError):                      # 기대: 다른 판단은 명시적으로 거부
            db.execute("INSERT OR IGNORE INTO ab_eligibility(snapshot_id, pool, strategy_version, eligible, reasons, "
                       "decision_role, logic_hash, created_at) VALUES (?,?,?,?,?,?,?,?)",
                       (e["snapshot_id"], e["pool"], e["strategy_version"], 1 - e["eligible"], '["other"]',
                        e["decision_role"], e["logic_hash"], e["created_at"]))
    finally:
        db.close()
    assert one(sh, "SELECT * FROM ab_eligibility ORDER BY rowid LIMIT 1") == e


# (5) P2 호출 제한 카운터: 정상 수집 확인 없이 초기화
@pytest.mark.parametrize("fetch", [{"requests": 0, "halted": None, "cached": 0},
                                   {"requests": 3, "halted": None, "failures": {"HTTP_404": 3}}],
                         ids=["requests_0", "failed_without_valid"])
def test_repro_rate_limit_counter_reset_without_confirmed_collection(shadow, fetch):
    db = connect_rw(shadow.journal_path)
    try:
        with tx(db):
            set_runtime(db, "yahoo_rate_limit", {"consecutive_jobs": 2, "last_limited_at": 1.0, "warned": False})
        _record_rate_limit(db, fetch)
        assert get_runtime(db, "yahoo_rate_limit")["consecutive_jobs"] == 2          # 기대: 보존
    finally:
        db.close()
