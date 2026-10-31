"""섹터 분류·최근 추세(표시 전용) 테스트. 외부 공급자는 주입 함수로만 흉내 낸다."""
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from scanner import sector_trends as st


def closes(values, end="2026-10-30"):
    index = pd.bdate_range(end=end, periods=len(values))
    return pd.Series(values, index=index, dtype=float)


def ramp(total_pct, n=21, end="2026-10-30", start=100.0):
    """n개 종가로 마지막/첫 값이 정확히 (1+total_pct%) 가 되게 만든다."""
    values = list(np.linspace(start, start * (1 + total_pct / 100), n))
    return closes(values, end=end)


NOW = date(2026, 10, 31)


# ── 5단계 판정 ───────────────────────────────────────────────────

@pytest.mark.parametrize("pct,trend", [
    (12.0, "STRONG_UP"), (5.0, "STRONG_UP"), (4.999, "UP"), (1.0, "UP"),
    (0.999, "FLAT"), (0.0, "FLAT"), (-0.999, "FLAT"), (-1.0, "DOWN"),
    (-4.999, "DOWN"), (-5.0, "STRONG_DOWN"), (-20.0, "STRONG_DOWN"),
])
def test_classify_trend_boundaries(pct, trend):
    assert st.classify_trend(pct) == trend


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), "x"])
def test_classify_trend_rejects_missing_values(value):
    assert st.classify_trend(value) is None


def test_classify_trend_follows_configured_thresholds(monkeypatch):
    monkeypatch.setattr(st, "SECTOR_TREND_STRONG_PCT", 8.0)
    monkeypatch.setattr(st, "SECTOR_TREND_FLAT_PCT", 2.0)
    assert st.classify_trend(5.0) == "UP"
    assert st.classify_trend(1.5) == "FLAT"
    assert st.classify_trend(-8.0) == "STRONG_DOWN"


# ── N거래일 수익률 ───────────────────────────────────────────────

def test_trailing_return_uses_exactly_lookback_plus_one_closes():
    # 22개 중 맨 앞(50)은 20거래일 창 밖이라 쓰이지 않아야 한다. 창: 100 → 110.
    series = closes([50.0, 100.0] + [105.0] * 19 + [110.0])
    result = st.trailing_return(series, now=NOW)
    assert result == {"status": "OK", "return_pct": pytest.approx(10.0), "asof": "2026-10-30"}


def test_trailing_return_is_insufficient_below_lookback_plus_one():
    result = st.trailing_return(ramp(3.0, n=20), now=NOW)
    assert result["status"] == st.INSUFFICIENT and result["return_pct"] is None
    assert result["asof"] == "2026-10-30"


def test_trailing_return_drops_bad_values_without_filling():
    series = ramp(4.0, n=21)
    dirty = pd.concat([series, closes([np.nan, 0.0, -5.0, np.inf], end="2026-09-01")]).sort_index()
    assert st.trailing_return(dirty, now=NOW)["return_pct"] == pytest.approx(4.0)
    # 유효 값이 20개가 되면 채우지 않고 부족으로 남긴다.
    holed = series.copy()
    holed.iloc[5] = np.nan
    assert st.trailing_return(holed, now=NOW)["status"] == st.INSUFFICIENT


def test_trailing_return_keeps_last_duplicate_and_ignores_future_rows():
    series = ramp(2.0, n=21)
    dup = pd.Series([999.0], index=[series.index[-1]])
    future = pd.Series([500.0], index=[pd.Timestamp("2026-11-05")])
    combined = pd.concat([dup, series, future])   # 중복 날짜는 마지막 값(정상 종가)이 남는다
    result = st.trailing_return(combined, now=NOW)
    assert result["asof"] == "2026-10-30"
    assert result["return_pct"] == pytest.approx(2.0)


def test_trailing_return_marks_old_data_stale(monkeypatch):
    series = ramp(6.0, n=21, end="2026-10-20")
    assert st.trailing_return(series, now=NOW)["status"] == st.STALE
    assert st.trailing_return(series, now=date(2026, 10, 27))["status"] == "OK"


def test_trailing_return_handles_missing_series():
    assert st.trailing_return(None)["status"] == st.UNAVAILABLE


def test_context_never_turns_missing_data_into_flat():
    assert st.context_from_return({"status": st.INSUFFICIENT, "return_pct": None, "asof": None}) == {
        "sector_trend": st.INSUFFICIENT, "sector_return_20d": None, "sector_trend_asof": None}
    ok = st.context_from_return({"status": "OK", "return_pct": -6.2, "asof": "2026-10-30"})
    assert ok == {"sector_trend": "STRONG_DOWN", "sector_return_20d": -6.2, "sector_trend_asof": "2026-10-30"}


# ── 국내 KRX-DESC 업종 ───────────────────────────────────────────

def cache_schema(rows):
    # 실제 fdr_krx_data_cache CSV 컬럼 (2026-10-30 파일에서 확인)
    return pd.DataFrame(rows, columns=["Code", "Name", "Market", "Sector", "Industry", "Products"])


def test_parse_kr_industries_uses_industry_column_in_cache_schema():
    df = cache_schema([
        ["060310", "3S", "KOSDAQ", "벤처기업부", "측정, 시험, 항해, 제어 및 기타 정밀기기 제조업; 광학기기 제외", "웨이퍼 캐리어"],
        ["5930", "삼성전자", "KOSPI", None, "통신 및 방송 장비 제조업", "휴대폰"],
        ["000001", "빈업종", "KOSPI", None, None, "x"],
        ["000002", "공백", "KOSPI", None, "  ", "x"],
    ])
    mapping = st.parse_kr_industries(df)
    assert mapping["005930"] == "통신 및 방송 장비 제조업"
    assert mapping["060310"].startswith("측정, 시험")     # 소속부(벤처기업부)가 아님
    assert len(mapping["060310"]) <= 50
    assert "000001" not in mapping and "000002" not in mapping


def test_parse_kr_industries_uses_sector_column_in_kind_schema():
    df = pd.DataFrame([["005930", "삼성전자", "통신 및 방송 장비 제조업", "휴대폰"]],
                      columns=["Code", "Name", "Sector", "Industry"])
    assert st.parse_kr_industries(df) == {"005930": "통신 및 방송 장비 제조업"}


def test_parse_kr_industries_rejects_unknown_schema():
    with pytest.raises(ValueError):
        st.parse_kr_industries(pd.DataFrame({"Symbol": ["005930"]}))


@pytest.mark.sector_providers
def test_kr_industry_map_caches_success_and_retries_failure_after_ttl(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(st._cache, "clock", lambda: clock["t"])
    calls = []

    def failing():
        calls.append("fail")
        raise OSError("network")

    assert st.kr_industry_map(failing) is None
    assert st.kr_industry_map(failing) is None          # 실패도 1시간 기억
    assert calls == ["fail"]

    clock["t"] += st.FAILURE_TTL_SECONDS + 1
    good = lambda: cache_schema([["005930", "삼성전자", "KOSPI", None, "반도체 제조업", "x"]])  # noqa: E731
    assert st.kr_industry_map(good) == {"005930": "반도체 제조업"}
    assert st.kr_industry_map(failing) == {"005930": "반도체 제조업"}   # 24시간 캐시
    clock["t"] += st.CLASSIFICATION_TTL_SECONDS + 1
    assert st.kr_industry_map(failing) is None


@pytest.mark.sector_providers
def test_yahoo_sector_maps_explicitly_and_caches():
    calls = []

    def fetch(ticker):
        calls.append(ticker)
        return {"sector": {"ASML": "Technology", "XYZ": "Conglomerates"}.get(ticker)}

    assert st.yahoo_gics_sector("ASML", fetch) == "Information Technology"
    assert st.yahoo_gics_sector("asml", fetch) == "Information Technology"
    assert st.yahoo_gics_sector("XYZ", fetch) is None           # 대응표에 없는 이름은 버린다
    assert st.yahoo_gics_sector("ERR", lambda t: 1 / 0) is None
    assert calls == ["ASML", "XYZ"]


# ── 미국 컨텍스트 ────────────────────────────────────────────────

ETF_ROWS = [
    {"ticker": "XLK", "return_20d": 6.4, "return_asof": "2026-10-30", "return_status": "OK"},
    {"ticker": "XLE", "return_20d": None, "return_asof": "2026-10-30", "return_status": st.INSUFFICIENT},
]


def test_us_context_uses_gics_and_sector_etf():
    ctx = st.us_sector_context("AAPL", "Information Technology", ETF_ROWS,
                               yahoo_lookup=lambda t: pytest.fail("GICS 가 있으면 Yahoo 를 부르지 않는다"))
    assert ctx["sector_name"] == "기술" and ctx["sector_source"] == "GICS"
    assert ctx["sector_trend_basis"] == "ETF:XLK"
    assert (ctx["sector_trend"], ctx["sector_return_20d"], ctx["sector_trend_asof"]) == ("STRONG_UP", 6.4, "2026-10-30")


def test_us_context_falls_back_to_yahoo_only_for_display():
    ctx = st.us_sector_context("ASML", None, ETF_ROWS, yahoo_lookup=lambda t: "Information Technology")
    assert (ctx["sector_name"], ctx["sector_source"], ctx["sector_trend"]) == ("기술", "YAHOO", "STRONG_UP")
    assert "sector_stage" not in ctx    # Gate 2 입력은 건드리지 않는다


def test_us_context_missing_data_states():
    assert st.us_sector_context("PDD", None, ETF_ROWS, yahoo_lookup=lambda t: None)["sector_name"] is None
    energy = st.us_sector_context("XOM", "Energy", ETF_ROWS)
    assert energy["sector_trend"] == st.INSUFFICIENT and energy["sector_return_20d"] is None
    no_rows = st.us_sector_context("JPM", "Financials", [])
    assert no_rows["sector_name"] == "금융" and no_rows["sector_trend"] == st.UNAVAILABLE


# ── 국내 업종 집계 ───────────────────────────────────────────────

def ok(pct, asof="2026-10-30"):
    return {"status": "OK", "return_pct": pct, "asof": asof}


def test_kr_aggregate_uses_median_so_one_outlier_does_not_flip_the_sector():
    industries = {t: "반도체 제조업" for t in "ABCD"}
    returns = {"A": ok(0.5), "B": ok(-0.4), "C": ok(0.2), "D": ok(80.0)}   # D: 테마 급등주
    out = st.aggregate_kr_sectors(industries, "ABCD", returns, now=NOW)["반도체 제조업"]
    assert out["sector_trend"] == "FLAT"
    assert out["sector_return_20d"] == pytest.approx(0.35)
    assert (out["sector_trend_sample"], out["sector_trend_members"]) == (4, 4)
    assert out["sector_trend_basis"] == st.KR_BASIS and out["sector_source"] == "KRX"


def test_kr_aggregate_requires_minimum_sample_and_coverage():
    industries = {"A": "소형", "B": "소형", "C": "중형", "D": "중형", "E": "중형", "F": "중형", "G": "중형"}
    returns = {"A": ok(3.0), "B": ok(4.0), "C": ok(2.0), "D": ok(2.5), "E": ok(1.5)}  # F, G 가격 없음
    out = st.aggregate_kr_sectors(industries, "ABCDEFG", returns, now=NOW)
    assert out["소형"]["sector_trend"] == st.INSUFFICIENT                     # 2 < 3
    assert out["중형"]["sector_trend"] == st.INSUFFICIENT                     # 3/5 = 60% < 70%
    assert (out["중형"]["sector_trend_sample"], out["중형"]["sector_trend_members"]) == (3, 5)
    assert out["중형"]["sector_return_20d"] is None


def test_kr_aggregate_excludes_members_not_on_the_latest_date():
    industries = {t: "화학" for t in "ABCD"}
    returns = {"A": ok(2.0), "B": ok(3.0), "C": ok(2.5), "D": ok(-30.0, asof="2026-10-23")}  # 거래정지 등
    out = st.aggregate_kr_sectors(industries, "ABCD", returns, now=NOW)["화학"]
    assert out["sector_trend_sample"] == 3 and out["sector_trend"] == "UP"
    assert out["sector_trend_asof"] == "2026-10-30"


def test_kr_aggregate_stale_and_unavailable_states():
    industries = {t: "화학" for t in "ABC"}
    stale = {t: {"status": st.STALE, "return_pct": 2.0, "asof": "2026-10-10"} for t in "ABC"}
    assert st.aggregate_kr_sectors(industries, "ABC", stale, now=NOW)["화학"]["sector_trend"] == st.STALE
    none = st.aggregate_kr_sectors(industries, "ABC", {}, now=NOW)["화학"]
    assert none["sector_trend"] == st.UNAVAILABLE and none["sector_trend_sample"] == 0
    # 분류는 있지만 유니버스 밖이면 결과에 넣지 않는다.
    assert st.aggregate_kr_sectors(industries, "XYZ", stale, now=NOW) == {}


def test_kr_sector_name_only_sets_classification():
    assert st.kr_sector_name("005930", {"005930": "반도체 제조업"}) == {
        "sector_name": "반도체 제조업", "sector_source": "KRX"}
    assert st.kr_sector_name("005930", None) == {"sector_name": None, "sector_source": None}


# ── market_analysis: ETF 수익률 필드 ─────────────────────────────

def test_analyze_index_adds_recent_return_fields():
    from scanner import market_analysis as ma
    from scanner.weinstein import stage_of, _slope

    index = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=300)
    close = pd.Series(np.linspace(100, 130, 300), index=index)
    df = pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99,
                       "Close": close, "Volume": 1_000_000}, index=index)
    out = []
    ma._analyze_index({"ticker": "XLK", "name": "기술"}, "US", lambda t: df, 150, stage_of, _slope, out)
    row = out[0]
    expected = (close.iloc[-1] / close.iloc[-21] - 1) * 100
    assert row["return_status"] == "OK"
    assert row["return_20d"] == pytest.approx(expected)
    assert row["return_asof"] == index[-1].date().isoformat()


# ── 저장·스캔 연결 ───────────────────────────────────────────────

def fresh_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from database.models import Base

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def signal(**overrides):
    sig = {"ticker": "AAPL", "name": "Apple", "market": "US", "signal_type": "BREAKOUT",
           "stage": "STAGE2", "price": 105.0, "ma150": 100.0, "volume_ratio": 3.0,
           "signal_date": "2026-10-30", "sector_name": "기술", "sector_stage": "STAGE2"}
    sig.update(overrides)
    return sig


def test_save_persists_us_sector_snapshot(monkeypatch):
    from database.models import ScanResult
    from scanner import market_analysis as ma
    from scanner.scan_engine import _save_signal

    monkeypatch.setattr(ma, "_cache", {"US_SECTORS": ETF_ROWS})
    db = fresh_db()
    _save_signal(db, signal(_gics_sector="Information Technology"))
    row = db.query(ScanResult).one()
    assert (row.sector_name, row.sector_stage, row.sector_source) == ("기술", "STAGE2", "GICS")
    assert (row.sector_trend, row.sector_return_20d, row.sector_trend_asof, row.sector_trend_basis) == \
        ("STRONG_UP", 6.4, "2026-10-30", "ETF:XLK")


def test_attach_never_fetches_market_stages_during_a_scan(monkeypatch):
    from scanner import market_analysis as ma
    from scanner.scan_engine import _attach_sector_context

    monkeypatch.setattr(ma, "_cache", {})
    monkeypatch.setattr(ma, "get_market_stages", lambda force=False: pytest.fail("재조회 금지"))
    res = signal(_gics_sector="Financials")
    _attach_sector_context(res)
    assert res["sector_name"] == "금융" and res["sector_trend"] == st.UNAVAILABLE


def test_yahoo_fallback_does_not_change_gate2_input(monkeypatch):
    """Yahoo 보완 분류는 표시만 한다. Gate 2 의 sector_stage 는 기존 GICS 경로 결과 그대로."""
    from scanner import market_analysis as ma, scan_engine

    monkeypatch.setattr(ma, "_cache", {"US_SECTORS": ETF_ROWS})
    monkeypatch.setattr(st, "yahoo_gics_sector", lambda ticker, fetcher=None: "Information Technology")
    seen = {}

    def fake_gate(sig, ctx):
        seen["sector_stage"] = ctx["sector_stage"]
        return True, []

    monkeypatch.setattr("scanner.strict_filter.apply_strict_filter", fake_gate)
    res = signal(ticker="ASML", sector_name=None, sector_stage=None, _gics_sector=None)
    scan_engine._evaluate_strict_filter(res, "BULL", None)
    scan_engine._attach_sector_context(res)
    assert seen["sector_stage"] is None and res["sector_stage"] is None
    assert (res["sector_name"], res["sector_source"]) == ("기술", "YAHOO")


def test_resave_without_trend_keeps_previous_kr_trend():
    from database.models import ScanResult
    from scanner.scan_engine import _save

    db = fresh_db()
    _save(db, signal(ticker="005930", market="KR", sector_name="반도체 제조업", sector_source="KRX",
                     sector_trend="UP", sector_return_20d=2.0, sector_trend_asof="2026-10-29",
                     sector_trend_basis=st.KR_BASIS, sector_trend_sample=5, sector_trend_members=6))
    # 다음 스캔의 재검출: 분류만 있고 추세는 스캔이 끝난 뒤 채워진다.
    _save(db, signal(ticker="005930", market="KR", sector_name="반도체 제조업", sector_source="KRX"))
    row = db.query(ScanResult).one()
    assert (row.sector_trend, row.sector_return_20d, row.sector_trend_sample) == ("UP", 2.0, 5)


def test_apply_kr_sector_trends_updates_only_rows_from_this_scan(monkeypatch):
    from database.models import ScanResult
    from scanner.scan_engine import _apply_kr_sector_trends

    industries = {"A00001": "화학", "A00002": "화학", "A00003": "화학", "B00001": "화학"}
    monkeypatch.setattr(st, "kr_industry_map", lambda loader=None: industries)
    db = fresh_db()
    started = datetime.utcnow()
    db.add(ScanResult(market="KR", ticker="A00001", name="새행", signal_type="BREAKOUT",
                      signal_date="2026-10-30", scan_time=started + timedelta(seconds=5)))
    db.add(ScanResult(market="KR", ticker="B00001", name="지난스캔", signal_type="BREAKOUT",
                      signal_date="2026-10-20", scan_time=started - timedelta(days=3)))
    db.commit()

    returns = {"A00001": ok(6.0), "A00002": ok(5.5), "A00003": ok(7.0), "B00001": ok(5.2)}
    _apply_kr_sector_trends(db, list(industries), returns, started)

    fresh, old = (db.query(ScanResult).filter(ScanResult.ticker == t).one() for t in ("A00001", "B00001"))
    assert (fresh.sector_trend, fresh.sector_trend_sample, fresh.sector_trend_members) == ("STRONG_UP", 4, 4)
    assert fresh.sector_return_20d == pytest.approx(5.75) and fresh.sector_source == "KRX"
    assert old.sector_trend is None


def test_apply_kr_sector_trends_is_a_noop_without_classification(monkeypatch):
    from scanner.scan_engine import _apply_kr_sector_trends

    db = fresh_db()
    _apply_kr_sector_trends(db, ["A00001"], {"A00001": ok(3.0)}, datetime.utcnow())   # 공급자 차단(None)


# ── API ─────────────────────────────────────────────────────────

def test_results_api_returns_sector_snapshot_and_rule(monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    import database.models as dbm
    import scheduler as sched
    import web.app as webapp

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Session = sessionmaker(bind=engine)
    dbm.Base.metadata.create_all(engine)
    monkeypatch.setattr(dbm, "init_db", lambda: None)
    monkeypatch.setattr(sched, "start_scheduler", lambda: None)
    monkeypatch.setattr(sched, "stop_scheduler", lambda: None)
    monkeypatch.setattr(webapp, "SITES_API_KEY", "test-key")
    today = datetime.utcnow().strftime("%Y-%m-%d")
    db = Session()
    db.add(dbm.ScanResult(market="KR", ticker="005930", name="삼성전자", signal_type="BREAKOUT",
                          signal_date=today, scan_time=datetime.utcnow(), price=1.0, ma150=1.0,
                          sector_name="통신 및 방송 장비 제조업", sector_source="KRX", sector_trend="DOWN",
                          sector_return_20d=-2.5, sector_trend_asof="2026-10-30",
                          sector_trend_basis=st.KR_BASIS, sector_trend_sample=8, sector_trend_members=10))
    db.add(dbm.ScanResult(market="US", ticker="OLD", name="Legacy", signal_type="BREAKOUT",
                          signal_date=today, scan_time=datetime.utcnow(), price=1.0, ma150=1.0))
    db.commit()
    db.close()

    def override():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    webapp.app.dependency_overrides[dbm.get_db] = override
    try:
        with TestClient(webapp.app, headers={"Authorization": "Bearer test-key"}) as client:
            rows = {r["ticker"]: r for r in client.get("/api/results?days=3&prioritize_13f=false").json()}
    finally:
        webapp.app.dependency_overrides.pop(dbm.get_db, None)

    kr = rows["005930"]
    assert (kr["sector_trend"], kr["sector_return_20d"], kr["sector_trend_sample"], kr["sector_trend_members"]) == \
        ("DOWN", -2.5, 8, 10)
    assert kr["sector_source"] == "KRX" and kr["sector_trend_basis"] == st.KR_BASIS
    assert "20거래일" in kr["sector_trend_rule"] and "±5%" in kr["sector_trend_rule"]
    legacy = rows["OLD"]
    assert legacy["sector_name"] is None and legacy["sector_trend"] is None and legacy["sector_source"] is None
