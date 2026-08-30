"""Step 4 entry timing controls — shadow-first behavior and opt-in gates."""
import json
import os
import sys
from copy import deepcopy
from datetime import datetime, timedelta

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _daily(closes, start="2026-08-03"):
    index = pd.bdate_range(start, periods=len(closes))
    return pd.DataFrame({
        "Open": closes,
        "High": [v + 1 for v in closes],
        "Low": [v - 1 for v in closes],
        "Close": closes,
        "Volume": [1_000_000] * len(closes),
    }, index=index)


def _signal(**overrides):
    signal = {
        "ticker": "TEST",
        "name": "Test",
        "market": "US",
        "signal_type": "BREAKOUT",
        "signal_date": "2026-08-03",
        "price": 110.0,
        "ma150": 90.0,
        "volume": 2_000_000,
        "volume_avg": 1_000_000,
        "volume_ratio": 2.0,
        "pivot_price": 100.0,
        "stop_loss": 90.0,
        "strict_price": 106.0,
        "warning_flags": [],
        "entry_warnings": [],
        "pivot_ext_pct": 6.0,
        "upthrust_failed": False,
        "cur_ext_pct": 10.0,
        "cur_stop_pct": 18.18,
        "strict_filter_passed": None,
        "filter_reasons": [],
    }
    signal.update(overrides)
    return signal


def _fresh_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from database.models import Base

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


class TestSignalEntryObservations:
    def test_pivot_extension_warning_and_boundary(self, monkeypatch):
        import config
        from scanner.entry_control import annotate_signal_entry

        monkeypatch.setattr(config, "MAX_PIVOT_EXT_PCT", 5.0)
        daily = _daily([105, 101, 102, 103])

        at_boundary = _signal(strict_price=105.0)
        annotate_signal_entry(at_boundary, daily)
        assert at_boundary["pivot_ext_pct"] == 5.0
        assert not any("추격 구간" in w for w in at_boundary["entry_warnings"])

        over = _signal(strict_price=105.01)
        annotate_signal_entry(over, daily)
        assert over["pivot_ext_pct"] == 5.01
        assert "피벗 대비 +5.0% (추격 구간)" in over["entry_warnings"]
        assert over["entry_warnings"][-1] in over["warning_flags"]

    def test_upthrust_pending_then_true_or_false(self):
        from scanner.entry_control import evaluate_upthrust

        pending = _daily([105, 99, 103])  # D + 2까지만 확정
        assert evaluate_upthrust(pending, "2026-08-03", 100.0, 3) == (None, None)

        failed = _daily([105, 101, 99, 103])
        result, failed_date = evaluate_upthrust(failed, "2026-08-03", 100.0, 3)
        assert result is True
        assert failed_date == "2026-08-05"

        held = _daily([105, 100, 100.01, 103])
        assert evaluate_upthrust(held, "2026-08-03", 100.0, 3) == (False, None)

    def test_post_signal_extremes_only_change_upthrust_observation(self, monkeypatch):
        """미래 봉은 이미 생성된 pivot/base 스냅샷을 바꿀 수 없다."""
        import config
        from scanner.entry_control import annotate_signal_entry

        monkeypatch.setattr(config, "UPTHRUST_CHECK_DAYS", 3)
        original = _signal(
            strict_price=104.0,
            pivot_price=100.0,
            base_start_date="2026-07-01",
            base_end_date="2026-07-31",
            base_high=100.0,
            base_range_low=82.0,
        )
        normal = deepcopy(original)
        extreme = deepcopy(original)
        annotate_signal_entry(normal, _daily([104, 103, 102, 101]))
        annotate_signal_entry(extreme, _daily([104, 999999, 0.01, 999999]))

        snapshot_keys = (
            "pivot_price", "base_start_date", "base_end_date",
            "base_high", "base_range_low", "strict_price", "pivot_ext_pct",
        )
        assert {k: normal[k] for k in snapshot_keys} == {
            k: extreme[k] for k in snapshot_keys
        }
        assert normal["upthrust_failed"] is False
        assert extreme["upthrust_failed"] is True


class TestEntryGateToggles:
    def test_pivot_gate_off_warns_but_on_rejects(self, monkeypatch):
        from scanner import strict_filter as sf

        monkeypatch.setattr(sf, "STRICT_WEINSTEIN_MODE", False)
        monkeypatch.setattr(sf, "MAX_PIVOT_EXT_PCT", 5.0)
        monkeypatch.setattr(sf, "UPTHRUST_AS_GATE", False)
        signal = _signal(pivot_ext_pct=5.01)

        monkeypatch.setattr(sf, "PIVOT_EXT_AS_GATE", False)
        assert sf.apply_strict_filter(signal, {}) == (True, [])
        monkeypatch.setattr(sf, "PIVOT_EXT_AS_GATE", True)
        passed, reasons = sf.apply_strict_filter(signal, {})
        assert passed is False
        assert reasons == [sf.PIVOT_EXTENSION_TOO_HIGH]

    def test_upthrust_gate_off_warns_but_on_rejects(self, monkeypatch):
        from scanner import strict_filter as sf

        monkeypatch.setattr(sf, "STRICT_WEINSTEIN_MODE", False)
        monkeypatch.setattr(sf, "PIVOT_EXT_AS_GATE", False)
        signal = _signal(pivot_ext_pct=0.0, upthrust_failed=True)

        monkeypatch.setattr(sf, "UPTHRUST_AS_GATE", False)
        assert sf.apply_strict_filter(signal, {}) == (True, [])
        monkeypatch.setattr(sf, "UPTHRUST_AS_GATE", True)
        passed, reasons = sf.apply_strict_filter(signal, {})
        assert passed is False
        assert reasons == [sf.UPTHRUST_FAILED]

    def test_alert_freshness_gate_off_notifies_on_suppresses_and_persists(self, monkeypatch):
        import config
        from scanner import strict_filter as sf
        from scanner.scan_engine import _prepare_alert_candidates, _process_signal
        from database.models import ScanResult

        monkeypatch.setattr(sf, "STRICT_WEINSTEIN_MODE", False)
        monkeypatch.setattr(sf, "PIVOT_EXT_AS_GATE", False)
        monkeypatch.setattr(sf, "UPTHRUST_AS_GATE", False)
        monkeypatch.setattr(config, "STRICT_WEINSTEIN_MODE", False)
        monkeypatch.setattr(config, "ALERT_MAX_CUR_EXT_PCT", 5.0)
        monkeypatch.setattr(config, "ALERT_MAX_CUR_STOP_PCT", 12.0)

        db = _fresh_db()
        try:
            monkeypatch.setattr(config, "ALERT_FRESHNESS_AS_GATE", False)
            off = _signal(ticker="OFF")
            assert _process_signal(db, off, "US", "BULL", object()) is True

            monkeypatch.setattr(config, "ALERT_FRESHNESS_AS_GATE", True)
            on = _signal(ticker="ON")
            assert _process_signal(db, on, "US", "BULL", object()) is True
            eligible, suppressed = _prepare_alert_candidates(db, [on])
            assert eligible == []
            assert suppressed == [on]
            assert on["_alert_suppressed"] is True
            row = db.query(ScanResult).filter(ScanResult.ticker == "ON").one()
            assert row.cur_ext_pct == 10.0
            assert row.cur_stop_pct == pytest.approx(18.18, abs=0.01)
            assert "추격 구간" in json.loads(row.entry_warnings)[0]
        finally:
            db.close()

    def test_alert_freshness_thresholds_are_strictly_greater(self, monkeypatch):
        import config
        from scanner.entry_control import annotate_alert_freshness

        monkeypatch.setattr(config, "ALERT_MAX_CUR_EXT_PCT", 5.0)
        monkeypatch.setattr(config, "ALERT_MAX_CUR_STOP_PCT", 12.0)
        boundary = _signal(price=105.0, pivot_price=100.0, stop_loss=92.4)
        annotate_alert_freshness(boundary)
        assert boundary["cur_ext_pct"] == 5.0
        assert boundary["cur_stop_pct"] == 12.0
        assert boundary["_alert_freshness_would_cut"] is False

        over = _signal(price=105.01, pivot_price=100.0, stop_loss=92.0)
        annotate_alert_freshness(over)
        assert over["_alert_freshness_would_cut"] is True
        assert any("추격 구간" in w for w in over["entry_warnings"])
        assert any("손절폭 과대" in w for w in over["entry_warnings"])

    # ── Claude Code 리뷰 반영 — cur_stop_pct 4상태(음수/0/12초과/정상) ──

    def test_cur_stop_pct_negative_warns_breach_not_excess(self, monkeypatch):
        """현재가가 이미 stop_loss 아래(음수)면 "손절가 이탈" 문구가 뜨고
        "손절폭 과대" 문구는 뜨지 않는다 — 서로 다른 심각도의 별개 경고."""
        import config
        from scanner.entry_control import annotate_alert_freshness

        monkeypatch.setattr(config, "ALERT_MAX_CUR_STOP_PCT", 12.0)
        signal = _signal(price=85.0, pivot_price=85.0, stop_loss=90.0)
        annotate_alert_freshness(signal)

        assert signal["cur_stop_pct"] == pytest.approx(-5.88, abs=0.01)
        assert signal["_alert_freshness_would_cut"] is True
        assert any("손절가 이탈" in w for w in signal["entry_warnings"])
        assert not any("손절폭 과대" in w for w in signal["entry_warnings"])
        assert "5.9%" in next(w for w in signal["entry_warnings"] if "손절가 이탈" in w)

    def test_cur_stop_pct_zero_is_boundary_no_warning(self, monkeypatch):
        """현재가 == stop_loss(정확히 0%)는 이탈도 과대도 아닌 경계값 — 무경고."""
        import config
        from scanner.entry_control import annotate_alert_freshness

        monkeypatch.setattr(config, "ALERT_MAX_CUR_STOP_PCT", 12.0)
        signal = _signal(price=100.0, pivot_price=100.0, stop_loss=100.0)
        annotate_alert_freshness(signal)

        assert signal["cur_stop_pct"] == 0.0
        assert signal["_alert_freshness_would_cut"] is False
        assert not any("손절" in w for w in signal["entry_warnings"])

    def test_cur_stop_pct_over_threshold_warns_excess_not_breach(self, monkeypatch):
        """양수이면서 기준(12%) 초과 — 기존 "손절폭 과대" 문구, "이탈" 문구는 없음."""
        import config
        from scanner.entry_control import annotate_alert_freshness

        monkeypatch.setattr(config, "ALERT_MAX_CUR_STOP_PCT", 12.0)
        signal = _signal(price=100.0, pivot_price=100.0, stop_loss=87.0)
        annotate_alert_freshness(signal)

        assert signal["cur_stop_pct"] == 13.0
        assert signal["_alert_freshness_would_cut"] is True
        assert any("손절폭 과대" in w for w in signal["entry_warnings"])
        assert not any("손절가 이탈" in w for w in signal["entry_warnings"])

    def test_cur_stop_pct_normal_range_no_warning(self, monkeypatch):
        """0% 초과 12% 이하 정상 범위 — 어떤 손절 경고도 뜨지 않는다."""
        import config
        from scanner.entry_control import annotate_alert_freshness

        monkeypatch.setattr(config, "ALERT_MAX_CUR_STOP_PCT", 12.0)
        signal = _signal(price=100.0, pivot_price=100.0, stop_loss=91.0)
        annotate_alert_freshness(signal)

        assert signal["cur_stop_pct"] == 9.0
        assert signal["_alert_freshness_would_cut"] is False
        assert not any("손절" in w for w in signal["entry_warnings"])

    def test_breakout_notification_contains_signal_current_and_warnings(self, monkeypatch):
        from scanner.scan_engine import _notify

        monkeypatch.setattr("scanner.scan_engine._sector_summary", lambda market: "")
        messages = []
        signal = _signal(
            _grade="S",
            entry_warnings=[
                "추격 구간 — 피벗 대비 +10.0% (기준 5%)",
                "손절폭 과대 — 18.2% (기준 12%)",
            ],
        )
        _notify([signal], [], messages.append)
        assert len(messages) == 1
        assert "신호일 08-03  진입 $106.00  손절 $90.00 (-15.1%)" in messages[0]
        assert "현재가 $110.00  피벗 대비 +10.0%" in messages[0]
        assert "⚠️ 추격 구간" in messages[0]
        assert "⚠️ 손절폭 과대" in messages[0]

    # ── Claude Code 리뷰 반영 — signal_stop_pct None 처리 + entry falsy 체크 ──

    def test_breakout_notification_omits_stop_clause_when_strict_price_missing(self, monkeypatch):
        """strict_price 가 없어 signal_stop_pct 를 못 구하면 "(--)" 같은 깨진
        표기 대신 "손절 ..." 절 자체가 통째로 생략된다."""
        from scanner.scan_engine import _notify

        monkeypatch.setattr("scanner.scan_engine._sector_summary", lambda market: "")
        messages = []
        signal = _signal(_grade="S", strict_price=None, entry_warnings=[])
        _notify([signal], [], messages.append)

        assert len(messages) == 1
        assert "(--)" not in messages[0]
        entry_line = next(l for l in messages[0].splitlines() if "진입" in l)
        assert "손절 " not in entry_line
        assert entry_line.strip().endswith("진입 -")

    def test_breakout_notification_omits_stop_clause_when_strict_price_zero(self, monkeypatch):
        """entry(strict_price)=0.0 은 falsy 지만 None 은 아니다 — 이전엔
        `if entry` 로 우연히 걸러졌지만, 명시적으로 `entry > 0` 조건이어야
        음수/0 가 실수로 통과하지 않는다는 걸 고정한다."""
        from scanner.scan_engine import _notify

        monkeypatch.setattr("scanner.scan_engine._sector_summary", lambda market: "")
        messages = []
        signal = _signal(_grade="S", strict_price=0.0, entry_warnings=[])
        _notify([signal], [], messages.append)

        assert len(messages) == 1
        assert "(--)" not in messages[0]
        entry_line = next(l for l in messages[0].splitlines() if "진입" in l)
        assert "손절 " not in entry_line


class TestUpthrustCooldown:
    def test_cooldown_warns_off_and_rejects_on_without_sliding_expiry(self, monkeypatch):
        import config
        from scanner import strict_filter as sf
        from scanner.scan_engine import _sync_upthrust_cooldown
        from database.models import UpthrustCooldown

        monkeypatch.setattr(config, "UPTHRUST_COOLDOWN_DAYS", 10)
        today = datetime.utcnow().strftime("%Y-%m-%d")
        db = _fresh_db()
        try:
            failed = _signal(
                ticker="COOL", signal_date=today, upthrust_failed=True,
                _upthrust_failed_date=today,
            )
            _sync_upthrust_cooldown(db, failed)
            db.commit()
            first_expiry = db.query(UpthrustCooldown).one().expires_at

            # 같은 실패를 다시 봐도 scan 시각 기준으로 만료가 밀리지 않는다.
            _sync_upthrust_cooldown(db, failed)
            db.commit()
            assert db.query(UpthrustCooldown).one().expires_at == first_expiry

            later = _signal(
                ticker="COOL",
                signal_date=(datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%d"),
                upthrust_failed=False,
            )
            _sync_upthrust_cooldown(db, later)
            assert later["_upthrust_cooldown_active"] is True
            assert any("쿨다운" in w for w in later["entry_warnings"])

            monkeypatch.setattr(sf, "STRICT_WEINSTEIN_MODE", False)
            monkeypatch.setattr(sf, "PIVOT_EXT_AS_GATE", False)
            monkeypatch.setattr(sf, "UPTHRUST_AS_GATE", False)
            assert sf.apply_strict_filter(later, {}) == (True, [])
            monkeypatch.setattr(sf, "UPTHRUST_AS_GATE", True)
            assert sf.apply_strict_filter(later, {}) == (False, [sf.UPTHRUST_COOLDOWN])
        finally:
            db.close()


class TestShadowAndRegression:
    def test_default_false_keeps_all_six_candidates(self, monkeypatch):
        import config
        from scanner import strict_filter as sf
        from scanner.scan_engine import _process_signal

        monkeypatch.setattr(config, "STRICT_WEINSTEIN_MODE", False)
        monkeypatch.setattr(config, "PIVOT_EXT_AS_GATE", False)
        monkeypatch.setattr(config, "UPTHRUST_AS_GATE", False)
        monkeypatch.setattr(config, "ALERT_FRESHNESS_AS_GATE", False)
        monkeypatch.setattr(sf, "STRICT_WEINSTEIN_MODE", False)
        monkeypatch.setattr(sf, "PIVOT_EXT_AS_GATE", False)
        monkeypatch.setattr(sf, "UPTHRUST_AS_GATE", False)

        candidates = [
            _signal(ticker="B1", pivot_ext_pct=8.0),
            _signal(ticker="B2", upthrust_failed=True),
            _signal(ticker="B3", upthrust_failed=None),
            _signal(ticker="R1", signal_type="REBOUND"),
            _signal(ticker="R2", signal_type="RE_BREAKOUT"),
            _signal(ticker="R3", signal_type="RE_BREAKOUT"),
        ]
        db = _fresh_db()
        try:
            kept = [
                s for s in candidates
                if _process_signal(db, s, "US", "BULL", object())
            ]
            assert len(kept) == 6
        finally:
            db.close()

    def test_funnel_shadow_counts_union_and_remaining(self, monkeypatch, tmp_path):
        import config
        from scanner.scan_engine import _new_funnel, _funnel_record, _finalize_funnel

        monkeypatch.setattr(config, "MAX_PIVOT_EXT_PCT", 5.0)
        funnel = _new_funnel()
        rows = [
            _signal(ticker="P", pivot_ext_pct=6.0,
                    upthrust_failed=False, _alert_freshness_would_cut=True),
            _signal(ticker="U", pivot_ext_pct=1.0,
                    upthrust_failed=True, _alert_freshness_would_cut=False),
            _signal(ticker="N", pivot_ext_pct=1.0,
                    upthrust_failed=None, _alert_freshness_would_cut=False),
            _signal(ticker="R1", signal_type="REBOUND"),
            _signal(ticker="R2", signal_type="RE_BREAKOUT"),
            _signal(ticker="R3", signal_type="RE_BREAKOUT"),
        ]
        for row in rows:
            row["_pre_entry_control_passed"] = True
            _funnel_record(funnel, row, {"reject": None}, notified=True)
        monkeypatch.setattr("scanner.scan_engine._write_funnel_file", lambda *a: None)
        _finalize_funnel("US", funnel, count=6)

        assert funnel["entry_control_shadow"] == {
            "pivot_ext_would_cut": 1,
            "upthrust_would_cut": 1,
            "upthrust_pending": 1,
            "alert_freshness_would_cut": 1,
            "any_would_cut": 2,
            "would_remain": 4,
        }
