"""자동매수 SHADOW (docs/auto_buy_shadow.md): 설정·Migration·불변 스냅샷·적격 판정·위험 예산·가상 계좌 엔진·API.

운영 DB 대신 메모리 SQLite(ScanResult/ScanLog), SHADOW 저널은 임시 파일. 일봉·계좌는 가짜 공급자. 실주문 없음.
"""
import json
import os
import sqlite3
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import Base, ScanLog, ScanResult
from trading.auto_buy import settings as S
from trading.auto_buy.shadow import eligibility as EL
from trading.auto_buy.shadow import engine as EN
from trading.auto_buy.shadow import report as RP
from trading.auto_buy.shadow import risk as RK
from trading.auto_buy.shadow.runner import run_shadow_job
from trading.auto_buy.shadow.schema import schema_status, upgrade_journal
from trading.auto_buy.shadow.snapshot import capture_scan_log, catch_up
from trading.auto_buy.shadow.store import SchemaNotReady, connect_rw, tx
from trading.auto_buy.shadow.vfills import Bar, check_stop, try_buy

D1, D2, D3 = "2026-11-23", "2026-11-24", "2026-11-25"     # 월·화·수 (11-26 추수감사절 휴장)


def utc(day, hh, mm=0):
    return datetime.fromisoformat(f"{day}T{hh:02d}:{mm:02d}:00")


# ── 픽스처 ─────────────────────────────────────────────────────────

@pytest.fixture
def prod():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[ScanResult.__table__, ScanLog.__table__])
    factory = sessionmaker(bind=engine)
    yield factory
    engine.dispose()


@pytest.fixture
def shadow(tmp_path, monkeypatch):
    path = str(tmp_path / "auto_buy_shadow.sqlite3")
    monkeypatch.setenv("AUTO_BUY_US_MODE", "SHADOW")
    monkeypatch.setenv("AUTO_BUY_SHADOW_JOURNAL_PATH", path)
    monkeypatch.setenv("SCANNER_ORDER_JOURNAL_PATH", str(tmp_path / "orders.sqlite3"))
    upgrade_journal(path, allow_create=True)
    return S.AutoBuySettings.from_env()


def sig(ticker, **kw):
    base = dict(ticker=ticker, name=ticker, signal_type="BREAKOUT", signal_date="2026-11-20", price=100.0,
                ma150=80.0, pivot_price=98.0, stop_loss=92.0, grade="A", signal_quality="STRONG",
                market_condition="BULL", strict_filter_passed=True, entry_warnings=None, sector_name="Technology")
    base.update(kw)
    return base


def scan(factory, started, rows, status="DONE", minutes=5, trigger="scheduler"):
    """운영 _save 와 같은 방식: 같은 (ticker, signal_date, signal_type) 행은 갱신, 없으면 first_detected_at 기록."""
    finished = started.replace(minute=started.minute + minutes)
    with factory() as s:
        log = ScanLog(market="US", status=status, started_at=started, finished_at=finished, triggered_by=trigger)
        s.add(log)
        s.flush()
        t = started.replace(minute=started.minute + 1)
        for r in rows:
            ex = s.query(ScanResult).filter_by(ticker=r["ticker"], signal_date=r["signal_date"],
                                               signal_type=r["signal_type"]).first()
            if ex:
                for k, v in r.items():
                    setattr(ex, k, v)
                ex.scan_time = t
            else:
                s.add(ScanResult(market="US", scan_time=t, first_detected_at=t, **r))
        s.commit()
        return log.id


class Bars:
    def __init__(self, table):
        self.table, self.calls = table, []

    def __call__(self, tickers, day):
        self.calls.append((tuple(tickers), day))
        return {t: _with_volume(self.table.get(day, {}).get(t)) for t in tickers}


def _with_volume(b):
    """거래량을 지정하지 않은 테스트 봉은 정상 거래(양수 거래량) 봉으로 본다. 거래량 0·미확인 시나리오는 명시한다."""
    import dataclasses
    from trading.auto_buy.shadow.vfills import Bar
    if isinstance(b, Bar) and b.volume is None:
        return dataclasses.replace(b, volume=1_000_000.0)
    return b


# 합성 계좌 (실제 계좌 관측값과 무관한 임의의 둥근 값)
ACCT = {"status": "OK", "equity": 100000.0, "cash": 40000.0,
        "holdings": [{"ticker": "XYZ", "value": 5000.0, "sector": "Energy"}], "as_of": "test"}


def job(shadow, prod, bars, now, acct=ACCT):
    return run_shadow_job(now=now, settings=shadow, bars_provider=bars, account_provider=lambda _l: acct,
                          session_factory=prod, scan_running=lambda: False)


def rows(path, q, *a):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in db.execute(q, a)]
    finally:
        db.close()


# ── 설정 ───────────────────────────────────────────────────────────

def test_mode_defaults_off_and_live_is_refused(monkeypatch, tmp_path):
    monkeypatch.delenv("AUTO_BUY_US_MODE", raising=False)
    assert S.AutoBuySettings.from_env().mode == "OFF"
    monkeypatch.setenv("AUTO_BUY_US_MODE", "LIVE")
    monkeypatch.setenv("AUTO_BUY_SHADOW_JOURNAL_PATH", str(tmp_path / "x.sqlite3"))
    s = S.AutoBuySettings.from_env()
    assert s.mode == "OFF" and s.live_requested and not s.shadow_enabled and "LIVE" in s.config_error


def test_shadow_journal_must_differ_from_order_journal(monkeypatch, tmp_path):
    p = str(tmp_path / "same.sqlite3")
    monkeypatch.setenv("AUTO_BUY_US_MODE", "SHADOW")
    monkeypatch.setenv("AUTO_BUY_SHADOW_JOURNAL_PATH", p)
    monkeypatch.setenv("SCANNER_ORDER_JOURNAL_PATH", p)
    assert not S.AutoBuySettings.from_env().shadow_enabled
    monkeypatch.setenv("AUTO_BUY_SHADOW_JOURNAL_PATH", "relative.sqlite3")
    assert "절대경로" in S.AutoBuySettings.from_env().config_error


def test_off_mode_touches_no_files(monkeypatch, tmp_path, prod):
    p = tmp_path / "never.sqlite3"
    monkeypatch.setenv("AUTO_BUY_US_MODE", "OFF")
    monkeypatch.setenv("AUTO_BUY_SHADOW_JOURNAL_PATH", str(p))
    sid = scan(prod, utc(D1, 21, 30), [sig("AAA")])
    assert run_shadow_job(session_factory=prod)["skipped"] == "mode=OFF"
    assert capture_scan_log(sid, session_factory=prod)["skipped"]
    assert RP.health()["schema_ready"] is False
    assert not p.exists()


def test_shadow_without_migration_does_not_create_journal(monkeypatch, tmp_path, prod):
    p = tmp_path / "missing.sqlite3"
    monkeypatch.setenv("AUTO_BUY_US_MODE", "SHADOW")
    monkeypatch.setenv("AUTO_BUY_SHADOW_JOURNAL_PATH", str(p))
    assert "schema_not_ready" in run_shadow_job(session_factory=prod)["skipped"]
    with pytest.raises(SchemaNotReady):
        connect_rw(str(p))
    assert RP.health()["migration_required"] and not p.exists()


# ── Migration ──────────────────────────────────────────────────────

def test_migration_roundtrip_and_refuses_foreign_db(tmp_path):
    p = str(tmp_path / "j.sqlite3")
    assert upgrade_journal(p, allow_create=True) == "ab0007"    # head (… → ab0005 충돌 교체 차단 → ab0006 테이블 계약)
    assert upgrade_journal(p) == "ab0007"                      # 재실행 무변경
    assert schema_status(p)["schema_ready"]
    other = str(tmp_path / "orders.sqlite3")
    sqlite3.connect(other).execute("CREATE TABLE scanner_buy_orders(id INTEGER)").connection.close()
    with pytest.raises(Exception):
        upgrade_journal(other)
    assert not schema_status(other)["schema_ready"]
    with pytest.raises(FileNotFoundError):
        upgrade_journal(str(tmp_path / "nope.sqlite3"))


def test_migrate_script_check_and_create(tmp_path, monkeypatch):
    from scripts.auto_buy_shadow_journal_migrate import Abort, run
    p = str(tmp_path / "s.sqlite3")
    monkeypatch.setenv("SCANNER_ORDER_JOURNAL_PATH", str(tmp_path / "o.sqlite3"))
    with pytest.raises(Abort) as e:
        run(p, str(tmp_path / "bk"))
    assert e.value.code == 2 and not os.path.exists(p)
    assert run(p, str(tmp_path / "bk"), allow_create=True)["result"] == "OK"
    assert run(p, str(tmp_path / "bk"))["result"] == "ALREADY_CURRENT"
    with pytest.raises(Abort):
        run(str(tmp_path / "o.sqlite3"), str(tmp_path / "bk"), allow_create=True)


# ── 불변 스냅샷 ─────────────────────────────────────────────────────

def test_snapshot_capture_rescan_keeps_old_rows_and_flags(shadow, prod):
    s1 = scan(prod, utc(D1, 21, 30), [sig("AAA"), sig("REJ", strict_filter_passed=False),
                                      sig("LATE", strict_filter_passed=False)])
    r1 = capture_scan_log(s1, settings=shadow, session_factory=prod)
    assert r1["rows"] == 3 and r1["complete"] == 1 and r1["trading_day"] == D1 and not r1["intraday"]
    assert capture_scan_log(s1, settings=shadow, session_factory=prod)["duplicate"]      # 중복 수집 없음
    s2 = scan(prod, utc(D2, 21, 30), [sig("AAA", price=103.0, stop_loss=95.0), sig("LATE")])
    capture_scan_log(s2, settings=shadow, session_factory=prod)
    snaps = rows(shadow.journal_path, "SELECT * FROM ab_signal_snapshots ORDER BY id")
    a1, rej, late1, a2, late2 = snaps
    assert a1["stop_loss"] == 92.0 and a1["is_first_detection"] == 1 and a1["first_strict_pass"] == 1
    assert a2["stop_loss"] == 95.0 and a2["first_strict_pass"] == 0 and a2["first_pass_basis"] == "PRIOR_STRICT_SNAPSHOT"
    assert rej["strict_passed"] == 0 and rej["first_strict_pass"] is None                 # 거부 행도 기록
    assert late2["is_first_detection"] == 0 and late2["first_strict_pass"] == 1           # 늦은 첫 Strict 통과
    assert a1["scan_result_id"] == a2["scan_result_id"] and a1["row_sha256"] != a2["row_sha256"]
    db = sqlite3.connect(shadow.journal_path)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE ab_signal_snapshots SET stop_loss=1 WHERE id=1")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM ab_signal_snapshots")
    db.close()


def test_catch_up_marks_overwritten_scans_incomplete_and_first_pass_unknown(shadow, prod):
    s0 = scan(prod, utc(D1, 16, 0), [sig("OLD", strict_filter_passed=False)])        # 11:00 ET 장중 (수집 시작점)
    catch_up(shadow, session_factory=prod)
    s1 = scan(prod, utc(D1, 21, 30), [sig("BBB", strict_filter_passed=False)])
    s2 = scan(prod, utc(D2, 21, 30), [sig("BBB")])                                    # s1 HOOK 실패 가정
    caps = catch_up(shadow, session_factory=prod)
    assert [c["scan_log_id"] for c in caps] == [s1, s2]
    c = {r["scan_log_id"]: r for r in rows(shadow.journal_path, "SELECT * FROM ab_scan_captures")}
    assert c[s0]["intraday"] == 1 and c[s1]["complete"] == 0 and c[s2]["complete"] == 1
    b = rows(shadow.journal_path, "SELECT * FROM ab_signal_snapshots WHERE ticker='BBB' ORDER BY id")
    # s1 은 s2 가 덮어써 행이 사라졌고 수집도 불완전 → 최초 Strict 통과 확정 불가
    assert len(b) == 1 and b[0]["first_strict_pass"] is None and b[0]["first_pass_basis"] == "CAPTURE_GAP"
    assert "FIRST_PASS_UNKNOWN" in EL.evaluate(b[0])["B00"][1]


def test_catch_up_skips_while_scan_running(shadow, prod):
    scan(prod, utc(D1, 21, 30), [sig("AAA")])
    assert catch_up(shadow, session_factory=prod, scan_running=lambda: True) == []


# ── 적격 판정 ───────────────────────────────────────────────────────

def snap(**kw):
    s = {"scan_status": "DONE", "intraday": 0, "strict_passed": 1, "first_strict_pass": 1, "is_first_detection": 1,
         "decision_price": 100.0, "stop_loss": 92.0, "stop_pct": 8.0, "ext_pct": 2.0, "market_condition": "BULL",
         "signal_type": "BREAKOUT", "entry_warnings": "[]", "grade": "B", "signal_quality": "WEAK"}
    s.update(kw)
    return s


@pytest.mark.parametrize("kw,pool,reason", [
    ({"strict_passed": None}, "B00", "STRICT_UNASSESSED"),
    ({"strict_passed": 0}, "B00", "NOT_STRICT"),
    ({"first_strict_pass": None}, "B00", "FIRST_PASS_UNKNOWN"),
    ({"first_strict_pass": 0}, "B00", "ALREADY_PASSED"),
    ({"intraday": 1}, "B00", "INTRADAY_SCAN"),
    ({"stop_loss": 101.0}, "B00", "INVALID_STOP"),
    ({"is_first_detection": 0}, "B01", "LATE_STRICT_PASS"),
    ({"market_condition": "NEUTRAL"}, "B01", "MARKET_NOT_BULL"),
    ({"ext_pct": -0.5}, "B01", "BELOW_PIVOT"),
    ({"entry_warnings": json.dumps(["추격 구간 — 피벗 대비 +6.0%"])}, "B01", "CHASE_WARNING"),
    ({"stop_pct": 15.5}, "B01", "STOP_GT_15"),
])
def test_eligibility_reasons(kw, pool, reason):
    ok, reasons = EL.evaluate(snap(**kw))[pool]
    assert not ok and reason in reasons


def test_eligibility_does_not_require_strong_grade_a_or_tight_stop():
    s = snap(grade="B", signal_quality="WEAK", stop_pct=14.0,
             entry_warnings=json.dumps(["주봉 거래량 미달", "판정 보류"]))
    assert EL.evaluate(s)["B01"] == (True, [])
    rb = snap(signal_type="REBOUND", ext_pct=None, is_first_detection=0)
    assert EL.evaluate(rb)["B00"][0] and EL.evaluate(rb)["B01"][1] == ["LATE_STRICT_PASS"]
    assert EL.evaluate(snap(signal_type="REBOUND", ext_pct=-3.0))["B01"][0]      # REBOUND 피벗 조건 없음


def test_random_rank_is_reproducible_and_seed_dependent():
    c = [{"signal_key": f"US:T{i}:BREAKOUT:2026-11-20", "ticker": f"T{i}"} for i in range(8)]
    a = [x["ticker"] for x in EL.ordered(c, "RANDOM", 3, D1)]
    assert a == [x["ticker"] for x in EL.ordered(list(reversed(c)), "RANDOM", 3, D1)]
    assert a != [x["ticker"] for x in EL.ordered(c, "RANDOM", 4, D1)]


# ── 가상 체결 / S01 ─────────────────────────────────────────────────

def test_daily_fill_model_and_stop_rules():
    assert try_buy(Bar(100, 102, 99, 101), 101.0, 92.0) == ("FILLED", 100, "open")
    assert try_buy(Bar(102, 103, 100.5, 101), 101.0, 92.0) == ("FILLED", 101.0, "limit")
    assert try_buy(Bar(102, 103, 101.5, 102), 101.0, 92.0)[0] == "UNFILLED"
    assert try_buy(Bar(94, 95, 90, 94), 101.0, 92.0)[0] == "SKIP_OPEN_STOP"           # R < 3%
    assert check_stop(Bar(100, 101, 91, 95), 92.0, day0=True) == ("STOP_DAY0", 92.0)
    assert check_stop(Bar(90, 93, 89, 91), 92.0, day0=False) == ("STOP_GAP", 90)
    assert check_stop(Bar(95, 96, 91.5, 93), 92.0, day0=False) == ("STOP", 92.0)


# ── 위험 예산 ───────────────────────────────────────────────────────

def ops_state(acct=ACCT, positions=(), res=()):
    flow = -sum(p["cost"] for p in positions)          # 가상 매수 누적 현금 흐름
    return RK.account_state(S.PROFILES["OPS"], list(positions), list(res), flow, 0.0, None, acct)


def test_entry_heat_vs_current_heat_and_current_heat_never_blocks():
    prof = S.PROFILES["P7C"]
    pos = [{"ticker": "W", "sector": "Tech", "qty": 100, "entry_price": 100.0, "stop_price": 92.0, "cost": 10050.0,
            "close": 200.0}]
    st = RK.account_state(prof, pos, [], 39950.0, 0.0, 50000.0, None)
    assert st["entry_heat_usd"] == pytest.approx(800.0)                        # 체결가 기준 고정
    assert st["current_heat_usd"] == pytest.approx(10800.0)                     # 평가이익 포함
    assert st["current_heat_level"] == "DANGER"
    assert RK.safety_blocks(prof, st, True, 0) == []                            # Current Heat 만으로 차단하지 않음
    qty, reason, _ = RK.size_order(prof, st, 50.0, 46.0, "Tech")
    assert reason is None and qty > 0


def test_stop_breached_open_position_keeps_entry_risk_and_blocks():
    prof = S.PROFILES["P7C"]
    pos = [{"ticker": "B", "sector": "Tech", "qty": 10, "entry_price": 100.0, "stop_price": 92.0, "cost": 1005.0,
            "close": 85.0}]
    st = RK.account_state(prof, pos, [], 49000.0, 0.0, 50000.0, None)
    assert st["current_heat_usd"] == pytest.approx(80.0) and st["entry_heat_usd"] == pytest.approx(80.0)
    assert RK.safety_blocks(prof, st, True, 0) == ["STOP_BREACHED_OPEN"]


def test_ops_sleeve_sector_and_account_rules():
    prof = S.PROFILES["OPS"]
    st = ops_state()
    assert st["sleeve_budget"] == pytest.approx(0.3 * 100000)
    a = st["acct"]
    assert a["manual_uncertain_risk"] == pytest.approx(5000 * 0.15)           # 손절가 없는 수동 보유 위험 ≠ 0
    assert a["max_stock_w"] == pytest.approx(5000 / 100000)
    qty, reason, info = RK.size_order(prof, st, 100.0, 92.0, "Technology")
    assert reason is None and qty == 25                                        # 0.75% × 30,000 / R ≈ $9
    assert RK.size_order(prof, st, 100.0, 92.0, None)[1] == "SECTOR_UNKNOWN"    # 섹터 불명 → 우회 불가
    full = ops_state(positions=[{"ticker": "Q", "sector": "Technology", "qty": 300, "entry_price": 40.0,
                                 "stop_price": 36.0, "cost": 12000.0, "close": 150.0}])   # 상승으로 sleeve 초과
    assert full["sleeve_used"] > full["sleeve_budget"] and full["acct"]["equity_as_if"] == pytest.approx(133000)
    assert RK.size_order(prof, full, 100.0, 92.0, "Energy")[1] == "SLEEVE_FULL"
    sec = ops_state(positions=[{"ticker": "Q", "sector": "Technology", "qty": 104, "entry_price": 100.0,
                                "stop_price": 92.0, "cost": 10400.0, "close": 100.0}])
    assert RK.size_order(prof, sec, 100.0, 92.0, "Technology")[1] == "SECTOR_CAP"
    poor = dict(ACCT, cash=15000.0)                                              # 현금 하한 20% 미만
    assert RK.size_order(prof, ops_state(poor), 100.0, 92.0, "Energy")[1] == "ACCOUNT_MIN_CASH"
    none = RK.account_state(prof, [], [], 0.0, 0.0, None, None)
    assert RK.safety_blocks(prof, none, False, 0) == ["ACCOUNT_SNAPSHOT_UNAVAILABLE"]


def test_p7c_sizing_matches_p7_simulator_rule():
    """P7 risk_portfolio 와 같은 입력(지정가·손절·순자산·현금 하한·Entry Heat)이면 같은 수량."""
    from scanner.sizing import calculate_position
    prof = S.PROFILES["P7C"]
    st = RK.account_state(prof, [], [{"ticker": "X", "sector": "S", "reserved_cash": 5000.0, "planned_risk": 300.0}],
                          50000.0, 0.0, 50000.0, None)
    qty, reason, info = RK.size_order(prof, st, 100.0, 92.0, "Tech")
    lim = RK.limit_price(100.0)
    slip = 1 + S.COSTS["slippage_bp"] / 1e4
    ref = calculate_position(lim, 92.0, 50000.0, (50000 - 5000 - 10000) / slip / (1 + S.COSTS["buy_commission"]),
                             300.0, "US", {k: prof[k] for k in ("RISK_PCT", "MAX_POSITION_PCT", "MAX_TOTAL_HEAT_PCT",
                                                                 "MIN_R_PCT", "MAX_R_PCT")})
    assert reason is None and qty == ref["qty"]


# ── 가상 계좌 엔진 (전체 흐름) ──────────────────────────────────────

def test_end_to_end_decision_fill_stop_idempotent(shadow, prod):
    scan(prod, utc(D1, 21, 30), [sig("AAA"), sig("BBB", entry_warnings=json.dumps(["추격 구간 — 피벗 대비 +6%"]),
                                                   grade="S"), sig("REJ", strict_filter_passed=False)])
    bars = Bars({D2: {"AAA": Bar(100.5, 103, 99, 102), "BBB": Bar(102, 103, 101.5, 102.5)},
                 D3: {"AAA": Bar(95, 96, 91, 93)}})
    r = job(shadow, prod, bars, utc(D1, 22, 0))
    assert r["days"] == [D1]
    p = shadow.journal_path
    orders = {(o["account_id"], o["ticker"]): o for o in rows(p, "SELECT * FROM ab_orders")}
    assert ("SV1:P7C:B00", "AAA") in orders and ("SV1:P7C:B00", "BBB") in orders
    assert ("SV1:P7C:B01", "AAA") in orders and ("SV1:P7C:B01", "BBB") not in orders      # 추격 경고 제외
    assert orders[("SV1:P7C:B01", "AAA")]["qty"] == 41 and orders[("SV1:OPS:B01", "AAA")]["qty"] == 25
    assert all(o["is_virtual"] == 1 and o["fill_model"] == S.FILL_MODEL for o in orders.values())
    assert "후보 확정 대기" in job(shadow, prod, bars, utc(D2, 22, 0))["waiting"]          # D2 정규 스캔 전
    scan(prod, utc(D2, 21, 40), [sig("AAA", price=103.0, stop_loss=96.0)])                   # 재스캔: 손절가 변경
    assert job(shadow, prod, bars, utc(D2, 22, 30))["days"] == [D2]
    o = {(x["account_id"], x["ticker"]): x for x in rows(p, "SELECT * FROM ab_orders")}
    assert o[("SV1:P7C:B01", "AAA")]["state"] == "FILLED" and o[("SV1:P7C:B01", "AAA")]["avg_fill_price"] == 100.5
    assert o[("SV1:P7C:B00", "BBB")]["state"] == "UNFILLED"
    pos = rows(p, "SELECT * FROM ab_positions WHERE account_id='SV1:P7C:B01'")[0]
    assert pos["stop_price"] == 92.0                                                          # 고정 손절가
    assert rows(p, "SELECT COUNT(*) n FROM ab_orders WHERE ticker='AAA' AND account_id='SV1:P7C:B01'")[0]["n"] == 1
    scan(prod, utc(D3, 21, 40), [])
    job(shadow, prod, bars, utc(D3, 22, 30))
    pos = rows(p, "SELECT * FROM ab_positions WHERE account_id='SV1:P7C:B01'")[0]
    assert pos["state"] == "CLOSED" and pos["exit_reason"] == "STOP" and pos["exit_price"] == 92.0
    assert -1.3 < pos["r_multiple"] < -0.9
    before = rows(p, "SELECT COUNT(*) n FROM ab_daily")[0]["n"]
    again = job(shadow, prod, bars, utc(D3, 23, 0))
    assert again["days"] == [] and rows(p, "SELECT COUNT(*) n FROM ab_daily")[0]["n"] == before   # 재실행 불변
    acc = rows(p, "SELECT * FROM ab_account_runs WHERE account_id='SV1:P7C:B01'")[0]
    assert acc["cash"] == pytest.approx(50000 + pos["pnl"])
    fill = rows(p, "SELECT * FROM ab_fill_ledger f JOIN ab_orders o ON o.id=f.order_id "
                   "WHERE o.account_id='SV1:P7C:B01'")[0]
    assert (fill["bar_open"], fill["bar_low"], fill["bar_provider"], fill["bar_input_rev"]) == (100.5, 99, 
                                                                                                "yahoo_chart_raw_v8_auto_adjust", 1)
    assert fill["basis"] == f"{S.FILL_MODEL}:open" and fill["bar_fetched_at"] > 0
    summ = RP.summary(shadow)
    b01 = next(a for a in summ["accounts"] if a["id"] == "SV1:P7C:B01")
    assert b01["closed_trades"] == 1 and b01["stop_rate"] == 1.0 and summ["health"]["non_virtual_rows"] == 0
    assert summ["b02"]["P7C"]["accounts"] == 20 and summ["validation"]["trading_days"] == 3


def test_b02_accounts_are_reproducible_random_top2(shadow, prod):
    scan(prod, utc(D1, 21, 30), [sig(f"T{i}") for i in range(6)])
    job(shadow, prod, Bars({}), utc(D1, 22, 0))
    picks = {}
    for o in rows(shadow.journal_path, "SELECT account_id, ticker FROM ab_orders WHERE account_id LIKE 'SV1:P7C:B02%'"):
        picks.setdefault(o["account_id"], set()).add(o["ticker"])
    assert len(picks) == 20 and all(len(v) == 2 for v in picks.values())
    assert len({frozenset(v) for v in picks.values()}) > 1
    expect = [s["ticker"] for s in EL.ordered([{"signal_key": f"US:T{i}:BREAKOUT:2026-11-20", "ticker": f"T{i}"}
                                               for i in range(6)], "RANDOM", 1, D1)][:2]
    assert picks["SV1:P7C:B02:s01"] == set(expect)
    assert len(rows(shadow.journal_path, "SELECT * FROM ab_orders WHERE account_id='SV1:P7C:B00N'")) == 2
    assert len(rows(shadow.journal_path, "SELECT * FROM ab_orders WHERE account_id='SV1:P7C:B00'")) == 6


def test_partial_fill_increments_and_stop_cancels_buy_remainder(shadow, prod):
    scan(prod, utc(D1, 21, 30), [sig("AAA")])
    job(shadow, prod, Bars({}), utc(D1, 22, 0))
    db = connect_rw(shadow.journal_path)
    try:
        o = db.execute("SELECT * FROM ab_orders WHERE account_id='SV1:P7C:B01'").fetchone()
        db.execute("BEGIN IMMEDIATE")
        assert EN.apply_fill(db, o["id"], 10, 100.5, D2) == 10
        assert EN.apply_fill(db, o["id"], 10, 100.5, D2) == 0                  # 같은 누적값 재전달 무시
        with pytest.raises(EN.FillAnomaly):
            EN.apply_fill(db, o["id"], 5, 100.5, D2)                          # 누적 감소 = 이상
        db.execute("COMMIT")
        r = db.execute("SELECT * FROM ab_reservations WHERE order_id=?", (o["id"],)).fetchone()
        assert r["state"] == "ACTIVE" and r["reserved_cash"] > 0
        # 잔량 미체결(PARTIAL) 상태에서 같은 날 S01 손절 (target_day 는 계약상 바꿀 수 없어 손절 처리를 직접 호출)
        pos = db.execute("SELECT * FROM ab_positions WHERE order_id=?", (o["id"],)).fetchone()
        reason, px = check_stop(Bar(95, 96, 90, 91), pos["stop_price"], pos["entry_day"] == D2)
        with tx(db):
            EN.close_position(db, pos, D2, px, reason)
        o2 = db.execute("SELECT * FROM ab_orders WHERE id=?", (o["id"],)).fetchone()
        ev = db.execute("SELECT * FROM ab_stop_events WHERE account_id='SV1:P7C:B01'").fetchone()
        assert o2["state"] == "CANCELLED_BY_STOP" and ev["qty"] == 10 and ev["cancelled_buy_qty"] == o["qty"] - 10
        assert ev["reason"] == "STOP_DAY0"                                    # 체결 당일 보수 가정
        assert db.execute("SELECT state FROM ab_reservations WHERE order_id=?", (o["id"],)).fetchone()[0] == "RELEASED"
    finally:
        db.close()


def test_ops_blocks_without_account_snapshot_and_data_gaps_are_reported(shadow, prod):
    scan(prod, utc(D1, 21, 30), [sig("AAA")])
    job(shadow, prod, Bars({}), utc(D1, 22, 0), acct=None)
    dec = rows(shadow.journal_path, "SELECT * FROM ab_decisions WHERE account_id='SV1:OPS:B01'")
    assert dec[0]["outcome"] == "BLOCKED" and dec[0]["reason"] == "ACCOUNT_SNAPSHOT_UNAVAILABLE"
    assert rows(shadow.journal_path, "SELECT * FROM ab_decisions WHERE account_id='SV1:P7C:B01'")[0]["outcome"] == "ORDERED"
    # D2 스캔·일봉 없이 D3 까지 지나면 D2 는 늦은 처리 + 결측으로 기록된다
    scan(prod, utc(D3, 21, 30), [])
    job(shadow, prod, Bars({D3: {}}), utc(D3, 22, 0))
    d = {r["day"]: r for r in rows(shadow.journal_path, "SELECT * FROM ab_days")}
    assert "NO_DECISION_SCAN" in d[D2]["notes"] and d[D2]["bars_missing"] == 1
    assert d[D2]["status"] == "DATA_INCOMPLETE" and d[D2]["account_status"] == "LATE"
    assert rows(shadow.journal_path, "SELECT state FROM ab_orders WHERE account_id='SV1:P7C:B01'")[0]["state"] == \
        "FILL_UNKNOWN"


def test_config_change_without_version_bump_is_refused(shadow, prod, monkeypatch):
    scan(prod, utc(D1, 21, 30), [sig("AAA")])
    job(shadow, prod, Bars({}), utc(D1, 22, 0))
    monkeypatch.setitem(S.PROFILES["P7C"], "RISK_PCT", 1.0)
    out = job(shadow, prod, Bars({}), utc(D1, 23, 0))
    assert out.get("error") == "VersionMismatch"
    ev = rows(shadow.journal_path, "SELECT * FROM ab_events WHERE kind='version_mismatch'")
    assert ev and "STRATEGY_VERSION" in ev[-1]["message"]


# ── API · 스케줄러 · Scanner hook ───────────────────────────────────

def test_api_is_read_only_and_reports(shadow, prod, monkeypatch):
    from fastapi.testclient import TestClient
    import web.app as webapp
    monkeypatch.setattr(webapp, "SITES_API_KEY", "k")
    c = TestClient(webapp.app, headers={"Authorization": "Bearer k"})
    scan(prod, utc(D1, 21, 30), [sig("AAA")])
    job(shadow, prod, Bars({}), utc(D1, 22, 0))
    h = c.get("/api/auto-buy/health").json()
    assert h["mode"] == "SHADOW" and h["schema_ready"] and h["non_virtual_rows"] == 0 and h["live_supported"] is False
    s = c.get("/api/auto-buy/summary").json()
    assert s["accounts"] and "가상" in s["notice"]
    assert c.get("/api/auto-buy/accounts/SV1:P7C:B01").json()["orders"][0]["is_virtual"] == 1
    assert c.get(f"/api/auto-buy/signals?day={D1}").json()["rows"][0]["ticker"] == "AAA"
    assert c.get("/api/auto-buy/accounts/..%2Fetc").status_code in (400, 404)
    from web.auto_buy_api import router
    assert {m for r in router.routes for m in r.methods} == {"GET"}        # 조회 전용 (POST/PUT/DELETE 없음)


def test_scheduler_registers_shadow_job_only_in_shadow(monkeypatch, tmp_path):
    import scheduler

    class Fake:
        def __init__(self):
            self.jobs = []

        def add_job(self, func, trigger, **kw):
            self.jobs.append(kw["id"])

    monkeypatch.setenv("AUTO_BUY_US_MODE", "OFF")
    f = Fake()
    assert scheduler._add_auto_buy_shadow_job(f) is False and f.jobs == []
    monkeypatch.setenv("AUTO_BUY_US_MODE", "LIVE")
    assert scheduler._add_auto_buy_shadow_job(f) is False and f.jobs == []
    monkeypatch.setenv("AUTO_BUY_US_MODE", "SHADOW")
    monkeypatch.setenv("AUTO_BUY_SHADOW_JOURNAL_PATH", str(tmp_path / "s.sqlite3"))
    assert scheduler._add_auto_buy_shadow_job(f) is True and f.jobs == ["auto_buy_us_shadow"]


def test_scan_hook_is_noop_when_off_and_never_raises(monkeypatch):
    from scanner import scan_engine
    monkeypatch.setenv("AUTO_BUY_US_MODE", "OFF")
    called = []
    monkeypatch.setattr("trading.auto_buy.shadow.snapshot.capture_scan_log", lambda *a, **k: called.append(a))
    scan_engine._auto_buy_snapshot_hook(1, "US")
    assert called == []
    monkeypatch.setenv("AUTO_BUY_US_MODE", "SHADOW")
    monkeypatch.setenv("AUTO_BUY_SHADOW_JOURNAL_PATH", "/nonexistent/x.sqlite3")

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr("trading.auto_buy.shadow.snapshot.capture_scan_log", boom)
    scan_engine._auto_buy_snapshot_hook(1, "US")                               # 예외가 스캔으로 새지 않음
    scan_engine._auto_buy_snapshot_hook(1, "KR")
