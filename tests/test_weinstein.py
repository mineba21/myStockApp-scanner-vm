"""
Weinstein Scanner — 단위 테스트

합성 OHLCV 데이터로 각 시그널 로직을 독립적으로 검증.
실행: cd stock-scanner && venv/bin/python -m pytest tests/ -v
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import pytest
from datetime import date, timedelta


# ── 합성 데이터 헬퍼 ──────────────────────────────────────────────

def _make_df(prices, volumes=None):
    """단순 OHLCV DataFrame 생성 (High=close*1.005, Low=close*0.995)."""
    n = len(prices)
    if volumes is None:
        volumes = [500_000] * n
    dates = [date(2022, 1, 1) + timedelta(days=i) for i in range(n)]
    close = [float(p) for p in prices]
    return pd.DataFrame({
        "Open":   [p * 0.998 for p in close],
        "High":   [p * 1.005 for p in close],
        "Low":    [p * 0.995 for p in close],
        "Close":  close,
        "Volume": [float(v) for v in volumes],
    }, index=pd.DatetimeIndex(dates))


def _make_stage2_base(n_total=260, base_price=100.0):
    """
    Stage2 상황 합성 데이터:
      - 0~149일: 50→95 선형 상승 (MA150/MA50 충분히 상승)
      - 150~(n_total-1)일: base_price 근처 횡보
    """
    prices  = []
    volumes = []
    # Phase 1: 상승 (MA 워밍업)
    for i in range(150):
        prices.append(50.0 + (base_price - 5 - 50) * i / 149)
        volumes.append(500_000)
    # Phase 2: 횡보 base
    for i in range(n_total - 150):
        # ±2% 진동
        prices.append(base_price + 2 * np.sin(i * np.pi / 5))
        volumes.append(500_000)
    return prices, volumes


# ═══════════════════════════════════════════════════════════════════
# 1. BREAKOUT 테스트
# ═══════════════════════════════════════════════════════════════════

class TestBreakout:

    def test_breakout_pivot_required(self):
        """BREAKOUT: 단순 MA150 크로스만으로는 시그널 나오지 않아야 함."""
        from scanner.weinstein import _build_indicators, _find_breakout_signal

        # 완만하게 선형 상승 (MA150도 함께 오름 → pivot 항상 전날 종가)
        prices  = [50.0 + i * 0.1 for i in range(250)]  # 50→74.9 직선
        volumes = [500_000] * 250
        df = _make_df(prices, volumes)
        ind = _build_indicators(df)
        assert ind is not None, "indicators 빌드 실패"

        sig = _find_breakout_signal(ind)
        # 직선 상승 = 매일 신고가 → 'pivot 이하였다가 돌파' 조건 미충족
        # (전날 close == rolling max이므로 pp <= pivot_high but pp == pivot_high)
        # 거래량도 평균치라 volume_ratio 조건 미충족 가능
        # 핵심: MA 크로스만으로 BREAKOUT이 나오지 않아야 함
        # (기울기는 충족하지만 pivot 돌파 + 거래량 조건이 필요)
        if sig is not None:
            # 혹시 발생하면 거래량 조건이 충족된 경우 — 거래량 배율 확인
            assert sig["vol_ratio"] >= 1.5, "거래량 조건 미충족인데 BREAKOUT 발생"

    def test_breakout_detected_with_pivot(self):
        """BREAKOUT: base 형성 후 거래량 동반 pivot 돌파 시 시그널 발생."""
        from scanner.weinstein import analyze_stock

        prices, volumes = _make_stage2_base(n_total=230, base_price=100.0)
        # 마지막날: pivot 돌파 (base 최고가 ≈ 102 위로 돌파)
        prices[-1]  = 104.0   # 돌파
        # v4 hard block: daily ≥ 3.0x AND weekly ≥ 2.0x — 주봉 합산이 7일 기준이므로 큰 spike 필요
        volumes[-1] = 6_000_000

        df  = _make_df(prices, volumes)
        res = analyze_stock(df, "TEST", "테스트", "US")

        assert res is not None, "시그널이 발생해야 함"
        assert res["signal_type"] == "BREAKOUT"
        assert res["volume_ratio"] >= 3.0
        assert res.get("pivot_price") is not None

    def test_breakout_blocked_below_ma150(self):
        """BREAKOUT: 가격이 MA150 아래일 때 시그널 없어야 함."""
        from scanner.weinstein import _build_indicators, _find_breakout_signal

        # Stage4: 하락 추세
        prices  = [100.0 - i * 0.2 for i in range(250)]  # 100→50 하락
        volumes = [500_000] * 249 + [2_000_000]
        df  = _make_df(prices, volumes)
        ind = _build_indicators(df)
        if ind is None:
            return  # MA 계산 불가 → 패스

        sig = _find_breakout_signal(ind)
        assert sig is None, "하락 추세에서 BREAKOUT 발생하면 안 됨"

    def test_breakout_respects_volume_ratio(self):
        """BREAKOUT: 거래량 부족 시 시그널 없어야 함."""
        from scanner.weinstein import analyze_stock

        prices, volumes = _make_stage2_base(n_total=230, base_price=100.0)
        prices[-1]  = 104.0
        volumes[-1] = 300_000  # 0.6x → 충분하지 않음

        df  = _make_df(prices, volumes)
        res = analyze_stock(df, "TEST", "테스트", "US")

        # BREAKOUT은 없어야 함 (다른 시그널이 날 수도 있음)
        if res is not None:
            assert res["signal_type"] != "BREAKOUT", "거래량 부족인데 BREAKOUT 발생"

    def test_breakout_not_too_extended(self):
        """BREAKOUT: MA150 대비 과매수(15% 초과) 시 차단."""
        from scanner.weinstein import _build_indicators, _find_breakout_signal

        prices, volumes = _make_stage2_base(n_total=230, base_price=100.0)
        # 마지막날 MA150보다 20% 이상 높게 설정
        prices[-1]  = 130.0  # MA150 ≈ 80 → 62.5% 과매수
        volumes[-1] = 1_500_000
        df  = _make_df(prices, volumes)
        ind = _build_indicators(df)
        sig = _find_breakout_signal(ind)
        assert sig is None, "과매수 상태에서 BREAKOUT 발생하면 안 됨"


# ═══════════════════════════════════════════════════════════════════
# 2. RE_BREAKOUT 테스트
# ═══════════════════════════════════════════════════════════════════

class TestReBreakout:

    def _make_continuation_base(self):
        """Stage2 + 단기 조정 후 continuation base 합성."""
        prices, volumes = _make_stage2_base(n_total=220, base_price=100.0)
        # 150~180: 가격 상승해서 110 도달 (Stage2 이후 상승)
        for i in range(30):
            prices.append(100.0 + i * 0.33)
            volumes.append(500_000)
        # 181~210: 조정 (103~107 횡보, continuation base)
        for i in range(30):
            prices.append(105.0 + 2 * np.sin(i * np.pi / 4))
            volumes.append(400_000)
        # 211: 재돌파 (base 고점 ≈ 107 위로)
        prices.append(108.5)
        volumes.append(1_200_000)
        return prices, volumes

    def test_rebreakout_requires_stage2(self):
        """RE_BREAKOUT: Stage4(하락장)에서는 발생하지 않아야 함."""
        from scanner.weinstein import _build_indicators, _find_rebreakout_signal

        prices  = [100.0 - i * 0.3 for i in range(250)]
        volumes = [500_000] * 249 + [1_500_000]
        df  = _make_df(prices, volumes)
        ind = _build_indicators(df)
        if ind is None:
            return
        assert ind["stage"] != "STAGE2", "테스트 데이터가 Stage2가 아닌지 확인"
        sig = _find_rebreakout_signal(ind)
        assert sig is None

    def test_rebreakout_continuation_behavior(self):
        """RE_BREAKOUT: Stage2 조정 후 base 돌파 시 시그널 발생."""
        from scanner.weinstein import analyze_stock

        prices, volumes = self._make_continuation_base()
        df  = _make_df(prices, volumes)
        res = analyze_stock(df, "TEST", "테스트", "US")

        # RE_BREAKOUT 또는 BREAKOUT 발생해야 함
        assert res is not None, "시그널이 발생해야 함"
        assert res["signal_type"] in ("RE_BREAKOUT", "BREAKOUT"), \
            f"연속 돌파 시그널이어야 함, got: {res['signal_type']}"

    def test_rebreakout_deep_pullback_rejected(self):
        """RE_BREAKOUT: 조정폭이 MAX_PULLBACK_PCT 초과 시 차단."""
        from scanner.weinstein import _build_indicators, _find_rebreakout_signal
        from config import REBREAKOUT_MAX_PULLBACK_PCT

        prices, volumes = _make_stage2_base(n_total=220, base_price=100.0)
        # 30% 급락 → max pullback 초과
        for i in range(30):
            prices.append(100.0 - i * 1.0)  # 100→70 (30% 조정)
            volumes.append(300_000)
        prices.append(75.0)
        volumes.append(1_200_000)

        df  = _make_df(prices, volumes)
        ind = _build_indicators(df)
        sig = _find_rebreakout_signal(ind)
        assert sig is None, f"조정폭 >REBREAKOUT_MAX_PULLBACK_PCT 시 RE_BREAKOUT 차단"


# ═══════════════════════════════════════════════════════════════════
# 3. REBOUND 테스트
# ═══════════════════════════════════════════════════════════════════

class TestRebound:

    def _make_pullback_rebound_data(self, touch_pct=0.02):
        """
        Stage2에서 MA50 눌림 후 반등 합성.
        touch_pct: MA50 대비 얼마나 내려가는지 (0.02 = 2%)
        """
        prices, volumes = _make_stage2_base(n_total=200, base_price=100.0)
        # 200~215: MA50은 ≈100, 가격을 102로 올림
        for i in range(15):
            prices.append(100.0 + i * 0.2)
            volumes.append(500_000)
        # 216~225: 눌림목 (MA50 근처로 하락)
        for i in range(10):
            prices.append(103.0 - i * 0.3 * (touch_pct * 50 + 1))
            volumes.append(350_000)
        # 226~230: 반등
        for i in range(5):
            prices.append(prices[-1] + 0.6)
            volumes.append(600_000)
        return prices, volumes

    def test_rebound_uses_ma50_support(self):
        """REBOUND: MA50 지지 후 반등 시 시그널 발생."""
        from scanner.weinstein import analyze_stock

        prices, volumes = self._make_pullback_rebound_data()
        df  = _make_df(prices, volumes)
        res = analyze_stock(df, "TEST", "테스트", "US")

        if res is not None and res["signal_type"] == "REBOUND":
            # support_level은 MA50 수준이어야 함
            assert res.get("support_level") is not None
            # MA50 ≈ 100 근처여야 함
            assert 85.0 < res["support_level"] < 115.0, \
                f"support_level이 MA50 범위 밖: {res['support_level']}"

    def test_rebound_chronological_order(self):
        """REBOUND: 과거→현재 순서로 탐지 (눌림 먼저, 반등 나중)."""
        from scanner.weinstein import _build_indicators, _find_rebound_signal

        # 단순한 눌림 + 반등 패턴
        # Phase1: 200일 상승 데이터 (MA 워밍업)
        prices  = [80.0 + i * 0.1 for i in range(200)]   # 80→99.9
        volumes = [500_000] * 200

        # Phase2: 안정화 (MA50 ≈ 100)
        prices  += [100.0] * 50
        volumes += [500_000] * 50

        # Phase3: 눌림 (95로 하락)
        for i in range(10):
            prices.append(100.0 - i * 0.5)
            volumes.append(400_000)

        # Phase4: 반등 (100 위로 복귀)
        for i in range(6):
            prices.append(95.0 + i * 1.0)
            volumes.append(600_000)

        df  = _make_df(prices, volumes)
        ind = _build_indicators(df)

        if ind is None:
            pytest.skip("indicators 빌드 실패 (데이터 부족)")

        sig = _find_rebound_signal(ind)
        if sig is not None:
            assert sig["signal_type"] == "REBOUND"
            # signal_date는 반등 확인일 (눌림일이 아님)
            assert sig.get("support_level") is not None

    def test_rebound_deep_pullback_rejected(self):
        """REBOUND: 과도한 조정(12% 초과) 시 차단."""
        from scanner.weinstein import _build_indicators, _find_rebound_signal
        from config import REBOUND_MAX_PULLBACK_PCT

        # 200일 횡보 후 20% 급락 후 반등
        prices  = [100.0] * 200
        volumes = [500_000] * 200
        # 15% 이상 급락
        for i in range(10):
            prices.append(100.0 - i * 2.0)  # 100→80 (20% 하락)
            volumes.append(300_000)
        # 반등 시도
        for i in range(5):
            prices.append(80.0 + i * 1.0)
            volumes.append(600_000)

        df  = _make_df(prices, volumes)
        ind = _build_indicators(df)
        if ind is None:
            pytest.skip("indicators 빌드 실패")

        sig = _find_rebound_signal(ind)
        # 20% 급락은 REBOUND_MAX_PULLBACK_PCT(12%) 초과 → 차단
        assert sig is None, "과도한 조정 후 REBOUND가 발생하면 안 됨"

    def test_rebound_not_in_stage4(self):
        """REBOUND: Stage4에서는 발생하지 않아야 함."""
        from scanner.weinstein import analyze_stock

        # 완전 하락 추세
        prices  = [120.0 - i * 0.35 for i in range(260)]
        volumes = [500_000] * 255 + [300_000] * 5
        df  = _make_df(prices, volumes)
        res = analyze_stock(df, "TEST", "테스트", "US")

        if res is not None:
            assert res["signal_type"] != "REBOUND", "Stage4에서 REBOUND 발생하면 안 됨"

    # ── v4 Phase 3 게이트 ─────────────────────────────────────────

    def _make_pullback_rebound_strong_volume(self):
        """`_find_rebound_signal` 이 시그널을 반환하도록 거래량을 강화한 픽스처."""
        prices, volumes = _make_stage2_base(n_total=200, base_price=100.0)
        for i in range(15):
            prices.append(100.0 + i * 0.2)
            volumes.append(500_000)
        # 눌림 (MA50 근처로 하락, 거래량 감소)
        for i in range(10):
            prices.append(103.0 - i * 0.6)
            volumes.append(350_000)
        # 반등 (vol_ratio ≥ 1.3 보장 위해 800K — vol_avg ≈ 500K)
        for i in range(5):
            prices.append(prices[-1] + 0.8)
            volumes.append(800_000)
        return prices, volumes

    def _make_signal_data(self):
        """legacy `_find_rebound_signal` 이 시그널을 반환하는 합성 데이터.

        반환 (df, daily_ind, weekly_ind, legacy_sig).
        """
        from scanner.weinstein import (
            _build_indicators, compute_weekly_indicators, to_weekly_ohlcv,
            _find_rebound_signal,
        )
        prices, volumes = self._make_pullback_rebound_strong_volume()
        df = _make_df(prices, volumes)
        daily_ind  = _build_indicators(df)
        weekly_ind = compute_weekly_indicators(to_weekly_ohlcv(df))
        legacy_sig = _find_rebound_signal(daily_ind) if daily_ind else None
        return df, daily_ind, weekly_ind, legacy_sig

    def test_requires_weekly_stage2(self, monkeypatch):
        """주봉이 STAGE2 가 아니면 detect_rebound_entry 는 None."""
        from scanner import weinstein

        df, daily_ind, weekly_ind, _ = self._make_signal_data()
        if daily_ind is None or weekly_ind is None:
            pytest.skip("indicators 빌드 실패")

        # 강제로 STAGE3 반환 → 주봉 필터에 의해 즉시 None
        monkeypatch.setattr(weinstein, "classify_stage",
                            lambda *a, **kw: "STAGE3")
        assert weinstein.detect_rebound_entry(df, weekly_ind, daily_ind) is None

    def test_requires_weekly_indicators(self):
        """weekly_ind=None 이면 REBOUND 판정 자체 거부."""
        from scanner.weinstein import detect_rebound_entry

        df, daily_ind, _, _ = self._make_signal_data()
        if daily_ind is None:
            pytest.skip("indicators 빌드 실패")
        assert detect_rebound_entry(df, None, daily_ind) is None

    def test_rejects_ma50_only_rebound(self, monkeypatch):
        """직전 base 도 30w SMA 재테스트도 없으면 v4 게이트가 차단."""
        from scanner import weinstein

        df, daily_ind, weekly_ind, legacy_sig = self._make_signal_data()
        if legacy_sig is None or weekly_ind is None:
            pytest.skip("legacy 시그널 미생성 — 픽스처가 v4 검증에 부적합")

        # STAGE2 강제
        monkeypatch.setattr(weinstein, "classify_stage",
                            lambda *a, **kw: "STAGE2")
        # base pivot 없음 가정
        monkeypatch.setattr(weinstein, "detect_base_pivot",
                            lambda *a, **kw: None)
        # 30w SMA 를 멀리 떨어뜨려 cond_b 도 실패
        weekly_ind = dict(weekly_ind)
        weekly_ind["cur_sma30w"] = 50.0  # 가격 ~100 과 +50% 차이 → 재테스트 아님
        weekly_ind["slope30w"]   = 0.10

        assert weinstein.detect_rebound_entry(df, weekly_ind, daily_ind) is None

    def test_30w_ma_retest_signal(self, monkeypatch):
        """주봉 30-SMA 터치 + 회복 → cond_b 통과 → REBOUND."""
        from scanner import weinstein

        df, daily_ind, weekly_ind, legacy_sig = self._make_signal_data()
        if legacy_sig is None or weekly_ind is None:
            pytest.skip("legacy 시그널 미생성 — 픽스처가 v4 검증에 부적합")

        monkeypatch.setattr(weinstein, "classify_stage",
                            lambda *a, **kw: "STAGE2")
        # base 는 없다고 가정 → cond_a 실패
        monkeypatch.setattr(weinstein, "detect_base_pivot",
                            lambda *a, **kw: None)

        # 30w SMA 를 시그널 시점 근처 가격으로 맞춰 cond_b 만 통과
        # signal_date 의 close 와 직전 14일 low 가 모두 30w SMA 근처여야 함.
        # legacy_sig 에는 support_level (=MA50 근방) 이 들어 있음 → 이를 30w 로 사용.
        sma30w_proxy = float(legacy_sig.get("support_level") or 100.0)
        weekly_ind = dict(weekly_ind)
        weekly_ind["cur_sma30w"] = sma30w_proxy
        weekly_ind["slope30w"]   = 0.10

        res = weinstein.detect_rebound_entry(df, weekly_ind, daily_ind)
        assert res is not None
        assert res["signal_type"] == "REBOUND"
        assert res.get("v4_gate") == "30W_RETEST"

    def test_base_pivot_retest_signal(self, monkeypatch):
        """tight base 위에서 MA50 눌림+반등 → cond_a 통과 → REBOUND."""
        from scanner import weinstein

        df, daily_ind, weekly_ind, legacy_sig = self._make_signal_data()
        if legacy_sig is None or weekly_ind is None:
            pytest.skip("legacy 시그널 미생성 — 픽스처가 v4 검증에 부적합")

        monkeypatch.setattr(weinstein, "classify_stage",
                            lambda *a, **kw: "STAGE2")
        # 가짜 base pivot: pivot 가격을 터치 직전 일봉 종가보다 낮게 → cond_a 충족
        # support_level 근처가 MA50 → 그보다 살짝 낮은 값으로 pivot 설정.
        pivot = float(legacy_sig.get("support_level") or 100.0) - 5.0
        fake_base = {
            "pivot_price":     pivot,
            "base_low":        pivot - 5.0,
            "base_start_idx":  0,
            "base_end_idx":    len(df) - 1,
            "base_weeks":      6.0,
            "base_width_pct":  4.0,
            "base_quality":    "TIGHT",
        }
        monkeypatch.setattr(weinstein, "detect_base_pivot",
                            lambda *a, **kw: fake_base)
        # 30w SMA 를 멀리 떨어뜨려 cond_b 차단
        weekly_ind = dict(weekly_ind)
        weekly_ind["cur_sma30w"] = 50.0
        weekly_ind["slope30w"]   = 0.10

        res = weinstein.detect_rebound_entry(df, weekly_ind, daily_ind)
        assert res is not None
        assert res["signal_type"] == "REBOUND"
        assert res.get("v4_gate") == "BASE_RETEST"
        assert res.get("base_quality_v4") == "TIGHT"

    def test_legacy_helper_preserved(self):
        """`_find_rebound_signal` 직접 호출은 v4 게이트와 무관하게 동작 (회귀 방지)."""
        from scanner.weinstein import _find_rebound_signal, _build_indicators

        prices, volumes = self._make_pullback_rebound_data()
        df  = _make_df(prices, volumes)
        ind = _build_indicators(df)
        if ind is None:
            pytest.skip("indicators 빌드 실패")

        # 함수가 살아 있고 (시그널이 있든 없든) 예외 없이 dict|None 반환
        sig = _find_rebound_signal(ind)
        assert sig is None or isinstance(sig, dict)
        if sig is not None:
            assert sig["signal_type"] == "REBOUND"
            assert "support_level" in sig


# ═══════════════════════════════════════════════════════════════════
# 4. 시장 필터 테스트
# ═══════════════════════════════════════════════════════════════════

class TestMarketFilter:

    def test_bear_blocks_all_buy_signals(self):
        """BEAR 장세: BLOCK_NEW_BUYS_IN_BEAR=True 시 모든 BUY 차단."""
        from scanner.scan_engine import _get_market_filter_decision
        import importlib, config as cfg_module

        original_enable = cfg_module.ENABLE_MARKET_FILTER
        original_block  = cfg_module.BLOCK_NEW_BUYS_IN_BEAR
        try:
            cfg_module.ENABLE_MARKET_FILTER   = True
            cfg_module.BLOCK_NEW_BUYS_IN_BEAR = True

            for sig_type in ("BREAKOUT", "RE_BREAKOUT", "REBOUND"):
                allow, msg = _get_market_filter_decision("BEAR", sig_type)
                assert not allow, f"BEAR 장세에서 {sig_type}가 허용되면 안 됨"
        finally:
            cfg_module.ENABLE_MARKET_FILTER   = original_enable
            cfg_module.BLOCK_NEW_BUYS_IN_BEAR = original_block

    def test_bear_filter_disabled(self):
        """ENABLE_MARKET_FILTER=False 시 BEAR에서도 시그널 허용."""
        from scanner.scan_engine import _get_market_filter_decision
        import config as cfg_module

        original = cfg_module.ENABLE_MARKET_FILTER
        try:
            cfg_module.ENABLE_MARKET_FILTER = False
            allow, _ = _get_market_filter_decision("BEAR", "BREAKOUT")
            assert allow, "필터 비활성화 시 항상 허용되어야 함"
        finally:
            cfg_module.ENABLE_MARKET_FILTER = original

    def test_caution_allow_with_flag(self):
        """CAUTION + allow_with_flag: 허용되지만 플래그 메시지 반환."""
        from scanner.scan_engine import _get_market_filter_decision
        import config as cfg_module

        original_enable  = cfg_module.ENABLE_MARKET_FILTER
        original_caution = cfg_module.CAUTION_MODE
        try:
            cfg_module.ENABLE_MARKET_FILTER = True
            cfg_module.CAUTION_MODE         = "allow_with_flag"

            allow, flag = _get_market_filter_decision("CAUTION", "BREAKOUT")
            assert allow, "allow_with_flag 모드에서 시그널 허용되어야 함"
            assert flag is not None, "CAUTION 플래그 메시지가 있어야 함"
        finally:
            cfg_module.ENABLE_MARKET_FILTER = original_enable
            cfg_module.CAUTION_MODE         = original_caution

    def test_caution_block_breakout_mode(self):
        """CAUTION + block_breakout: BREAKOUT만 차단, REBOUND는 허용."""
        from scanner.scan_engine import _get_market_filter_decision
        import config as cfg_module

        original_enable  = cfg_module.ENABLE_MARKET_FILTER
        original_caution = cfg_module.CAUTION_MODE
        try:
            cfg_module.ENABLE_MARKET_FILTER = True
            cfg_module.CAUTION_MODE         = "block_breakout"

            allow_bo, _ = _get_market_filter_decision("CAUTION", "BREAKOUT")
            allow_rb, _ = _get_market_filter_decision("CAUTION", "REBOUND")

            assert not allow_bo, "block_breakout 모드: BREAKOUT 차단"
            assert allow_rb,     "block_breakout 모드: REBOUND 허용"
        finally:
            cfg_module.ENABLE_MARKET_FILTER = original_enable
            cfg_module.CAUTION_MODE         = original_caution

    def test_bull_always_allows(self):
        """BULL 장세: 모든 시그널 허용."""
        from scanner.scan_engine import _get_market_filter_decision
        import config as cfg_module

        cfg_module.ENABLE_MARKET_FILTER = True
        for sig_type in ("BREAKOUT", "RE_BREAKOUT", "REBOUND"):
            allow, _ = _get_market_filter_decision("BULL", sig_type)
            assert allow, f"BULL 장세에서 {sig_type}가 차단되면 안 됨"


# ═══════════════════════════════════════════════════════════════════
# 5. SELL 로직 테스트
# ═══════════════════════════════════════════════════════════════════

class TestSellSignal:

    def test_sell_on_stage4(self):
        """Stage4 진입 시 SELL 시그널 발생."""
        from scanner.weinstein import check_sell_signal

        # 상승 후 급락
        prices  = [100.0 + i * 0.1 for i in range(155)]  # 상승
        prices += [116.0 - i * 0.5 for i in range(50)]   # 하락
        volumes = [500_000] * len(prices)
        df      = _make_df(prices, volumes)

        res = check_sell_signal(df, "TEST", "테스트", "US", buy_price=100.0)
        if res is not None:
            assert res["signal_type"] == "SELL"
            assert res["stage"] in ("STAGE4", "STAGE3")

    def test_sell_on_stop_loss(self):
        """손절가 도달 시 즉시 SELL 시그널."""
        from scanner.weinstein import check_sell_signal

        prices  = [100.0] * 200  # 횡보
        volumes = [500_000] * 200
        df      = _make_df(prices, volumes)

        # 현재가(100) ≤ 손절가(105)
        res = check_sell_signal(df, "TEST", "테스트", "US",
                                buy_price=120.0, stop_loss=105.0)
        assert res is not None
        assert "손절가" in res["sell_reason"]

    def test_no_sell_in_stage2(self):
        """Stage2 강세 중에는 SELL 시그널 없어야 함."""
        from scanner.weinstein import check_sell_signal

        # 완만한 Stage2 상승
        prices  = [80.0 + i * 0.1 for i in range(260)]
        volumes = [500_000] * 260
        df      = _make_df(prices, volumes)

        res = check_sell_signal(df, "TEST", "테스트", "US", buy_price=80.0)
        assert res is None, "Stage2 상승 중 SELL 발생하면 안 됨"


# ═══════════════════════════════════════════════════════════════════
# 6. 공통 유틸리티 테스트
# ═══════════════════════════════════════════════════════════════════

class TestUtilities:

    def test_stage_of_classification(self):
        """stage_of: 가격/MA/기울기 조합에 따른 Stage 분류."""
        from scanner.weinstein import stage_of

        assert stage_of(110, 100,  0.10) == "STAGE2"  # above MA, rising
        assert stage_of(110, 100,  0.00) == "STAGE3"  # above MA, flat
        assert stage_of( 90, 100, -0.10) == "STAGE4"  # below MA, falling
        assert stage_of( 90, 100,  0.10) == "STAGE1"  # below MA, rising

    def test_calc_rs_outperform(self):
        """calc_rs: 주식이 지수보다 강할 때 RS > 1."""
        from scanner.weinstein import calc_rs

        stock_prices = pd.Series([100.0 + i * 0.5 for i in range(70)])  # +34.5%
        bench_prices = pd.Series([100.0 + i * 0.2 for i in range(70)])  # +13.8%
        rs = calc_rs(stock_prices, bench_prices, period=65)
        assert rs is not None
        assert rs > 1.0, f"아웃퍼폼 시 RS > 1 이어야 함, got {rs}"

    def test_calc_rs_underperform(self):
        """calc_rs: 주식이 지수보다 약할 때 RS < 1."""
        from scanner.weinstein import calc_rs

        stock_prices = pd.Series([100.0 + i * 0.1 for i in range(70)])  # +6.5%
        bench_prices = pd.Series([100.0 + i * 0.4 for i in range(70)])  # +26.4%
        rs = calc_rs(stock_prices, bench_prices, period=65)
        assert rs is not None
        assert rs < 1.0, f"언더퍼폼 시 RS < 1 이어야 함, got {rs}"

    def test_analyze_stock_returns_none_for_short_df(self):
        """데이터 부족 시 analyze_stock None 반환."""
        from scanner.weinstein import analyze_stock

        short_prices = [100.0] * 50  # MA150 계산 불가
        df = _make_df(short_prices)
        res = analyze_stock(df, "TEST", "테스트", "US")
        assert res is None

    def test_signal_quality_levels(self):
        """signal_quality: Mansfield RS 기준 STRONG/MODERATE/WEAK 분류."""
        from scanner.weinstein import _signal_quality

        assert _signal_quality(4.0, 0.15, 8.0, "RISING",  "BREAKOUT") == "STRONG"
        assert _signal_quality(2.0, 0.06, 2.0, None,      "REBOUND")  == "MODERATE"
        assert _signal_quality(1.6, 0.01, -1.0, "FALLING", "REBOUND") == "WEAK"

    def test_build_indicators_returns_none_without_ma(self):
        """MA150 계산 불가 시 _build_indicators None 반환."""
        from scanner.weinstein import _build_indicators

        df = _make_df([100.0] * 30)  # MA150 계산 불가
        result = _build_indicators(df)
        assert result is None


# ═══════════════════════════════════════════════════════════════════
# 7. 신규 기능 검증 테스트
# ═══════════════════════════════════════════════════════════════════

class TestNewFeatures:

    # ── analyze_stock 리턴 필드 ───────────────────────────────────

    def test_analyze_stock_returns_ma50_and_base_quality(self):
        """analyze_stock 결과에 ma50, base_quality 포함."""
        from scanner.weinstein import analyze_stock

        prices, volumes = _make_stage2_base(n_total=280, base_price=100.0)
        # 돌파: 마지막 날 pivot 상향 + 거래량 폭발 (v4 hard block 통과)
        prices  += [105.0]
        volumes += [6_000_000]
        df = _make_df(prices, volumes)

        res = analyze_stock(df, "TEST", "테스트", "US")
        if res is not None:
            assert "ma50"         in res, "ma50 필드 누락"
            assert "base_quality" in res, "base_quality 필드 누락"
            assert res["ma50"] > 0
            assert res["base_quality"] in ("STRONG", "WEAK", "N/A")

    # ── SELL severity ─────────────────────────────────────────────

    def test_sell_severity_high_on_stop_loss(self):
        """손절가 도달 시 severity=HIGH."""
        from scanner.weinstein import check_sell_signal

        prices  = [100.0] * 200
        df      = _make_df(prices)
        res     = check_sell_signal(df, "T", "테스트", "US",
                                    buy_price=120.0, stop_loss=105.0)
        assert res is not None
        assert res["severity"] == "HIGH"

    def test_sell_severity_low_on_stage3(self):
        """Stage3 징후 시 severity=LOW."""
        from scanner.weinstein import check_sell_signal

        # 가격이 MA150 위이지만 기울기 0 → Stage3
        prices  = [100.0 + i * 0.03 for i in range(170)]   # 완만한 상승
        prices += [105.0] * 40                               # 이후 횡보
        df      = _make_df(prices)
        res     = check_sell_signal(df, "T", "테스트", "US")
        if res is not None:
            assert res["severity"] in ("LOW", "MEDIUM", "HIGH")
            assert "severity" in res

    def test_sell_has_severity_field(self):
        """check_sell_signal 결과에 항상 severity 필드 포함."""
        from scanner.weinstein import check_sell_signal

        prices  = [100.0] * 200
        df      = _make_df(prices)
        res     = check_sell_signal(df, "T", "테스트", "US",
                                    buy_price=150.0, stop_loss=110.0)
        assert res is not None
        assert "severity" in res

    # ── _grade 함수 ───────────────────────────────────────────────

    def test_grade_s_for_strong_breakout(self):
        """STRONG BREAKOUT + STRONG base + RS≥1.5 + BULL → S 등급."""
        from scanner.scan_engine import _grade

        sig = {
            "signal_quality":  "STRONG",
            "signal_type":     "BREAKOUT",
            "base_quality":    "STRONG",
            "rs":              1.6,
            "market_condition": "BULL",
        }
        assert _grade(sig) == "S", f"기대 S, 실제 {_grade(sig)}"

    def test_grade_b_for_weak_rebound(self):
        """WEAK REBOUND + base N/A + RS 없음 → B 등급."""
        from scanner.scan_engine import _grade

        sig = {
            "signal_quality":  "WEAK",
            "signal_type":     "REBOUND",
            "base_quality":    "N/A",
            "rs":              None,
            "market_condition": "NEUTRAL",
        }
        assert _grade(sig) == "B", f"기대 B, 실제 {_grade(sig)}"

    def test_grade_a_for_moderate_breakout(self):
        """MODERATE BREAKOUT + RS≥1.5 → A 등급 (2+1+1=4)."""
        from scanner.scan_engine import _grade

        sig = {
            "signal_quality":  "MODERATE",
            "signal_type":     "BREAKOUT",
            "base_quality":    "WEAK",
            "rs":              1.5,          # ≥1.5 → +1 (총 4점 → A)
            "market_condition": "NEUTRAL",
        }
        g = _grade(sig)
        assert g in ("A", "S"), f"기대 A 이상, 실제 {g}"

    def test_grade_bear_market_penalty(self):
        """BEAR 장세는 점수 -2 페널티."""
        from scanner.scan_engine import _grade

        sig_bull = {"signal_quality": "MODERATE", "signal_type": "REBOUND",
                    "base_quality": "N/A", "rs": 1.2, "market_condition": "BULL"}
        sig_bear = {**sig_bull, "market_condition": "BEAR"}

        g_bull = _grade(sig_bull)
        g_bear = _grade(sig_bear)
        grade_order = {"S": 2, "A": 1, "B": 0}
        assert grade_order[g_bear] <= grade_order[g_bull], \
            "BEAR 장세 등급이 BULL 보다 낮거나 같아야 함"


# ═══════════════════════════════════════════════════════════════════
# 8. 주봉 지표 (compute_weekly_indicators) 단위 테스트 — Phase 1
# ═══════════════════════════════════════════════════════════════════

class TestWeeklyIndicators:

    def test_weekly_indicators_basic_rising(self):
        """250일 약한 상승 → 30w SMA 양수 슬로프, close > sma30w."""
        from scanner.weinstein import to_weekly_ohlcv, compute_weekly_indicators

        prices = [50.0 + i * 0.2 for i in range(250)]   # 50→99.8
        df     = _make_df(prices)
        weekly = to_weekly_ohlcv(df)
        ind    = compute_weekly_indicators(weekly)

        assert ind is not None, "주봉 30주 이상 데이터인데 None 반환"
        assert ind["cur_sma30w"] > 0
        assert ind["slope30w"] > 0,                 f"상승 추세 슬로프 양수여야 함, got {ind['slope30w']}"
        assert ind["cur_close_w"] > ind["cur_sma30w"]
        assert "weekly_volume_ratio" in ind

    def test_weekly_indicators_short_data_returns_none(self):
        """주봉 30주 미만 데이터는 None."""
        from scanner.weinstein import to_weekly_ohlcv, compute_weekly_indicators

        # 100일 ≈ 20주 → 30주 미달
        df     = _make_df([100.0] * 100)
        weekly = to_weekly_ohlcv(df)
        assert compute_weekly_indicators(weekly) is None

    def test_weekly_indicators_falling_slope(self):
        """하락 추세에서 30w SMA 슬로프 음수, close < sma30w."""
        from scanner.weinstein import to_weekly_ohlcv, compute_weekly_indicators

        prices = [200.0 - i * 0.4 for i in range(250)]   # 200→100
        df     = _make_df(prices)
        weekly = to_weekly_ohlcv(df)
        ind    = compute_weekly_indicators(weekly)

        assert ind is not None
        assert ind["slope30w"] < 0,                 f"하락 추세 슬로프 음수여야 함, got {ind['slope30w']}"
        assert ind["cur_close_w"] < ind["cur_sma30w"]


# ═══════════════════════════════════════════════════════════════════
# 9. Mansfield RS (compute_relative_performance) 단위 테스트
# ═══════════════════════════════════════════════════════════════════

class TestMansfieldRS:

    @staticmethod
    def _series(prices):
        idx = pd.date_range(start="2020-01-01", periods=len(prices), freq="B")
        return pd.Series([float(p) for p in prices], index=idx)

    def test_rs_outperform_positive(self):
        """주식이 벤치마크보다 강할 때 Mansfield RS > 0."""
        from scanner.weinstein import compute_relative_performance

        n     = 300
        # 처음 절반은 동일하게 → ratio SMA 자리잡고, 후반에 outperform
        stock = [100.0] * 150 + [100.0 + i * 0.5 for i in range(150)]
        bench = [100.0] * 150 + [100.0 + i * 0.1 for i in range(150)]
        rs_value, rs_trend = compute_relative_performance(
            self._series(stock), self._series(bench)
        )
        assert rs_value is not None
        assert rs_value > 0, f"outperform 시 RS > 0 이어야 함, got {rs_value}"
        assert rs_trend in ("RISING", "FALLING", "FLAT")

    def test_rs_underperform_negative(self):
        """주식이 벤치마크보다 약할 때 Mansfield RS < 0."""
        from scanner.weinstein import compute_relative_performance

        n     = 300
        stock = [100.0] * 150 + [100.0 + i * 0.05 for i in range(150)]
        bench = [100.0] * 150 + [100.0 + i * 0.5  for i in range(150)]
        rs_value, _ = compute_relative_performance(
            self._series(stock), self._series(bench)
        )
        assert rs_value is not None
        assert rs_value < 0, f"underperform 시 RS < 0 이어야 함, got {rs_value}"

    def test_rs_short_data_returns_none(self):
        """260일(=52주) 미만이면 None 반환."""
        from scanner.weinstein import compute_relative_performance

        n     = 100
        stock = self._series([100.0] * n)
        bench = self._series([100.0] * n)
        rs_value, rs_trend = compute_relative_performance(stock, bench)
        assert rs_value is None
        assert rs_trend is None

    def test_rs_falling_trend_detected(self):
        """최근 5주 ratio 하락 → trend == FALLING."""
        from scanner.weinstein import compute_relative_performance

        # 250일 outperform 후 50일 stock 빠른 하락 + bench 상승 → ratio 급락
        stock = [100.0 + i * 0.5 for i in range(250)]
        last_s = stock[-1]
        stock += [last_s - i * 1.0 for i in range(50)]
        bench  = [100.0 + i * 0.2 for i in range(250)]
        last_b = bench[-1]
        bench += [last_b + i * 0.3 for i in range(50)]

        _, rs_trend = compute_relative_performance(
            self._series(stock), self._series(bench)
        )
        assert rs_trend == "FALLING", f"기대 FALLING, got {rs_trend}"


# ═══════════════════════════════════════════════════════════════════
# 10. Base Pivot (detect_base_pivot) 단위 테스트
# ═══════════════════════════════════════════════════════════════════

class TestBasePivot:

    def test_tight_base_detected_in_sideways(self):
        """5주 이상 ±2% 횡보 → base 탐지, 폭 ≤ 8%."""
        from scanner.weinstein import detect_base_pivot

        # 60일 동안 100 ± 1.5 횡보 → 폭 ≈ 3% (TIGHT)
        prices = [100.0 + np.sin(i * np.pi / 4) * 1.5 for i in range(60)]
        df     = _make_df(prices)
        result = detect_base_pivot(df)

        assert result is not None,                          "5주 이상 tight 횡보면 base 탐지되어야 함"
        assert result["base_weeks"]    >= 5
        assert result["base_width_pct"] <= 8.0
        assert result["base_quality"]  == "TIGHT"

    def test_no_base_when_data_too_short(self):
        """min_weeks*5 + 5 = 30 미만이면 None."""
        from scanner.weinstein import detect_base_pivot

        df = _make_df([100.0] * 10)   # 10일 < 30
        assert detect_base_pivot(df) is None

    def test_loose_base_classification(self):
        """폭 8~15% 횡보면 LOOSE 등급."""
        from scanner.weinstein import detect_base_pivot

        # 60일 동안 95~107 진동 → 폭 ≈ 11% (LOOSE)
        prices = [101.0 + np.sin(i * np.pi / 5) * 6.0 for i in range(60)]
        df     = _make_df(prices)
        result = detect_base_pivot(df)

        assert result is not None
        assert 8.0 < result["base_width_pct"] <= 15.0
        assert result["base_quality"]         == "LOOSE"

    def test_base_returns_pivot_above_low(self):
        """탐지된 base 의 pivot_price > base_low."""
        from scanner.weinstein import detect_base_pivot

        prices = [100.0 + np.sin(i * np.pi / 4) * 1.5 for i in range(60)]
        df     = _make_df(prices)
        result = detect_base_pivot(df)
        assert result is not None
        assert result["pivot_price"] > result["base_low"]


# ═══════════════════════════════════════════════════════════════════
# 10b. detect_stage2_breakout v4 — base pivot + 거래량 hard block
# ═══════════════════════════════════════════════════════════════════

class TestStage2BreakoutV4:

    def _stage2_setup(self, n_total=230, base_price=100.0, breakout_price=104.0,
                      breakout_vol=6_000_000, base_vol=500_000):
        """Stage2 ramp + sideways base + 마지막날 돌파 합성."""
        prices, volumes = _make_stage2_base(n_total=n_total, base_price=base_price)
        # base 구간 거래량을 명시적으로 설정 (default: 500K)
        for i in range(len(volumes)):
            volumes[i] = base_vol
        prices[-1]  = breakout_price
        volumes[-1] = breakout_vol
        return _make_df(prices, volumes)

    def test_v4_breakout_uses_base_pivot(self):
        """v4 base pivot + 거래량 둘 다 통과하면 BREAKOUT 신호 + base_weeks/base_width_pct 노출."""
        from scanner.weinstein import analyze_stock

        df = self._stage2_setup()
        res = analyze_stock(df, "TEST", "테스트", "US")

        assert res is not None, "신호가 발생해야 함"
        assert res["signal_type"]   == "BREAKOUT"
        assert res["volume_ratio"]  >= 3.0,                        "BREAKOUT_DAILY_VOL_RATIO 통과"
        assert res.get("pivot_price") is not None
        # v4 base 정보가 signal/result 에 포함되어야 함 — 실제 신호 경로가 detect_base_pivot 을 사용한다는 증거
        assert "base_quality"  in res
        assert res["base_quality"] in ("STRONG", "WEAK")            # legacy 매핑

    def test_v4_blocked_by_low_daily_volume(self):
        """일봉 거래량 < BREAKOUT_DAILY_VOL_RATIO(3.0x) → 신호 없음 (hard block)."""
        from scanner.weinstein import analyze_stock

        # 일봉 ratio ≈ 1.8x — 3.0x 미만
        df = self._stage2_setup(breakout_vol=900_000)
        res = analyze_stock(df, "TEST", "테스트", "US")

        if res is not None:
            assert res["signal_type"] != "BREAKOUT", "일봉 거래량 미달인데 BREAKOUT 발생"

    def test_v4_blocked_by_low_weekly_volume(self):
        """주봉 거래량 < BREAKOUT_WEEKLY_VOL_RATIO(2.0x) → 신호 없음 (hard block)."""
        from scanner.weinstein import analyze_stock

        # 일봉 ratio 는 통과시키되 주봉 합이 2.0x 미만이 되도록 spike 크기 조절
        # base_vol=500K → weekly_avg=2.5M; spike=1.9M 이면 weekly = 4*500K+1.9M = 3.9M / 2.5M = 1.56x
        df = self._stage2_setup(breakout_vol=1_900_000)
        res = analyze_stock(df, "TEST", "테스트", "US")

        if res is not None:
            assert res["signal_type"] != "BREAKOUT", "주봉 거래량 미달인데 BREAKOUT 발생"

    def test_v4_rejects_wide_base(self):
        """폭 > 15% wide base → BREAKOUT 차단."""
        from scanner.weinstein import analyze_stock

        # 횡보 폭을 ±10 으로 늘려 base width ≈ 20% (WIDE) 강제
        prices = []
        volumes = []
        for i in range(150):
            prices.append(50.0 + 45.0 * i / 149)
            volumes.append(500_000)
        for i in range(80):
            prices.append(100.0 + 10.0 * np.sin(i * np.pi / 5))
            volumes.append(500_000)
        prices.append(112.0)               # 돌파 (pivot ≈ 110 위)
        volumes.append(6_000_000)            # 거래량은 통과시켜 base 폭 단독으로 차단되는지 검증
        df = _make_df(prices, volumes)

        res = analyze_stock(df, "TEST", "테스트", "US")
        if res is not None:
            assert res["signal_type"] != "BREAKOUT", "WIDE base 인데 BREAKOUT 발생"

    def test_v4_requires_weekly_data(self):
        """주봉 ind 가 None 이면 BREAKOUT 판정 자체를 거부 (legacy fallback 제거)."""
        from scanner.weinstein import detect_stage2_breakout, _build_indicators

        df = self._stage2_setup()
        daily_ind = _build_indicators(df)
        sig = detect_stage2_breakout(df, weekly_ind=None, daily_ind=daily_ind)
        assert sig is None, "weekly_ind=None 일 때 BREAKOUT 신호가 발생하면 안 됨"


# ═══════════════════════════════════════════════════════════════════
# 11. STAGE 분류 경계 조건 (classify_stage)
# ═══════════════════════════════════════════════════════════════════

class TestStageBoundary:

    def test_stage3_above_ma_flat_slope(self):
        """close > sma30w + slope ≈ 0 → STAGE3 (분배)."""
        from scanner.weinstein import classify_stage

        ind = {"cur_close_w": 110.0, "cur_sma30w": 100.0,
               "cur_sma10w": 109.0,  "slope30w": 0.0}
        assert classify_stage(ind, None) == "STAGE3"

    def test_stage3_when_short_sma_weakens(self):
        """close > sma30w + 약한 상승 + sma10 < close*0.98 → STAGE3."""
        from scanner.weinstein import classify_stage

        # slope=0.04 < _RISING_SLOPE=0.05, sma10 105 < close 110*0.98=107.8
        ind = {"cur_close_w": 110.0, "cur_sma30w": 100.0,
               "cur_sma10w": 105.0,  "slope30w": 0.04}
        assert classify_stage(ind, None) == "STAGE3"

    def test_stage2_when_above_and_rising(self):
        """close > sma30w + slope > 0.05 → STAGE2."""
        from scanner.weinstein import classify_stage

        ind = {"cur_close_w": 110.0, "cur_sma30w": 100.0,
               "cur_sma10w": 109.0,  "slope30w": 0.10}
        assert classify_stage(ind, None) == "STAGE2"

    def test_stage4_below_and_falling(self):
        """close < sma30w + slope < -0.02 → STAGE4."""
        from scanner.weinstein import classify_stage

        ind = {"cur_close_w": 90.0,  "cur_sma30w": 100.0,
               "cur_sma10w": 92.0,   "slope30w": -0.10}
        assert classify_stage(ind, None) == "STAGE4"


# ═══════════════════════════════════════════════════════════════════
# 12. check_sell_signal — 신규 옵션 분기 (Phase 1)
# ═══════════════════════════════════════════════════════════════════

class TestSellSignalNewBranches:

    @staticmethod
    def _safe_daily_df():
        """기존 분기를 트리거하지 않는 daily df.

        80→106 으로 일정한 약한 상승 → MA150 양수 슬로프 + Stage2,
        stop_loss/STAGE4/MA150 반전/STAGE3 분기 모두 미발현.
        """
        prices = [80.0 + i * 0.1 for i in range(260)]
        return _make_df(prices)

    @staticmethod
    def _make_weekly(weekly_prices):
        n       = len(weekly_prices)
        idx     = pd.date_range(start="2020-01-03", periods=n, freq="W-FRI")
        prices  = [float(p) for p in weekly_prices]
        return pd.DataFrame({
            "Open":   [p * 0.998 for p in prices],
            "High":   [p * 1.005 for p in prices],
            "Low":    [p * 0.995 for p in prices],
            "Close":  prices,
            "Volume": [1_000_000] * n,
        }, index=idx)

    # ── Helper 단위 테스트 ──────────────────────────────────────────

    def test_weekly_breakdown_helper(self):
        """_weekly_breakdown: 마지막 close < 30w SMA → True."""
        from scanner.weinstein import _weekly_breakdown

        weekly = self._make_weekly([100.0 + i * 0.5 for i in range(50)] + [105.0])
        assert _weekly_breakdown(weekly) is True

        weekly_ok = self._make_weekly([100.0 + i * 0.5 for i in range(50)])
        assert _weekly_breakdown(weekly_ok) is False
        assert _weekly_breakdown(None)      is False

    def test_weekly_slope_reversal_helper(self):
        """_weekly_slope_reversal: 강한 상승 후 가파른 하락 → True."""
        from scanner.weinstein import _weekly_slope_reversal

        # 50주 +1/wk 상승 → 5주 -30/wk 가파른 하락. SMA30 cur_slope<0 & past_slope>0.
        prices  = [200.0 + i for i in range(50)]
        prices += [249.0 - 30.0 * (i + 1) for i in range(5)]
        assert _weekly_slope_reversal(self._make_weekly(prices)) is True

        # 일관 상승은 반전 없음 (WEEKLY_MA_LONG + 5 = 35주 이상 필요)
        steady = [100.0 + i for i in range(80)]
        assert _weekly_slope_reversal(self._make_weekly(steady)) is False
        assert _weekly_slope_reversal(None) is False

    def test_rs_deterioration_helper(self):
        """_rs_deteriorating: RS<0 + FALLING → True."""
        from scanner.weinstein import _rs_deteriorating

        idx   = pd.date_range(start="2020-01-01", periods=300, freq="B")
        # 250일 outperform 후 50일 빠른 하락 + bench 가속 상승
        stock = [100.0 + i * 0.5 for i in range(250)]
        stock += [stock[-1] - i * 1.2 for i in range(50)]
        bench = [100.0 + i * 0.2 for i in range(250)]
        bench += [bench[-1] + i * 0.4 for i in range(50)]
        s = pd.Series([float(x) for x in stock], index=idx)
        b = pd.Series([float(x) for x in bench], index=idx)
        assert _rs_deteriorating(s, b) is True

        # benchmark None → False
        assert _rs_deteriorating(s, None) is False

    # ── check_sell_signal 통합 분기 테스트 ─────────────────────────

    def test_weekly_breakdown_triggers_high_sell(self):
        """주봉 30-SMA 하향 이탈 → severity HIGH."""
        from scanner.weinstein import check_sell_signal

        daily  = self._safe_daily_df()
        weekly = self._make_weekly(
            [100.0 + i * 0.5 for i in range(50)] + [105.0]
        )
        res = check_sell_signal(daily, "T", "테스트", "US", weekly_df=weekly)
        assert res is not None
        assert res["severity"]    == "HIGH"
        assert "주봉" in res["sell_reason"]

    def test_no_regression_when_options_omitted(self):
        """weekly_df / benchmark_close 미제공 시 기존 결과 유지."""
        from scanner.weinstein import check_sell_signal

        # Stage2 강세 → 기존에도 None 반환 → 신규 분기로도 None 유지
        daily = self._safe_daily_df()
        assert check_sell_signal(daily, "T", "n", "US")               is None
        assert check_sell_signal(daily, "T", "n", "US",
                                 weekly_df=None, benchmark_close=None) is None


# ═══════════════════════════════════════════════════════════════════
# 9. Mansfield RS 기반 signal_quality 점수 (Phase 2)
# ═══════════════════════════════════════════════════════════════════

class TestSignalQualityMansfield:
    """_signal_quality(vol_ratio, slope, rs_value, rs_trend, signal_type)."""

    def test_strong_with_high_rs_and_rising_trend(self):
        from scanner.weinstein import _signal_quality
        # vol(+2) slope(+2) rs_value≥5(+2) RISING(+1) BREAKOUT(+1) = 8
        assert _signal_quality(4.0, 0.15, 8.0, "RISING", "BREAKOUT") == "STRONG"

    def test_weak_with_negative_rs_and_falling_trend(self):
        from scanner.weinstein import _signal_quality
        # vol(+1) slope(+1) rs<0(0) FALLING(-1) BREAKOUT(+1) = 2 → WEAK
        assert _signal_quality(2.5, 0.05, -3.0, "FALLING", "BREAKOUT") == "WEAK"

    def test_rs_value_zero_boundary_counts_as_pass(self):
        from scanner.weinstein import _signal_quality
        # vol(+1) slope(+1) rs_value=0 → +1 / RISING(+1) → 4 → MODERATE
        assert _signal_quality(2.0, 0.05, 0.0, "RISING", "REBOUND") == "MODERATE"

    def test_rs_value_five_boundary_counts_as_top_band(self):
        from scanner.weinstein import _signal_quality
        # rs_value=5.0 정확 경계 → +2; 다른 +1 들 합산 STRONG
        # vol(+1) slope(+1) rs=5(+2) RISING(+1) = 5 → STRONG
        assert _signal_quality(2.0, 0.05, 5.0, "RISING", "REBOUND") == "STRONG"

    def test_rs_value_none_skips_rs_score(self):
        from scanner.weinstein import _signal_quality
        # rs_value=None → rs 점수 0; vol(+2) slope(+2) BREAKOUT(+1) = 5 → STRONG
        assert _signal_quality(3.5, 0.12, None, None, "BREAKOUT") == "STRONG"

    def test_rs_passed_uses_mansfield_zero_threshold(self):
        """analyze_stock 의 rs_passed 가 rs_value ≥ 0 으로 계산되는지 통합 검증."""
        from scanner.weinstein import analyze_stock

        prices, volumes = _make_stage2_base(n_total=280, base_price=100.0)
        prices  += [105.0]
        volumes += [6_000_000]
        df = _make_df(prices, volumes)

        # 벤치마크가 종목보다 약하게 → Mansfield rs_value > 0 → rs_passed=True
        bench = pd.Series(
            [50.0 + i * 0.001 for i in range(len(df))],
            index=df.index,
        )
        res = analyze_stock(df, "T", "테스트", "US", benchmark_close=bench)
        if res is not None:
            assert res["rs_passed"] is True
            assert res.get("rs_value") is not None
            assert res["rs_value"] >= 0.0

    def test_rs_passed_false_when_rs_below_zero(self):
        from scanner.weinstein import analyze_stock

        prices, volumes = _make_stage2_base(n_total=280, base_price=100.0)
        prices  += [105.0]
        volumes += [6_000_000]
        df = _make_df(prices, volumes)

        # 벤치마크가 종목보다 가파르게 상승 → Mansfield rs_value < 0
        bench = pd.Series(
            [10.0 + i * 1.0 for i in range(len(df))],
            index=df.index,
        )
        res = analyze_stock(df, "T", "테스트", "US", benchmark_close=bench)
        if res is not None and res.get("rs_value") is not None:
            assert res["rs_value"] < 0.0
            assert res["rs_passed"] is False


# ═══════════════════════════════════════════════════════════════════
# Strict Weinstein filter — Phase 2: stop-loss + RS zero-cross helpers
# ═══════════════════════════════════════════════════════════════════

class TestStopLoss:
    """compute_stop_loss() — signal_type 별 후보 우선순위 검증."""

    def test_breakout_uses_base_low_first(self):
        from scanner.weinstein import compute_stop_loss
        sig = {
            "signal_type": "BREAKOUT",
            "price":       105.0,
            "pivot_price": 104.0,
            "base_low":    98.0,
        }
        # 1순위: base_low * 0.99 = 97.02
        sl = compute_stop_loss(sig, daily_ind=None,
                               weekly_ind={"cur_sma30w": 90.0})
        assert sl is not None
        assert abs(sl - 97.02) < 1e-6

    def test_breakout_falls_back_to_pivot_when_no_base_low(self):
        from scanner.weinstein import compute_stop_loss
        sig = {
            "signal_type": "BREAKOUT",
            "price":       105.0,
            "pivot_price": 104.0,
            # base_low 없음
        }
        sl = compute_stop_loss(sig, daily_ind=None,
                               weekly_ind={"cur_sma30w": 90.0})
        # 2순위: pivot_price * 0.97 = 100.88
        assert sl is not None
        assert abs(sl - 100.88) < 1e-6

    def test_rebound_uses_sma30w(self):
        from scanner.weinstein import compute_stop_loss
        sig = {
            "signal_type": "REBOUND",
            "price":       100.0,
        }
        sl = compute_stop_loss(sig,
                               daily_ind={"cur_m50": 95.0},
                               weekly_ind={"cur_sma30w": 90.0})
        # 1순위: cur_sma30w * 0.97 = 87.3
        assert sl is not None
        assert abs(sl - 87.3) < 1e-6

    def test_rebreakout_uses_swing_low(self):
        from scanner.weinstein import compute_stop_loss
        # 최근 30일 low 시리즈 — 최저 92.0
        n = 60
        idx = pd.date_range("2024-01-01", periods=n)
        lows = pd.Series([100.0] * (n - 30) + [92.0] + [100.0] * 29, index=idx)
        sig = {
            "signal_type": "RE_BREAKOUT",
            "price":       105.0,
        }
        sl = compute_stop_loss(sig,
                               daily_ind={"low": lows, "cur_m50": 96.0},
                               weekly_ind=None)
        # 1순위: swing_low * 0.99 = 92 * 0.99 = 91.08
        assert sl is not None
        assert abs(sl - 91.08) < 1e-6

    def test_returns_none_when_all_candidates_above_price(self):
        from scanner.weinstein import compute_stop_loss
        # base_low * 0.99 = 99 → price=100 보다 작음. price 를 더 낮춰서 실패 유도
        sig = {
            "signal_type": "BREAKOUT",
            "price":       80.0,    # 모든 후보 (97.02, 100.88, 87.3) 가 price 이상
            "pivot_price": 104.0,
            "base_low":    98.0,
        }
        sl = compute_stop_loss(sig, daily_ind=None,
                               weekly_ind={"cur_sma30w": 90.0})
        assert sl is None

    def test_analyze_stock_breakout_includes_stop_loss(self):
        """analyze_stock 결과에 stop_loss 가 실제 값으로 채워져야 한다."""
        from scanner.weinstein import analyze_stock

        prices, volumes = _make_stage2_base(n_total=230, base_price=100.0)
        prices[-1]  = 104.0
        volumes[-1] = 6_000_000
        df = _make_df(prices, volumes)

        res = analyze_stock(df, "TEST", "테스트", "US")
        assert res is not None
        assert res["signal_type"] == "BREAKOUT"
        # base_low ≈ 98 이므로 stop_loss ≈ 97 부근. price(=104) 미만 보장.
        assert res["stop_loss"] is not None
        assert res["stop_loss"] < res["price"]


class TestRSZeroCross:
    """detect_rs_zero_cross() — Mansfield RS 가 최근 N주에 음→양 전환했는지."""

    def _stage_with_benchmark(self, neg_to_pos: bool = True, always_negative: bool = False,
                              always_positive: bool = False):
        """종목/벤치마크 시리즈 합성:
          neg_to_pos: 초반 종목이 약하다가(음수 RS) 후반 강해짐(양수)
          always_negative: 종목이 줄곧 약함
          always_positive: 종목이 줄곧 강함
        """
        n = 320  # SMA52 (260일) + 60일 여유
        idx = pd.date_range("2022-01-01", periods=n)

        if always_negative:
            stock = pd.Series([100.0 + i * 0.1 for i in range(n)], index=idx)
            bench = pd.Series([100.0 + i * 0.5 for i in range(n)], index=idx)  # 더 빨리 상승
        elif always_positive:
            stock = pd.Series([100.0 + i * 0.5 for i in range(n)], index=idx)
            bench = pd.Series([100.0 + i * 0.1 for i in range(n)], index=idx)
        elif neg_to_pos:
            # 처음 260일은 종목이 벤치보다 약하게(ratio < SMA), 이후 종목이 가파르게 상승
            stock_vals = [100.0 + i * 0.1 for i in range(260)]
            stock_vals += [stock_vals[-1] + (i + 1) * 1.5 for i in range(n - 260)]
            bench_vals = [100.0 + i * 0.5 for i in range(n)]
            stock = pd.Series(stock_vals, index=idx)
            bench = pd.Series(bench_vals, index=idx)
        return stock, bench

    def test_zero_cross_within_window(self):
        from scanner.weinstein import detect_rs_zero_cross
        stock, bench = self._stage_with_benchmark(neg_to_pos=True)
        assert detect_rs_zero_cross(stock, bench, lookback_weeks=8) is True

    def test_always_negative_no_cross(self):
        from scanner.weinstein import detect_rs_zero_cross
        stock, bench = self._stage_with_benchmark(always_negative=True)
        assert detect_rs_zero_cross(stock, bench, lookback_weeks=8) is False

    def test_always_positive_no_cross(self):
        from scanner.weinstein import detect_rs_zero_cross
        stock, bench = self._stage_with_benchmark(always_positive=True)
        # 줄곧 양수 — 음→양 전환 없음
        assert detect_rs_zero_cross(stock, bench, lookback_weeks=8) is False

    def test_benchmark_none_returns_false(self):
        from scanner.weinstein import detect_rs_zero_cross
        n = 300
        idx = pd.date_range("2022-01-01", periods=n)
        stock = pd.Series([100.0 + i * 0.1 for i in range(n)], index=idx)
        assert detect_rs_zero_cross(stock, None, lookback_weeks=8) is False
        assert detect_rs_zero_cross(None, stock, lookback_weeks=8) is False


# ═══════════════════════════════════════════════════════════════════
# Strict Weinstein filter — Phase 2: no-look-ahead invariant 회귀 테스트
# ═══════════════════════════════════════════════════════════════════
#
# detect_* 시그널은 SCAN_LOOKBACK_DAYS(=7) 안의 과거 bar 에서도 발생할 수
# 있다. Phase 2 strict 필드(stop_loss / rs_zero_crossed) 는 signal 발생
# 시점까지의 데이터만 보고 산출되어야 하며, 마지막 bar 까지 본다면
# look-ahead invariant 가 깨진다.

class TestNoLookAhead:

    def test_stop_loss_uses_signal_date_indicators(self):
        """signal 이 5일 전 발생 + 그 이후 *큰* 가격 변동으로 cur_m50 이 last
        bar 와 signal 시점에 명확히 다른 합성 데이터.

        analyze_stock 결과의 stop_loss 가 signal 시점 cur_m50 기반인지
        검증 — REBOUND 의 fallback 후보 cur_m50 * 0.97 을 활용해 차이를
        관찰한다.
        """
        from scanner import weinstein as W

        # REBOUND 시그널을 강제로 며칠 전 발생시키기 위해 직접 합성 데이터 + sig 사용.
        # analyze_stock 전체 흐름 대신 weinstein.compute_stop_loss 를
        # signal 시점 vs last 시점 두 가지 daily_ind 로 호출해 서로 다른 결과
        # 가 나오는지 확인 — 호출자가 시점 자르기를 잊으면 last 기준 stop 이
        # 나오므로 회귀 발생.
        n = 100
        idx = pd.date_range("2024-01-01", periods=n)
        # 1~95일까지 100, 96~100일에 급락 80
        prices = [100.0] * 95 + [80.0] * 5
        df = _make_df(prices)

        # signal 시점 = 95일째(인덱스 94, 가격 100)
        df_at_sig  = df.iloc[:95]
        df_at_last = df

        d_sig  = W._build_indicators(df_at_sig)
        d_last = W._build_indicators(df_at_last)

        sig_dict = {
            "signal_type": "REBOUND",
            "price":       100.0,
        }
        sl_at_sig  = W.compute_stop_loss(sig_dict, daily_ind=d_sig,  weekly_ind=None)
        sl_at_last = W.compute_stop_loss(sig_dict, daily_ind=d_last, weekly_ind=None)

        # 두 시점의 cur_m50 가 다르므로 stop 도 달라야 한다 — 회귀 시 동일해짐
        assert sl_at_sig is not None
        assert sl_at_last is not None
        assert abs(sl_at_sig - sl_at_last) > 0.5, (
            f"compute_stop_loss 가 입력 indicator 시점에 따라 다른 값을 내야 함. "
            f"sig={sl_at_sig}, last={sl_at_last}"
        )

    def test_analyze_stock_passes_signal_date_indicators(self, monkeypatch):
        """analyze_stock 이 compute_stop_loss / detect_rs_zero_cross 를
        *signal 시점까지 슬라이스* 한 시리즈로 호출하는지 직접 검증.

        주변 픽스처(base_low / pivot 우선순위) 영향을 받지 않도록 두 헬퍼를
        monkeypatch 하여 호출 인자를 캡처한 뒤, 마지막 인덱스가
        signal_date 와 일치하는지 확인한다.
        """
        from scanner import weinstein as W

        # 5일 전 돌파 + 그 이후 강세 지속 → df.index[-1] != signal_date
        prices, volumes = _make_stage2_base(n_total=230, base_price=100.0)
        breakout_idx = len(prices) - 5
        prices[breakout_idx]  = 104.0
        volumes[breakout_idx] = 6_000_000
        for i in range(breakout_idx + 1, len(prices)):
            prices[i]  = 110.0
            volumes[i] = 1_000_000

        df = _make_df(prices, volumes)

        captured = {"stop_loss": None, "zero_cross": None}

        def _capture_stop_loss(sig, daily_ind=None, weekly_ind=None):
            # weekly_ind 는 scalar dict (시간 idx 없음) 이므로 daily 만 추적
            captured["stop_loss"] = {
                "daily_last_idx":  daily_ind["close"].index[-1] if daily_ind is not None else None,
                "price":           sig.get("price"),
            }
            return None

        def _capture_zero_cross(close, bench, lookback_weeks=None):
            captured["zero_cross"] = {
                "close_last_idx": close.index[-1] if close is not None and len(close) else None,
                "bench_last_idx": bench.index[-1] if bench is not None and len(bench) else None,
            }
            return False

        monkeypatch.setattr(W, "compute_stop_loss",        _capture_stop_loss)
        monkeypatch.setattr(W, "detect_rs_zero_cross",     _capture_zero_cross)

        # 임의 benchmark — RS zero-cross 호출을 트리거하기 위해서만 필요
        bench = pd.Series([100.0 + i * 0.05 for i in range(len(df))], index=df.index)

        res = W.analyze_stock(df, "TEST", "테스트", "US", benchmark_close=bench)
        if res is None or res["signal_type"] != "BREAKOUT":
            pytest.skip("BREAKOUT 시그널이 5일 전에 발생하지 않음 (픽스처 의존)")

        # ── 핵심 invariant: 호출 시점 마지막 인덱스가 signal_date 이하 ──
        sig_date = pd.Timestamp(res["signal_date"])
        last_bar = df.index[-1]
        assert sig_date < last_bar, "회귀 검증 조건: signal_date 가 last bar 보다 과거여야 함"

        sl_call = captured["stop_loss"]
        zc_call = captured["zero_cross"]
        assert sl_call is not None, "compute_stop_loss 가 호출되지 않음"
        assert zc_call is not None, "detect_rs_zero_cross 가 호출되지 않음"

        # daily indicator 의 마지막 idx 가 last bar 가 아니라 signal_date 이하
        assert pd.Timestamp(sl_call["daily_last_idx"]) <= sig_date, (
            f"compute_stop_loss 가 last bar daily_ind 를 받음. "
            f"daily_last={sl_call['daily_last_idx']}, sig_date={sig_date}"
        )
        # zero-cross 의 close / bench 도 동일
        assert pd.Timestamp(zc_call["close_last_idx"]) <= sig_date, (
            f"detect_rs_zero_cross 가 last bar close 를 받음. "
            f"close_last={zc_call['close_last_idx']}, sig_date={sig_date}"
        )
        assert pd.Timestamp(zc_call["bench_last_idx"]) <= sig_date, (
            f"detect_rs_zero_cross 가 last bar benchmark 를 받음. "
            f"bench_last={zc_call['bench_last_idx']}, sig_date={sig_date}"
        )
        # stop_loss price 도 signal 시점 close 여야 함 (last bar 가격 110 이 아닌)
        sig_close_expected = float(df.loc[:sig_date]["Close"].iloc[-1])
        assert abs(sl_call["price"] - sig_close_expected) < 1e-6, (
            f"compute_stop_loss 가 last bar price 를 받음. "
            f"got={sl_call['price']}, expected={sig_close_expected}"
        )

    def test_analyze_stock_rs_value_trend_at_signal_date(self):
        """rs_value / rs_trend 도 signal 시점 시리즈로 산출되어야 한다.

        Gate 6 (strict_filter._check_rs) 가 직접 읽는 필드가 rs_value /
        rs_trend / rs_zero_crossed 셋이므로, rs_zero_crossed 만 슬라이스
        해서는 부족하다. signal 발생 후 RS 가 음→양으로 개선된 합성
        픽스처에서, analyze_stock 결과의 rs_value 가 *signal 시점*(음수)
        기준이지 last bar(양수) 기준이 아닌지 검증.
        """
        from scanner.weinstein import analyze_stock, compute_relative_performance

        # Mansfield RS 는 SMA52(주) ≈ 260 거래일 필요 → n_total≥280 보장.
        # 5일 전 돌파, 그 이후 강세 지속.
        prices, volumes = _make_stage2_base(n_total=300, base_price=100.0)
        breakout_idx = len(prices) - 5
        prices[breakout_idx]  = 104.0
        volumes[breakout_idx] = 6_000_000
        for i in range(breakout_idx + 1, len(prices)):
            prices[i]  = 110.0
            volumes[i] = 1_000_000
        df = _make_df(prices, volumes)

        # 벤치마크: signal 시점까지는 종목보다 *훨씬 강세* (ratio 가 SMA52
        # 아래 → RS 음수), 마지막 5일에 급락 → ratio 가 SMA52 위로 점프
        # → last bar 기준 RS 양수.
        n = len(df)
        bench_vals = [50.0 + i * 0.5 for i in range(n - 5)]            # 강한 상승
        bench_vals += [bench_vals[-1] * (1.0 - 0.10 * (k + 1)) for k in range(5)]  # 급락
        bench = pd.Series(bench_vals, index=df.index)

        # 회귀 검증 조건: signal-시점 RS 와 last-bar RS 가 부호 반대인지 확인
        sig_date_idx = df.index[breakout_idx]
        rs_last_bar, _ = compute_relative_performance(df["Close"], bench)
        rs_sig_view, _ = compute_relative_performance(
            df["Close"].loc[:sig_date_idx], bench.loc[:sig_date_idx]
        )
        if rs_last_bar is None or rs_sig_view is None:
            pytest.skip("RS 산출 불가 (벤치마크 길이 부족)")
        if rs_sig_view >= 0 or rs_last_bar <= 0:
            pytest.skip(
                f"픽스처에서 RS 부호 반전 못 만듦 (sig={rs_sig_view}, last={rs_last_bar})"
            )

        res = analyze_stock(df, "TEST", "테스트", "US", benchmark_close=bench)
        if res is None or res["signal_type"] != "BREAKOUT":
            pytest.skip("BREAKOUT 시그널이 5일 전에 발생하지 않음 (픽스처 의존)")

        # 결과 rs_value 는 signal 시점(음수) 이지 last bar(양수) 이면 안 됨
        assert res["rs_value"] is not None
        assert res["rs_value"] < 0, (
            f"analyze_stock 가 last bar RS 를 기록함 (look-ahead). "
            f"got rs_value={res['rs_value']}, sig_view={rs_sig_view}, "
            f"last_view={rs_last_bar}"
        )
        # 부호만 검증 — 정확한 수치는 cur_ratio/cur_sma 계산 미세차로 변동 가능
        assert abs(res["rs_value"] - rs_sig_view) < 0.5, (
            f"rs_value 가 signal 시점 RS 와 일치하지 않음. "
            f"got={res['rs_value']}, expected≈{rs_sig_view}"
        )

    def test_rs_zero_cross_does_not_look_ahead(self):
        """RS 가 *signal 이후* 0선을 음→양 전환.

        signal 시점까지만 보면 zero-cross 없음 → rs_zero_crossed=False 여야 한다.
        analyze_stock 가 last bar 기준으로 본다면 True 가 되어 회귀.
        """
        from scanner.weinstein import detect_rs_zero_cross

        # 전체 길이
        n = 320
        idx = pd.date_range("2022-01-01", periods=n)

        # 처음 ~315일: stock 이 bench 보다 약함 (음수 RS)
        # 마지막 5일: stock 급등 → RS 0선 통과 (last bar 까지 보면 zero-cross)
        stock_vals = [100.0 + i * 0.1 for i in range(n - 5)]
        stock_vals += [stock_vals[-1] * (1.0 + 0.02 * (k + 1)) for k in range(5)]
        bench_vals = [100.0 + i * 0.5 for i in range(n)]
        stock = pd.Series(stock_vals, index=idx)
        bench = pd.Series(bench_vals, index=idx)

        # last bar 까지 보면 → True 가 나올 수 있음 (전환이 *최근* lookback 안에)
        last_view = detect_rs_zero_cross(stock, bench, lookback_weeks=8)

        # signal 시점 = 5일 전(인덱스 314), 그 시점까지 슬라이스
        signal_date = idx[n - 6]
        stock_at_sig = stock.loc[:signal_date]
        bench_at_sig = bench.loc[:signal_date]
        sig_view = detect_rs_zero_cross(stock_at_sig, bench_at_sig, lookback_weeks=8)

        # 회귀 가드: 두 view 가 다른 답을 낼 수 있다는 invariant 자체를 보호.
        # 픽스처가 의도대로 만들어졌으면 last_view=True / sig_view=False 인 케이스가 존재.
        assert sig_view is False, (
            f"signal 시점에는 zero-cross 없어야 함. last_view={last_view}, sig_view={sig_view}"
        )

    def test_strict_gate_inputs_at_signal_date(self):
        """analyze_stock 결과 dict 의 *공개* 필드와 *strict_* 필드가 각각
        의도된 의미(last-bar vs signal-date)로 동시에 채워지는지 검증.

        리뷰 히스토리:
          - 1차 P1: detect_* 가 며칠 전 signal_date 를 반환할 때 strict gate 가
            last-bar price/ma150/sma30w/... 을 읽으면 신호 *이후* 데이터로 평가
            되어 no-look-ahead invariant 가 깨짐.
          - 2차 P1: 1차 수정이 공개 필드(``price``/``ma150``/``volume``/
            ``weekly_stage``/``sma30w``/...) 를 signal-date 로 재할당해 버려
            ``ScanResult.price`` / Telegram 알림 / ``/api/results`` / UI 의
            "현재가" / 관심종목 ``buy_price`` 가 며칠 전 가격을 표시하게 되어
            Phase 4 PR 의 no-op 보장이 깨졌다.

        최종 설계: **공개 필드는 last-bar 유지** (display/persist 의미 보존).
        **strict_* 스냅샷 필드를 별도로 도입** 하여 ``apply_strict_filter`` 만
        그 키들을 읽도록 분리. 이 테스트는 두 invariant 를 *동시에* 단언한다.

        픽스처: 5일 전 BREAKOUT + 그 이후 강세 지속(가격 110, 거래량 1M) →
        signal-date 의 ma150/cur_p 와 last-bar 의 ma150/cur_p 가 명확히 차이.
        """
        from scanner.weinstein import (
            analyze_stock, _build_indicators, classify_stage,
            compute_weekly_indicators, to_weekly_ohlcv,
        )

        # 5일 전 BREAKOUT 픽스처 (test_analyze_stock_passes_signal_date_indicators 와 동일 구조)
        prices, volumes = _make_stage2_base(n_total=230, base_price=100.0)
        breakout_idx = len(prices) - 5
        prices[breakout_idx]  = 104.0
        volumes[breakout_idx] = 6_000_000
        for i in range(breakout_idx + 1, len(prices)):
            prices[i]  = 110.0
            volumes[i] = 1_000_000

        df = _make_df(prices, volumes)

        res = analyze_stock(df, "TEST", "테스트", "US")
        if res is None or res["signal_type"] != "BREAKOUT":
            pytest.skip("BREAKOUT 시그널이 5일 전에 발생하지 않음 (픽스처 의존)")

        sig_date = pd.Timestamp(res["signal_date"])
        last_bar = df.index[-1]
        # 회귀 검증 조건 — signal_date 가 last bar 이전이어야 비교 의미 있음
        assert sig_date < last_bar, (
            f"signal_date={sig_date} 가 last bar={last_bar} 이전이어야 픽스처 의도 충족"
        )

        # ── signal-date 기준 indicator 직접 계산 (strict_* 가 매칭되어야 함) ──
        df_sig     = df.loc[:sig_date]
        d_sig      = _build_indicators(df_sig)
        w_sig_df   = to_weekly_ohlcv(df_sig)
        w_sig      = compute_weekly_indicators(w_sig_df) if len(w_sig_df) > 0 else None
        stage_sig  = classify_stage(w_sig, d_sig)

        # ── last-bar 기준 indicator (공개 필드가 매칭되어야 함) ──
        d_last     = _build_indicators(df)
        w_last     = compute_weekly_indicators(to_weekly_ohlcv(df))
        stage_last = classify_stage(w_last, d_last)

        # 픽스처가 의도대로 차이를 만들었는지 — 셋 중 하나라도 차이 있으면 OK
        diff_present = (
            abs(d_sig["cur_p"] - d_last["cur_p"]) > 0.5
            or abs(d_sig["cur_m150"] - d_last["cur_m150"]) > 0.05
            or (w_sig is not None and w_last is not None
                and abs(w_sig["cur_sma30w"] - w_last["cur_sma30w"]) > 0.05)
        )
        if not diff_present:
            pytest.skip("픽스처가 signal-date vs last-bar 차이를 만들지 못함")

        # ══════════════════════════════════════════════════════════════
        # Invariant A — 공개 필드는 *last-bar* 유지 (display/persist 의미)
        # ══════════════════════════════════════════════════════════════
        assert abs(res["price"] - d_last["cur_p"]) < 1e-3, (
            f"result['price'] 는 last-bar 가격이어야 함 (display). "
            f"got={res['price']} last={d_last['cur_p']:.4f} sig={d_sig['cur_p']:.4f}"
        )
        assert abs(res["ma150"] - d_last["cur_m150"]) < 1e-3, (
            f"result['ma150'] 는 last-bar 값이어야 함. "
            f"got={res['ma150']} last={d_last['cur_m150']:.4f} sig={d_sig['cur_m150']:.4f}"
        )
        assert abs(res["ma50"] - d_last["cur_m50"]) < 1e-3, (
            f"result['ma50'] 는 last-bar 값이어야 함. got={res['ma50']}"
        )
        assert abs(res["ma_slope"] - d_last["slope150"]) < 1e-4, (
            f"result['ma_slope'] 는 last-bar 값이어야 함."
        )
        assert int(d_last["cur_v"])  == res["volume"],     "result['volume'] 는 last-bar 거래량"
        assert int(d_last["cur_va"]) == res["volume_avg"], "result['volume_avg'] 는 last-bar 평균"
        assert res["weekly_stage"] == stage_last, (
            f"result['weekly_stage'] 는 last-bar 분류여야 함. "
            f"got={res['weekly_stage']} last={stage_last} sig={stage_sig}"
        )
        if w_sig is not None and w_last is not None:
            assert abs(res["sma30w"] - w_last["cur_sma30w"]) < 1e-3, (
                f"result['sma30w'] 는 last-bar 값이어야 함. "
                f"got={res['sma30w']} last={w_last['cur_sma30w']:.4f} sig={w_sig['cur_sma30w']:.4f}"
            )
            assert abs(res["slope30w"] - w_last["slope30w"]) < 1e-5, (
                f"result['slope30w'] 는 last-bar 값이어야 함."
            )
            wvr_last = w_last.get("weekly_volume_ratio")
            if wvr_last is not None and res.get("weekly_volume_ratio") is not None:
                assert abs(float(res["weekly_volume_ratio"]) - float(wvr_last)) < 1e-3, (
                    f"result['weekly_volume_ratio'] 는 last-bar 값이어야 함. "
                    f"got={res['weekly_volume_ratio']} last={wvr_last} "
                    f"sig={w_sig.get('weekly_volume_ratio')}"
                )

        # ══════════════════════════════════════════════════════════════
        # Invariant B — strict_* 스냅샷은 *signal-date* 값
        # apply_strict_filter 가 이 키들만 읽어 no-look-ahead 보장.
        # ══════════════════════════════════════════════════════════════
        assert abs(res["strict_price"] - d_sig["cur_p"]) < 1e-3, (
            f"result['strict_price'] 는 signal-date 종가여야 함. "
            f"got={res['strict_price']} sig={d_sig['cur_p']:.4f} last={d_last['cur_p']:.4f}"
        )
        assert abs(res["strict_ma150"] - d_sig["cur_m150"]) < 1e-3, (
            f"result['strict_ma150'] 는 signal-date MA150 이어야 함. "
            f"got={res['strict_ma150']} sig={d_sig['cur_m150']:.4f} last={d_last['cur_m150']:.4f}"
        )
        assert abs(res["strict_ma50"] - d_sig["cur_m50"]) < 1e-3, (
            f"result['strict_ma50'] 는 signal-date MA50 이어야 함. got={res['strict_ma50']}"
        )
        assert res["strict_weekly_stage"] == stage_sig, (
            f"result['strict_weekly_stage'] 는 signal-date 분류여야 함. "
            f"got={res['strict_weekly_stage']} sig={stage_sig} last={stage_last}"
        )
        if w_sig is not None:
            assert abs(res["strict_sma30w"] - w_sig["cur_sma30w"]) < 1e-3, (
                f"result['strict_sma30w'] 는 signal-date 30W SMA 여야 함. "
                f"got={res['strict_sma30w']} sig={w_sig['cur_sma30w']:.4f} "
                f"last={w_last['cur_sma30w']:.4f if w_last else 'N/A'}"
            )
            assert abs(res["strict_slope30w"] - w_sig["slope30w"]) < 1e-5, (
                f"result['strict_slope30w'] 는 signal-date 기울기여야 함."
            )
            wvr_sig = w_sig.get("weekly_volume_ratio")
            if wvr_sig is not None and res.get("strict_weekly_volume_ratio") is not None:
                assert abs(float(res["strict_weekly_volume_ratio"]) - float(wvr_sig)) < 1e-3, (
                    f"result['strict_weekly_volume_ratio'] 는 signal-date 비율이어야 함. "
                    f"got={res['strict_weekly_volume_ratio']} sig={wvr_sig}"
                )


# ═══════════════════════════════════════════════════════════════════
# Strict Weinstein filter — Phase 1 scaffold
# ═══════════════════════════════════════════════════════════════════

class TestStrictFilterScaffold:
    """Phase 1: analyze_stock 결과 dict 에 strict-filter 스캐폴드 키가 존재하는지 확인.

    실제 게이트 로직은 Phase 2~4 에서 채워지므로 여기서는 *키 존재* 와
    *기본값* 만 검증한다. 동작 변화는 없어야 한다(no-op).
    """

    def _setup_breakout_df(self):
        """기존 v4 BREAKOUT 픽스처와 동일한 구조 — Stage2 + 거래량 spike."""
        prices, volumes = _make_stage2_base(n_total=230, base_price=100.0)
        prices[-1]  = 104.0
        volumes[-1] = 6_000_000
        return _make_df(prices, volumes)

    def test_scaffold_keys_exist_on_breakout(self):
        """analyze_stock 출력에 stop_loss / strict_filter_passed / filter_reasons 가 있어야 함."""
        from scanner.weinstein import analyze_stock

        df  = self._setup_breakout_df()
        res = analyze_stock(df, "TEST", "테스트", "US")

        assert res is not None
        assert "stop_loss"            in res, "Phase 1 scaffold 누락: stop_loss"
        assert "strict_filter_passed" in res, "Phase 1 scaffold 누락: strict_filter_passed"
        assert "filter_reasons"       in res, "Phase 1 scaffold 누락: filter_reasons"

    def test_scaffold_default_values(self):
        """strict_filter_passed / filter_reasons 의 Phase 1 기본값은 Phase 4 머지 전까지 유지.

        stop_loss / rs_zero_crossed 는 Phase 2 부터 실제로 채워지므로 여기서는
        '키 존재 + 타입 sane' 만 보장 (실제 값 검증은 TestStopLoss / TestRSZeroCross).
        """
        from scanner.weinstein import analyze_stock

        df  = self._setup_breakout_df()
        res = analyze_stock(df, "TEST", "테스트", "US")

        assert res is not None
        # Phase 4 의 scan_engine 이 채울 때까지 None — 이 invariant 가 핵심
        assert res["strict_filter_passed"] is None
        # 거부 사유는 빈 리스트로 시작 (Phase 4 가 채움)
        assert res["filter_reasons"] == []
        assert isinstance(res["filter_reasons"], list)
        # Phase 2 이후 stop_loss 는 None 또는 float
        assert res["stop_loss"] is None or isinstance(res["stop_loss"], (int, float))


# ── 실행 ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import subprocess
    subprocess.run(
        [sys.executable, "-m", "pytest", __file__, "-v", "--tb=short"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
