"""
2단(2-tier) Base Pivot v2 — 단위 테스트 (Step 2)

detect_base_pivot_v2 의 세 탈락 사유(base_too_wide / tight_too_wide /
no_contraction), 신호일 제외(look-ahead 방지) 규약, BASE_MODE v1/v2 토글,
시장별 파라미터(market_param) 조회를 검증한다.

실행: venv/bin/python -m pytest tests/test_base_pivot_v2.py -v
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import pytest

from scanner.weinstein import (
    detect_base_pivot_v2, detect_base_pivot, detect_stage2_breakout,
    _build_indicators, compute_weekly_indicators, to_weekly_ohlcv,
    REJECT_BASE_TOO_WIDE, REJECT_TIGHT_TOO_WIDE, REJECT_NO_CONTRACTION,
    REJECT_BASE_TOO_SHORT,
)
from config import market_param


# ── 합성 데이터 헬퍼 ──────────────────────────────────────────────

def _outer_inner_bounds(outer_width_pct, inner_width_pct, outer_high=100.0):
    """outer(자격 구간) 폭/inner(tight 구간) 폭을 독립적으로 지정하되, inner
    범위가 항상 outer 범위 **안**에 완전히 포함되도록 inner_high 를 outer_high
    에서부터 역탐색한다. 포함 관계가 보장되므로 자격 구간의 global max/min
    은 항상 outer 값으로 고정되고, inner 만의 폭을 자유롭게 조절할 수 있다.
    """
    outer_low = outer_high * (1 - outer_width_pct / 100)
    inner_high = outer_high
    while True:
        inner_low = inner_high * (1 - inner_width_pct / 100)
        if inner_low >= outer_low - 1e-9:
            break
        inner_high -= 0.01
    assert inner_high <= outer_high + 1e-9
    assert inner_low >= outer_low - 1e-9
    return outer_high, outer_low, round(inner_high, 4), round(inner_low, 4)


def _v2_df(outer_high, outer_low, inner_high, inner_low,
          base_lookback=25, tight_lookback=10, warmup=20,
          signal_high=None, signal_low=None, signal_close=None):
    """신호일 직전 base_lookback 개 bar 를 outer(오래된 구간)/inner(최근
    tight 구간) 두 세그먼트로 나눠 구성. 신호일(마지막 행)은 별도 값 —
    detect_base_pivot_v2 는 이를 제외해야 한다(look-ahead 방지 검증용).
    """
    outer_days = base_lookback - tight_lookback
    prices = [outer_high] * warmup
    prices += [outer_high if i % 2 == 0 else outer_low for i in range(outer_days)]
    prices += [inner_high if i % 2 == 0 else inner_low for i in range(tight_lookback)]

    sh = signal_high if signal_high is not None else inner_high
    sl = signal_low if signal_low is not None else inner_low
    sc = signal_close if signal_close is not None else inner_high

    highs  = prices + [sh]
    lows   = prices + [sl]
    closes = prices + [sc]
    idx = pd.date_range("2023-01-02", periods=len(highs), freq="D")
    return pd.DataFrame({
        "Open": closes, "High": highs, "Low": lows, "Close": closes,
        "Volume": [500_000] * len(highs),
    }, index=idx)


# ═══════════════════════════════════════════════════════════════════
# 1. 세 탈락 사유
# ═══════════════════════════════════════════════════════════════════

class TestDetectBasePivotV2Rejects:

    def test_base_too_wide(self):
        """자격 구간(25일) 폭이 BASE_MAX_WIDTH_PCT(25%) 초과 → base_too_wide."""
        oh, ol, ih, il = _outer_inner_bounds(outer_width_pct=30, inner_width_pct=5)
        df = _v2_df(oh, ol, ih, il)

        diag = {}
        result = detect_base_pivot_v2(df, market="US", diag=diag)

        assert result is None
        assert diag["reject"] == REJECT_BASE_TOO_WIDE
        assert diag["values"]["width_pct"] == pytest.approx(30.0, abs=0.1)
        assert diag["values"]["max_width_pct"] == 25.0

    def test_tight_too_wide(self):
        """자격 구간은 통과(20%)하지만 tight 구간(10일) 폭이 10% 초과 → tight_too_wide."""
        oh, ol, ih, il = _outer_inner_bounds(outer_width_pct=20, inner_width_pct=18)
        df = _v2_df(oh, ol, ih, il)

        diag = {}
        result = detect_base_pivot_v2(df, market="US", diag=diag)

        assert result is None
        assert diag["reject"] == REJECT_TIGHT_TOO_WIDE
        assert diag["values"]["width_pct"] == pytest.approx(18.0, abs=0.1)
        assert diag["values"]["max_width_pct"] == 10.0

    def test_no_contraction(self):
        """자격/tight 폭 둘 다 통과하지만 tight 폭이 자격 폭의 85% 이상
        (= 충분히 수축하지 않음) → no_contraction."""
        oh, ol, ih, il = _outer_inner_bounds(outer_width_pct=10, inner_width_pct=9)
        df = _v2_df(oh, ol, ih, il)

        diag = {}
        result = detect_base_pivot_v2(df, market="US", diag=diag)

        assert result is None
        assert diag["reject"] == REJECT_NO_CONTRACTION
        assert diag["values"]["contraction_ratio"] == pytest.approx(0.9, abs=0.02)
        assert diag["values"]["max_ratio"] == 0.85

    def test_passes_when_width_and_contraction_both_satisfied(self):
        """자격 20% / tight 5% (수축비 0.25 < 0.85) → 성공."""
        oh, ol, ih, il = _outer_inner_bounds(outer_width_pct=20, inner_width_pct=5)
        df = _v2_df(oh, ol, ih, il)

        result = detect_base_pivot_v2(df, market="US")

        assert result is not None
        assert result["pivot_price"] == pytest.approx(ih, abs=0.01)
        assert result["stop_ref"]    == pytest.approx(il, abs=0.01)
        assert result["base_width"]  == pytest.approx(20.0, abs=0.1)
        assert result["tight_width"] == pytest.approx(5.0, abs=0.1)
        assert result["contraction_ratio"] < 0.85
        end = len(df) - 1  # 마지막 행은 신호일로 구간에서 제외
        assert result["base_start_date"] == df.index[end - 25].strftime("%Y-%m-%d")
        assert result["base_end_date"] == df.index[end - 1].strftime("%Y-%m-%d")
        assert result["tight_start_date"] == df.index[end - 10].strftime("%Y-%m-%d")
        assert result["base_high"] == pytest.approx(oh, abs=0.01)
        assert result["base_low"] == pytest.approx(ol, abs=0.01)
        assert result["tight_high"] == pytest.approx(ih, abs=0.01)
        assert result["tight_low"] == pytest.approx(il, abs=0.01)

    def test_insufficient_bars_returns_base_too_short(self):
        """base_lookback+1 개 미만이면 REJECT_BASE_TOO_SHORT."""
        df = _v2_df(100.0, 90.0, 100.0, 97.0, warmup=0)  # 총 26일 (25+신호일)
        short_df = df.iloc[-10:]  # 10일 < 26일 필요

        diag = {}
        assert detect_base_pivot_v2(short_df, market="US", diag=diag) is None
        assert diag["reject"] == REJECT_BASE_TOO_SHORT


# ═══════════════════════════════════════════════════════════════════
# 2. 신호일 제외 (look-ahead 방지)
# ═══════════════════════════════════════════════════════════════════

class TestLookAheadExclusion:

    def test_signal_day_high_does_not_affect_pivot(self):
        """신호일 High 를 극단값으로 바꿔도 pivot_price/base_width 가 불변."""
        oh, ol, ih, il = _outer_inner_bounds(outer_width_pct=20, inner_width_pct=5)

        normal_df  = _v2_df(oh, ol, ih, il, signal_high=ih, signal_low=il)
        extreme_df = _v2_df(oh, ol, ih, il, signal_high=999_999.0, signal_low=il)

        r_normal  = detect_base_pivot_v2(normal_df,  market="US")
        r_extreme = detect_base_pivot_v2(extreme_df, market="US")

        assert r_normal is not None and r_extreme is not None
        assert r_normal == r_extreme, "신호일 High 극단값이 base/pivot 계산에 흘러들어갔다"

    def test_signal_day_low_does_not_affect_stop_ref(self):
        """신호일 Low 를 극단값(0 근접)으로 바꿔도 stop_ref 가 불변."""
        oh, ol, ih, il = _outer_inner_bounds(outer_width_pct=20, inner_width_pct=5)

        normal_df  = _v2_df(oh, ol, ih, il, signal_high=ih, signal_low=il)
        extreme_df = _v2_df(oh, ol, ih, il, signal_high=ih, signal_low=0.0001)

        r_normal  = detect_base_pivot_v2(normal_df,  market="US")
        r_extreme = detect_base_pivot_v2(extreme_df, market="US")

        assert r_normal is not None and r_extreme is not None
        assert r_normal == r_extreme, "신호일 Low 극단값이 base/pivot 계산에 흘러들어갔다"

    def test_detect_stage2_breakout_end_to_end_ignores_signal_bar_high(self):
        """detect_stage2_breakout 전체 경로에서도 신호일(마지막 bar) 의 High
        를 극단값으로 바꾸면 — Close/breakout 판정에는 손대지 않고 High 만
        바꾸면 — pivot_price 가 불변이어야 한다 (base 계산이 신호일을
        제외한다는 증거를 detect_base_pivot_v2 가 아닌 상위 경로에서 확인)."""
        from tests.test_weinstein import _make_df, _make_stage2_base

        prices, volumes = _make_stage2_base(n_total=230, base_price=100.0)
        prices[-1], volumes[-1] = 104.0, 6_000_000
        df_normal = _make_df(prices, volumes)

        df_extreme = df_normal.copy()
        # 신호일 High 만 극단적으로 올린다 — Close/breakout 판정(pp<=pivot<cp)
        # 은 Close 기준이라 그대로 유지된다.
        df_extreme.iloc[-1, df_extreme.columns.get_loc("High")] = 99_999.0

        weekly_normal  = compute_weekly_indicators(to_weekly_ohlcv(df_normal),  df_normal)
        weekly_extreme = compute_weekly_indicators(to_weekly_ohlcv(df_extreme), df_extreme)

        sig_normal = detect_stage2_breakout(
            df_normal, weekly_normal, _build_indicators(df_normal), market="US")
        sig_extreme = detect_stage2_breakout(
            df_extreme, weekly_extreme, _build_indicators(df_extreme), market="US")

        assert sig_normal is not None and sig_extreme is not None
        assert sig_normal["pivot_price"] == sig_extreme["pivot_price"], (
            "신호일 High 극단값이 pivot_price 계산에 흘러들어갔다 (look-ahead)")
        assert sig_normal["stop_ref"] == sig_extreme["stop_ref"]


# ═══════════════════════════════════════════════════════════════════
# 3. BASE_MODE v1 / v2 토글
# ═══════════════════════════════════════════════════════════════════

class TestBaseModeToggle:

    def _inputs(self):
        from tests.test_weinstein import _make_df, _make_stage2_base
        prices, volumes = _make_stage2_base(n_total=230, base_price=100.0)
        prices[-1], volumes[-1] = 104.0, 6_000_000
        df = _make_df(prices, volumes)
        daily_ind  = _build_indicators(df)
        weekly_ind = compute_weekly_indicators(to_weekly_ohlcv(df), df)
        return df, daily_ind, weekly_ind

    def test_v2_default_uses_two_tier_base(self, monkeypatch):
        from scanner import weinstein

        monkeypatch.setattr(weinstein, "BASE_MODE", "v2")
        df, daily_ind, weekly_ind = self._inputs()
        sig = weinstein.detect_stage2_breakout(df, weekly_ind, daily_ind, market="US")

        assert sig is not None
        assert sig["base_mode"] == "v2"
        assert sig["stop_ref"] is not None
        assert "tight_width_pct" in sig
        assert sig["base_start_date"] < sig["signal_date"]
        assert sig["base_end_date"] < sig["signal_date"]
        assert sig["tight_start_date"] < sig["signal_date"]
        assert sig["base_high"] >= sig["tight_high"]
        assert sig["base_range_low"] <= sig["tight_low"]

    def test_v1_toggle_uses_single_expanding_base(self, monkeypatch):
        from scanner import weinstein

        monkeypatch.setattr(weinstein, "BASE_MODE", "v1")
        df, daily_ind, weekly_ind = self._inputs()
        sig = weinstein.detect_stage2_breakout(df, weekly_ind, daily_ind, market="US")

        assert sig is not None
        assert sig["base_mode"] == "v1"
        assert sig.get("stop_ref") is None
        assert "tight_width_pct" not in sig
        assert sig["base_quality_v4"] in ("TIGHT", "LOOSE")
        assert sig["base_start_date"] < sig["signal_date"]
        assert sig["base_end_date"] < sig["signal_date"]
        assert sig.get("tight_start_date") is None

    def test_v1_and_v2_agree_on_gate_but_differ_on_base_fields(self, monkeypatch):
        """같은 입력에서 v1/v2 둘 다 BREAKOUT 을 내지만 base 관련 필드는 다르다."""
        from scanner import weinstein

        df, daily_ind, weekly_ind = self._inputs()

        monkeypatch.setattr(weinstein, "BASE_MODE", "v1")
        sig_v1 = weinstein.detect_stage2_breakout(df, weekly_ind, daily_ind, market="US")
        monkeypatch.setattr(weinstein, "BASE_MODE", "v2")
        sig_v2 = weinstein.detect_stage2_breakout(df, weekly_ind, daily_ind, market="US")

        assert sig_v1 is not None and sig_v2 is not None
        assert sig_v1["signal_type"] == sig_v2["signal_type"] == "BREAKOUT"
        assert sig_v1["base_low"] != sig_v2["base_low"], (
            "v1(전체 base 최저가)과 v2(tight 구간 최저가)의 손절 기준은 서로 달라야 함")


# ═══════════════════════════════════════════════════════════════════
# 4. 시장별 파라미터 (config.market_param)
# ═══════════════════════════════════════════════════════════════════

class TestMarketParam:

    def test_market_specific_override_wins(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "KR_BASE_MAX_WIDTH_PCT", 30.0, raising=False)
        monkeypatch.setattr(config, "BASE_MAX_WIDTH_PCT", 25.0, raising=False)

        assert market_param("BASE_MAX_WIDTH_PCT", "KR", 0.0) == 30.0
        assert market_param("BASE_MAX_WIDTH_PCT", "US", 0.0) == 25.0  # US 전용 없으면 공통값

    def test_falls_back_to_common_when_no_market_override(self, monkeypatch):
        import config
        # TIGHT_LOOKBACK_DAYS 는 기본적으로 시장별 오버라이드가 없다
        monkeypatch.setattr(config, "TIGHT_LOOKBACK_DAYS", 10, raising=False)
        assert market_param("TIGHT_LOOKBACK_DAYS", "KR", 0) == 10
        assert market_param("TIGHT_LOOKBACK_DAYS", "US", 0) == 10

    def test_falls_back_to_default_when_nothing_defined(self):
        assert market_param("NONEXISTENT_PARAM_XYZ", "KR", 42) == 42
        assert market_param("NONEXISTENT_PARAM_XYZ", None, 42) == 42

    def test_none_market_skips_market_specific_lookup(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "KR_BASE_MAX_WIDTH_PCT", 30.0, raising=False)
        monkeypatch.setattr(config, "BASE_MAX_WIDTH_PCT", 25.0, raising=False)
        assert market_param("BASE_MAX_WIDTH_PCT", None, 0.0) == 25.0

    def test_kr_wider_base_width_lets_wider_candidates_through(self):
        """동일한 종목 데이터(자격 폭 27%)를 시장만 바꿔 넣으면 KR(30%)은
        통과하고 US(25%)는 거부되어야 한다."""
        oh, ol, ih, il = _outer_inner_bounds(outer_width_pct=27, inner_width_pct=5)
        df = _v2_df(oh, ol, ih, il)

        diag_us = {}
        result_us = detect_base_pivot_v2(df, market="US", diag=diag_us)
        result_kr = detect_base_pivot_v2(df, market="KR")

        assert result_us is None
        assert diag_us["reject"] == REJECT_BASE_TOO_WIDE
        assert result_kr is not None


class TestMarketParamEnvInheritance:
    """config.py 모듈 레벨에서 US_/KR_ 오버라이드가 공통 env 를 상속하는지
    (Codex 리뷰 P2). config.py 는 import 시점에 os.getenv() 로 값을 굳히므로
    monkeypatch 로는 이 3단 조회 자체를 검증할 수 없다 — 서브프로세스로
    실제 재-import 해서 확인한다."""

    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _import_config_with_env(self, env_overrides: dict) -> dict:
        import subprocess, json
        code = (
            "import sys, json; sys.path.insert(0, %r)\n"
            "import config\n"
            "print(json.dumps({"
            "'US_BASE_MAX_WIDTH_PCT': config.US_BASE_MAX_WIDTH_PCT,"
            "'KR_BASE_MAX_WIDTH_PCT': config.KR_BASE_MAX_WIDTH_PCT,"
            "'US_TIGHT_MAX_WIDTH_PCT': config.US_TIGHT_MAX_WIDTH_PCT,"
            "'KR_TIGHT_MAX_WIDTH_PCT': config.KR_TIGHT_MAX_WIDTH_PCT,"
            "}))"
        ) % (self.REPO_ROOT,)
        env = {k: v for k, v in os.environ.items()
              if not k.endswith(("BASE_MAX_WIDTH_PCT", "TIGHT_MAX_WIDTH_PCT"))}
        env.update(env_overrides)
        result = subprocess.run([sys.executable, "-c", code], cwd="/tmp",
                                env=env, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def test_no_env_set_keeps_intentional_kr_us_divergence(self):
        """아무것도 안 설정하면 오늘의 기본값(US 25 / KR 30)이 그대로 유지."""
        vals = self._import_config_with_env({})
        assert vals["US_BASE_MAX_WIDTH_PCT"] == 25.0
        assert vals["KR_BASE_MAX_WIDTH_PCT"] == 30.0

    def test_common_override_propagates_to_both_markets(self):
        """공통 BASE_MAX_WIDTH_PCT 만 설정하면 US/KR 둘 다 그 값을 따른다
        (수정 전에는 두 값 다 하드코딩 기본값(25/30)에 갇혀 무시됐다)."""
        vals = self._import_config_with_env({"BASE_MAX_WIDTH_PCT": "20"})
        assert vals["US_BASE_MAX_WIDTH_PCT"] == 20.0
        assert vals["KR_BASE_MAX_WIDTH_PCT"] == 20.0

    def test_market_specific_env_still_wins_over_common(self):
        """시장별 env 가 있으면 공통 env 보다 우선한다."""
        vals = self._import_config_with_env({
            "BASE_MAX_WIDTH_PCT": "20", "KR_BASE_MAX_WIDTH_PCT": "35",
        })
        assert vals["US_BASE_MAX_WIDTH_PCT"] == 20.0   # 공통값 상속
        assert vals["KR_BASE_MAX_WIDTH_PCT"] == 35.0   # 시장별 env 가 최우선

    def test_tight_max_width_pct_has_same_inheritance(self):
        """TIGHT_MAX_WIDTH_PCT 도 동일한 3단 조회를 따른다."""
        vals = self._import_config_with_env({"TIGHT_MAX_WIDTH_PCT": "8"})
        assert vals["US_TIGHT_MAX_WIDTH_PCT"] == 8.0
        assert vals["KR_TIGHT_MAX_WIDTH_PCT"] == 8.0


# ═══════════════════════════════════════════════════════════════════
# 5. scan_engine funnel — Step 2 신규 필드
# ═══════════════════════════════════════════════════════════════════

class TestFunnelStep2Fields:

    def test_base_mode_reflects_live_base_mode_at_scan_start(self, monkeypatch):
        """funnel["base_mode"] 는 _new_funnel() 호출 시점의 BASE_MODE 를 반영."""
        from scanner import weinstein, scan_engine

        monkeypatch.setattr(weinstein, "BASE_MODE", "v1")
        assert scan_engine._new_funnel()["base_mode"] == "v1"

        monkeypatch.setattr(weinstein, "BASE_MODE", "v2")
        assert scan_engine._new_funnel()["base_mode"] == "v2"

    def test_weekly_gate_cut_but_would_pass_daily_counts_only_flagged_stocks(self):
        """probe 플래그(would_pass_daily_volume=True) 가 있는 종목만 센다."""
        from scanner.scan_engine import _new_funnel, _funnel_record

        funnel = _new_funnel()

        # 주봉 게이트에 걸렸고, 일봉 조건은 충족했을 것 (카운트 대상)
        _funnel_record(funnel, None,
                       {"reject": "weekly_volume_insufficient",
                        "detectors": {"BREAKOUT": {"reject": "weekly_volume_insufficient",
                                                   "would_pass_daily_volume": True},
                                      "RE_BREAKOUT": {"reject": "no_pivot_breakout"},
                                      "REBOUND": {"reject": "no_rebound_touch"}}},
                       notified=False)
        # 주봉 게이트에 걸렸지만 일봉 조건도 미달 (카운트 제외)
        _funnel_record(funnel, None,
                       {"reject": "weekly_volume_insufficient",
                        "detectors": {"BREAKOUT": {"reject": "weekly_volume_insufficient",
                                                   "would_pass_daily_volume": False},
                                      "RE_BREAKOUT": {"reject": "no_pivot_breakout"},
                                      "REBOUND": {"reject": "no_rebound_touch"}}},
                       notified=False)
        # 다른 사유로 탈락 (probe 자체가 없음 — 카운트 제외)
        _funnel_record(funnel, None,
                       {"reject": "weekly_stage_not_1_or_2",
                        "detectors": {"BREAKOUT": {"reject": "weekly_stage_not_1_or_2"},
                                      "RE_BREAKOUT": {"reject": "no_pivot_breakout"},
                                      "REBOUND": {"reject": "no_rebound_touch"}}},
                       notified=False)

        assert funnel["weekly_gate_cut_but_would_pass_daily"] == 1

    def test_base_and_stop_pct_stats_computed_from_breakout_signals_only(self):
        """base_stats/stop_pct_stats 는 BREAKOUT 시그널에서만 뽑고, 통과 여부와
        무관하게(strict 거부돼도) 표본에 들어간다.

        stop_pct_stats 는 strict_price(signal_date 시점 종가) 기준이고,
        stop_pct_at_current_price 는 price(최신 종가) 기준 — 둘이 다른
        값일 때 서로 다른 통계가 나와야 한다 (Codex 리뷰 P2)."""
        from scanner.scan_engine import _new_funnel, _funnel_record, _finalize_funnel

        funnel = _new_funnel()

        # BREAKOUT #1 — strict 통과. signal_date 이후 가격이 100→110 으로
        # 올라 strict_price(진입 시점) 와 price(최신) 가 다르다.
        _funnel_record(funnel, {
            "signal_type": "BREAKOUT", "filter_reasons": [],
            "base_width_pct": 20.0, "tight_width_pct": 5.0,
            "contraction_ratio": 0.25,
            "price": 110.0, "strict_price": 100.0, "stop_loss": 92.0,
        }, {"reject": None}, notified=True)

        # BREAKOUT #2 — strict 거부되어도 base/stop 표본에는 들어가야 함
        _funnel_record(funnel, {
            "signal_type": "BREAKOUT", "filter_reasons": ["rs_below_zero"],
            "base_width_pct": 24.0, "tight_width_pct": 9.0,
            "contraction_ratio": 0.375,
            "price": 50.0, "strict_price": 50.0, "stop_loss": 45.0,
        }, {"reject": None}, notified=False)

        # BREAKOUT #3 — strict_price 가 없는 신호 (예: 주봉 데이터 부족) →
        # stop_pct_stats(진입가 기준) 표본에서는 제외되지만, base_stats 와
        # stop_pct_at_current_price(현재가 기준) 표본에는 여전히 들어간다.
        _funnel_record(funnel, {
            "signal_type": "BREAKOUT", "filter_reasons": [],
            "base_width_pct": 22.0, "tight_width_pct": 6.0,
            "contraction_ratio": 0.27,
            "price": 200.0, "stop_loss": 180.0,
        }, {"reject": None}, notified=False)

        # REBOUND — signal_type 이 다르므로 base_stats/stop_pct_stats 표본에서 제외
        _funnel_record(funnel, {
            "signal_type": "REBOUND", "filter_reasons": [],
            "price": 30.0, "strict_price": 30.0, "stop_loss": 27.0,
        }, {"reject": None}, notified=True)

        _finalize_funnel("US", funnel, count=4)

        assert funnel["base_stats"]["n"] == 3
        assert funnel["base_stats"]["base_width_median"] == pytest.approx(22.0, abs=0.01)
        assert funnel["base_stats"]["tight_width_median"] == pytest.approx(6.0, abs=0.01)
        assert funnel["base_stats"]["contraction_ratio_median"] == pytest.approx(0.27, abs=0.001)

        # stop_pct(strict_price 기준) = (strict_price-stop)/strict_price
        #   #1: 8/100=0.08, #2: 5/50=0.10, #3: strict_price 없어 제외 → n=2
        assert funnel["stop_pct_stats"]["n"] == 2
        assert funnel["stop_pct_stats"]["median"] == pytest.approx(0.09, abs=0.001)

        # stop_pct_at_current_price(price 기준) = (price-stop)/price
        #   #1: 18/110=0.1636, #2: 5/50=0.10, #3: 20/200=0.10 → n=3
        assert funnel["stop_pct_at_current_price"]["n"] == 3
        assert funnel["stop_pct_at_current_price"]["median"] == pytest.approx(0.10, abs=0.001)

        # 괴리 확인 — #1 은 strict_price 기준(0.08)과 current 기준이 다름
        assert funnel["stop_pct_stats"]["median"] != funnel["stop_pct_at_current_price"]["median"]

    def test_funnel_json_includes_step2_fields_and_no_internal_leak(self):
        """JSON 스냅샷에 Step 2 필드가 실리고, 내부 누적용 키(_로 시작)는
        전부 pop 되어 새지 않는다."""
        import json
        from scanner.scan_engine import _new_funnel, _funnel_record, _finalize_funnel

        funnel = _new_funnel()
        _funnel_record(funnel, {
            "signal_type": "BREAKOUT", "filter_reasons": [],
            "base_width_pct": 20.0, "tight_width_pct": 5.0,
            "contraction_ratio": 0.25, "price": 100.0, "stop_loss": 92.0,
        }, {"reject": None}, notified=True)
        _finalize_funnel("US", funnel, count=1)

        assert set(funnel) >= {
            "base_mode", "base_stats", "stop_pct_stats",
            "weekly_gate_cut_but_would_pass_daily", "weekly_vol_ratio_stats",
        }
        assert not any(k.startswith("_") for k in funnel), "내부 샘플 리스트 키가 누출됨"
        # JSON 직렬화 가능해야 함 (파일 기록과 동일 경로)
        json.dumps(funnel, ensure_ascii=False)


# ── 실행 ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import subprocess
    subprocess.run(
        [sys.executable, "-m", "pytest", __file__, "-v", "--tb=short"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
