"""
`GET /api/results` strict-filter 노출/필터 동작 테스트 (Phase 4 P2 follow-up).

- 기본값(`include_rejected=False`): strict_filter_passed != False 행만 반환
  · NULL(legacy) 포함, True(strict-pass) 포함, False(거부) 제외
- `include_rejected=True`: 모든 행 반환
- 응답에 `strict_filter_passed`, `filter_reasons` 노출 (JSON 파싱)
- `DELETE /api/results` 도 같은 기본 필터 적용 (거부 행 보존)
"""
import json
import os
import sys
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── 인메모리 DB + 앱 부팅 부작용 차단 ─────────────────────────────────

@pytest.fixture(scope="function")
def client_with_db(monkeypatch):
    """ScanResult 테이블을 가진 인메모리 SQLite + TestClient.

    각 테스트마다 새 DB → 격리.
    """
    import database.models as dbm

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    dbm.Base.metadata.create_all(bind=engine)

    # 앱이 자체 init_db / scheduler 부팅하지 않도록 차단
    monkeypatch.setattr(dbm, "init_db", lambda: None)
    import scheduler as sched
    monkeypatch.setattr(sched, "start_scheduler", lambda: None)
    monkeypatch.setattr(sched, "stop_scheduler", lambda: None)
    monkeypatch.setattr(sched, "get_next_run_times", lambda: {})

    import web.app as webapp
    monkeypatch.setattr(webapp, "SITES_API_KEY", "test-key")
    app = webapp.app

    def _override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[dbm.get_db] = _override_get_db
    try:
        with TestClient(app, headers={"Authorization": "Bearer test-key"}) as c:
            yield c, TestSession
    finally:
        app.dependency_overrides.pop(dbm.get_db, None)


def _insert_result(session_factory, *, ticker, strict_filter_passed, filter_reasons=None,
                   market="US", signal_type="BREAKOUT", days_ago=0, sector_name=None,
                   sector_stage=None, grade=None, signal_quality=None,
                   rs_value=None, rs_trend=None, pivot_price=None, stop_loss=None):
    """ScanResult 한 행 삽입 후 id 반환."""
    from database.models import ScanResult

    db = session_factory()
    try:
        signal_date = (datetime.utcnow() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        row = ScanResult(
            scan_time=datetime.utcnow() - timedelta(days=days_ago),
            market=market,
            ticker=ticker,
            name=f"{ticker} Inc.",
            signal_type=signal_type,
            stage="STAGE2",
            price=100.0,
            ma150=90.0,
            volume_ratio=2.5,
            signal_date=signal_date,
            strict_filter_passed=strict_filter_passed,
            filter_reasons=(json.dumps(filter_reasons)
                            if filter_reasons is not None else None),
            sector_name=sector_name,
            sector_stage=sector_stage,
            grade=grade,
            signal_quality=signal_quality,
            rs_value=rs_value,
            rs_trend=rs_trend,
            pivot_price=pivot_price,
            stop_loss=stop_loss,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id
    finally:
        db.close()


# ══════════════════════════════════════════════════════════════════
# include_rejected 기본값 동작
# ══════════════════════════════════════════════════════════════════

class TestResultsRejectedFilter:

    def test_default_excludes_rejected_signals(self, client_with_db):
        """STRICT_PERSIST_REJECTED=True 로 저장된 거부 행은 기본 응답에서 제외."""
        client, session = client_with_db
        _insert_result(session, ticker="PASS", strict_filter_passed=True)
        _insert_result(session, ticker="REJECT",
                       strict_filter_passed=False,
                       filter_reasons=["rs_below_zero", "stop_loss_missing"])
        _insert_result(session, ticker="LEGACY", strict_filter_passed=None)

        r = client.get("/api/results")
        assert r.status_code == 200
        tickers = {row["ticker"] for row in r.json()}
        assert "PASS" in tickers
        assert "LEGACY" in tickers
        assert "REJECT" not in tickers, \
            "거부 행이 기본 응답에 노출되면 일반 매수 후보처럼 사용됨 (P2)"

    def test_include_rejected_returns_all(self, client_with_db):
        """include_rejected=true → 거부 행도 함께 반환 (QA/백테스팅용)."""
        client, session = client_with_db
        _insert_result(session, ticker="PASS", strict_filter_passed=True)
        _insert_result(session, ticker="REJECT",
                       strict_filter_passed=False,
                       filter_reasons=["rs_falling"])

        r = client.get("/api/results?include_rejected=true")
        assert r.status_code == 200
        tickers = {row["ticker"] for row in r.json()}
        assert tickers == {"PASS", "REJECT"}

    def test_response_exposes_strict_fields(self, client_with_db):
        """API 응답이 strict_filter_passed / filter_reasons 를 명시적으로 노출."""
        client, session = client_with_db
        _insert_result(session, ticker="PASS", strict_filter_passed=True,
                       filter_reasons=[])
        _insert_result(session, ticker="LEGACY", strict_filter_passed=None)

        r = client.get("/api/results")
        assert r.status_code == 200
        rows = {row["ticker"]: row for row in r.json()}

        # 두 행 모두 필드 노출
        assert rows["PASS"]["strict_filter_passed"] is True
        assert rows["PASS"]["filter_reasons"] == []
        assert rows["LEGACY"]["strict_filter_passed"] is None
        assert rows["LEGACY"]["filter_reasons"] == []  # NULL → 빈 리스트

    def test_response_exposes_sector_name(self, client_with_db):
        """API 응답이 sector_name 을 명시적으로 노출 (NULL 도 키로 존재).

        UI sector 배지가 자연스럽게 fallback 하려면 키 자체는 항상 응답에 있어야
        한다. 후속 sector 매핑 plan 머지 시 값이 채워지면 UI 가 자동 활성.
        """
        client, session = client_with_db
        _insert_result(session, ticker="WITHOUT_SECTOR", strict_filter_passed=True)
        _insert_result(session, ticker="WITH_SECTOR", strict_filter_passed=True,
                       sector_name="Technology")

        r = client.get("/api/results")
        assert r.status_code == 200
        rows = {row["ticker"]: row for row in r.json()}

        assert "sector_name" in rows["WITHOUT_SECTOR"]
        assert rows["WITHOUT_SECTOR"]["sector_name"] is None
        assert rows["WITH_SECTOR"]["sector_name"] == "Technology"

    def test_response_exposes_strict_meta_fields(self, client_with_db):
        """API 응답이 sector_stage / grade / signal_quality / rs_value / rs_trend /
        pivot_price / stop_loss 도 노출 (UI 카드 보강용).

        값이 채워진 경우 그대로, 비어 있는 경우(legacy 또는 sector 매핑 미배포)
        에도 키 자체는 응답에 항상 존재해야 한다 — UI 가 graceful fallback 가능.
        """
        client, session = client_with_db
        _insert_result(session, ticker="RICH", strict_filter_passed=True,
                       sector_stage="STAGE2", grade="A", signal_quality="STRONG",
                       rs_value=1.42, rs_trend="RISING",
                       pivot_price=99.5, stop_loss=88.0)
        _insert_result(session, ticker="BARE", strict_filter_passed=None)

        r = client.get("/api/results")
        assert r.status_code == 200
        rows = {row["ticker"]: row for row in r.json()}

        rich = rows["RICH"]
        assert rich["sector_stage"] == "STAGE2"
        assert rich["grade"] == "A"
        assert rich["signal_quality"] == "STRONG"
        assert rich["rs_value"] == 1.42
        assert rich["rs_trend"] == "RISING"
        assert rich["pivot_price"] == 99.5
        assert rich["stop_loss"] == 88.0

        bare = rows["BARE"]
        # 키는 항상 존재 — graceful fallback 위해
        for key in ("sector_stage", "grade", "signal_quality",
                    "rs_value", "rs_trend", "pivot_price", "stop_loss"):
            assert key in bare, f"{key} 키가 응답에 없으면 UI graceful fallback 깨짐"
            assert bare[key] is None

    def test_filter_reasons_parsed_as_json_list(self, client_with_db):
        """filter_reasons 는 DB에 JSON 문자열로 저장되어도 API 는 리스트로 반환."""
        client, session = client_with_db
        reasons = ["rs_below_zero", "breakout_daily_volume", "stop_loss_missing"]
        _insert_result(session, ticker="REJ",
                       strict_filter_passed=False,
                       filter_reasons=reasons)

        r = client.get("/api/results?include_rejected=true")
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) == 1
        assert rows[0]["filter_reasons"] == reasons
        assert isinstance(rows[0]["filter_reasons"], list)

    def test_invalid_filter_reasons_falls_back_to_empty_list(self, client_with_db):
        """저장된 JSON 이 손상돼도 API 는 빈 리스트로 graceful degrade."""
        client, session = client_with_db
        from database.models import ScanResult

        db = session()
        try:
            row = ScanResult(
                scan_time=datetime.utcnow(),
                market="US", ticker="BAD", name="Bad Inc.",
                signal_type="BREAKOUT", stage="STAGE2",
                price=100.0, ma150=90.0, volume_ratio=2.5,
                signal_date=datetime.utcnow().strftime("%Y-%m-%d"),
                strict_filter_passed=False,
                filter_reasons="not-valid-json",
            )
            db.add(row)
            db.commit()
        finally:
            db.close()

        r = client.get("/api/results?include_rejected=true")
        assert r.status_code == 200
        rows = r.json()
        assert rows[0]["filter_reasons"] == []

    def test_market_and_signal_type_filters_still_apply_with_strict_filter(
            self, client_with_db):
        """기존 market/signal_type 필터와 strict 필터 동시 적용."""
        client, session = client_with_db
        _insert_result(session, ticker="USPASS", market="US",
                       strict_filter_passed=True)
        _insert_result(session, ticker="KRPASS", market="KR",
                       strict_filter_passed=True)
        _insert_result(session, ticker="USREJ", market="US",
                       strict_filter_passed=False,
                       filter_reasons=["rs_below_zero"])

        r = client.get("/api/results?market=US")
        tickers = {row["ticker"] for row in r.json()}
        assert tickers == {"USPASS"}, \
            "US + 기본(거부 제외) → USPASS 만"


# ══════════════════════════════════════════════════════════════════
# DELETE /api/results 대칭
# ══════════════════════════════════════════════════════════════════

class TestResultsBulkDelete:

    def test_default_delete_preserves_rejected(self, client_with_db):
        """기본 bulk delete 는 거부 행을 보존해야 QA 데이터가 안전."""
        client, session = client_with_db
        _insert_result(session, ticker="PASS", strict_filter_passed=True)
        _insert_result(session, ticker="REJ",
                       strict_filter_passed=False,
                       filter_reasons=["rs_below_zero"])

        r = client.delete("/api/results")
        assert r.status_code == 200
        assert r.json()["count"] == 1  # PASS 만 삭제

        # 거부 행이 살아있는지 확인 (include_rejected=true 로 검증)
        r2 = client.get("/api/results?include_rejected=true")
        tickers = {row["ticker"] for row in r2.json()}
        assert tickers == {"REJ"}

    def test_include_rejected_delete_removes_all(self, client_with_db):
        """include_rejected=true 시 거부 행도 함께 삭제."""
        client, session = client_with_db
        _insert_result(session, ticker="PASS", strict_filter_passed=True)
        _insert_result(session, ticker="REJ",
                       strict_filter_passed=False,
                       filter_reasons=["rs_below_zero"])

        r = client.delete("/api/results?include_rejected=true")
        assert r.status_code == 200
        assert r.json()["count"] == 2

        r2 = client.get("/api/results?include_rejected=true")
        assert r2.json() == []
