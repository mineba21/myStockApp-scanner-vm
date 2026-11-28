import json
from datetime import date, datetime, timedelta, timezone

import pytest

from tests.auto_buy_us_fakes import seed_scan, utc_naive
from trading.auto_buy_us.schema import upgrade_journal
from trading.auto_buy_us.settings import Settings
from trading.auto_buy_us.signals import capture_day, signal_key
from trading.auto_buy_us.store import Store
from trading.auto_stop.us_calendar import NEW_YORK

D1, D2, D3 = date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 25)


def et(d, hh, mm=0):
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=NEW_YORK)


def fd(d, hh=16, mm=31):
    return utc_naive(et(d, hh, mm))


class Env:
    def __init__(self, store, factory, session, settings):
        self.store, self.factory, self.session, self.settings = store, factory, session, settings

    def scan(self, d, rows, start=(16, 30), end=(16, 40), **kw):
        return seed_scan(self.session, utc_naive(et(d, *start)), utc_naive(et(d, *end)), rows, **kw)

    def capture(self, d, now_et=None):
        now = (now_et or et(d, 22, 5)).timestamp()
        return capture_day(self.store, self.factory, d, now, self.settings)

    def q(self, sql, *a):
        with self.store.read() as db:
            return db.execute(sql, a).fetchall()


@pytest.fixture
def env(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from database.models import Base

    eng = create_engine("sqlite:///" + str(tmp_path / "op.db"))
    Base.metadata.create_all(eng)
    factory = sessionmaker(bind=eng, autocommit=False, autoflush=False)
    p = str(tmp_path / "j.sqlite")
    upgrade_journal(p, allow_create=True)
    s = factory()
    yield Env(Store(p), factory, s, Settings.from_env())
    s.close()
    eng.dispose()


def test_signal_key():
    assert signal_key("AAPL", "BREAKOUT", "2026-11-23") == "US:AAPL:BREAKOUT:2026-11-23"


def test_captures_scheduler_scan_rows(env):
    env.scan(D1, [{"ticker": "AAA", "strict_filter_passed": True, "first_detected_at": fd(D1), "name": "A Inc",
                   "sector_name": "Tech", "entry_warnings": '["w"]'},
                  {"ticker": "BBB", "strict_filter_passed": None}])
    assert env.capture(D1) == "CAPTURED"
    rows = env.q("SELECT * FROM abu_signals ORDER BY ticker")
    assert [r["ticker"] for r in rows] == ["AAA", "BBB"]
    a, b = rows
    assert a["strict"] == 1 and b["strict"] is None
    assert a["is_first_detection"] == 1 and a["first_strict_pass"] == 1 and a["first_pass_basis"] == "FIRST_DETECTION"
    assert a["ext_pct"] == pytest.approx((100 / 98 - 1) * 100)
    assert a["stop_pct"] == pytest.approx((1 - 92 / 100) * 100)
    assert a["strategy_version"] == "RT1" and a["entry_warnings"] == '["w"]' and a["sector_name"] == "Tech"
    assert a["first_detected_at"].endswith("+00:00")
    raw = json.loads(a["raw_json"])
    assert raw["ticker"] == "AAA" and "scan_time" in raw
    assert b["is_first_detection"] == 0 and b["first_detected_at"] is None
    assert env.q("SELECT status FROM abu_signal_days")[0]["status"] == "CAPTURED"
    assert env.q("SELECT value FROM abu_runtime WHERE key='collection_start'")


def test_manual_scan_ignored(env):
    env.scan(D1, [{"ticker": "AAA"}], triggered_by="manual")
    assert env.capture(D1) == "NO_SIGNAL_SCAN"
    assert env.q("SELECT COUNT(*) c FROM abu_signals")[0]["c"] == 0


def test_before_cutoff_pending_writes_nothing(env):
    assert env.capture(D1, et(D1, 21)) == "PENDING"
    assert env.q("SELECT COUNT(*) c FROM abu_signal_days")[0]["c"] == 0


def test_later_scan_started_marks_missed(env):
    env.scan(D1, [{"ticker": "AAA"}])
    env.scan(D1, [{"ticker": "AAA"}], start=(17, 0), end=(17, 10), triggered_by="manual")
    assert env.capture(D1) == "MISSED"
    assert env.q("SELECT COUNT(*) c FROM abu_signals")[0]["c"] == 0
    assert env.q("SELECT status FROM abu_signal_days")[0]["status"] == "MISSED"


def test_intraday_scan_not_decision(env):
    env.scan(D1, [{"ticker": "AAA"}], start=(15, 0), end=(15, 10))
    assert env.capture(D1) == "NO_SIGNAL_SCAN"


def test_rows_outside_scan_window_excluded(env):
    env.scan(D1, [{"ticker": "OLD", "scan_time": utc_naive(et(D1, 15, 0))}, {"ticker": "NEW"}])
    assert env.capture(D1) == "CAPTURED"
    assert [r["ticker"] for r in env.q("SELECT ticker FROM abu_signals")] == ["NEW"]


def test_zero_rows_still_captured(env):
    env.scan(D1, [])
    assert env.capture(D1) == "CAPTURED"


def test_non_session_day_raises(env):
    with pytest.raises(ValueError):
        env.capture(date(2026, 11, 28))


def test_idempotent(env):
    env.scan(D1, [{"ticker": "AAA"}])
    assert env.capture(D1) == "CAPTURED"
    n = env.q("SELECT COUNT(*) c FROM abu_signals")[0]["c"]
    assert env.capture(D1, et(D1, 23)) == "CAPTURED"
    assert env.q("SELECT COUNT(*) c FROM abu_signals")[0]["c"] == n == 1


def test_first_pass_prior_strict(env):
    row = {"ticker": "AAA", "strict_filter_passed": True, "first_detected_at": fd(D1)}
    env.scan(D1, [row])
    env.capture(D1)
    env.scan(D2, [row])
    assert env.capture(D2) == "CAPTURED"
    r = env.q("SELECT first_strict_pass, first_pass_basis FROM abu_signals WHERE session_day=?", D2.isoformat())[0]
    assert r["first_strict_pass"] == 0 and r["first_pass_basis"] == "PRIOR_STRICT"


def test_first_pass_no_prior_strict(env):
    env.scan(D1, [{"ticker": "AAA", "strict_filter_passed": False, "first_detected_at": fd(D1)}])
    env.capture(D1)
    env.scan(D2, [{"ticker": "AAA", "strict_filter_passed": True, "first_detected_at": fd(D1)}])
    env.capture(D2)
    r = env.q("SELECT first_strict_pass, first_pass_basis FROM abu_signals WHERE session_day=?", D2.isoformat())[0]
    assert r["first_strict_pass"] == 1 and r["first_pass_basis"] == "NO_PRIOR_STRICT"


def test_first_pass_before_collection_unknown(env):
    env.scan(D1, [{"ticker": "AAA"}])
    env.capture(D1)
    old = utc_naive(et(D1, 10))
    env.scan(D2, [{"ticker": "ZZZ", "strict_filter_passed": True, "first_detected_at": old}])
    env.capture(D2)
    r = env.q("SELECT first_strict_pass, first_pass_basis FROM abu_signals WHERE ticker='ZZZ'")[0]
    assert r["first_strict_pass"] is None and r["first_pass_basis"] == "BEFORE_COLLECTION"
    env.scan(D3, [{"ticker": "NOFD", "strict_filter_passed": True}])
    env.capture(D3)
    r = env.q("SELECT first_strict_pass, first_pass_basis FROM abu_signals WHERE ticker='NOFD'")[0]
    assert r["first_strict_pass"] is None and r["first_pass_basis"] == "BEFORE_COLLECTION"


def test_first_pass_capture_gap_unknown(env):
    env.scan(D1, [{"ticker": "AAA"}])
    env.capture(D1)
    # D2: 결정 스캔 뒤 수동 스캔 → MISSED
    env.scan(D2, [{"ticker": "AAA"}])
    env.scan(D2, [], start=(17, 0), end=(17, 5), triggered_by="manual")
    assert env.capture(D2) == "MISSED"
    # D1 20:00 ET 최초 탐지 (수집 시작 이후) → D3 에서 D2 가 MISSED 라 불명
    env.scan(D3, [{"ticker": "CCC", "strict_filter_passed": True, "first_detected_at": fd(D1, 21, 0)}])
    env.capture(D3)
    r = env.q("SELECT first_strict_pass, first_pass_basis FROM abu_signals WHERE ticker='CCC'")[0]
    assert r["first_strict_pass"] is None and r["first_pass_basis"] == "CAPTURE_GAP"


def test_operational_db_read_only(env):
    env.scan(D1, [{"ticker": "AAA"}, {"ticker": "BBB"}])
    env.scan(D1, [{"ticker": "AAA"}], start=(17, 0), end=(17, 10), triggered_by="manual")

    def snap():
        from database.models import ScanLog, ScanResult
        s = env.factory()
        try:
            return ([(r.id, r.ticker, r.scan_time, r.first_detected_at, r.price) for r in s.query(ScanResult).all()],
                    [(l.id, l.started_at, l.finished_at, l.status) for l in s.query(ScanLog).all()])
        finally:
            s.close()

    before = snap()
    env.capture(D1)
    assert snap() == before
    env.scan(D2, [{"ticker": "AAA"}])
    before = snap()
    env.capture(D2)
    assert snap() == before


def test_duplicate_ticker_keeps_strict_row_and_warns(env):
    log = env.scan(D1, [{"ticker": "DUP", "strict_filter_passed": False, "signal_type": "BREAKOUT"},
                        {"ticker": "DUP", "strict_filter_passed": True, "signal_type": "REBOUND"},
                        {"ticker": "OK"}])
    assert env.capture(D1) == "CAPTURED"
    rows = env.q("SELECT scan_result_id, strict, signal_type FROM abu_signals WHERE ticker='DUP'")
    assert len(rows) == 1 and rows[0]["strict"] == 1 and rows[0]["signal_type"] == "REBOUND"
    ev = env.q("SELECT level, kind, details_json FROM abu_system_events")
    assert len(ev) == 1 and ev[0]["level"] == "WARN" and ev[0]["kind"] == "DUPLICATE_TICKER_ROWS"
    d = json.loads(ev[0]["details_json"])
    assert d["scan_log_id"] == log.id and d["ticker"] == "DUP"
    assert d["kept_id"] == rows[0]["scan_result_id"] and len(d["dropped_ids"]) == 1
    assert d["dropped_ids"][0] < d["kept_id"]


def test_first_pass_first_detection_day_missed_is_capture_gap(env):
    env.scan(D1, [{"ticker": "AAA"}])
    env.capture(D1)
    env.scan(D2, [{"ticker": "AAA"}])
    env.scan(D2, [], start=(17, 0), end=(17, 5), triggered_by="manual")
    assert env.capture(D2) == "MISSED"
    env.scan(D3, [{"ticker": "CCC", "strict_filter_passed": True, "first_detected_at": fd(D2)}])
    env.capture(D3)
    r = env.q("SELECT first_strict_pass, first_pass_basis FROM abu_signals WHERE ticker='CCC'")[0]
    assert r["first_strict_pass"] is None and r["first_pass_basis"] == "CAPTURE_GAP"


def test_first_pass_detected_after_day_unknown(env):
    env.scan(D1, [{"ticker": "LATE", "strict_filter_passed": True, "first_detected_at": fd(D2)}])
    env.capture(D1)
    r = env.q("SELECT first_strict_pass, first_pass_basis FROM abu_signals")[0]
    assert r["first_strict_pass"] is None and r["first_pass_basis"] == "DETECTED_AFTER_DAY"
