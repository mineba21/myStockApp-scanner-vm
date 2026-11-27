"""Codex 4차 리뷰(2026-11-27): Scanner 저널 객체 판정, LEGACY·SLOT_V2 혼합 집계, 과거 감시 품질 판정의 일관성.

실주문 없음. 조건: 60초 감시, 30초 tick.
"""
import sqlite3
from decimal import Decimal

import pytest

from tests.auto_stop_fakes import Harness, ts
from tests.test_auto_stop_gate_matrix import KINDS, Case
from trading.auto_stop import service
from trading.auto_stop.store import AutoStopStore, ScannerJournalUnreadable
from trading.scanner_buy_orders import ScannerBuyOrders

SHADOW_ENV = {"KIWOOM_WEB_ENABLED": "true", "AUTO_STOP_US_MODE": "SHADOW"}
DAY = "2026-11-23"
COLS = ("id TEXT PRIMARY KEY, account_profile TEXT NOT NULL, market TEXT NOT NULL, currency TEXT NOT NULL, "
        "scan_result_id INTEGER, ticker TEXT NOT NULL, name TEXT, exchange TEXT NOT NULL, quantity INTEGER NOT NULL, "
        "price TEXT NOT NULL, state TEXT NOT NULL, order_number TEXT, broker_state TEXT, created_at REAL NOT NULL, "
        "expires_at REAL NOT NULL, sent_at REAL, updated_at REAL NOT NULL, details TEXT NOT NULL")


def _insert(db, table, state="UNKNOWN", broker_state=None, ticker="T", account="account1"):
    db.execute(f"INSERT INTO {table}(id,account_profile,market,currency,scan_result_id,ticker,name,exchange,quantity,"
               "price,state,order_number,broker_state,created_at,expires_at,sent_at,updated_at,details) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (f"{state}-{ticker}-{account}-{broker_state}", account, "US", "USD", 1, ticker, ticker, "ND", 1, "40",
                state, "77", broker_state, 0, 1e12, 0, 0, "{}"))


def scanner_object(path, kind, **row):
    """사전 검증 이후 별도 연결에서 Scanner 저널 객체를 만들고 커밋한다."""
    db = sqlite3.connect(path)
    if kind in ("lower", "rows_none", "terminal", "other"):
        db.close()
        ScannerBuyOrders(path, broker_factory=lambda m: None)          # 실제 Scanner DDL (소문자)
        db = sqlite3.connect(path)
        if kind == "lower":
            _insert(db, "scanner_buy_orders")
        elif kind == "terminal":
            _insert(db, "scanner_buy_orders", state="SUBMITTED", broker_state="FILLED")
        elif kind == "other":
            _insert(db, "scanner_buy_orders", ticker="OTHER")
            _insert(db, "scanner_buy_orders", account="account4")
    elif kind == "upper":
        db.execute(f"CREATE TABLE SCANNER_BUY_ORDERS ({COLS})")
        _insert(db, "SCANNER_BUY_ORDERS")
    elif kind == "mixed":
        db.execute(f"CREATE TABLE Scanner_Buy_Orders ({COLS})")
        _insert(db, "Scanner_Buy_Orders")
    elif kind == "view":
        db.execute(f"CREATE TABLE scanner_rows_base ({COLS})")
        _insert(db, "scanner_rows_base")
        db.execute("CREATE VIEW scanner_buy_orders AS SELECT * FROM scanner_rows_base")
    elif kind == "index":
        db.execute("CREATE TABLE other_table (x)")
        db.execute("CREATE INDEX scanner_buy_orders ON other_table(x)")
    elif kind == "missing_column":
        db.execute("CREATE TABLE scanner_buy_orders (id TEXT, account_profile TEXT, ticker TEXT, state TEXT)")
        db.execute("INSERT INTO scanner_buy_orders VALUES ('a','account1','T','UNKNOWN')")
    db.commit()
    db.close()


# ── STEP 1: Scanner 저널 객체 판정 (LIVE 전 주문 경로) ─────────────────
BLOCK = ("lower", "upper", "mixed", "view", "index", "missing_column")
ALLOW = ("absent", "rows_none", "other", "terminal")


@pytest.mark.parametrize("kind", BLOCK + ALLOW)
@pytest.mark.parametrize("order_kind", KINDS)
def test_scanner_object_judgement_on_every_live_path(tmp_path, monkeypatch, order_kind, kind):
    c = Case(tmp_path, monkeypatch, order_kind, "LIVE")
    seen = {}
    if kind not in ("absent",):
        c.inject(lambda c: scanner_object(c.h.st.journal_path, kind))
    real = AutoStopStore.scanner_buy_in_flight_db

    def spy(db, ticker):
        seen["in_tx"] = db.in_transaction                           # 선점 트랜잭션 안에서 확인하는지
        return real(db, ticker)
    monkeypatch.setattr(AutoStopStore, "scanner_buy_in_flight_db", staticmethod(spy))
    c.run()
    if kind in BLOCK:
        assert c.count() == c.before, f"{kind}: sell/cancel 0회여야 한다"
    else:
        assert c.count() == c.before + 1, f"{kind}: 정상 진행"
    assert seen.get("in_tx") is True


class _FailingDb:
    """메타데이터·주문 조회 실패 흉내 (같은 db 객체 인터페이스)."""

    def __init__(self, db, fail_on):
        self.db, self.fail_on = db, fail_on

    def execute(self, sql, *a):
        if self.fail_on in sql:
            raise sqlite3.OperationalError("disk I/O error")
        return self.db.execute(sql, *a)


@pytest.mark.parametrize("fail_on", ["sqlite_master", "main.scanner_buy_orders"])
def test_metadata_or_order_query_failure_fails_closed(tmp_path, fail_on):
    path = str(tmp_path / "s.sqlite")
    ScannerBuyOrders(path, broker_factory=lambda m: None)
    db = sqlite3.connect(path)
    with pytest.raises(ScannerJournalUnreadable):
        AutoStopStore.scanner_buy_in_flight_db(_FailingDb(db, fail_on), "T")


def test_temp_object_cannot_hide_main_table(tmp_path):
    path = str(tmp_path / "s.sqlite")
    ScannerBuyOrders(path, broker_factory=lambda m: None)
    db = sqlite3.connect(path)
    _insert(db, "scanner_buy_orders")
    db.commit()
    db.execute(f"CREATE TEMP TABLE scanner_buy_orders ({COLS})")      # 비어 있는 TEMP 동명 객체
    assert AutoStopStore.scanner_buy_in_flight_db(db, "T") is True


def test_temp_object_without_main_table_is_not_trusted(tmp_path):
    path = str(tmp_path / "s.sqlite")
    db = sqlite3.connect(path)
    db.execute(f"CREATE TEMP TABLE scanner_buy_orders ({COLS})")
    _insert(db, "temp.scanner_buy_orders")
    with pytest.raises(ScannerJournalUnreadable):
        AutoStopStore.scanner_buy_in_flight_db(db, "T")


# ── STEP 2: LEGACY·SLOT_V2 혼합 집계 ────────────────────────────────
def sh(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch, env=SHADOW_ENV, mode="SHADOW", price_interval_sec=60)


def pos_of(rep, pid):
    return [p for p in rep["watch"]["positions"] if p["id"] == pid][0]


def ticks(h, n):
    for _ in range(n):
        h.tick(); h.clock.advance(30)


def legacy_pos(h, ticker, start, minutes, ok, *, end_state="DISABLED"):
    """구버전 기간의 포지션: 등록 → (구버전이 quote_ok 를 ok 회 기록) → 관리 종료."""
    h.broker.holding[ticker] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
    h.clock.set(start)
    pid = h.add(mode="SHADOW", ticker=ticker)
    h.store.bump(DAY, "SHADOW", "quote_ok", ticker, ok)
    if end_state:
        h.clock.set(start + minutes * 60)
        h.store.update_position(pid, h.clock(), state=end_state, auto_paused=1)
    return pid


def test_counterexample_a_legacy_then_slot_v2_reregistration_5_of_5(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    p1 = legacy_pos(h, "CINF", ts(2026, 11, 23, 10, 0), 2, 2)      # 구버전 2/2 후 관리 종료
    h.clock.set(ts(2026, 11, 23, 10, 5))
    p2 = h.add(mode="SHADOW", ticker="CINF")                        # 신버전 재등록
    ticks(h, 6)                                                     # 신버전 3/3
    rep = service.daily_report(h.store, DAY, "SHADOW", h.st, now=h.clock())
    w = rep["watch"]
    assert (w["ok"], w["expected"], w["missed"]) == (5, 5, 0)
    assert w["normal_rate_pct"] == 100.0
    assert (pos_of(rep, p1)["ok_checks"], pos_of(rep, p2)["ok_checks"]) == (2, 3)


def test_counterexample_b_switch_within_tolerance_keeps_legacy_success(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    start = ts(2026, 11, 23, 10, 0)
    pid = legacy_pos(h, "CINF", start, 0, 1, end_state=None)         # 구버전이 감시 시작 시각에 1회 성공
    h.clock.set(start + 25)                                         # 첫 신버전 tick 은 25초 뒤
    ticks(h, 4)                                                     # 신버전 2회
    rep = service.daily_report(h.store, DAY, "SHADOW", h.st, now=h.clock())
    p = pos_of(rep, pid)
    assert p["format"] == "MIXED" and rep["format"] == "MIXED"
    assert p["ok_checks"] == 3 and rep["watch"]["ok"] == 3
    assert rep["watch"]["gaps"]["coverage"] != "full"


# ── STEP 3: 과거 감시 품질 판정의 일관성 ─────────────────────────────
def test_counterexample_unaudited_pause_same_result_on_every_requery(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    h.broker.holding["CINF"] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
    h.clock.set(ts(2026, 11, 23, 9, 0))
    pid = h.add(mode="SHADOW", ticker="CINF")
    h.tick()                                                        # 장 전 tick
    h.store.update_position(pid, h.clock(), auto_paused=1)          # 감사 기록 없는 일시중지
    h.clock.set(ts(2026, 11, 23, 16, 5)); h.tick()                  # 장 마감 후 첫 tick (마감 처리)
    results = []
    for now in (ts(2026, 11, 23, 16, 10), ts(2026, 11, 23, 22, 30), ts(2026, 11, 24, 10, 0),
                ts(2026, 11, 30, 10, 0)):
        rep = service.daily_report(h.store, DAY, "SHADOW", h.st, now=now)
        g = pos_of(rep, pid)["gaps"]
        results.append((g["max_sec"], g["max_normal_sec"], rep["watch"]["gaps"]["coverage"],
                        rep["watch"]["gaps"]["normal_coverage"]))
    assert len(set(results)) == 1, results
    assert results[0][0] == pytest.approx(23400, abs=1) and results[0][1] is None


def _snapshot_db(path):
    db = sqlite3.connect(path)
    out = {t: db.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
           for t in ("auto_stop_daily_stats", "auto_stop_runtime", "auto_stop_audit", "auto_stop_positions")}
    db.close()
    return out


def _check_totals(rep):
    """동일 의미의 성공이 전체 집계에 두 번 들어가지 않는다: 전체 정상 = 포지션(측정 가능) + 티커 그룹 합계."""
    w = rep["watch"]
    pos_ok = sum(p["ok_checks"] for p in w["positions"] if p["ok_checks"] is not None)
    grp_ok = sum(a["ok"] for a in w["legacy_ticker_totals"].values())
    assert w["ok"] == pos_ok + grp_ok
    if w["missed"] is not None:
        assert w["missed"] == w["expected"] - w["ok"] >= 0
    assert w["normal_rate_pct"] is None or w["normal_rate_pct"] <= 100


# ── STEP 2 추가 ────────────────────────────────────────────────────
def test_legacy_mixed_and_slot_v2_on_same_ticker(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    p1 = legacy_pos(h, "CINF", ts(2026, 11, 23, 9, 30), 5, 5)        # LEGACY (구버전 중 종료)
    p2 = legacy_pos(h, "CINF", ts(2026, 11, 23, 9, 40), 0, 4, end_state=None)   # 구버전 4회 후 전환 → MIXED
    h.clock.set(ts(2026, 11, 23, 10, 0))
    ticks(h, 4)                                                     # 신버전 시작 (p2 신버전 2회)
    h.store.update_position(p2, h.clock(), state="DISABLED", auto_paused=1)
    h.clock.advance(5)
    p3 = h.add(mode="SHADOW", ticker="CINF")                        # 신버전 재등록 → SLOT_V2
    ticks(h, 4)
    rep = service.daily_report(h.store, DAY, "SHADOW", h.st, now=h.clock())
    per = {p["id"]: p for p in rep["watch"]["positions"]}
    assert (per[p1]["format"], per[p2]["format"], per[p3]["format"]) == ("LEGACY", "MIXED", "SLOT_V2")
    assert per[p1]["ok_checks"] is None and per[p2]["ok_checks"] is None         # 구버전 몫 배분 안 함
    grp = rep["watch"]["legacy_ticker_totals"]["CINF"]
    assert grp["ok"] == 9 + per[p2]["slots"]["ok"]                  # 구버전 5+4 + p2 신버전 완료 슬롯
    assert per[p3]["ok_checks"] == per[p3]["slots"]["ok"]
    _check_totals(rep)


def test_mixed_formats_on_different_tickers(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    a = legacy_pos(h, "CINF", ts(2026, 11, 23, 9, 30), 3, 3)
    b = legacy_pos(h, "EVRG", ts(2026, 11, 23, 9, 35), 0, 2, end_state=None)
    h.clock.set(ts(2026, 11, 23, 10, 0))
    c = h.add(mode="SHADOW", ticker="AEE")
    h.broker.holding["AEE"] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
    ticks(h, 6)
    rep = service.daily_report(h.store, DAY, "SHADOW", h.st, now=h.clock())
    per = {p["id"]: p for p in rep["watch"]["positions"]}
    assert (per[a]["format"], per[b]["format"], per[c]["format"]) == ("LEGACY", "MIXED", "SLOT_V2")
    assert per[a]["ok_checks"] == 3 and per[b]["ok_checks"] == 2 + per[b]["slots"]["ok"]
    _check_totals(rep)


def test_first_tick_within_tolerance_without_legacy_records_is_slot_v2(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    h.broker.holding["CINF"] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
    start = ts(2026, 11, 23, 10, 0)
    h.clock.set(start)
    pid = h.add(mode="SHADOW", ticker="CINF")
    h.clock.set(start + 25)
    ticks(h, 4)
    rep = service.daily_report(h.store, DAY, "SHADOW", h.st, now=h.clock())
    p = pos_of(rep, pid)
    assert p["format"] == "SLOT_V2" and rep["watch"]["gaps"]["coverage"] == "full"
    _check_totals(rep)


def test_in_progress_slot_success_not_double_counted(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = legacy_pos(h, "CINF", ts(2026, 11, 23, 9, 30), 0, 10, end_state=None)
    h.clock.set(ts(2026, 11, 23, 10, 0))
    ticks(h, 5)                                                     # 마지막 tick 은 진행 중 슬롯에서 성공
    rep = service.daily_report(h.store, DAY, "SHADOW", h.st, now=h.clock() - 25)
    p = pos_of(rep, pid)
    assert p["slots"]["in_progress"] is not None
    assert p["ok_checks"] == 10 + p["slots"]["ok"]                  # 진행 중 슬롯 성공은 어느 쪽에도 넣지 않음
    _check_totals(rep)


def test_close_report_for_mixed_day(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = legacy_pos(h, "CINF", ts(2026, 11, 23, 15, 30), 0, 10, end_state=None)
    h.clock.set(ts(2026, 11, 23, 15, 45))
    ticks(h, 20)
    h.clock.set(ts(2026, 11, 23, 16, 5)); h.tick()
    rep = service.daily_report(h.store, DAY, "SHADOW", h.st, now=ts(2026, 11, 23, 17, 0))
    p = pos_of(rep, pid)
    assert p["format"] == "MIXED" and p["slots"]["in_progress"] is None
    assert p["ok_checks"] == 10 + p["slots"]["ok"]
    _check_totals(rep)


def test_aggregate_mismatch_is_flagged_not_clamped(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = legacy_pos(h, "CINF", ts(2026, 11, 23, 9, 30), 0, 0, end_state=None)
    h.broker.holding["EVRG"] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
    h.clock.set(ts(2026, 11, 23, 10, 0))
    other = h.add(mode="SHADOW", ticker="EVRG")                     # 측정 가능한 다른 종목
    ticks(h, 6)
    db = sqlite3.connect(h.st.journal_path)                         # 테스트 저널: 일계가 상세보다 작은 불일치 구성
    db.execute("UPDATE auto_stop_daily_stats SET value=1 WHERE metric='quote_ok' AND key='CINF'")
    db.commit(); db.close()
    rep = service.daily_report(h.store, DAY, "SHADOW", h.st, now=h.clock())
    p = pos_of(rep, pid)
    assert p["ok_checks"] is None and p["missed_checks"] is None
    assert any("aggregate_mismatch" in q for q in p["data_quality"])
    assert rep["watch"]["ok_coverage"] == "partial" and rep["watch"]["ok_unmeasured_positions"] == 1
    assert pos_of(rep, other)["ok_checks"] is not None
    _check_totals(rep)


# ── STEP 3 추가 ────────────────────────────────────────────────────
def unaudited_pause_day(h):
    h.broker.holding["CINF"] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
    h.clock.set(ts(2026, 11, 23, 9, 0))
    pid = h.add(mode="SHADOW", ticker="CINF")
    h.tick()
    h.store.update_position(pid, h.clock(), auto_paused=1)
    h.clock.set(ts(2026, 11, 23, 16, 5)); h.tick()
    return pid


def _past_view(h, pid, now):
    rep = service.daily_report(h.store, DAY, "SHADOW", h.st, now=now)
    g = pos_of(rep, pid)["gaps"]
    return (g["max_sec"], g["max_normal_sec"], g["measurable"], rep["watch"]["gaps"]["coverage"],
            rep["watch"]["gaps"]["normal_coverage"])


def test_requery_does_not_change_journal_or_result(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    pid = unaudited_pause_day(h)
    before = _snapshot_db(h.st.journal_path)
    first = _past_view(h, pid, ts(2026, 11, 23, 16, 10))
    for now in (ts(2026, 11, 23, 22, 30), ts(2026, 11, 24, 10, 0), ts(2026, 11, 30, 10, 0)):
        assert _past_view(h, pid, now) == first
    assert _snapshot_db(h.st.journal_path) == before


@pytest.mark.parametrize("next_day", ["pause_resume", "fault_recover"])
def test_next_day_events_do_not_change_past_day(tmp_path, monkeypatch, next_day):
    h = sh(tmp_path, monkeypatch)
    pid = unaudited_pause_day(h)
    first = _past_view(h, pid, ts(2026, 11, 23, 16, 10))
    if next_day == "pause_resume":
        service.set_paused(h.store, pid, False, now=ts(2026, 11, 24, 9, 0))
        service.set_paused(h.store, pid, True, now=ts(2026, 11, 24, 11, 0))
        service.set_paused(h.store, pid, False, now=ts(2026, 11, 24, 12, 0))
    else:
        h.store.audit(ts(2026, 11, 24, 10, 0), "engine", "quote_fault", "SHADOW:CINF", {"position_id": pid})
        h.store.audit(ts(2026, 11, 24, 10, 30), "engine", "quote_recovered", "SHADOW:CINF", {"position_id": pid})
    assert _past_view(h, pid, ts(2026, 11, 24, 13, 0)) == first


def test_past_day_without_close_evidence_stays_unknown(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    h.broker.holding["CINF"] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
    h.clock.set(ts(2026, 11, 23, 10, 0))
    pid = h.add(mode="SHADOW", ticker="CINF")
    ticks(h, 4)                                                     # 마감 처리 tick 없음 (앱 중단)
    same_day = _past_view(h, pid, ts(2026, 11, 23, 16, 30))
    next_day = _past_view(h, pid, ts(2026, 11, 24, 10, 0))
    assert same_day[2] is True                                      # 당일: 현재 상태로 확인 가능
    assert next_day[1] is None and next_day[2] is False             # 다음 날: 근거 부족 → 측정 불가로 유지
    rep = service.daily_report(h.store, DAY, "SHADOW", h.st, now=ts(2026, 11, 24, 10, 0))
    assert any("state_evidence_missing" in q for q in pos_of(rep, pid)["data_quality"])


def test_partial_and_none_normal_coverage(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    for t in ("CINF", "EVRG"):
        h.broker.holding[t] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
    h.clock.set(ts(2026, 11, 23, 10, 0))
    a = h.add(mode="SHADOW", ticker="CINF")
    h.add(mode="SHADOW", ticker="EVRG")
    ticks(h, 4)
    h.store.update_position(a, h.clock(), auto_paused=1)            # CINF 만 감사 기록 없는 상태
    ticks(h, 2)
    g = service.daily_report(h.store, DAY, "SHADOW", h.st, now=h.clock())["watch"]["gaps"]
    assert g["normal_coverage"] == "partial" and (g["normal_measured_positions"], g["normal_unmeasured_positions"]) == (1, 1)
    assert g["coverage"] == "full" and g["measured_positions"] == 2


def test_actual_zero_normal_gaps_is_zero_not_null(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    h.broker.holding["CINF"] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
    h.clock.set(ts(2026, 11, 23, 10, 0))
    h.add(mode="SHADOW", ticker="CINF")
    ticks(h, 10)
    g = service.daily_report(h.store, DAY, "SHADOW", h.st, now=h.clock())["watch"]["gaps"]
    assert g["normal_over_120"] == 0 and g["over_120"] == 0 and g["normal_coverage"] == "full"


def test_legacy_report_text_and_web_do_not_show_fake_arithmetic(tmp_path, monkeypatch):
    from tests.test_auto_stop_review3 import legacy_day
    h = sh(tmp_path, monkeypatch)
    day, c, e, _, rows = legacy_day(h)
    for metric, key, n in rows:
        h.store.bump(day, "SHADOW", metric, key, n)
    rep = service.daily_report(h.store, day, "SHADOW", h.st, now=ts(2026, 11, 25, 17, 0))
    w = rep["watch"]
    assert (w["ok"], w["expected"], w["missed"]) == (73, 734, 661)
    assert w["held_slots"] is None and w["cause_unmeasured_missed"] == 661
    assert w["gaps"]["max_sec"] is None and w["gaps"]["normal_coverage"] == "none"
    assert w["gaps"]["over_120"] is None and w["gaps"]["normal_over_120"] is None
    text = service.report_text(rep)
    assert "누락 661 (세부 원인 측정 불가)" in text and "보류 미실행 0" not in text
    assert "최대 감시 공백 측정 불가" in text
    html = open("web/templates/index.html", encoding="utf-8").read()
    assert "report.watch.held_slots ?? 0" not in html and "w.held_slots ?? 0" not in html
    assert "report.watch.missed ?? '측정 불가'" in html and "세부 원인 측정 불가" in html
