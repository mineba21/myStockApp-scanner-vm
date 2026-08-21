"""Strict Weinstein optimal-buy filter — gate decision unit tests.

Phase 3 scope: 8 게이트 전부 + ``apply_strict_filter`` 엔트리포인트 검증.

설계 원칙:
- 외부 데이터 fetch / DB 접근 없음 — 순수 dict 입력만 사용.
- monkeypatch 로 STRICT_* 플래그를 *명시적* 으로 set 하여 기본값 변경에
  영향받지 않게 한다 (CLAUDE.md "Strategy invariants").
- 각 게이트는 단위 fail/pass 케이스 + 해당 reason enum 상수 직접 단언.
- ``TestApplyStrictFilter`` 는 여러 게이트가 *동시에* 실패할 때 모든
  사유가 누적되는지(early-exit 안 함) 통합 검증.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


# ── 헬퍼 ──────────────────────────────────────────────────────────

def _force_rs_strict_flags(monkeypatch, *,
                           require_positive: bool = True,
                           require_rising:   bool = True,
                           require_zero_cross_for_breakout: bool = True):
    """strict_filter 모듈이 import 시 캡처한 config 플래그를 명시적으로 강제."""
    from scanner import strict_filter
    monkeypatch.setattr(strict_filter, "STRICT_REQUIRE_RS_POSITIVE",
                        require_positive, raising=False)
    monkeypatch.setattr(strict_filter, "STRICT_REQUIRE_RS_RISING",
                        require_rising, raising=False)
    monkeypatch.setattr(strict_filter, "STRICT_REQUIRE_RS_ZERO_CROSS_FOR_BREAKOUT",
                        require_zero_cross_for_breakout, raising=False)


def _passing_breakout_signal(**overrides):
    """RS gate 를 모두 통과하는 baseline BREAKOUT 시그널."""
    sig = {
        "signal_type":     "BREAKOUT",
        "rs_value":        4.5,
        "rs_trend":        "RISING",
        "rs_zero_crossed": True,
    }
    sig.update(overrides)
    return sig


# ══════════════════════════════════════════════════════════════════
# Gate 6 — Mansfield Relative Strength
# ══════════════════════════════════════════════════════════════════

class TestRSGate:
    def test_rs_below_zero_blocks(self, monkeypatch):
        """rs_value < 0 + STRICT_REQUIRE_RS_POSITIVE → 'rs_below_zero'."""
        from scanner.strict_filter import _check_rs, RS_BELOW_ZERO
        _force_rs_strict_flags(monkeypatch)

        sig = _passing_breakout_signal(rs_value=-1.0)
        ctx = {"benchmark_present": True}
        reasons = []
        _check_rs(sig, ctx, reasons)

        assert RS_BELOW_ZERO in reasons

    def test_rs_falling_blocks(self, monkeypatch):
        """rs_trend == 'FALLING' + STRICT_REQUIRE_RS_RISING → 'rs_falling'."""
        from scanner.strict_filter import _check_rs, RS_FALLING
        _force_rs_strict_flags(monkeypatch)

        sig = _passing_breakout_signal(rs_trend="FALLING")
        ctx = {"benchmark_present": True}
        reasons = []
        _check_rs(sig, ctx, reasons)

        assert RS_FALLING in reasons

    def test_no_benchmark_blocks(self, monkeypatch):
        """벤치마크 자체가 없으면 (benchmark_present=False) → 'rs_benchmark_missing'.

        CLAUDE.md spec: 시장 RS 비교가 불가능하면 strict 매수 불허.
        """
        from scanner.strict_filter import _check_rs, RS_BENCHMARK_MISSING
        _force_rs_strict_flags(monkeypatch)

        sig = _passing_breakout_signal(rs_value=None, rs_zero_crossed=False)
        ctx = {"benchmark_present": False}
        reasons = []
        _check_rs(sig, ctx, reasons)

        assert RS_BENCHMARK_MISSING in reasons

    def test_no_zero_cross_blocks_breakout(self, monkeypatch):
        """BREAKOUT + rs_zero_crossed != True → 'rs_no_zero_cross'.

        CLAUDE.md: BREAKOUT 의 strict 조건은 *최근* RS 가 0선 위로 올라온 흔적.
        """
        from scanner.strict_filter import _check_rs, RS_NO_ZERO_CROSS
        _force_rs_strict_flags(monkeypatch)

        sig = _passing_breakout_signal(rs_zero_crossed=False)
        ctx = {"benchmark_present": True}
        reasons = []
        _check_rs(sig, ctx, reasons)

        assert RS_NO_ZERO_CROSS in reasons

        # None 도 동일 — 미산출은 fail
        sig2 = _passing_breakout_signal(rs_zero_crossed=None)
        reasons2 = []
        _check_rs(sig2, ctx, reasons2)
        assert RS_NO_ZERO_CROSS in reasons2

    def test_rebound_does_not_require_zero_cross(self, monkeypatch):
        """REBOUND 은 RS zero-cross 강제 게이트 대상 아님 (BREAKOUT 한정)."""
        from scanner.strict_filter import _check_rs, RS_NO_ZERO_CROSS
        _force_rs_strict_flags(monkeypatch)

        sig = _passing_breakout_signal(signal_type="REBOUND",
                                       rs_zero_crossed=False)
        ctx = {"benchmark_present": True}
        reasons = []
        _check_rs(sig, ctx, reasons)

        assert RS_NO_ZERO_CROSS not in reasons

    def test_all_rs_gates_pass(self, monkeypatch):
        """모든 RS 조건 만족 → reasons 변화 없음."""
        from scanner.strict_filter import _check_rs
        _force_rs_strict_flags(monkeypatch)

        sig = _passing_breakout_signal()
        ctx = {"benchmark_present": True}
        reasons = []
        _check_rs(sig, ctx, reasons)

        assert reasons == []


# ══════════════════════════════════════════════════════════════════
# 공통 헬퍼 — Phase 3 게이트
# ══════════════════════════════════════════════════════════════════

def _force_strict_flag(monkeypatch, name: str, value):
    """strict_filter 모듈에서 캡처한 단일 STRICT_* 플래그 강제."""
    from scanner import strict_filter
    monkeypatch.setattr(strict_filter, name, value, raising=False)


def _force_all_strict_flags(monkeypatch, *,
                            mode: bool = True,
                            require_market_confirmation: bool = True,
                            block_caution_breakouts: bool = True,
                            require_sector_stage2: bool = False,
                            require_price_above_weekly_30ma: bool = True,
                            require_price_above_daily_150ma: bool = True,
                            require_breakout_volume: bool = True,
                            require_rs_positive: bool = True,
                            require_rs_rising: bool = True,
                            require_rs_zero_cross_for_breakout: bool = True,
                            require_stop_loss: bool = True):
    """apply_strict_filter 통합 테스트용 — 14 STRICT_* 모두 명시 강제.

    기본값은 plan 의 production 권장값과 동일(mode=True, sector=False).
    """
    _force_strict_flag(monkeypatch, "STRICT_WEINSTEIN_MODE", mode)
    _force_strict_flag(monkeypatch, "STRICT_REQUIRE_MARKET_CONFIRMATION", require_market_confirmation)
    _force_strict_flag(monkeypatch, "STRICT_BLOCK_CAUTION_BREAKOUTS",     block_caution_breakouts)
    _force_strict_flag(monkeypatch, "STRICT_REQUIRE_SECTOR_STAGE2",       require_sector_stage2)
    _force_strict_flag(monkeypatch, "STRICT_REQUIRE_PRICE_ABOVE_WEEKLY_30MA", require_price_above_weekly_30ma)
    _force_strict_flag(monkeypatch, "STRICT_REQUIRE_PRICE_ABOVE_DAILY_150MA", require_price_above_daily_150ma)
    _force_strict_flag(monkeypatch, "STRICT_REQUIRE_BREAKOUT_VOLUME",     require_breakout_volume)
    _force_strict_flag(monkeypatch, "STRICT_REQUIRE_RS_POSITIVE",         require_rs_positive)
    _force_strict_flag(monkeypatch, "STRICT_REQUIRE_RS_RISING",           require_rs_rising)
    _force_strict_flag(monkeypatch, "STRICT_REQUIRE_RS_ZERO_CROSS_FOR_BREAKOUT", require_rs_zero_cross_for_breakout)
    _force_strict_flag(monkeypatch, "STRICT_REQUIRE_STOP_LOSS",           require_stop_loss)


def _full_passing_breakout_signal(**overrides):
    """8 게이트 모두 통과하는 baseline BREAKOUT 시그널 dict.

    공개 필드(price/ma150/sma30w/...) 는 display/persist 용으로 유지하되,
    Gate 3/5/7/8 은 ``strict_*`` 스냅샷만 읽으므로 두 부류를 모두 갖는다.
    헬퍼는 ``signal_date == last_bar`` 즉 두 값이 동일한 normal-case 를
    표현. 게이트별 단위 테스트가 필요할 때 override 로 차이를 줄 수 있다.
    """
    sig = {
        "signal_type":         "BREAKOUT",
        # 공개 (display/persist) — last-bar 의미
        "price":               110.0,
        "ma150":               100.0,
        "sma30w":               95.0,
        "slope30w":              0.5,
        "weekly_stage":         "STAGE2",
        "volume_ratio":          3.5,    # detect_* 가 sig 그대로 노출 → signal-date
        "weekly_volume_ratio":   2.5,
        # strict_* (gate 평가) — signal-date 의미
        "strict_price":              110.0,    # ext = +10% (< 15% limit)
        "strict_ma150":              100.0,
        "strict_sma30w":              95.0,    # ext_w = +15.8% (< 30% limit)
        "strict_slope30w":             0.5,    # 양수
        "strict_weekly_stage":     "STAGE2",
        "strict_weekly_volume_ratio":  2.5,    # ≥ 2.0
        # base / RS / stop
        "pivot_price":         105.0,
        "base_low":             95.0,
        "base_weeks":            8.0,    # ≥ BASE_MIN_WEEKS=5
        "base_quality_v4":     "TIGHT",
        "rs_value":              4.5,
        "rs_trend":             "RISING",
        "rs_zero_crossed":     True,
        "stop_loss":            94.0,    # < strict_price=110
    }
    sig.update(overrides)
    return sig


def _full_passing_ctx(**overrides):
    """8 게이트 모두 통과하는 baseline ctx."""
    ctx = {
        "market_condition":  "BULL",
        "sector_stage":      None,        # STRICT_REQUIRE_SECTOR_STAGE2=False 기본
        "benchmark_present": True,
    }
    ctx.update(overrides)
    return ctx


# ══════════════════════════════════════════════════════════════════
# Gate 1 — Market
# ══════════════════════════════════════════════════════════════════

class TestMarketGate:
    def test_bear_blocks(self, monkeypatch):
        from scanner.strict_filter import _check_market, MARKET_BEAR
        _force_strict_flag(monkeypatch, "STRICT_REQUIRE_MARKET_CONFIRMATION", True)
        _force_strict_flag(monkeypatch, "STRICT_BLOCK_CAUTION_BREAKOUTS",     True)

        reasons = []
        _check_market({"signal_type": "BREAKOUT"},
                      {"market_condition": "BEAR"}, reasons)
        assert MARKET_BEAR in reasons

    def test_unknown_blocks_when_required(self, monkeypatch):
        from scanner.strict_filter import _check_market, MARKET_UNKNOWN
        _force_strict_flag(monkeypatch, "STRICT_REQUIRE_MARKET_CONFIRMATION", True)
        _force_strict_flag(monkeypatch, "STRICT_BLOCK_CAUTION_BREAKOUTS",     True)

        reasons = []
        _check_market({"signal_type": "BREAKOUT"},
                      {"market_condition": "UNKNOWN"}, reasons)
        assert MARKET_UNKNOWN in reasons

    def test_unknown_passes_when_not_required(self, monkeypatch):
        from scanner.strict_filter import _check_market, MARKET_UNKNOWN
        _force_strict_flag(monkeypatch, "STRICT_REQUIRE_MARKET_CONFIRMATION", False)

        reasons = []
        _check_market({"signal_type": "BREAKOUT"},
                      {"market_condition": "UNKNOWN"}, reasons)
        assert MARKET_UNKNOWN not in reasons

    def test_caution_blocks_breakout(self, monkeypatch):
        from scanner.strict_filter import _check_market, MARKET_CAUTION_BREAKOUT
        _force_strict_flag(monkeypatch, "STRICT_REQUIRE_MARKET_CONFIRMATION", True)
        _force_strict_flag(monkeypatch, "STRICT_BLOCK_CAUTION_BREAKOUTS",     True)

        reasons = []
        _check_market({"signal_type": "BREAKOUT"},
                      {"market_condition": "CAUTION"}, reasons)
        assert MARKET_CAUTION_BREAKOUT in reasons

    def test_caution_does_not_block_rebound(self, monkeypatch):
        """CAUTION 은 BREAKOUT/RE_BREAKOUT 만 차단; REBOUND 는 통과."""
        from scanner.strict_filter import _check_market, MARKET_CAUTION_BREAKOUT
        _force_strict_flag(monkeypatch, "STRICT_BLOCK_CAUTION_BREAKOUTS", True)

        reasons = []
        _check_market({"signal_type": "REBOUND"},
                      {"market_condition": "CAUTION"}, reasons)
        assert MARKET_CAUTION_BREAKOUT not in reasons

    def test_bull_passes(self, monkeypatch):
        from scanner.strict_filter import _check_market
        _force_strict_flag(monkeypatch, "STRICT_REQUIRE_MARKET_CONFIRMATION", True)

        reasons = []
        _check_market({"signal_type": "BREAKOUT"},
                      {"market_condition": "BULL"}, reasons)
        assert reasons == []


# ══════════════════════════════════════════════════════════════════
# Gate 2 — Sector (stub)
# ══════════════════════════════════════════════════════════════════

class TestSectorGate:
    def test_sector_stage2_passes_when_required(self, monkeypatch):
        from scanner.strict_filter import _check_sector
        _force_strict_flag(monkeypatch, "STRICT_REQUIRE_SECTOR_STAGE2", True)

        reasons = []
        _check_sector({}, {"sector_stage": "STAGE2"}, reasons)
        assert reasons == []

    def test_sector_stage4_blocks(self, monkeypatch):
        from scanner.strict_filter import _check_sector, SECTOR_STAGE4
        _force_strict_flag(monkeypatch, "STRICT_REQUIRE_SECTOR_STAGE2", True)

        reasons = []
        _check_sector({}, {"sector_stage": "STAGE4"}, reasons)
        assert SECTOR_STAGE4 in reasons

    def test_unknown_sector_blocks_when_required(self, monkeypatch):
        from scanner.strict_filter import _check_sector, SECTOR_NOT_STAGE2
        _force_strict_flag(monkeypatch, "STRICT_REQUIRE_SECTOR_STAGE2", True)

        reasons = []
        _check_sector({}, {"sector_stage": None}, reasons)
        assert SECTOR_NOT_STAGE2 in reasons

    def test_disabled_default_passes_anything(self, monkeypatch):
        """STRICT_REQUIRE_SECTOR_STAGE2=False (기본) 면 sector 무시."""
        from scanner.strict_filter import _check_sector
        _force_strict_flag(monkeypatch, "STRICT_REQUIRE_SECTOR_STAGE2", False)

        for stage in (None, "UNKNOWN", "STAGE1", "STAGE3", "STAGE4"):
            reasons = []
            _check_sector({}, {"sector_stage": stage}, reasons)
            assert reasons == [], f"sector_stage={stage} 가 잘못 차단됨: {reasons}"


# ══════════════════════════════════════════════════════════════════
# Gate 3 — Stock weekly stage
# ══════════════════════════════════════════════════════════════════

class TestStageGate:
    def _flags(self, monkeypatch):
        _force_strict_flag(monkeypatch, "STRICT_REQUIRE_PRICE_ABOVE_WEEKLY_30MA", True)
        _force_strict_flag(monkeypatch, "STRICT_REQUIRE_PRICE_ABOVE_DAILY_150MA", True)

    def test_weekly_data_missing_blocks(self, monkeypatch):
        from scanner.strict_filter import _check_weekly_stage, WEEKLY_DATA_MISSING
        self._flags(monkeypatch)

        reasons = []
        _check_weekly_stage({"signal_type": "BREAKOUT", "strict_price": 100.0,
                             "strict_sma30w": None, "strict_weekly_stage": None}, reasons)
        assert WEEKLY_DATA_MISSING in reasons

    def test_below_weekly_30ma_blocks(self, monkeypatch):
        from scanner.strict_filter import _check_weekly_stage, BELOW_WEEKLY_30MA
        self._flags(monkeypatch)

        reasons = []
        _check_weekly_stage({"signal_type": "BREAKOUT", "strict_price": 90.0,
                             "strict_ma150": 85.0, "strict_sma30w": 95.0,
                             "strict_slope30w": 0.5, "strict_weekly_stage": "STAGE2"}, reasons)
        assert BELOW_WEEKLY_30MA in reasons

    def test_below_daily_150ma_breakout(self, monkeypatch):
        from scanner.strict_filter import _check_weekly_stage, BELOW_DAILY_150MA
        self._flags(monkeypatch)

        reasons = []
        # strict_price 가 strict_sma30w 위지만 strict_ma150 아래 — BREAKOUT 만 차단
        _check_weekly_stage({"signal_type": "BREAKOUT", "strict_price": 96.0,
                             "strict_ma150": 100.0, "strict_sma30w": 95.0,
                             "strict_slope30w": 0.5, "strict_weekly_stage": "STAGE2"}, reasons)
        assert BELOW_DAILY_150MA in reasons

    def test_below_daily_150ma_does_not_block_rebound(self, monkeypatch):
        """일봉 MA150 아래 차단은 BREAKOUT 한정."""
        from scanner.strict_filter import _check_weekly_stage, BELOW_DAILY_150MA
        self._flags(monkeypatch)

        reasons = []
        _check_weekly_stage({"signal_type": "REBOUND", "strict_price": 96.0,
                             "strict_ma150": 100.0, "strict_sma30w": 95.0,
                             "strict_slope30w": 0.5, "strict_weekly_stage": "STAGE2"}, reasons)
        assert BELOW_DAILY_150MA not in reasons

    def test_stage3_blocks(self, monkeypatch):
        from scanner.strict_filter import _check_weekly_stage, STAGE_STAGE3
        self._flags(monkeypatch)

        reasons = []
        _check_weekly_stage({"signal_type": "BREAKOUT", "strict_price": 110.0,
                             "strict_ma150": 100.0, "strict_sma30w": 95.0,
                             "strict_slope30w": 0.0, "strict_weekly_stage": "STAGE3"}, reasons)
        assert STAGE_STAGE3 in reasons

    def test_stage4_blocks(self, monkeypatch):
        from scanner.strict_filter import _check_weekly_stage, STAGE_STAGE4
        self._flags(monkeypatch)

        reasons = []
        _check_weekly_stage({"signal_type": "BREAKOUT", "strict_price": 110.0,
                             "strict_ma150": 100.0, "strict_sma30w": 95.0,
                             "strict_slope30w": -0.5, "strict_weekly_stage": "STAGE4"}, reasons)
        assert STAGE_STAGE4 in reasons

    def test_stage2_with_negative_slope_blocks(self, monkeypatch):
        from scanner.strict_filter import _check_weekly_stage, WEEKLY_30MA_SLOPE_NEGATIVE
        self._flags(monkeypatch)

        reasons = []
        _check_weekly_stage({"signal_type": "BREAKOUT", "strict_price": 110.0,
                             "strict_ma150": 100.0, "strict_sma30w": 95.0,
                             "strict_slope30w": -0.1, "strict_weekly_stage": "STAGE2"}, reasons)
        assert WEEKLY_30MA_SLOPE_NEGATIVE in reasons

    def test_stage2_with_positive_slope_passes(self, monkeypatch):
        from scanner.strict_filter import _check_weekly_stage
        self._flags(monkeypatch)

        reasons = []
        _check_weekly_stage({"signal_type": "BREAKOUT", "strict_price": 110.0,
                             "strict_ma150": 100.0, "strict_sma30w": 95.0,
                             "strict_slope30w": 0.5, "strict_weekly_stage": "STAGE2"}, reasons)
        assert reasons == []


# ══════════════════════════════════════════════════════════════════
# Gate 4 — Base / Pivot
# ══════════════════════════════════════════════════════════════════

class TestBaseGate:
    def test_breakout_missing_pivot_blocks(self):
        from scanner.strict_filter import _check_base, BASE_INSUFFICIENT
        reasons = []
        _check_base({"signal_type": "BREAKOUT",
                     "pivot_price": None,
                     "base_weeks": 8.0,
                     "base_quality_v4": "TIGHT"}, reasons)
        assert BASE_INSUFFICIENT in reasons

    def test_breakout_short_base_blocks(self):
        from scanner.strict_filter import _check_base, BASE_INSUFFICIENT
        reasons = []
        _check_base({"signal_type": "BREAKOUT",
                     "pivot_price": 100.0,
                     "base_weeks": 3.0,    # < BASE_MIN_WEEKS=5
                     "base_quality_v4": "TIGHT"}, reasons)
        assert BASE_INSUFFICIENT in reasons

    def test_breakout_wide_base_blocks(self):
        from scanner.strict_filter import _check_base, BASE_TOO_WIDE
        reasons = []
        _check_base({"signal_type": "BREAKOUT",
                     "pivot_price": 100.0,
                     "base_weeks": 8.0,
                     "base_quality_v4": "WIDE"}, reasons)
        assert BASE_TOO_WIDE in reasons

    def test_breakout_loose_base_passes(self):
        """LOOSE 는 sub-optimal 이지만 hard-block 대상은 WIDE 만."""
        from scanner.strict_filter import _check_base
        reasons = []
        _check_base({"signal_type": "BREAKOUT",
                     "pivot_price": 100.0,
                     "base_weeks": 8.0,
                     "base_quality_v4": "LOOSE"}, reasons)
        assert reasons == []

    def test_rebound_no_retest_blocks(self):
        from scanner.strict_filter import _check_base, REBOUND_NO_RETEST
        reasons = []
        _check_base({"signal_type": "REBOUND",
                     "v4_gate": None}, reasons)
        assert REBOUND_NO_RETEST in reasons

    def test_rebound_with_retest_passes(self):
        from scanner.strict_filter import _check_base
        reasons = []
        for gate in ("BASE_RETEST", "30W_RETEST"):
            r = []
            _check_base({"signal_type": "REBOUND", "v4_gate": gate}, r)
            assert r == [], f"v4_gate={gate} 통과해야 함"


# ══════════════════════════════════════════════════════════════════
# Gate 5 — Volume
# ══════════════════════════════════════════════════════════════════

class TestVolumeGate:
    def test_low_daily_volume_blocks_breakout(self, monkeypatch):
        from scanner.strict_filter import _check_volume, BREAKOUT_DAILY_VOLUME
        _force_strict_flag(monkeypatch, "STRICT_REQUIRE_BREAKOUT_VOLUME", True)
        _force_strict_flag(monkeypatch, "BREAKOUT_DAILY_VOL_RATIO",  3.0)
        _force_strict_flag(monkeypatch, "BREAKOUT_WEEKLY_VOL_RATIO", 2.0)

        reasons = []
        _check_volume({"signal_type": "BREAKOUT",
                       "volume_ratio": 1.5,
                       "strict_weekly_volume_ratio": 2.5}, reasons)
        assert BREAKOUT_DAILY_VOLUME in reasons

    def test_low_weekly_volume_blocks_breakout(self, monkeypatch):
        from scanner.strict_filter import _check_volume, BREAKOUT_WEEKLY_VOLUME
        _force_strict_flag(monkeypatch, "STRICT_REQUIRE_BREAKOUT_VOLUME", True)
        _force_strict_flag(monkeypatch, "BREAKOUT_DAILY_VOL_RATIO",  3.0)
        _force_strict_flag(monkeypatch, "BREAKOUT_WEEKLY_VOL_RATIO", 2.0)

        reasons = []
        _check_volume({"signal_type": "BREAKOUT",
                       "volume_ratio": 3.5,
                       "strict_weekly_volume_ratio": 1.0}, reasons)
        assert BREAKOUT_WEEKLY_VOLUME in reasons

    def test_volume_gate_skips_rebound(self, monkeypatch):
        from scanner.strict_filter import _check_volume
        _force_strict_flag(monkeypatch, "STRICT_REQUIRE_BREAKOUT_VOLUME", True)

        reasons = []
        _check_volume({"signal_type": "REBOUND",
                       "volume_ratio": 0.5,
                       "strict_weekly_volume_ratio": 0.5}, reasons)
        assert reasons == []

    def test_disabled_passes(self, monkeypatch):
        from scanner.strict_filter import _check_volume
        _force_strict_flag(monkeypatch, "STRICT_REQUIRE_BREAKOUT_VOLUME", False)

        reasons = []
        _check_volume({"signal_type": "BREAKOUT",
                       "volume_ratio": 0.1,
                       "strict_weekly_volume_ratio": 0.1}, reasons)
        assert reasons == []

    def test_missing_weekly_ratio_does_not_block(self, monkeypatch):
        """주봉 데이터 부재(strict_weekly_volume_ratio=None)는 Gate 3 으로 흡수."""
        from scanner.strict_filter import _check_volume, BREAKOUT_WEEKLY_VOLUME
        _force_strict_flag(monkeypatch, "STRICT_REQUIRE_BREAKOUT_VOLUME", True)

        reasons = []
        _check_volume({"signal_type": "BREAKOUT",
                       "volume_ratio": 5.0,
                       "strict_weekly_volume_ratio": None}, reasons)
        assert BREAKOUT_WEEKLY_VOLUME not in reasons


# ══════════════════════════════════════════════════════════════════
# Gate 7 — Extension
# ══════════════════════════════════════════════════════════════════

class TestExtensionGate:
    def test_extended_above_ma150_blocks(self, monkeypatch):
        from scanner.strict_filter import _check_extension, EXTENDED_ABOVE_MA150
        _force_strict_flag(monkeypatch, "BREAKOUT_MAX_EXTENDED_PCT", 15.0)

        reasons = []
        # +20% over strict_ma150
        _check_extension({"signal_type": "BREAKOUT",
                          "strict_price": 120.0, "strict_ma150": 100.0,
                          "strict_sma30w":  95.0}, reasons)
        assert EXTENDED_ABOVE_MA150 in reasons

    def test_extended_above_30w_blocks_breakout(self, monkeypatch):
        from scanner.strict_filter import _check_extension, EXTENDED_ABOVE_30W
        _force_strict_flag(monkeypatch, "BREAKOUT_MAX_EXTENDED_PCT", 15.0)

        reasons = []
        # MA150 +10% (under limit) but 30W +35% (over 30%)
        _check_extension({"signal_type": "BREAKOUT",
                          "strict_price": 110.0, "strict_ma150": 100.0,
                          "strict_sma30w":  81.0}, reasons)   # 110/81 - 1 ≈ +35.8%
        assert EXTENDED_ABOVE_30W in reasons

    def test_extended_above_30w_does_not_apply_to_rebound(self, monkeypatch):
        """30W 연장 차단은 BREAKOUT 한정 (REBOUND 는 그 자체가 retest)."""
        from scanner.strict_filter import _check_extension, EXTENDED_ABOVE_30W
        _force_strict_flag(monkeypatch, "BREAKOUT_MAX_EXTENDED_PCT", 15.0)

        reasons = []
        _check_extension({"signal_type": "REBOUND",
                          "strict_price": 110.0, "strict_ma150": 100.0,
                          "strict_sma30w":  81.0}, reasons)
        assert EXTENDED_ABOVE_30W not in reasons

    def test_within_limit_passes(self, monkeypatch):
        from scanner.strict_filter import _check_extension
        _force_strict_flag(monkeypatch, "BREAKOUT_MAX_EXTENDED_PCT", 15.0)

        reasons = []
        _check_extension({"signal_type": "BREAKOUT",
                          "strict_price": 110.0, "strict_ma150": 100.0,
                          "strict_sma30w":  95.0}, reasons)
        assert reasons == []


# ══════════════════════════════════════════════════════════════════
# Gate 8 — Stop-loss
# ══════════════════════════════════════════════════════════════════

class TestStopLossGate:
    def test_missing_stop_loss_blocks_when_required(self, monkeypatch):
        from scanner.strict_filter import _check_stop_loss, STOP_LOSS_MISSING
        _force_strict_flag(monkeypatch, "STRICT_REQUIRE_STOP_LOSS", True)

        reasons = []
        _check_stop_loss({"stop_loss": None, "strict_price": 100.0}, reasons)
        assert STOP_LOSS_MISSING in reasons

    def test_missing_stop_loss_passes_when_not_required(self, monkeypatch):
        from scanner.strict_filter import _check_stop_loss, STOP_LOSS_MISSING
        _force_strict_flag(monkeypatch, "STRICT_REQUIRE_STOP_LOSS", False)

        reasons = []
        _check_stop_loss({"stop_loss": None, "strict_price": 100.0}, reasons)
        assert STOP_LOSS_MISSING not in reasons

    def test_stop_above_price_blocks_always(self, monkeypatch):
        """sanity 검사는 STRICT_REQUIRE_STOP_LOSS 와 무관 — 항상 활성.

        sanity 비교 대상은 ``strict_price`` (signal-date close) — Phase 2 에서
        stop_loss 가 신호일 가격 기준으로 산출되므로 일관성 위해.
        """
        from scanner.strict_filter import _check_stop_loss, STOP_LOSS_ABOVE_PRICE
        _force_strict_flag(monkeypatch, "STRICT_REQUIRE_STOP_LOSS", False)

        reasons = []
        _check_stop_loss({"stop_loss": 105.0, "strict_price": 100.0}, reasons)
        assert STOP_LOSS_ABOVE_PRICE in reasons

    def test_stop_equal_price_blocks(self, monkeypatch):
        """stop == strict_price (의미 없음) 도 sanity 차단."""
        from scanner.strict_filter import _check_stop_loss, STOP_LOSS_ABOVE_PRICE
        reasons = []
        _check_stop_loss({"stop_loss": 100.0, "strict_price": 100.0}, reasons)
        assert STOP_LOSS_ABOVE_PRICE in reasons

    def test_valid_stop_passes(self, monkeypatch):
        from scanner.strict_filter import _check_stop_loss
        _force_strict_flag(monkeypatch, "STRICT_REQUIRE_STOP_LOSS", True)

        reasons = []
        _check_stop_loss({"stop_loss": 95.0, "strict_price": 100.0}, reasons)
        assert reasons == []


# ══════════════════════════════════════════════════════════════════
# Entry-point — apply_strict_filter
# ══════════════════════════════════════════════════════════════════

class TestApplyStrictFilter:
    def test_all_gates_pass_returns_true(self, monkeypatch):
        """8 게이트 모두 통과 → (True, [])."""
        from scanner.strict_filter import apply_strict_filter
        _force_all_strict_flags(monkeypatch)

        passed, reasons = apply_strict_filter(
            _full_passing_breakout_signal(),
            _full_passing_ctx(),
        )
        assert passed is True
        assert reasons == []

    def test_strict_mode_off_bypasses_all_gates(self, monkeypatch):
        """STRICT_WEINSTEIN_MODE=False → 모든 검사 우회 (legacy 호환)."""
        from scanner.strict_filter import apply_strict_filter
        _force_all_strict_flags(monkeypatch, mode=False)

        # 거의 모든 게이트가 fail 할 시그널이지만 우회
        bad_sig = _full_passing_breakout_signal(
            rs_value=-5.0, rs_trend="FALLING", rs_zero_crossed=False,
            stop_loss=None, base_quality_v4="WIDE",
            volume_ratio=0.1, weekly_volume_ratio=0.1)
        bad_ctx = _full_passing_ctx(market_condition="BEAR")

        passed, reasons = apply_strict_filter(bad_sig, bad_ctx)
        assert passed is True
        assert reasons == []

    def test_multiple_gates_fail_accumulates_all_reasons(self, monkeypatch):
        """early-exit 안 함 — 실패한 모든 게이트의 사유가 누적."""
        from scanner.strict_filter import (
            apply_strict_filter,
            MARKET_BEAR, RS_BELOW_ZERO, STOP_LOSS_MISSING,
            BREAKOUT_DAILY_VOLUME,
        )
        _force_all_strict_flags(monkeypatch)

        # Market BEAR + RS 음수 + stop_loss 누락 + 거래량 부족
        sig = _full_passing_breakout_signal(
            rs_value=-2.0, stop_loss=None, volume_ratio=0.5)
        ctx = _full_passing_ctx(market_condition="BEAR")

        passed, reasons = apply_strict_filter(sig, ctx)
        assert passed is False
        # 정확한 enum 4 가지 모두 누적되어 있어야 함
        for r in (MARKET_BEAR, RS_BELOW_ZERO, STOP_LOSS_MISSING,
                  BREAKOUT_DAILY_VOLUME):
            assert r in reasons, f"{r} 누락 — got {reasons}"

    def test_single_gate_fail_returns_false(self, monkeypatch):
        from scanner.strict_filter import apply_strict_filter, RS_BELOW_ZERO
        _force_all_strict_flags(monkeypatch)

        sig = _full_passing_breakout_signal(rs_value=-1.0)
        ctx = _full_passing_ctx()

        passed, reasons = apply_strict_filter(sig, ctx)
        assert passed is False
        assert RS_BELOW_ZERO in reasons
        # 다른 게이트는 통과했어야 — 정확히 RS_BELOW_ZERO 1개만
        assert len(reasons) == 1, f"기대 1개, got {reasons}"

    def test_rebound_signal_pass(self, monkeypatch):
        """REBOUND 시그널 — base/volume gate 우회, retest gate 만 적용."""
        from scanner.strict_filter import apply_strict_filter
        _force_all_strict_flags(monkeypatch)

        sig = _full_passing_breakout_signal(
            signal_type="REBOUND",
            v4_gate="30W_RETEST",
            # REBOUND 은 base_* / volume_ratio 없어도 OK
            base_weeks=None, base_quality_v4=None, pivot_price=None,
            volume_ratio=0.5, weekly_volume_ratio=0.5,
            # REBOUND 은 RS zero-cross 강제 안 됨
            rs_zero_crossed=False)
        ctx = _full_passing_ctx()

        passed, reasons = apply_strict_filter(sig, ctx)
        assert passed is True, f"REBOUND 통과해야 함: {reasons}"


# ── 실행 ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import subprocess
    subprocess.run(
        [sys.executable, "-m", "pytest", __file__, "-v", "--tb=short"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
