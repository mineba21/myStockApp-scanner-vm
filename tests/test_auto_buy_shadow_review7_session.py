"""PR #14 Codex 재리뷰(head 36e5d2f) 반영 (2) — 거래소 세션 설정 일치(P1)·Yahoo 호출 제한 카운터(P2).

- runner 의 캘린더 설정(추가 휴장일·조기폐장일)이 ChartFetcher 생성부터 ``parse_chart`` 세션 확인까지 그대로 전달된다.
  설정상 13:00 ET 폐장일의 15:00 ET 타임스탬프 + volume:0 (HTTP 200) 응답은 일봉으로 쓰지 않는다 → ZERO_VOLUME_EXPIRED·COMPLETE
  없음, FILL_UNKNOWN·DATA_INCOMPLETE → 결측 복구에서도 같은 세션으로 다시 거부 → 정상 일봉 확보 후 replay 복구.
- 호출 제한 카운터는 일봉 계약을 통과한 실제 신규 응답이 있는 job 에서만 초기화한다.
HTTP 계층(``market_data._http_get``)에 응답·예외를 주입한다. 실제 네트워크·운영 DB·주문 없음.
"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from yfinance.exceptions import YFRateLimitError

from tests.test_auto_buy_shadow import ACCT, D1, D2, D3, Bars, prod, scan, shadow, sig, utc  # noqa: F401
from tests.test_auto_buy_shadow_integrity import evening, final, one, q
from tests.test_auto_buy_shadow_review4 import A2, A3, B2, B3, C3, chart_payload
from tests.test_auto_buy_shadow_review5_http import R, http, ok
from tests.test_auto_buy_shadow_review6_contract import variant
from trading.auto_buy import settings as S
from trading.auto_buy.shadow import calendar as CAL
from trading.auto_buy.shadow import market_data as MD
from trading.auto_buy.shadow.runner import _record_rate_limit, run_shadow_job
from trading.auto_buy.shadow.store import connect_rw, get_runtime, set_runtime, tx

NY = ZoneInfo("America/New_York")


def _ts(day, hh, mm=0, ss=0):
    return int(datetime.fromisoformat(day).replace(hour=hh, minute=mm, second=ss, tzinfo=NY).timestamp())


def at(day, hh, mm=0, ss=0, quote=(100.0, 101.0, 99.0, 100.5, 1000), ticker="AAA"):
    return variant(chart_payload(ticker, day, quote), ts=[_ts(day, hh, mm, ss)])


def _settings(monkeypatch, early=(), closed=()):
    monkeypatch.setenv("AUTO_BUY_US_EXTRA_EARLY_CLOSE_DATES", ",".join(early))
    monkeypatch.setenv("AUTO_BUY_US_EXTRA_CLOSED_DATES", ",".join(closed))
    return S.AutoBuySettings.from_env()


# ── 세션 경계 (단위) ───────────────────────────────────────────────────────

BOUNDARY = [  # (설정, 요청일, 시각, 수용 여부, 실패 코드)
    ("default", D2, (9, 29, 0), True, None),                 # 개장 60초 전까지 허용 (일봉 타임스탬프 여유)
    ("default", D2, (9, 28, 59), False, "BAR_TIME_OUTSIDE_SESSION"),
    ("default", D2, (16, 0, 0), True, None),
    ("default", D2, (16, 0, 1), False, "BAR_TIME_OUTSIDE_SESSION"),
    ("early", D2, (9, 30, 0), True, None),                   # 사용자 추가 조기폐장일의 정상 일봉
    ("early", D2, (13, 0, 0), True, None),
    ("early", D2, (13, 0, 1), False, "BAR_TIME_OUTSIDE_SESSION"),
    ("early", D2, (15, 0, 0), False, "BAR_TIME_OUTSIDE_SESSION"),
    ("closed", D2, (9, 30, 0), False, "DAY_NOT_TRADING_SESSION"),   # 사용자 추가 휴장일
    ("default", "2026-11-27", (9, 30, 0), True, None),       # 기본 조기폐장일 (추수감사절 다음 날, 13:00)
    ("default", "2026-11-27", (13, 0, 1), False, "BAR_TIME_OUTSIDE_SESSION"),
    ("default", "2026-11-26", (9, 30, 0), False, "DAY_NOT_TRADING_SESSION"),   # 기본 휴장일
    ("default", "2026-03-06", (9, 30, 0), True, None),       # DST 시작 전 (EST)
    ("default", "2026-03-09", (9, 30, 0), True, None),       # DST 시작 후 (EDT)
    ("default", "2026-03-09", (8, 30, 0), False, "BAR_TIME_OUTSIDE_SESSION"),  # 잘못된 오프셋으로 계산한 시각
    ("default", "2026-10-30", (9, 30, 0), True, None),       # DST 종료 전
    ("default", "2026-11-02", (9, 30, 0), True, None),       # DST 종료 후
    ("default", "2026-11-02", (10, 30, 0), True, None),
    ("default", "2026-11-02", (16, 30, 0), False, "BAR_TIME_OUTSIDE_SESSION"),
]


@pytest.mark.parametrize("cfg,day,hms,accepted,code", BOUNDARY,
                         ids=[f"{c}-{d}-{h[0]:02d}{h[1]:02d}{h[2]:02d}" for c, d, h, _, _ in BOUNDARY])
def test_session_boundaries_follow_job_calendar(monkeypatch, cfg, day, hms, accepted, code):
    s = _settings(monkeypatch, early=(D2,) if cfg == "early" else (), closed=(D2,) if cfg == "closed" else ())
    bar, ev = MD.parse_chart(at(day, *hms), "AAA", day, settings=s)
    assert (bar is not None) == accepted, ev["issues"]
    if code:
        assert ev["issues"][0] == code
    else:
        assert ev.get("session_close_et") == ("13:00" if cfg == "early" or day == "2026-11-27" else "16:00")
    sess = CAL.session(datetime.fromisoformat(day).date(), s)          # parse_chart 에 세션을 직접 넘겨도 같음
    assert (MD.parse_chart(at(day, *hms), "AAA", day, session=sess)[0] is not None) == accepted


def test_fetcher_uses_given_settings_not_default_calendar(monkeypatch):
    s = _settings(monkeypatch, early=(D2,))
    table = {("BBB", D2): [R(at(D2, 15, 0, quote=(100.2,) * 4 + (0,), ticker="BBB"))]}
    got = MD.ChartFetcher(http(table), settings=s)(["BBB"], D2)["BBB"]
    assert got.code == "BAR_TIME_OUTSIDE_SESSION"
    assert got.evidence["session_close_et"] == "13:00" and got.evidence["bar_time_et"] == "15:00"
    assert MD.ChartFetcher(http(table), settings=s).stats()["contract_ok"] == 0
    assert MD.yfinance_bars(["BBB"], D2, http(table), settings=s)["BBB"].code == "BAR_TIME_OUTSIDE_SESSION"
    default = MD.ChartFetcher(http(table))(["BBB"], D2)["BBB"]          # 기본 캘린더(16:00)로는 받아들여짐 = 수정 전 결함
    assert default.volume == 0


def test_runner_passes_job_settings_to_fetcher(shadow, prod, monkeypatch):
    seen = []
    real = MD.ChartFetcher

    class Spy(real):
        def __init__(self, *a, **k):
            seen.append(k.get("settings"))
            super().__init__(*a, **k)
    monkeypatch.setattr(MD, "ChartFetcher", Spy)
    s = _settings(monkeypatch, early=(D2,))
    run_shadow_job(now=utc(D1, 22, 0), settings=s, account_provider=lambda _l: ACCT, session_factory=prod,
                   scan_running=lambda: False)
    assert seen == [s]


# ── E2E: 설정상 조기폐장일 15:00 ET + volume:0 → 결측 → 복구에서도 같은 세션 → 정상 일봉 후 replay ────────────

def _job(s, prod, now, bars=None):
    return run_shadow_job(now=now, settings=s, bars_provider=bars, account_provider=lambda _l: ACCT,
                          session_factory=prod, scan_running=lambda: False)


def test_user_early_close_late_timestamp_volume_zero_never_expires_and_recovers(shadow, prod, monkeypatch):
    s = _settings(monkeypatch, early=(D2,))
    scan(prod, utc(D1, 21, 30), [sig("AAA"), sig("BBB")])
    _job(s, prod, evening(D1), Bars({}))
    scan(prod, utc(D2, 21, 30), [sig("CCC", signal_date=D2, price=50.0, pivot_price=49.0, stop_loss=46.0)])
    bad_bbb = R(at(D2, 15, 0, quote=(100.2,) * 4 + (0,), ticker="BBB"))
    monkeypatch.setattr(MD, "_http_get", http({("AAA", D2): [ok("AAA", D2, A2 + (5e5,))], ("BBB", D2): [bad_bbb]}))
    out = _job(s, prod, final(D2))                                       # 신규 수집 (runner 기본 ChartFetcher)
    assert out["days"] == [D2] and out["fetch"]["contract_ok"] == 1
    bb = one(s, "SELECT * FROM ab_bars WHERE ticker='BBB' AND day=?", D2)
    ev = json.loads(bb["source_evidence"])
    assert bb["status"] == "MISSING" and ev["error"] == "BAR_TIME_OUTSIDE_SESSION" and ev["session_close_et"] == "13:00"
    o = one(s, "SELECT * FROM ab_orders WHERE account_id='SV1:P7C:B00' AND ticker='BBB'")
    assert o["state"] == "FILL_UNKNOWN" and "ZERO_VOLUME" not in (o["details"] or "")
    assert not q(s, "SELECT 1 FROM ab_orders WHERE details LIKE '%ZERO_VOLUME_EXPIRED%'")
    assert one(s, "SELECT status FROM ab_days WHERE day=?", D2)["status"] == "DATA_INCOMPLETE"
    assert one(s, "SELECT state FROM ab_orders WHERE account_id='SV1:P7C:B00' AND ticker='AAA'")["state"] == "FILLED"

    scan(prod, utc(D3, 21, 30), [])
    d3 = {("AAA", D3): [ok("AAA", D3, A3 + (6e5,))], ("BBB", D3): [ok("BBB", D3, B3 + (7e5,))],
          ("CCC", D3): [ok("CCC", D3, C3 + (1e5,))]}
    monkeypatch.setattr(MD, "_http_get", http({**d3, ("BBB", D2): [bad_bbb]}))
    out = _job(s, prod, utc(D3, 22, 0))                                  # 결측 복구: 같은 세션으로 다시 거부
    assert out.get("replay") is None
    assert {r["status"] for r in q(s, "SELECT status FROM ab_bars WHERE ticker='BBB' AND day=?", D2)} == {"MISSING"}
    assert not q(s, "SELECT 1 FROM ab_days WHERE day=? AND status='COMPLETE'", D2)

    monkeypatch.setattr(MD, "_http_get", http({**d3, ("BBB", D2): [ok("BBB", D2, B2 + (1500,))]}))
    out = _job(s, prod, utc(D3, 22, 30))                                 # 정상 일봉 → replay
    run = out["replay"]["run"]
    assert one(s, "SELECT state FROM ab_orders WHERE run_id=? AND account_id='SV1:P7C:B00' AND ticker='BBB'",
               run)["state"] == "FILLED"
    assert one(s, "SELECT status FROM ab_days WHERE run_id=? AND day=?", run, D2)["status"] == "COMPLETE"


def test_changing_session_settings_does_not_reinterpret_frozen_inputs(shadow, prod, monkeypatch):
    """조기폐장 설정은 설정 해시에 들어간다. 처리 후 설정을 바꾸면 VersionMismatch 로 멈추고 동결 입력·원장은 그대로다."""
    s = _settings(monkeypatch, early=(D2,))
    scan(prod, utc(D1, 21, 30), [sig("AAA")])
    _job(s, prod, evening(D1), Bars({}))
    snap = {t: q(s, f"SELECT * FROM {t}") for t in ("ab_day_inputs", "ab_days", "ab_orders", "ab_bars", "ab_accounts")}
    s2 = _settings(monkeypatch)                                          # 조기폐장 설정 제거
    out = _job(s2, prod, evening(D1), Bars({}))
    assert out.get("error") == "VersionMismatch"
    assert {t: q(s, f"SELECT * FROM {t}") for t in snap} == snap


# ── P2: 호출 제한 카운터 ─────────────────────────────────────────────────────

def _seed_counter(path, n=2, warned=True):
    db = connect_rw(path)
    try:
        with tx(db):
            set_runtime(db, "yahoo_rate_limit", {"consecutive_jobs": n, "last_limited_at": 1.0, "warned": warned})
    finally:
        db.close()


def _counter(path):
    db = connect_rw(path)
    try:
        return get_runtime(db, "yahoo_rate_limit")
    finally:
        db.close()


PRESERVE = {
    "unknown": {},
    "requests_0": {"requests": 0, "contract_ok": 0, "halted": None},
    "cache_only": {"requests": 0, "contract_ok": 0, "cached": 4, "halted": None},
    "failed_without_valid": {"requests": 3, "contract_ok": 0, "halted": None},
    "circuit_open": {"requests": 5, "contract_ok": 0, "halted": "CIRCUIT_OPEN"},
    "budget_exhausted": {"requests": 300, "contract_ok": 0, "halted": "BUDGET_EXHAUSTED"},
}


@pytest.mark.parametrize("case", sorted(PRESERVE))
def test_counter_preserved_without_confirmed_collection(shadow, case):
    _seed_counter(shadow.journal_path)
    before = _counter(shadow.journal_path)
    db = connect_rw(shadow.journal_path)
    try:
        _record_rate_limit(db, PRESERVE[case])
    finally:
        db.close()
    assert _counter(shadow.journal_path) == before


def test_counter_reset_only_with_contract_valid_response_and_rate_limit_wins(shadow):
    _seed_counter(shadow.journal_path)
    db = connect_rw(shadow.journal_path)
    try:
        _record_rate_limit(db, {"requests": 4, "contract_ok": 3, "halted": "RATE_LIMITED"})   # 같은 job 에 둘 다 → 제한 우선
        assert get_runtime(db, "yahoo_rate_limit")["consecutive_jobs"] == 3
        _record_rate_limit(db, {"requests": 2, "contract_ok": 1, "halted": None})              # 정상 수집 확인 → 초기화
        st = get_runtime(db, "yahoo_rate_limit")
        assert st["consecutive_jobs"] == 0 and st["warned"] is False
    finally:
        db.close()


def test_contract_ok_counts_only_fresh_contract_valid_responses():
    table = {("AAA", D2): [ok("AAA", D2, (100.0, 101.0, 99.0, 100.5, None))],      # 거래량 null: 계약 통과 → 인정
             ("BBB", D2): [R(at(D2, 9, 30, ticker="BBB"))],
             ("CCC", D2): [R(variant(chart_payload("CCC", D2, (1, 1, 1, 1, 0)), meta={"dataGranularity": "1m"}))]}
    f = MD.ChartFetcher(http(table))
    got = f(["AAA", "BBB", "CCC"], D2)
    assert got["AAA"].volume is None and got["CCC"].code == "GRANULARITY_NOT_DAILY"
    assert f.stats()["contract_ok"] == 2 and f.stats()["requests"] == 3
    f(["AAA", "BBB", "CCC"], D2)                                         # 같은 job 재요청 = 캐시 → 늘지 않음
    assert f.stats()["contract_ok"] == 2 and f.stats()["requests"] == 3


def test_counter_preserved_when_scanner_busy_or_only_invalid_responses(shadow, prod, monkeypatch):
    scan(prod, utc(D1, 21, 30), [sig("AAA"), sig("BBB")])
    run_shadow_job(now=evening(D1), settings=shadow, bars_provider=Bars({}), account_provider=lambda _l: ACCT,
                   session_factory=prod, scan_running=lambda: False)
    _seed_counter(shadow.journal_path)
    before = _counter(shadow.journal_path)
    calls = []
    monkeypatch.setattr(MD, "_http_get", http({}, calls))
    out = run_shadow_job(now=final(D2), settings=shadow, account_provider=lambda _l: ACCT, session_factory=prod,
                         scan_running=lambda: True)                       # Scanner 실행 중 → Yahoo 조회 보류
    assert calls == [] and out["fetch"]["requests"] == 0 and _counter(shadow.journal_path) == before
    wrong = {(t, D2): [R(variant(chart_payload(t, D2, (100.0,) * 4 + (0,)), meta={"dataGranularity": "1m"}))]
             for t in ("AAA", "BBB")}
    monkeypatch.setattr(MD, "_http_get", http(wrong, calls))
    out = run_shadow_job(now=final(D2), settings=shadow, account_provider=lambda _l: ACCT, session_factory=prod,
                         scan_running=lambda: False)                      # HTTP 200 이지만 계약 위반만
    assert out["fetch"]["requests"] > 0 and out["fetch"]["contract_ok"] == 0
    assert _counter(shadow.journal_path) == before
    monkeypatch.setattr(MD, "_http_get", http({("AAA", D2): [YFRateLimitError()]}, calls))
    out = run_shadow_job(now=final(D2), settings=shadow, account_provider=lambda _l: ACCT, session_factory=prod,
                         scan_running=lambda: False)                      # 제한 → 증가 (복구 경로 첫 요청에서 멈춤)
    assert out["fetch"]["halted"] == "RATE_LIMITED" and _counter(shadow.journal_path)["consecutive_jobs"] == 3
