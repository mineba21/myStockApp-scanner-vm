"""STEP 1 — Scanner 신호 저장 구조 · 데이터 품질 감사.

운영 DB 를 직접 열지 않는다. Scanner MCP(읽기 전용) 덤프와 저장소 코드에서 확인한
저장 방식만 근거로 쓴다.
"""
from __future__ import annotations

import json
from typing import Dict, Optional

import pandas as pd

# 저장소 코드에서 확인한 사실 (근거 위치 포함)
CODE_FACTS = [
    {"topic": "DB", "fact": "운영 Neon(Postgres), 로컬/테스트 SQLite. 스키마는 Alembic 소유.",
     "evidence": "database/models.py, CLAUDE.md"},
    {"topic": "결과 테이블", "fact": "scan_results 단일 테이블. 신호별 1행 (ticker+signal_date+signal_type).",
     "evidence": "scanner/scan_engine.py::_save"},
    {"topic": "Overwrite", "fact": "같은 신호 재스캔 시 price·ma150·pivot·stop_loss·grade·quality·RS·entry 관측·sizing·strict 판정이 최신 값으로 덮어써지고 scan_time 이 갱신된다. 최초 값은 보존되지 않는다.",
     "evidence": "scanner/scan_engine.py::_save (existing 분기)"},
    {"topic": "최초 탐지 시각", "fact": "first_detected_at 은 신규 행 생성 시에만 기록 (마이그레이션 7b21d9c4e6a0 이전 행은 NULL).",
     "evidence": "alembic/versions/7b21d9c4e6a0_add_first_detected_at.py"},
    {"topic": "삭제", "fact": "DELETE /api/results/{id}, DELETE /api/results(일괄) 로 행 삭제 가능. 삭제 이력은 남지 않는다. 일괄 삭제 기본값은 strict 거부 행 보존.",
     "evidence": "web/app.py::delete_result, delete_results_bulk"},
    {"topic": "strict 거부 행", "fact": "STRICT_PERSIST_REJECTED 기본 true 로 거부 행도 저장되지만 Scanner MCP·조회 API 는 passed/미평가 행만 노출한다.",
     "evidence": "web/scanner_read_api.py::visible"},
    {"topic": "스캔 로그", "fact": "scan_logs 는 실행 단위 집계(총 스캔 수, 신호 수)만 보존. 신호 단위 이벤트 이력 없음.",
     "evidence": "database/models.py::ScanLog"},
    {"topic": "OHLCV 저장", "fact": "과거 OHLCV 를 DB 에 저장하지 않는다. 스캔마다 yfinance(US, auto_adjust) / FinanceDataReader(KR) 로 2년치를 새로 받는다.",
     "evidence": "scanner/us_stocks.py, scanner/kr_stocks.py"},
    {"topic": "기존 백테스트", "fact": "저장소에 매도 전략 백테스트 코드 없음 (check_sell_signal 은 보유 종목 경고용).",
     "evidence": "scanner/weinstein.py::check_sell_signal"},
    {"topic": "실제 체결", "fact": "transactions/holdings 테이블은 수동 거래일지. 키움 체결 근거 모듈은 주문 검증용이며 스캐너 신호와 연결된 체결 이력은 확인되지 않음.",
     "evidence": "database/models.py, trading/kiwoom_*execution_evidence.py"},
    {"topic": "Scanner 버전 이력", "fact": "행에 스캐너 버전 필드가 없다. 로직 변경 이력은 git 커밋으로만 추적 가능 → 과거 행이 어떤 버전으로 판정됐는지 행 단위로 알 수 없다.",
     "evidence": "git log, database/models.py"},
]


def audit_snapshot(snap: pd.DataFrame, ohlcv_tickers: Optional[Dict[str, set]] = None) -> Dict:
    s = snap.copy()
    out: Dict = {"source": "Scanner MCP scanner_signals (current rows; not an immutable event history)"}
    out["rows"] = int(len(s))
    out["first_signal_date"] = s["signal_date"].min()
    out["last_signal_date"] = s["signal_date"].max()
    out["unique_tickers"] = int(s[["market", "ticker"]].drop_duplicates().shape[0])
    out["unique_signal_keys"] = int(s["signal_key"].nunique())
    out["by_market"] = s["market"].value_counts().to_dict()
    out["by_signal_type"] = s.groupby(["market", "signal_type"]).size().rename("n").reset_index().to_dict("records")
    out["by_grade"] = s["grade"].fillna("NULL").value_counts().to_dict()
    out["by_quality"] = s["signal_quality"].fillna("NULL").value_counts().to_dict()
    out["by_strict"] = s["strict_assessment"].fillna("NULL").value_counts().to_dict()
    out["by_sector_stage"] = s["sector_stage"].fillna("NULL").value_counts().to_dict()
    out["entry_warning_rows"] = int(s["has_entry_warnings"].sum())
    out["min_result_id"] = int(s["result_id"].min())
    out["max_result_id"] = int(s["result_id"].max())
    out["id_gaps_not_visible"] = int(s["result_id"].max() - len(s))
    dup = s.groupby(["market", "ticker"]).size()
    out["tickers_with_multiple_signals"] = int((dup > 1).sum())
    out["same_ticker_same_signal_date_multi_type"] = int(
        s.groupby(["market", "ticker", "signal_date"]).size().gt(1).sum())
    out["first_detected_null"] = int(s["first_detected_at"].isna().sum())
    out["values_at_first_detection"] = int(s["values_at_first_detection"].sum())
    out["rows_updated_after_first_detection"] = int(
        (s["first_detected_at"].notna() & ~s["values_at_first_detection"]).sum())
    lag = (s["first_detected_at"].dt.normalize() - pd.to_datetime(s["signal_date"])).dt.days
    out["detect_lag_days"] = {"median": float(lag.median()), "max": float(lag.max()),
                              "min": float(lag.min())} if lag.notna().any() else None
    cols = ["price", "ma150", "pivot_price", "stop_loss", "grade", "signal_quality", "volume_ratio",
            "weekly_volume_ratio", "weekly_volume_ratio_4w", "rs_value", "rs_trend", "sector_stage",
            "upthrust_failed", "pivot_ext_pct", "cur_ext_pct", "cur_stop_pct", "suggested_qty",
            "first_detected_at"]
    out["missing_rate"] = {c: float(s[c].isna().mean()) for c in cols if c in s}
    out["pivot_missing_by_type"] = s.assign(m=s["pivot_price"].isna()).groupby("signal_type")["m"].mean().to_dict()
    if ohlcv_tickers is not None:
        ok = s.apply(lambda r: r["ticker"] in ohlcv_tickers.get(r["market"], set()), axis=1)
        out["ohlcv_match_rate"] = float(ok.mean())
        out["ohlcv_missing_tickers"] = sorted(s.loc[~ok, "ticker"].unique().tolist())
    out["quality_labels"] = {
        "VERIFIED_HISTORICAL": 0,
        "SNAPSHOT_ONLY": int(len(s)),
        "SNAPSHOT_ONLY_values_unmodified": int(s["values_at_first_detection"].sum()),
    }
    return out


def audit_reconstructed(rec: pd.DataFrame) -> Dict:
    if rec is None or rec.empty:
        return {"rows": 0}
    first = rec[rec["first_seen"]]
    passed = rec[rec["strict_filter_passed"]]
    out = {
        "rows_first_seen": int(len(first)),
        "rows_strict_passed": int(len(passed)),
        "first_signal_date": str(rec["signal_date"].min()),
        "last_signal_date": str(rec["signal_date"].max()),
        "by_market_type_all": first.groupby(["market", "signal_type"]).size().rename("n").reset_index().to_dict("records"),
        "by_market_type_strict": passed.groupby(["market", "signal_type"]).size().rename("n").reset_index().to_dict("records"),
        "by_grade_strict": passed["grade"].value_counts().to_dict(),
        "by_quality_strict": passed["signal_quality"].value_counts().to_dict(),
        "unique_tickers": int(first[["market", "ticker"]].drop_duplicates().shape[0]),
        "strict_pass_on_first_detection": int((first["strict_filter_passed"]).sum()),
        "strict_pass_later": int((~passed["first_seen"]).sum()),
        "detect_lag_days_median": float(rec["detect_lag_days"].median()),
        "top_reject_reasons": (first.loc[~first["strict_filter_passed"], "filter_reasons"]
                               .map(json.loads).explode().value_counts().head(12).to_dict()),
    }
    return out
