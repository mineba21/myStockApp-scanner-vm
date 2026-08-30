"""
주봉 거래량 산출 기준 (Step 1) — 단위 테스트

W-FRI resample + Volume sum 구조상 주중 스캔 시 현재 주 행은 부분합이다.
compute_weekly_indicators(weekly_df, daily_df) 가 세 basis 정책을 정확히
적용하는지, 그리고 두 함정(10주 평균 오염 / 공휴일 단축주)을 피하는지 검증.

실행: venv/bin/python -m pytest tests/test_weekly_volume_basis.py -v
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import pytest

from scanner.weinstein import (
    to_weekly_ohlcv, compute_weekly_indicators,
    WEEK_BASIS_CURRENT_COMPLETE, WEEK_BASIS_CURRENT_NORMALIZED,
    WEEK_BASIS_PREVIOUS_COMPLETE,
)


# ── 거래일(월~금) 기반 합성 데이터 ────────────────────────────────

def _trading_days(start: str, n_weeks: int, drop=()):
    """월~금만 포함하는 거래일 인덱스. drop 에 든 date 는 휴장일로 제외."""
    days = pd.bdate_range(start=start, periods=n_weeks * 5)
    dropped = {pd.Timestamp(d) for d in drop}
    return [d for d in days if d not in dropped]


def _df_from_days(days, weekly_volume=1_000_000, last_week_daily_vol=None):
    """일봉 OHLCV 생성.

    일봉 거래량을 weekly_volume/5 로 **고정** 한다. 따라서 완성 주(5거래일)의
    주봉 거래량은 weekly_volume 이고, 부분 주는 경과일에 비례한 부분합이 된다
    — 실제 W-FRI resample 이 겪는 왜곡과 동일한 구조.
    last_week_daily_vol 을 주면 마지막 ISO 주의 일봉 거래량만 그 값으로 덮어씀.
    """
    rows = []
    last_key = days[-1].isocalendar()[:2]
    per_day = weekly_volume / 5

    for d in days:
        if last_week_daily_vol is not None and d.isocalendar()[:2] == last_key:
            rows.append(last_week_daily_vol)
        else:
            rows.append(per_day)

    close = [100.0 + i * 0.05 for i in range(len(days))]
    return pd.DataFrame({
        "Open":   [c * 0.999 for c in close],
        "High":   [c * 1.004 for c in close],
        "Low":    [c * 0.996 for c in close],
        "Close":  close,
        "Volume": rows,
    }, index=pd.DatetimeIndex(days))


def _ind(df):
    weekly = to_weekly_ohlcv(df)
    ind = compute_weekly_indicators(weekly, df)
    assert ind is not None, "주봉 지표 계산 실패 (데이터 길이 부족)"
    return ind


# ═══════════════════════════════════════════════════════════════════
# 1. 세 basis 경로
# ═══════════════════════════════════════════════════════════════════

class TestWeekBasisPaths:

    def test_complete_week_uses_current_without_normalization(self):
        """금요일 마감 = 완성 주 → CURRENT_COMPLETE, 정규화 없음."""
        days = _trading_days("2024-01-01", 40)          # 금요일로 끝남
        assert days[-1].weekday() == 4
        ind = _ind(_df_from_days(days))

        assert ind["week_volume_basis"] == WEEK_BASIS_CURRENT_COMPLETE
        assert ind["week_elapsed_days"] == 5
        # 매주 거래량이 동일하므로 비율은 1.0, 정규화 전후 동일
        assert ind["weekly_volume_ratio"] == pytest.approx(1.0, abs=0.01)
        assert ind["weekly_volume_ratio"] == ind["weekly_volume_ratio_raw"]

    def test_elapsed_3_days_normalizes_current_week(self):
        """수요일 마감(경과 3일) → CURRENT_NORMALIZED, raw * 5/3."""
        days = _trading_days("2024-01-01", 40)[:-2]     # 수요일까지
        assert days[-1].weekday() == 2
        ind = _ind(_df_from_days(days))

        assert ind["week_volume_basis"] == WEEK_BASIS_CURRENT_NORMALIZED
        assert ind["week_elapsed_days"] == 3
        # raw: 분자 0.6주 / 분모(부분 주가 섞인 10주 평균 0.96) = 0.625
        assert ind["weekly_volume_ratio_raw"] == pytest.approx(0.625, abs=0.02)
        # 정규화 + 부분 주 제외 분모 → 완성 주와 같은 수준(1.0)으로 복원
        assert ind["weekly_volume_ratio"] == pytest.approx(1.0, abs=0.02)

    def test_elapsed_2_days_falls_back_to_previous_week(self):
        """화요일 마감(경과 2일) → PREVIOUS_COMPLETE, 정규화 없음."""
        days = _trading_days("2024-01-01", 40)[:-3]     # 화요일까지
        assert days[-1].weekday() == 1
        ind = _ind(_df_from_days(days))

        assert ind["week_volume_basis"] == WEEK_BASIS_PREVIOUS_COMPLETE
        assert ind["week_elapsed_days"] == 2
        # 직전 완성 주(정상 거래량)를 쓰므로 1.0
        assert ind["weekly_volume_ratio"] == pytest.approx(1.0, abs=0.02)
        # raw: 분자 0.4주 / 분모(부분 주가 섞인 10주 평균 0.94) = 0.426
        assert ind["weekly_volume_ratio_raw"] == pytest.approx(0.426, abs=0.02)

    def test_daily_df_omitted_keeps_legacy_behavior(self):
        """daily_df=None 이면 기존 동작 그대로 (정규화·basis 판정 없음)."""
        days = _trading_days("2024-01-01", 40)[:-3]     # 화요일 부분 주
        df = _df_from_days(days)
        weekly = to_weekly_ohlcv(df)

        legacy = compute_weekly_indicators(weekly)
        assert legacy["week_volume_basis"] == WEEK_BASIS_CURRENT_COMPLETE
        assert legacy["week_elapsed_days"] is None
        assert legacy["weekly_volume_ratio"] == legacy["weekly_volume_ratio_raw"]


# ═══════════════════════════════════════════════════════════════════
# 2. 함정 (a) — 10주 평균 오염
# ═══════════════════════════════════════════════════════════════════

class TestRollingAverageNotPolluted:

    def test_previous_complete_average_excludes_partial_week(self):
        """PREVIOUS_COMPLETE 경로에서 부분 주 거래량이 분모 평균에 섞이면 안 된다.

        부분 주 거래량을 극단값(정상의 0.1배 / 50배)으로 흔들어도 비율이
        변하지 않아야 분모가 오염되지 않은 것이다.
        """
        days = _trading_days("2024-01-01", 40)[:-3]     # 화요일 = 부분 주

        tiny = _ind(_df_from_days(days, last_week_daily_vol=10_000))
        huge = _ind(_df_from_days(days, last_week_daily_vol=25_000_000))

        assert tiny["week_volume_basis"] == WEEK_BASIS_PREVIOUS_COMPLETE
        assert huge["week_volume_basis"] == WEEK_BASIS_PREVIOUS_COMPLETE
        # 부분 주 거래량이 2500배 차이나도 최종 비율은 동일
        assert tiny["weekly_volume_ratio"] == huge["weekly_volume_ratio"]
        # raw 는 부분 주를 보므로 당연히 크게 다르다 (대조군)
        assert tiny["weekly_volume_ratio_raw"] != huge["weekly_volume_ratio_raw"]

    def test_normalized_average_also_excludes_partial_week(self):
        """CURRENT_NORMALIZED 경로의 분모 평균도 부분 주를 제외한다.

        부분 주가 rolling(10) 에 섞이면 평균이 낮아져 비율이 부풀려진다.
        분자만 정규화하고 분모를 오염된 채로 두면 이중 보정이 된다.
        """
        days = _trading_days("2024-01-01", 40)[:-2]     # 수요일 = 부분 주
        df = _df_from_days(days)
        ind = _ind(df)
        assert ind["week_volume_basis"] == WEEK_BASIS_CURRENT_NORMALIZED

        # 분모를 직접 계산: 부분 주(마지막 행) 제외한 10주 평균
        weekly = to_weekly_ohlcv(df)
        expected_avg = float(
            weekly["Volume"].iloc[:-1].rolling(10, min_periods=5).mean().iloc[-1])
        expected_num = float(weekly["Volume"].iloc[-1]) * 5 / 3
        assert ind["weekly_volume_ratio"] == pytest.approx(
            round(expected_num / expected_avg, 2), abs=0.01)


# ═══════════════════════════════════════════════════════════════════
# 3. 함정 (b) — 공휴일 단축주
# ═══════════════════════════════════════════════════════════════════

class TestHolidayShortenedWeek:

    def test_four_day_week_ending_friday_is_complete(self):
        """월요일 휴장으로 거래일이 4일뿐인 주 → CURRENT_COMPLETE.

        경과일이 4라고 5/4 를 곱하면 25% 과대평가된다. 금요일 마감이면
        그 주의 마지막 거래일이므로 완성으로 판정해야 한다.
        """
        days = _trading_days("2024-01-01", 40)
        holiday = days[-5]                              # 마지막 주 월요일
        assert holiday.weekday() == 0
        days = [d for d in days if d != holiday]
        assert days[-1].weekday() == 4                  # 금요일 마감

        ind = _ind(_df_from_days(days))
        assert ind["week_elapsed_days"] == 4
        assert ind["week_volume_basis"] == WEEK_BASIS_CURRENT_COMPLETE
        # 5/4 곱셈이 일어나지 않았다
        assert ind["weekly_volume_ratio"] == ind["weekly_volume_ratio_raw"]

    def test_four_day_week_followed_by_next_week_is_complete(self):
        """뒤에 다음 주 일봉이 있으면(슬라이스가 아닌 경우) 확정적으로 완성.

        금요일 휴장으로 목요일 마감한 4일 주라도, 인덱스에 다음 주 일봉이
        있으면 '다음 거래일이 다음 주에 속한다' → 완성.
        """
        days = _trading_days("2024-01-01", 40)
        friday = days[-6]                               # 마지막 직전 주 금요일
        assert friday.weekday() == 4
        days = [d for d in days if d != friday]

        short_week_key = friday.isocalendar()[:2]
        df = _df_from_days(days)
        # 그 단축주까지만 잘라내면 목요일 마감 4일 주가 마지막 주가 된다
        upto_thu = [d for d in days if d.isocalendar()[:2] <= short_week_key]

        full = _ind(df)
        assert full["week_volume_basis"] == WEEK_BASIS_CURRENT_COMPLETE

        # 대조군 — 같은 단축주를 마지막 주로 자르면 미래를 볼 수 없어
        # 목요일 마감이 완성인지 판정 불가 → 미완성 취급 (문서화된 한계)
        sliced = _ind(_df_from_days(upto_thu))
        assert sliced["week_elapsed_days"] == 4
        assert sliced["week_volume_basis"] == WEEK_BASIS_CURRENT_NORMALIZED


# ═══════════════════════════════════════════════════════════════════
# 4. 적용 범위 — 거래량 외 지표 불변
# ═══════════════════════════════════════════════════════════════════

class TestScopeLimitedToVolume:

    def test_stage_and_sma_untouched_by_basis_policy(self):
        """basis 정책은 sma30w / slope30w / classify_stage 에 영향이 없어야 한다."""
        from scanner.weinstein import classify_stage, _build_indicators

        for cut in (0, 2, 3):                           # 금/수/화 마감
            days = _trading_days("2024-01-01", 40)
            days = days[:len(days) - cut] if cut else days
            df = _df_from_days(days)
            weekly = to_weekly_ohlcv(df)
            daily_ind = _build_indicators(df)

            legacy = compute_weekly_indicators(weekly)
            new    = compute_weekly_indicators(weekly, df)

            assert new["cur_sma30w"] == legacy["cur_sma30w"]
            assert new["cur_sma10w"] == legacy["cur_sma10w"]
            assert new["slope30w"]   == legacy["slope30w"]
            assert new["cur_close_w"] == legacy["cur_close_w"]
            assert (classify_stage(new, daily_ind)
                    == classify_stage(legacy, daily_ind))


# ═══════════════════════════════════════════════════════════════════
# 5. AS_GATE env 토글
# ═══════════════════════════════════════════════════════════════════

class TestWeeklyVolumeGateToggle:

    def _stage2_breakout_inputs(self):
        from tests.test_weinstein import _make_df, _make_stage2_base
        from scanner.weinstein import _build_indicators

        prices, volumes = _make_stage2_base(n_total=230, base_price=100.0)
        prices[-1], volumes[-1] = 104.0, 6_000_000
        df = _make_df(prices, volumes)
        return df, _build_indicators(df), compute_weekly_indicators(to_weekly_ohlcv(df), df)

    def test_gate_true_blocks_and_gate_false_warns(self, monkeypatch):
        """AS_GATE=True → 미달 시 None. False → 통과 + warning_flags 기록."""
        from scanner import weinstein

        df, daily_ind, weekly_ind = self._stage2_breakout_inputs()
        if daily_ind is None or weekly_ind is None:
            pytest.skip("indicators 빌드 실패")

        # 주봉 거래량을 임계값 미달로 강제 — 절대값이 아니라 *현재* 임계값의
        # 1/10 로 계산해 BREAKOUT_WEEKLY_VOL_RATIO 기본값이 바뀌어도 항상
        # 확실히 미달이 되게 한다 (Step 2: 2.0 → 0.5).
        forced_wvr = weinstein.BREAKOUT_WEEKLY_VOL_RATIO * 0.1
        weekly_ind = dict(weekly_ind)
        weekly_ind["weekly_volume_ratio"] = forced_wvr

        monkeypatch.setattr(weinstein, "BREAKOUT_WEEKLY_VOL_AS_GATE", True)
        diag = {}
        assert weinstein.detect_stage2_breakout(df, weekly_ind, daily_ind, diag=diag) is None
        assert diag["reject"] == weinstein.REJECT_WEEKLY_VOLUME_INSUFFICIENT
        assert diag["wvr"] == forced_wvr

        monkeypatch.setattr(weinstein, "BREAKOUT_WEEKLY_VOL_AS_GATE", False)
        diag = {}
        sig = weinstein.detect_stage2_breakout(df, weekly_ind, daily_ind, diag=diag)
        assert sig is not None, "AS_GATE=False 면 주봉 거래량 미달로 차단되지 않아야 함"
        assert any("주봉 거래량 미달" in f for f in sig["warning_flags"])
        assert diag["reject"] is None


# ═══════════════════════════════════════════════════════════════════
# 6. AS_GATE 가 strict filter(Gate 5)까지 일관 적용되는지 (Codex 리뷰 P1)
# ═══════════════════════════════════════════════════════════════════

class TestGateToggleReachesStrictFilter:
    """탐지기만 토글하고 strict filter 를 두면 env A/B 실험이 성립하지 않는다.

    AS_GATE=false 로 탐지기를 통과시켜도 _check_volume 이 같은 임계값으로
    breakout_weekly_volume 을 붙이면 후보가 결국 차단된다.
    """

    SIGNAL = {
        "signal_type":                "BREAKOUT",
        "volume_ratio":               3.5,     # 일봉은 충분 (임계값 3.0)
        "strict_weekly_volume_ratio": 1.0,     # 주봉은 미달 (임계값 2.0)
    }

    def _reasons(self, monkeypatch, as_gate):
        from scanner import strict_filter
        from scanner.strict_filter import _check_volume
        monkeypatch.setattr(strict_filter, "STRICT_REQUIRE_BREAKOUT_VOLUME", True)
        monkeypatch.setattr(strict_filter, "BREAKOUT_DAILY_VOL_RATIO",  3.0)
        monkeypatch.setattr(strict_filter, "BREAKOUT_WEEKLY_VOL_RATIO", 2.0)
        monkeypatch.setattr(strict_filter, "BREAKOUT_WEEKLY_VOL_AS_GATE", as_gate)
        reasons = []
        _check_volume(dict(self.SIGNAL), reasons)
        return reasons

    def test_gate_true_keeps_weekly_volume_reason(self, monkeypatch):
        from scanner.strict_filter import BREAKOUT_WEEKLY_VOLUME
        assert BREAKOUT_WEEKLY_VOLUME in self._reasons(monkeypatch, True)

    def test_gate_false_drops_weekly_volume_reason(self, monkeypatch):
        from scanner.strict_filter import BREAKOUT_WEEKLY_VOLUME
        assert BREAKOUT_WEEKLY_VOLUME not in self._reasons(monkeypatch, False)

    def test_daily_volume_condition_ignores_toggle(self, monkeypatch):
        """일봉 조건은 토글과 무관하게 항상 평가된다."""
        from scanner import strict_filter
        from scanner.strict_filter import _check_volume, BREAKOUT_DAILY_VOLUME
        monkeypatch.setattr(strict_filter, "STRICT_REQUIRE_BREAKOUT_VOLUME", True)
        monkeypatch.setattr(strict_filter, "BREAKOUT_DAILY_VOL_RATIO",  3.0)
        monkeypatch.setattr(strict_filter, "BREAKOUT_WEEKLY_VOL_RATIO", 2.0)

        for as_gate in (True, False):
            monkeypatch.setattr(strict_filter, "BREAKOUT_WEEKLY_VOL_AS_GATE", as_gate)
            reasons = []
            _check_volume({"signal_type": "BREAKOUT", "volume_ratio": 1.5,
                           "strict_weekly_volume_ratio": 5.0}, reasons)
            assert BREAKOUT_DAILY_VOLUME in reasons, f"AS_GATE={as_gate}"


# ═══════════════════════════════════════════════════════════════════
# 7. _REJECT_RANK 가 각 탐지기의 실제 체크 순서와 일치하는지 (P2)
# ═══════════════════════════════════════════════════════════════════

class TestRejectRankMatchesCheckOrder:
    """랭크가 코드 순서보다 낮으면 나중 단계 탈락이 앞 단계 사유를 밀어내지
    못해 funnel 이 잘못된 병목을 지목한다."""

    # 각 탐지기가 실제로 체크하는 순서 (코드 순서대로)
    ORDERS = {
        "_find_rebreakout_signal": [
            "daily_stage_not_2", "no_ma150", "price_below_ma150",
            "price_below_ma50", "ma150_not_rising", "base_too_short",
            "pullback_too_shallow", "base_too_wide", "no_pivot_breakout",
            "daily_volume_insufficient", "no_volume_dryup",
        ],
        "_find_rebound_signal": [
            "daily_stage_not_2_or_3", "ma150_not_rising", "no_rebound_touch",
            "no_rebound_confirm", "daily_volume_insufficient", "rebound_too_old",
        ],
        "detect_stage2_breakout": [
            "no_weekly_data", "weekly_stage_not_1_or_2",
            "weekly_volume_insufficient", "base_too_short",
            "no_pivot_breakout", "extension_too_high",
            "daily_volume_insufficient",
        ],
    }

    def test_ranks_strictly_increase_along_check_order(self):
        from scanner.weinstein import _REJECT_RANK

        for detector, order in self.ORDERS.items():
            ranks = [_REJECT_RANK[r] for r in order]
            for i in range(1, len(ranks)):
                assert ranks[i] > ranks[i - 1], (
                    f"{detector}: '{order[i-1]}'(rank {ranks[i-1]}) 다음에 오는 "
                    f"'{order[i]}'(rank {ranks[i]}) 의 랭크가 더 높아야 한다")

    def test_regression_dryup_and_too_old_outrank_daily_volume(self):
        """Codex 리뷰 P2 가 지목한 두 역전이 실제로 해소됐는지."""
        from scanner.weinstein import _REJECT_RANK as R
        assert R["no_volume_dryup"] > R["daily_volume_insufficient"]
        assert R["rebound_too_old"] > R["daily_volume_insufficient"]

    def test_breakout_weekly_volume_has_no_competitor(self):
        """BREAKOUT 의 주봉 거래량 체크는 bar 루프 밖 단독 체크라 경쟁이 없다.

        = 랭크 재정렬로 BREAKOUT funnel 수치가 흔들리지 않는다는 근거.
        """
        from scanner.weinstein import _REJECT_RANK as R
        loop_reasons = ["no_base_found", "base_too_short", "base_too_wide",
                        "no_pivot_breakout", "extension_too_high",
                        "daily_volume_insufficient"]
        # 루프 안 사유는 전부 주봉 거래량보다 깊다 → 덮어쓰기가 일어나도
        # weekly_volume_insufficient 는 애초에 즉시 return 이라 도달 불가
        assert all(R[r] > R["weekly_volume_insufficient"] for r in loop_reasons)


# ── 실행 ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import subprocess
    subprocess.run(
        [sys.executable, "-m", "pytest", __file__, "-v", "--tb=short"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
