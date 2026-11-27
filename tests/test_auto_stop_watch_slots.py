"""Codex 2차 리뷰 P2: position_id 기준 감시 슬롯과 최대 연속 감시 공백. 실주문 없음.

슬롯 식별자 = 거래일 + 모드 + position_id + 슬롯 시작 시각 (auto_stop_daily_stats metric='slot').
완료된 슬롯은 정확히 하나의 결과(ok / watch_fail / invalid / held / paused / missed)를 갖는다.
"""
from decimal import Decimal

import pytest

from tests.auto_stop_fakes import Harness, ts
from trading.auto_stop import service
from trading.auto_stop.quotes import QuoteUnavailable

ENV = {"KIWOOM_WEB_ENABLED": "true", "AUTO_STOP_US_MODE": "SHADOW"}
OPEN = ts(2026, 11, 23, 9, 30)
DAY = "2026-11-23"
CLASSES = ("ok", "watch_fail", "invalid", "held", "paused", "missed")


@pytest.fixture
def h(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, env=ENV, mode="SHADOW", price_interval_sec=60)
    h.clock.set(OPEN)
    return h


def add(h, ticker="CINF"):
    h.broker.holding[ticker] = {"qty": 10, "sellable": 10, "avg_cost": Decimal("60")}
    return h.add(mode="SHADOW", ticker=ticker)


def ticks(h, n, step=30):
    for _ in range(n):
        h.tick()
        h.clock.advance(step)


def report(h, now=None):
    return service.daily_report(h.store, DAY, "SHADOW", h.st, now=now or h.clock())


def pos(rep, pid):
    return [p for p in rep["watch"]["positions"] if p["id"] == pid][0]


def check_consistency(rep):
    w = rep["watch"]
    for p in w["positions"]:
        s = p["slots"]
        assert sum(s[c] for c in CLASSES) == s["expected"], p        # 완료 슬롯은 정확히 한 분류
        assert p["ok_checks"] <= p["expected_checks"]
        assert p["normal_rate_pct"] is None or p["normal_rate_pct"] <= 100
        assert p["missed_checks"] == p["expected_checks"] - p["ok_checks"]
    for c in CLASSES:
        assert w["slots"][c] == sum(p["slots"][c] for p in w["positions"])
    assert w["expected"] == sum(p["expected_checks"] for p in w["positions"])
    assert w["ok"] == sum(p["ok_checks"] for p in w["positions"])


def test_in_progress_slot_reported_separately(h):
    pid = add(h)
    ticks(h, 7)                                                 # 9:30:00 ~ 9:33:00 tick, 시계 9:33:30
    rep = report(h, now=OPEN + 3 * 60 + 20)                     # 9:33:20 (슬롯 9:33 진행 중)
    p = pos(rep, pid)
    assert p["slots"]["expected"] == 3 and p["slots"]["in_progress"] is not None
    assert p["normal_rate_pct"] <= 100
    check_consistency(rep)


def test_same_ticker_reregistered_keeps_stats_separate(h):
    p1 = add(h)
    ticks(h, 10)
    h.store.update_position(p1, h.clock(), state="DISABLED", auto_paused=1)
    p2 = add(h)
    ticks(h, 10)
    rep = report(h)
    a, b = pos(rep, p1), pos(rep, p2)
    assert a["ok_checks"] == 5 and b["ok_checks"] == 5           # 이전 포지션 통계를 재사용하지 않는다
    assert a["gaps"]["successes"] == 5 and b["gaps"]["successes"] == 5
    check_consistency(rep)


def test_first_slot_failure_then_pause_keeps_failed_slot(h):
    pid = add(h)
    h.broker.quote_exc_by = {"CINF": QuoteUnavailable("x")}
    ticks(h, 2)                                                 # 첫 슬롯: 시도 2회 모두 실패
    service.set_paused(h.store, pid, True, now=h.clock())
    ticks(h, 6)
    rep = report(h)
    p = pos(rep, pid)
    assert p["slots"]["watch_fail"] == 1 and p["slots"]["paused"] == 3
    assert p["attempts"]["watch"] == 2
    check_consistency(rep)


def test_registration_and_management_end_bound_the_window(h):
    h.clock.set(OPEN + 600)
    pid = add(h)
    ticks(h, 8)                                                 # 4분
    h.store.update_position(pid, h.clock(), state="DISABLED", auto_paused=1)
    ticks(h, 8)
    rep = report(h)
    p = pos(rep, pid)
    assert p["slots"]["expected"] == 4 and p["ok_checks"] == 4
    check_consistency(rep)


def test_same_slot_retry_counts_attempts_not_slots(h):
    pid = add(h)
    h.broker.exc_seq = [None, QuoteUnavailable("x")]            # 9:30 슬롯 성공(첫 tick 은 개장 전 시세라 무효)
    ticks(h, 4)
    rep = report(h, now=OPEN + 120)
    p = pos(rep, pid)
    assert p["slots"]["expected"] == 2
    assert p["attempts"]["watch"] > p["slots"]["expected"]
    check_consistency(rep)


def test_zero_successful_watch_gap_is_whole_window(h):
    pid = add(h)
    h.broker.age_by = {"CINF": 500}                              # 종일 무효 시세
    ticks(h, 20)
    rep = report(h)
    g = pos(rep, pid)["gaps"]
    assert g["successes"] == 0 and g["max_sec"] == pytest.approx(600, abs=1)
    assert g["over_120"] == 1 and g["items"][0]["start"] == OPEN


def test_gap_over_120s_from_missing_ticks_is_normal_state(h):
    pid = add(h)
    ticks(h, 6)
    h.clock.advance(240)                                         # 4분간 tick 없음 (스케줄 누락)
    ticks(h, 6)
    rep = report(h)
    g = pos(rep, pid)["gaps"]
    assert g["max_normal_sec"] > 120 and g["normal_over_120"] == 1
    item = [i for i in g["items"] if i["duration"] > 120][0]
    assert item["normal"] is True and item["classes"].get("missed", 0) >= 3
    check_consistency(rep)


def test_fault_gap_is_not_normal_state_but_counted_in_total(h):
    pid = add(h)
    add(h, "EVRG")
    ticks(h, 4)
    h.broker.quote_exc_by = {"CINF": QuoteUnavailable("x")}
    ticks(h, 12)
    h.broker.quote_exc_by = {}
    ticks(h, 8)
    rep = report(h)
    g = pos(rep, pid)["gaps"]
    assert g["max_sec"] > 120 and g["over_120"] >= 1
    item = max(g["items"], key=lambda i: i["duration"])
    assert item["normal"] is False and item["classes"].get("held", 0) >= 1
    assert any(t["action"] == "quote_fault" for t in item["transitions"])
    check_consistency(rep)


def test_gap_from_last_success_to_close(h):
    h.clock.set(ts(2026, 11, 23, 15, 50))
    pid = add(h)
    ticks(h, 6)                                                  # 15:50 ~ 15:52:30 정상
    h.broker.quote_exc_by = {"CINF": QuoteUnavailable("x")}
    ticks(h, 4)
    h.clock.set(ts(2026, 11, 23, 16, 10))
    h.tick()                                                     # 장 마감 후 tick: 마지막 슬롯 확정
    rep = report(h, now=ts(2026, 11, 23, 16, 30))
    g = pos(rep, pid)["gaps"]
    last = max(g["items"], key=lambda i: i["end"])
    assert last["end"] == ts(2026, 11, 23, 16, 0) and last["duration"] > 120
    check_consistency(rep)


def test_restart_preserves_slots_and_success_times(h):
    pid = add(h)
    ticks(h, 6)
    before = pos(report(h), pid)
    h.engine = h.new_engine("w2")                                 # 재시작
    ticks(h, 6)
    after = pos(report(h), pid)
    assert after["ok_checks"] == before["ok_checks"] + 3
    assert after["gaps"]["successes"] == before["gaps"]["successes"] + 3
    assert after["gaps"]["max_sec"] <= 61


def test_recovery_quotes_are_not_watch_successes(h):
    pid = add(h)
    add(h, "EVRG")
    h.broker.quote_exc_by = {"CINF": QuoteUnavailable("x")}
    ticks(h, 12)
    h.broker.quote_exc_by = {}
    ticks(h, 6)
    p = pos(report(h), pid)
    assert p["attempts"]["recovery"] >= 2
    assert p["gaps"]["successes"] == p["ok_checks"]              # 성공 시각 = 일반 감시 성공 슬롯 수


def test_storage_growth_and_report_speed(h):
    import os
    import time as _t
    ids = [add(h, f"T{i}") for i in range(10)]
    size0 = os.path.getsize(h.st.journal_path)
    h.st = h.st                                                   # 60초 감시, 30초 tick
    for _ in range(120):                                          # 1시간
        h.tick()
        h.clock.advance(30)
    rows = [r for r in h.store.stats(DAY, "SHADOW") if r["metric"] in ("slot", "watch_ok_at")]
    assert 2 * 60 * len(ids) * 0.9 <= len(rows) <= 2 * 61 * len(ids)   # 종목·분당 약 2행
    started = _t.perf_counter()
    rep = report(h)
    elapsed = _t.perf_counter() - started
    assert elapsed < 5.0 and rep["watch"]["ok"] >= 59 * len(ids)
    print(f"\n[growth] rows={len(rows)} file_delta={os.path.getsize(h.st.journal_path) - size0}B report={elapsed:.3f}s")
