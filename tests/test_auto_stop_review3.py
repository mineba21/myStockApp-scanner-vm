"""Codex 3차 리뷰(2026-11-27) 반례: 주문 종류별 최종 게이트, Scanner 매수 경쟁, 정상 상태 공백, 과거 보고서 호환.

실주문 없음. 조건: 60초 감시, 30초 tick (Codex 재현과 같음).
"""
import sqlite3
from decimal import Decimal

import pytest

from tests.auto_stop_fakes import Harness, NoOrderReader, ts
from trading.auto_stop import service
from trading.auto_stop.brokers import ShadowBroker
from trading.scanner_buy_orders import ScannerBuyOrders

SHADOW_ENV = {"KIWOOM_WEB_ENABLED": "true", "AUTO_STOP_US_MODE": "SHADOW"}
DAY = "2026-11-23"


# ── 공통 대역 ──────────────────────────────────────────────────
def live(tmp_path, monkeypatch, **over):
    h = Harness(tmp_path, monkeypatch, price_interval_sec=60, **over)
    h.broker.quote_age = 5
    return h


def shadow(tmp_path, monkeypatch, **over):
    holder = {}
    h = Harness(tmp_path, monkeypatch, env=SHADOW_ENV, mode="SHADOW", shadow_replace_enabled=True,
                price_interval_sec=60, factory=lambda mode: holder["s"], **over)
    reader = h.broker = NoOrderReader(h.clock)
    reader.quote_age = 5
    holder["s"] = s = ShadowBroker(reader, "NEXT_QUOTE", h.clock)
    h.shadow_sells, h.shadow_cancels = [], []
    real_sell, real_cancel = s.sell, s.cancel
    s.sell = lambda *a: h.shadow_sells.append(a) or real_sell(*a)
    s.cancel = lambda *a: h.shadow_cancels.append(a) or real_cancel(*a)
    return h


def on_claim(h, name, fn):
    real = getattr(h.store, name)

    def wrapped(*a, **k):
        fn()
        return real(*a, **k)
    setattr(h.store, name, wrapped)


def to_replace_live(h):
    """LIVE: 원주문 → 300초 → 재주문용 취소 → 취소 확정 직전. 다음 tick 에서 REPLACE 매도가 실행된다."""
    pid = h.add()
    h.broker.price = Decimal("44")
    h.tick()
    h.clock.advance(301); h.tick()
    assert h.broker.cancels == ["1000"]
    h.broker.fill("1000", 0, state="CANCELLED")
    h.clock.advance(30)
    return pid


def scanner_row(path, *, state, broker_state=None, ticker="T", account="account1"):
    """사전 검증 이후 별도 SQLite 연결에서 Scanner 매수 행을 실제 커밋한다."""
    ScannerBuyOrders(path, broker_factory=lambda m: None)            # 기존 Scanner DDL 로 표 생성
    db = sqlite3.connect(path)
    db.execute("INSERT INTO scanner_buy_orders(id,account_profile,market,currency,scan_result_id,ticker,name,"
               "exchange,quantity,price,state,order_number,broker_state,created_at,expires_at,sent_at,updated_at,"
               "details) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (f"s-{state}-{broker_state}-{ticker}-{account}", account, "US", "USD", 1, ticker, ticker, "ND", 1,
                "40", state, "77" if state == "SUBMITTED" else None, broker_state, 0, 1e12, 0, 0, "{}"))
    db.commit()
    db.close()


# ── 반례 1 (P1): REPLACE 사전 검증 후 재주문 스위치 OFF ─────────────
def test_counterexample_replace_switch_off_after_precheck(tmp_path, monkeypatch):
    h = live(tmp_path, monkeypatch)
    pid = to_replace_live(h)
    trigger = h.event(pid)["trigger_price"]
    on_claim(h, "claim_sell", lambda: h.set(replace_enabled=False))
    h.tick()
    assert len(h.broker.sells) == 1, "재주문 스위치가 꺼진 뒤 REPLACE 매도가 전송되면 안 된다"
    ev = h.event(pid)
    assert ev["state"] == "OPEN" and ev["trigger_price"] == trigger
    assert h.store.runtime_get(f"replace_waiting:{ev['id']}")


# ── 반례 2 (P1): 사전 검증 후 Scanner 미확정 매수 커밋 ─────────────
def test_counterexample_scanner_buy_committed_after_precheck(tmp_path, monkeypatch):
    h = live(tmp_path, monkeypatch)
    pid = h.add()
    h.store.create_event(h.clock(), h.pos(pid), state="OPEN", trigger_price=Decimal("44"),
                         trigger_as_of=h.clock(), session_date=DAY)
    h.broker.price = Decimal("44")
    on_claim(h, "claim_sell", lambda: scanner_row(h.st.journal_path, state="SENDING"))
    h.tick()
    assert h.broker.sells == []


# ── 반례 3 (P2): tick 없이 일시중지·재개된 300초 공백 ───────────────
def test_counterexample_pause_resume_without_ticks_is_not_normal_gap(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, env=SHADOW_ENV, mode="SHADOW", price_interval_sec=60)
    t0 = ts(2026, 11, 23, 10, 0)
    h.clock.set(t0)
    h.broker.holding["CINF"] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
    pid = h.add(mode="SHADOW", ticker="CINF")
    for _ in range(4):
        h.tick(); h.clock.advance(30)                              # 10:00:00 ~ 10:01:30, 마지막 성공 10:01:00
    last = h.clock() - 60
    h.clock.set(last + 30); service.set_paused(h.store, pid, True, now=h.clock())
    h.clock.set(last + 240); service.set_paused(h.store, pid, False, now=h.clock())
    h.clock.set(last + 300)
    for _ in range(4):
        h.tick(); h.clock.advance(30)
    p = [x for x in service.daily_report(h.store, DAY, "SHADOW", h.st, now=h.clock())["watch"]["positions"]
         if x["id"] == pid][0]
    big = [g for g in p["gaps"]["items"] if g["duration"] >= 290]
    assert big and big[0]["normal"] is False, "일시중지 구간과 겹친 공백은 정상 상태가 아니다"
    assert p["gaps"]["max_sec"] >= 290                              # 실제 전체 공백은 그대로
    assert (p["gaps"]["max_normal_sec"] or 0) < 290


# ── 반례 4 (P2): 11월 25일 기존 보고 73/734 ─────────────────────────
def legacy_day(h, *, extra_same_ticker=False):
    """이전 버전이 남긴 11월 25일 저널 상태: 티커별 일계 집계만 있고 슬롯 기록이 없다."""
    day = "2026-11-25"
    for t in ("CINF", "EVRG"):
        h.broker.holding[t] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
    h.clock.set(ts(2026, 11, 25, 9, 0))
    c = h.add(mode="SHADOW", ticker="CINF")                       # 개장 전 등록 → 기대 390
    h.clock.set(ts(2026, 11, 25, 10, 16))
    e = h.add(mode="SHADOW", ticker="EVRG")                       # 10:16 등록 → 기대 344
    rows = [("quote_ok", "CINF", 53), ("quote_ok", "EVRG", 20), ("quote_fail", "CINF", 2), ("quote_fail", "EVRG", 4),
            ("ticks", "", 780), ("api_error", "quote", 6)]
    if extra_same_ticker:
        h.clock.set(ts(2026, 11, 25, 11, 0))
        h.store.update_position(e, h.clock(), state="DISABLED")
        h.clock.set(ts(2026, 11, 25, 12, 0))
        e2 = h.add(mode="SHADOW", ticker="EVRG")
        return day, c, e, e2, rows
    return day, c, e, None, rows


def test_counterexample_nov25_legacy_report_73_of_734(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, env=SHADOW_ENV, mode="SHADOW", price_interval_sec=60)
    day, c, e, _, rows = legacy_day(h)
    for metric, key, n in rows:
        h.store.bump(day, "SHADOW", metric, key, n)
    rep = service.daily_report(h.store, day, "SHADOW", h.st, now=ts(2026, 11, 25, 17, 0))
    w = rep["watch"]
    assert (w["ok"], w["expected"], w["missed"]) == (73, 734, 661)
    per = {p["ticker"]: p for p in w["positions"]}
    assert (per["CINF"]["ok_checks"], per["CINF"]["expected_checks"]) == (53, 390)
    assert (per["EVRG"]["ok_checks"], per["EVRG"]["expected_checks"]) == (20, 344)


# ── STEP 3: 정상 상태 공백 (감사 기록으로 상태 구간 복원) ────────────
def sh(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch, env=SHADOW_ENV, mode="SHADOW", price_interval_sec=60)


def one(h, ticker="CINF"):
    h.broker.holding[ticker] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
    return h.add(mode="SHADOW", ticker=ticker)


def gaps_of(h, pid, now=None, day=DAY):
    rep = service.daily_report(h.store, day, "SHADOW", h.st, now=now or h.clock())
    return [x for x in rep["watch"]["positions"] if x["id"] == pid][0]


def run_ticks(h, n):
    for _ in range(n):
        h.tick(); h.clock.advance(30)


def test_pause_before_open_without_ticks(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    h.clock.set(ts(2026, 11, 23, 9, 0))
    pid = one(h)
    h.tick()                                                         # 장 전 tick (형식 표식)
    service.set_paused(h.store, pid, True, now=ts(2026, 11, 23, 9, 10))
    p = gaps_of(h, pid, now=ts(2026, 11, 23, 10, 0))                 # 장중 tick 없음 (앱 중단)
    g = p["gaps"]
    assert g["state_at_start"]["paused"] is True and g["max_sec"] == pytest.approx(1800, abs=1)
    assert g["items"][0]["normal"] is False and g["max_normal_sec"] is None


def test_symbol_fault_carried_from_previous_day(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    h.clock.set(ts(2026, 11, 20, 15, 0))                              # 전 거래일(금)
    pid = one(h)
    h.store.audit(h.clock(), "engine", "quote_fault", "SHADOW:CINF", {"position_id": pid})
    h.store.runtime_set("quote_health:SHADOW", {str(pid): {"state": "FAULT", "ticker": "CINF"}}, h.clock())
    h.clock.set(ts(2026, 11, 23, 9, 0)); h.tick()
    p = gaps_of(h, pid, now=ts(2026, 11, 23, 9, 50))
    item = p["gaps"]["items"][0]
    assert p["gaps"]["state_at_start"]["fault"] is True
    assert item["normal"] is False and item["cause"] == "symbol_fault"


def test_global_hold_carried_from_previous_day(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    h.clock.set(ts(2026, 11, 20, 15, 0))
    pid = one(h)
    h.store.audit(h.clock(), "engine", "global_hold", "SHADOW", {"from": [], "to": ["quote_system"]})
    h.clock.set(ts(2026, 11, 23, 9, 0)); h.tick()
    p = gaps_of(h, pid, now=ts(2026, 11, 23, 9, 50))
    item = p["gaps"]["items"][0]
    assert p["gaps"]["state_at_start"]["global_hold"] is True
    assert item["normal"] is False and item["cause"] == "global_hold"


def test_multiple_transitions_in_one_gap(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    h.clock.set(ts(2026, 11, 23, 10, 0))
    pid = one(h)
    run_ticks(h, 4)
    last = h.clock() - 60
    service.set_paused(h.store, pid, True, now=last + 30)
    service.set_paused(h.store, pid, False, now=last + 60)
    h.store.audit(last + 90, "engine", "quote_fault", "SHADOW:CINF", {"position_id": pid})
    h.store.audit(last + 150, "engine", "quote_recovered", "SHADOW:CINF", {"position_id": pid})
    h.clock.set(last + 300)
    run_ticks(h, 4)
    item = [g for g in gaps_of(h, pid)["gaps"]["items"] if g["duration"] >= 290][0]
    assert item["normal"] is False and len(item["transitions"]) >= 4
    assert set(item["states"]) >= {"paused", "fault"}


def test_fault_held_until_close(tmp_path, monkeypatch):
    from trading.auto_stop.quotes import QuoteUnavailable
    h = sh(tmp_path, monkeypatch)
    h.clock.set(ts(2026, 11, 23, 15, 40))
    pid = one(h)
    run_ticks(h, 4)
    h.broker.quote_exc_by = {"CINF": QuoteUnavailable("x")}
    run_ticks(h, 30)                                                 # 마감까지 장애
    h.clock.set(ts(2026, 11, 23, 16, 5)); h.tick()
    g = gaps_of(h, pid, now=ts(2026, 11, 23, 16, 30))["gaps"]
    last = max(g["items"], key=lambda i: i["end"])
    assert last["end"] == ts(2026, 11, 23, 16, 0) and last["normal"] is False and "fault" in last["states"]


def test_fully_normal_300s_gap_is_normal(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    h.clock.set(ts(2026, 11, 23, 10, 0))
    pid = one(h)
    run_ticks(h, 4)
    h.clock.advance(270)                                             # 상태 변화 없이 tick 만 5분 없음
    run_ticks(h, 4)
    g = gaps_of(h, pid)["gaps"]
    assert g["max_normal_sec"] >= 290 and g["normal_over_120"] == 1


def test_unaudited_state_makes_normal_gap_unmeasurable(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    h.clock.set(ts(2026, 11, 23, 10, 0))
    pid = one(h)
    run_ticks(h, 4)
    h.store.update_position(pid, h.clock(), auto_paused=1)          # 감사 기록 없는 상태 변경
    p = gaps_of(h, pid)
    assert p["gaps"]["measurable"] is False and p["gaps"]["max_normal_sec"] is None
    assert any("state_unaudited" in q for q in p["data_quality"])
    assert p["gaps"]["max_sec"] is not None                          # 실제 전체 공백은 그대로


# ── STEP 4: 과거 보고서 호환 (LEGACY / SLOT_V2 / MIXED) ──────────────
def _snapshot_db(path):
    db = sqlite3.connect(path)
    out = {t: db.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
           for t in ("auto_stop_daily_stats", "auto_stop_runtime", "auto_stop_audit", "auto_stop_positions",
                     "auto_stop_alerts")}
    db.close()
    return out


def test_nov25_legacy_format_and_unmeasurable_gaps(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    day, c, e, _, rows = legacy_day(h)
    for metric, key, n in rows:
        h.store.bump(day, "SHADOW", metric, key, n)
    before = _snapshot_db(h.st.journal_path)
    rep = service.daily_report(h.store, day, "SHADOW", h.st, now=ts(2026, 11, 25, 17, 0))
    text = service.report_text(rep)
    assert rep["format"] == "LEGACY" and rep["watch"]["gaps"]["coverage"] == "none"
    for p in rep["watch"]["positions"]:
        assert p["format"] == "LEGACY" and p["gaps"]["measurable"] is False and p["gaps"]["max_sec"] is None
        assert p["slots"] is None
    assert "정상 감시 73/734" in text and "누락 661" in text
    assert _snapshot_db(h.st.journal_path) == before                  # 보고서 생성이 저널을 바꾸지 않는다


def test_legacy_same_ticker_reregistration_is_not_split(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    day, c, e, e2, rows = legacy_day(h, extra_same_ticker=True)
    for metric, key, n in rows:
        h.store.bump(day, "SHADOW", metric, key, n)
    rep = service.daily_report(h.store, day, "SHADOW", h.st, now=ts(2026, 11, 25, 17, 0))
    per = {p["id"]: p for p in rep["watch"]["positions"]}
    assert per[e]["ok_checks"] is None and per[e2]["ok_checks"] is None      # 임의 배분하지 않는다
    assert any("legacy_ticker_shared" in q for q in per[e]["data_quality"])
    agg = rep["watch"]["legacy_ticker_totals"]["EVRG"]
    assert agg["ok"] == 20 and set(agg["positions"]) == {e, e2}
    assert rep["watch"]["ok"] == 73                                   # 티커 합계는 한 번만 센다


def test_slot_v2_format_report(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    h.clock.set(ts(2026, 11, 23, 9, 30))
    one(h)
    run_ticks(h, 10)
    rep = service.daily_report(h.store, DAY, "SHADOW", h.st, now=h.clock())
    p = rep["watch"]["positions"][0]
    assert rep["format"] == "SLOT_V2" and p["format"] == "SLOT_V2" and p["ok_checks"] == 5
    assert rep["watch"]["gaps"]["coverage"] == "full"


def test_mixed_format_after_midday_version_switch(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    h.clock.set(ts(2026, 11, 23, 9, 30))
    one(h)
    h.store.bump(DAY, "SHADOW", "quote_ok", "CINF", 60)              # 이전 버전이 오전에 센 정상 감시
    h.clock.set(ts(2026, 11, 23, 11, 0))
    run_ticks(h, 10)                                                 # 11:00 부터 신규 형식
    before = _snapshot_db(h.st.journal_path)
    rep = service.daily_report(h.store, DAY, "SHADOW", h.st, now=h.clock())
    p = rep["watch"]["positions"][0]
    assert rep["format"] == "MIXED" and p["format"] == "MIXED"
    assert p["ok_checks"] == 65                                      # 일계 집계 60 + 신규 5 (중복 없음)
    assert p["expected_checks"] == int((h.clock() - ts(2026, 11, 23, 9, 30)) // 60)
    assert p["gaps"]["partial"] is True and p["gaps"]["measured_from"] == ts(2026, 11, 23, 11, 0)
    assert p["slots"]["ok"] == 5 and any("mixed_format" in q for q in p["data_quality"])
    assert rep["watch"]["gaps"]["coverage"] == "partial"
    assert _snapshot_db(h.st.journal_path) == before


def test_slot_v2_zero_success_is_zero_not_null(tmp_path, monkeypatch):
    h = sh(tmp_path, monkeypatch)
    h.clock.set(ts(2026, 11, 23, 9, 30))
    pid = one(h)
    h.broker.age_by = {"CINF": 500}
    run_ticks(h, 10)
    p = gaps_of(h, pid)
    assert p["format"] == "SLOT_V2" and p["ok_checks"] == 0 and p["gaps"]["successes"] == 0
    assert p["gaps"]["max_sec"] == pytest.approx(300, abs=1)
