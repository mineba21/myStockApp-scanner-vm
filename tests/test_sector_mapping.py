"""Gate 2 (섹터) — 종목→GICS→ETF→주봉 Stage 매핑.

네트워크를 타지 않는다. 위키 표는 합성 HTML 로, 섹터 Stage 는
``get_market_stages`` 를 monkeypatch 해 대체한다.
"""
import io

import pandas as pd
import pytest

from scanner import market_analysis as ma


# ── 매핑표 완전성 ──────────────────────────────────────────────────

# 위키 S&P500 표의 GICS Sector 실측값 11종 (2026-09 확인).
# 위키가 섹터 체계를 바꾸면 여기서 먼저 깨져야 한다 — 조용히 미매핑으로
# 흘러가면 Gate 2 를 켰을 때 해당 섹터 종목이 통째로 탈락한다.
GICS_SECTORS = {
    "Industrials", "Financials", "Information Technology", "Health Care",
    "Consumer Discretionary", "Consumer Staples", "Utilities", "Real Estate",
    "Materials", "Communication Services", "Energy",
}


class TestSectorEtfMap:
    def test_covers_every_gics_sector(self):
        mapped = {e["gics"] for e in ma.US_SECTOR_ETFS if e.get("gics")}
        assert mapped == GICS_SECTORS, (
            f"매핑 누락: {GICS_SECTORS - mapped} / 정체불명: {mapped - GICS_SECTORS}"
        )

    def test_tickers_and_labels_are_unique(self):
        tickers = [e["ticker"] for e in ma.US_SECTOR_ETFS]
        labels = [e["name"] for e in ma.US_SECTOR_ETFS]
        assert len(set(tickers)) == len(tickers)
        assert len(set(labels)) == len(labels)

    def test_kr_etfs_have_no_gics_key(self):
        """국내는 종목→섹터 소스가 없어 Gate 2 에 참여하지 않는다."""
        assert all("gics" not in e for e in ma.KR_SECTOR_ETFS)


# ── get_sector_stage ───────────────────────────────────────────────

def _fake_stages(monkeypatch, rows):
    monkeypatch.setattr(ma, "get_market_stages", lambda force=False: {"US_SECTORS": rows})


class TestGetSectorStage:
    def test_resolves_label_and_weekly_stage(self, monkeypatch):
        _fake_stages(monkeypatch, [
            {"ticker": "XLK", "stage": "STAGE1", "stage_weekly": "STAGE2"},
        ])
        assert ma.get_sector_stage("Information Technology") == ("기술", "STAGE2")

    def test_reports_stage4(self, monkeypatch):
        _fake_stages(monkeypatch, [{"ticker": "XLE", "stage_weekly": "STAGE4"}])
        assert ma.get_sector_stage("Energy") == ("에너지", "STAGE4")

    def test_uses_weekly_not_daily_stage(self, monkeypatch):
        """일봉 stage 와 주봉 stage 가 갈릴 때 주봉을 따라야 한다."""
        _fake_stages(monkeypatch, [
            {"ticker": "XLF", "stage": "STAGE2", "stage_weekly": "STAGE3"},
        ])
        assert ma.get_sector_stage("Financials")[1] == "STAGE3"

    @pytest.mark.parametrize("value", [None, "", "Nonexistent Sector"])
    def test_unmapped_sector_returns_none_pair(self, monkeypatch, value):
        _fake_stages(monkeypatch, [{"ticker": "XLK", "stage_weekly": "STAGE2"}])
        assert ma.get_sector_stage(value) == (None, None)

    def test_non_us_market_is_skipped(self, monkeypatch):
        _fake_stages(monkeypatch, [{"ticker": "XLK", "stage_weekly": "STAGE2"}])
        assert ma.get_sector_stage("Information Technology", "KR") == (None, None)

    def test_missing_etf_row_keeps_label(self, monkeypatch):
        """ETF 데이터가 안 잡혀도 라벨(표시용)은 살린다."""
        _fake_stages(monkeypatch, [])
        assert ma.get_sector_stage("Materials") == ("소재", None)

    def test_market_stages_failure_is_contained(self, monkeypatch):
        def boom(force=False):
            raise RuntimeError("network down")
        monkeypatch.setattr(ma, "get_market_stages", boom)
        assert ma.get_sector_stage("Utilities") == ("유틸리티", None)


# ── get_sp500_tickers 의 sector 필드 ───────────────────────────────

def _wiki_html(include_sector: bool) -> str:
    cols = ["Symbol", "Security"] + (["GICS Sector"] if include_sector else [])
    rows = [["AAPL", "Apple Inc.", "Information Technology"],
            ["XOM", "Exxon Mobil", "Energy"]]
    if not include_sector:
        rows = [r[:2] for r in rows]
    return pd.DataFrame(rows, columns=cols).to_html(index=False)


class TestSp500SectorColumn:
    def test_sector_is_carried_through(self, monkeypatch):
        from scanner import us_stocks
        monkeypatch.setattr(us_stocks, "_read_html_wiki",
                            lambda url: pd.read_html(io.StringIO(_wiki_html(True))))
        rows = us_stocks.get_sp500_tickers()
        assert {r["ticker"]: r["sector"] for r in rows} == {
            "AAPL": "Information Technology", "XOM": "Energy",
        }

    def test_missing_column_degrades_to_none(self, monkeypatch):
        """위키 표가 바뀌어도 종목 목록 자체는 계속 나와야 한다."""
        from scanner import us_stocks
        monkeypatch.setattr(us_stocks, "_read_html_wiki",
                            lambda url: pd.read_html(io.StringIO(_wiki_html(False))))
        rows = us_stocks.get_sp500_tickers()
        assert len(rows) == 2
        assert all(r["sector"] is None for r in rows)


# ── scan_engine 주입 + shadow 불변 ─────────────────────────────────

class TestStrictFilterSectorInjection:
    def _signal(self, **over):
        sig = {
            "ticker": "AAPL", "name": "Apple", "market": "US",
            "signal_type": "BREAKOUT", "_gics_sector": "Information Technology",
            "strict_price": 100.0, "strict_ma150": 90.0, "strict_ma50": 95.0,
            "strict_weekly_stage": "STAGE2", "strict_sma30w": 88.0,
            "strict_slope30w": 0.5, "strict_weekly_volume_ratio": 2.0,
            "volume_ratio": 2.0, "rs_value": 1.0, "rs_trend": "RISING",
            "rs_zero_crossed": True, "stop_loss": 92.0,
            "base_weeks": 8, "base_quality_v4": "TIGHT", "warning_flags": [],
        }
        sig.update(over)
        return sig

    def test_populates_sector_fields(self, monkeypatch):
        from scanner import scan_engine
        monkeypatch.setattr(ma, "get_sector_stage",
                            lambda gics, market=None: ("기술", "STAGE2"))
        sig = self._signal()
        scan_engine._evaluate_strict_filter(sig, "BULL", object())
        assert sig["sector_name"] == "기술"
        assert sig["sector_stage"] == "STAGE2"

    def test_unmapped_signal_gets_null_sector(self, monkeypatch):
        from scanner import scan_engine
        monkeypatch.setattr(ma, "get_sector_stage",
                            lambda gics, market=None: (None, None))
        sig = self._signal(market="KR", _gics_sector=None)
        scan_engine._evaluate_strict_filter(sig, "BULL", object())
        assert sig["sector_name"] is None
        assert sig["sector_stage"] is None

    def test_shadow_mode_never_rejects_on_sector(self, monkeypatch):
        """STRICT_REQUIRE_SECTOR_STAGE2=False 면 STAGE4 여도 차단하지 않는다.

        이번 변경의 핵심 안전장치 — 섹터 값이 채워지기 시작해도 판정 결과는
        그대로여야 한다.
        """
        import config
        from scanner import scan_engine, strict_filter
        monkeypatch.setattr(config, "STRICT_REQUIRE_SECTOR_STAGE2", False)
        monkeypatch.setattr(strict_filter, "STRICT_REQUIRE_SECTOR_STAGE2", False)
        monkeypatch.setattr(ma, "get_sector_stage",
                            lambda gics, market=None: ("에너지", "STAGE4"))
        sig = self._signal()
        _, reasons = scan_engine._evaluate_strict_filter(sig, "BULL", object())
        assert strict_filter.SECTOR_STAGE4 not in reasons
        assert strict_filter.SECTOR_NOT_STAGE2 not in reasons
        assert sig["sector_stage"] == "STAGE4"   # 관측은 되고 있어야 한다


# ── 시장별 면제 (Gate 2 를 켰을 때) ────────────────────────────────

class TestSectorGateMarketExemption:
    """게이트를 켜도 국내는 면제된다.

    ``_check_sector`` 는 sector_stage=None 을 통과가 아니라 **실패**로
    취급한다. 국내는 종목→섹터 매핑 소스가 없어 항상 None 이므로, 면제가
    없으면 공통 플래그를 켜는 순간 KR 시그널이 전멸한다.
    """

    def _on(self, monkeypatch, exempt={"KR"}):
        from scanner import strict_filter
        monkeypatch.setattr(strict_filter, "STRICT_REQUIRE_SECTOR_STAGE2", True)
        monkeypatch.setattr(strict_filter, "SECTOR_GATE_EXEMPT_MARKETS", exempt)
        return strict_filter

    def test_kr_is_exempt_even_with_gate_on(self, monkeypatch):
        sf = self._on(monkeypatch)
        reasons = []
        sf._check_sector({"market": "KR"}, {"sector_stage": None}, reasons)
        assert reasons == []

    def test_kr_exempt_regardless_of_sector_stage(self, monkeypatch):
        sf = self._on(monkeypatch)
        for stage in (None, "STAGE1", "STAGE3", "STAGE4"):
            reasons = []
            sf._check_sector({"market": "KR"}, {"sector_stage": stage}, reasons)
            assert reasons == [], f"KR 이 {stage} 로 차단됨"

    def test_us_is_still_gated(self, monkeypatch):
        sf = self._on(monkeypatch)
        reasons = []
        sf._check_sector({"market": "US"}, {"sector_stage": "STAGE4"}, reasons)
        assert sf.SECTOR_STAGE4 in reasons

    def test_us_unmapped_still_fails(self, monkeypatch):
        """면제 목록에 없는 시장은 미매핑이 여전히 실패다 — 켤 때의 주의점."""
        sf = self._on(monkeypatch)
        reasons = []
        sf._check_sector({"market": "US"}, {"sector_stage": None}, reasons)
        assert sf.SECTOR_NOT_STAGE2 in reasons

    def test_exempt_list_is_configurable(self, monkeypatch):
        sf = self._on(monkeypatch, exempt={"KR", "US"})
        reasons = []
        sf._check_sector({"market": "US"}, {"sector_stage": "STAGE4"}, reasons)
        assert reasons == []

    def test_market_matching_is_case_insensitive(self, monkeypatch):
        sf = self._on(monkeypatch)
        reasons = []
        sf._check_sector({"market": "kr"}, {"sector_stage": "STAGE4"}, reasons)
        assert reasons == []

    def test_missing_market_key_is_not_exempt(self, monkeypatch):
        """market 이 없는 시그널을 조용히 면제하지 않는다."""
        sf = self._on(monkeypatch)
        reasons = []
        sf._check_sector({}, {"sector_stage": "STAGE4"}, reasons)
        assert sf.SECTOR_STAGE4 in reasons
