"""
탐지기 진단 계측(diag out-parameter) — 단위 테스트

diag 는 순수 out-parameter 다. 아래 테스트는 두 가지를 보장한다.
  1. diag=None (기본값) 일 때 기존 동작/반환값이 100% 동일하다.
  2. diag dict 를 넘기면 탈락 사유가 REJECT_* enum 으로 정확히 기록된다.

실행: venv/bin/python -m pytest tests/test_diagnostics.py -v
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from tests.test_weinstein import _make_df, _make_stage2_base


# ── 합성 데이터 헬퍼 ──────────────────────────────────────────────

def _breakout_df():
    """BREAKOUT 시그널이 나오는 데이터 (거래량 spike 포함)."""
    prices, volumes = _make_stage2_base(n_total=230, base_price=100.0)
    prices[-1]  = 104.0
    volumes[-1] = 6_000_000
    return _make_df(prices, volumes)


def _no_signal_df():
    """아무 시그널도 안 나오는 데이터 (spike 없는 밋밋한 횡보 base)."""
    prices, volumes = _make_stage2_base(n_total=230, base_price=100.0)
    return _make_df(prices, volumes)


# ═══════════════════════════════════════════════════════════════════
# 1. diag=None invariant — 기존 동작 불변
# ═══════════════════════════════════════════════════════════════════

class TestDiagIsPureOutParameter:

    def test_diag_does_not_change_analyze_stock_result(self):
        """diag 유무와 무관하게 analyze_stock 반환값이 완전히 동일해야 한다."""
        from scanner.weinstein import analyze_stock

        for df in (_breakout_df(), _no_signal_df()):
            without = analyze_stock(df, "TEST", "테스트", "US")
            diag = {}
            with_diag = analyze_stock(df, "TEST", "테스트", "US", diag=diag)
            assert without == with_diag
            # diag 는 채워졌지만 반환값에는 흔적이 없어야 한다
            assert "reject" in diag
            if with_diag is not None:
                assert "diag" not in with_diag

    def test_diag_does_not_change_detector_results(self):
        """세 탐지기 모두 diag 유무로 반환값이 달라지지 않아야 한다."""
        from scanner.weinstein import (
            _build_indicators, compute_weekly_indicators, to_weekly_ohlcv,
            detect_stage2_breakout, detect_continuation_breakout,
            detect_rebound_entry,
        )

        for df in (_breakout_df(), _no_signal_df()):
            daily_ind  = _build_indicators(df)
            weekly_ind = compute_weekly_indicators(to_weekly_ohlcv(df))
            if daily_ind is None:
                pytest.skip("indicators 빌드 실패")

            for detector in (detect_stage2_breakout,
                             detect_continuation_breakout,
                             detect_rebound_entry):
                without = detector(df, weekly_ind, daily_ind)
                diag = {}
                with_diag = detector(df, weekly_ind, daily_ind, diag=diag)
                assert without == with_diag, detector.__name__
                assert "reject" in diag, detector.__name__


# ═══════════════════════════════════════════════════════════════════
# 2. 탈락 사유 기록
# ═══════════════════════════════════════════════════════════════════

class TestRejectReasons:

    def test_weekly_volume_insufficient_recorded(self):
        """주봉 거래량 미달 — 사유 키 + wvr/threshold 수치가 기록돼야 한다."""
        from scanner import weinstein
        from scanner.weinstein import (
            _build_indicators, compute_weekly_indicators, to_weekly_ohlcv,
            detect_stage2_breakout,
        )

        df = _no_signal_df()
        daily_ind  = _build_indicators(df)
        weekly_ind = compute_weekly_indicators(to_weekly_ohlcv(df), df)
        if daily_ind is None or weekly_ind is None:
            pytest.skip("indicators 빌드 실패")

        # 임계값의 절대값이 아니라 *현재* BREAKOUT_WEEKLY_VOL_RATIO 의 1/10 로
        # 강제해 미달을 재현한다 — Step 2 에서 기본값이 2.0 → 0.5 로 바뀌었고
        # fixture 의 자연 비율(≈1.0x)은 더 이상 구 임계값 밑이 아니다.
        weekly_ind = dict(weekly_ind)
        weekly_ind["weekly_volume_ratio"] = weinstein.BREAKOUT_WEEKLY_VOL_RATIO * 0.1

        diag = {}
        assert detect_stage2_breakout(df, weekly_ind, daily_ind, diag=diag) is None
        assert diag["reject"] == weinstein.REJECT_WEEKLY_VOLUME_INSUFFICIENT
        assert diag["values"]["threshold"] == weinstein.BREAKOUT_WEEKLY_VOL_RATIO
        assert diag["values"]["wvr"] < weinstein.BREAKOUT_WEEKLY_VOL_RATIO

    def test_stage_and_missing_data_reasons(self, monkeypatch):
        """Stage 미달 / 주봉 데이터 없음 — 서로 다른 사유 키로 구분돼야 한다."""
        from scanner import weinstein
        from scanner.weinstein import (
            _build_indicators, compute_weekly_indicators, to_weekly_ohlcv,
            detect_stage2_breakout, detect_rebound_entry,
        )

        df = _breakout_df()
        daily_ind  = _build_indicators(df)
        weekly_ind = compute_weekly_indicators(to_weekly_ohlcv(df))
        if daily_ind is None or weekly_ind is None:
            pytest.skip("indicators 빌드 실패")

        # 주봉 데이터 없음 (REBOUND 는 weekly_ind 필수)
        diag = {}
        assert detect_rebound_entry(df, None, daily_ind, diag=diag) is None
        assert diag["reject"] == weinstein.REJECT_NO_WEEKLY_DATA

        # Stage4 강제 → BREAKOUT 은 STAGE1/2 아님으로 탈락
        monkeypatch.setattr(weinstein, "classify_stage", lambda *a, **kw: "STAGE4")
        diag = {}
        assert detect_stage2_breakout(df, weekly_ind, daily_ind, diag=diag) is None
        assert diag["reject"] == weinstein.REJECT_WEEKLY_STAGE_NOT_1_OR_2
        assert diag["values"]["stage"] == "STAGE4"

    def test_analyze_stock_records_detector_breakdown(self, monkeypatch):
        """시그널이 없으면 대표 사유 + 탐지기별 사유가 모두 기록된다."""
        from scanner import weinstein
        from scanner.weinstein import analyze_stock, REJECT_WEEKLY_VOLUME_INSUFFICIENT

        # analyze_stock 은 weekly_ind 를 내부에서 계산하므로 직접 주입할 수
        # 없다 — 대신 임계값을 fixture 의 자연 비율(≈1.0x)보다 확실히 높게
        # 강제해 weekly_volume_insufficient 가 항상 재현되게 한다.
        monkeypatch.setattr(weinstein, "BREAKOUT_WEEKLY_VOL_RATIO", 5.0)

        diag = {}
        assert analyze_stock(_no_signal_df(), "T", "테스트", "US", diag=diag) is None

        assert diag["reject"] is not None
        assert set(diag["detectors"]) == {"BREAKOUT", "RE_BREAKOUT", "REBOUND"}
        # 각 탐지기가 자기 경로의 사유를 독립적으로 남긴다
        assert all(d["reject"] is not None for d in diag["detectors"].values())
        assert (diag["detectors"]["BREAKOUT"]["reject"]
                == REJECT_WEEKLY_VOLUME_INSUFFICIENT)

    def test_analyze_stock_reject_is_none_on_signal(self):
        """시그널이 생성되면 diag["reject"] 는 None 이어야 한다."""
        from scanner.weinstein import analyze_stock

        diag = {}
        res = analyze_stock(_breakout_df(), "T", "테스트", "US", diag=diag)

        assert res is not None
        assert diag["reject"] is None
        assert diag["values"] == {}
        assert isinstance(diag["rs_flags"], list)   # 벤치마크 없으면 빈 리스트

    def test_insufficient_history_recorded(self):
        """데이터 길이 부족 — analyze_stock 첫 return None 지점도 계측된다."""
        from scanner.weinstein import analyze_stock, REJECT_INSUFFICIENT_HISTORY

        df = _make_df([100.0] * 30)
        diag = {}
        assert analyze_stock(df, "T", "테스트", "US", diag=diag) is None
        assert diag["reject"] == REJECT_INSUFFICIENT_HISTORY
        assert diag["values"]["bars"] == 30


# ═══════════════════════════════════════════════════════════════════
# 3. scan_engine funnel 집계
# ═══════════════════════════════════════════════════════════════════

class TestFunnelAggregation:

    def test_funnel_counts_rejects_signals_and_strict_rejects(self):
        """_funnel_record 가 세 부류(탈락/strict 탈락/시그널)를 나눠 센다."""
        from scanner.scan_engine import (
            _new_funnel, _funnel_record, MARKET_FILTER_BLOCKED,
        )

        funnel = _new_funnel()

        # 1) analyze_stock 이 None → rejects + rejects_by_detector
        _funnel_record(funnel, None,
                       {"reject": "weekly_volume_insufficient",
                        "detectors": {"BREAKOUT":    {"reject": "weekly_volume_insufficient"},
                                      "RE_BREAKOUT": {"reject": "no_pivot_breakout"},
                                      "REBOUND":     {"reject": "no_rebound_touch"}}},
                       notified=False)
        # 2) 시그널은 났지만 strict 필터 거부 (사유 복수 가능)
        _funnel_record(funnel,
                       {"signal_type": "BREAKOUT",
                        "filter_reasons": ["rs_below_zero", "rs_no_zero_cross"]},
                       {"reject": None}, notified=False)
        # 3) market filter 단계에서 잘려 사유가 비어 있는 경우
        _funnel_record(funnel, {"signal_type": "BREAKOUT", "filter_reasons": []},
                       {"reject": None}, notified=False)
        # 4) 알림까지 통과한 시그널
        _funnel_record(funnel, {"signal_type": "REBOUND", "filter_reasons": []},
                       {"reject": None}, notified=True)

        assert funnel["rejects"] == {"weekly_volume_insufficient": 1}
        assert funnel["rejects_by_detector"]["BREAKOUT"] == {"weekly_volume_insufficient": 1}
        assert funnel["rejects_by_detector"]["REBOUND"] == {"no_rebound_touch": 1}
        assert funnel["strict_rejects"] == {"rs_below_zero": 1,
                                            "rs_no_zero_cross": 1,
                                            MARKET_FILTER_BLOCKED: 1}
        assert funnel["signals"] == {"REBOUND": 1}

    def test_funnel_json_snapshot_written(self):
        """logs/funnel_{market}_{YYYYMMDD_HHMM}.json 스냅샷이 기록된다."""
        import json, re
        from scanner.scan_engine import _new_funnel, _write_funnel_file

        funnel = _new_funnel()
        funnel["total_scanned"] = 503
        funnel["rejects"]["weekly_volume_insufficient"] = 412

        path = _write_funnel_file("KR", funnel)
        assert path is not None
        try:
            assert re.fullmatch(r"funnel_KR_\d{8}_\d{4}\.json",
                                os.path.basename(path))
            assert os.path.basename(os.path.dirname(path)) == "logs"
            with open(path, encoding="utf-8") as f:
                written = json.load(f)
            assert written["total_scanned"] == 503
            assert written["rejects"]["weekly_volume_insufficient"] == 412
        finally:
            os.unlink(path)

    def test_funnel_record_is_noop_without_funnel(self):
        """funnel=None 이면 아무것도 하지 않는다 (기존 경로 무영향)."""
        from scanner.scan_engine import _funnel_record

        assert _funnel_record(None, None, {"reject": "no_signal"}, False) is None


# ── 실행 ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import subprocess
    subprocess.run(
        [sys.executable, "-m", "pytest", __file__, "-v", "--tb=short"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
