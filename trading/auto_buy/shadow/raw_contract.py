"""스냅샷 원본(``row_json``) ↔ 정규화 필드 검증 계약 (Codex 재리뷰 head d95beab P2-3).

``row_sha256`` 은 ``row_json`` 만 보호한다. 엔진은 ``row_json`` 이 아니라 정규화 컬럼(판단가·손절가·피벗·신호 유형 등)을
읽으므로, 정규화 컬럼만 바뀌면(``row_json``·``row_sha256``·건수 유지) 해시·건수 대조로는 드러나지 않는다. ab0007 backfill
봉인(SSV2)은 이관 당시 저장된 정규화 값을 그대로 봉인하므로 그 이전 오염도 정상으로 봉인될 수 있었다.

이 모듈은 **원본에서 재현 가능한 필드**를 실제 수집 코드(``snapshot._capture``, P7-OPS 하네스
``ops_parity.write_scan_capture``)와 같은 규칙으로 다시 계산해 저장된 정규화 값과 정확히 대조한다. 같은 Python 연산을
다시 하므로 정상 정규화(문자열 → 실수, NaN·무한대 → NULL, 비율 파생)는 오탐하지 않는다.

원본 형식 (``capture_source`` 로 고른다. 알 수 없는 출처·해석 불가 원본은 오류 — 우회 경로 없음)
- ``SCANNER_ROW``  (HOOK·CATCHUP): 운영 ``ScanResult`` 행 전체 컬럼 (``snapshot._row_dict``).
- ``P7_RECONSTRUCTED`` (BACKTEST_REPLAY, 원본 ``source=P7_RECONSTRUCTED_CANDIDATE``): P7-OPS 재구성 후보 JSON.
  같은 엄격도로 자기 원본의 모든 판단 필드를 대조한다 (백테스트 전용 완화 없음).

원본 근거가 부족한 파생 필드 (이 계약으로 정확성을 보증하지 않는다 — ``DERIVED_UNVERIFIED``)
- 최초 탐지·최초 Strict 통과 판정(``is_first_detection``·``first_strict_pass``·``first_pass_basis``): 수집 이력·캘린더
  설정에 의존 (SCANNER_ROW). P7 형식은 ``is_first_detection`` 이 원본에 있어 대조한다.
- 수집 소속(``trading_day``·``intraday``·``scan_status``·``scan_completed_at``·``late_after_decision``): 수집 기록과의
  관계로 따로 검증 (DB 관계 트리거·``integrity.scan_set_issues``).
- 수집 메타데이터(``scanner_commit``·``threshold_hash``·``capture_logic_hash``·``capture_source``·``created_at``).
불일치 시: 자동 수정·해시 재생성·재봉인을 하지 않는다. 호출 측이 사용·Migration 을 중단하고 원인을 기록한다.
"""
from __future__ import annotations

import json
import math
from typing import Optional

SCANNER_SOURCES = ("HOOK", "CATCHUP")
P7_SOURCES = ("BACKTEST_REPLAY",)
P7_RAW_SOURCE = "P7_RECONSTRUCTED_CANDIDATE"

# 원본에서 재현해 대조하는 정규화 필드 (형식별). 테스트와 문서가 이 목록을 계약으로 쓴다.
VERIFIED_FIELDS = {
    "SCANNER_ROW": ("scan_result_id", "signal_key", "market", "ticker", "name", "signal_type", "signal_date",
                    "first_detected_at", "strict_passed", "strict_reasons", "grade", "signal_quality",
                    "market_condition", "decision_price", "pivot_price", "stop_loss", "ext_pct", "stop_pct",
                    "entry_warnings", "rs_value", "volume_ratio", "weekly_volume_ratio", "sector_name", "sector_source"),
    "P7_RECONSTRUCTED": ("scan_result_id", "signal_key", "market", "ticker", "name", "signal_type", "signal_date",
                         "is_first_detection", "strict_passed", "grade", "signal_quality", "market_condition",
                         "decision_price", "pivot_price", "stop_loss", "ext_pct", "stop_pct", "entry_warnings",
                         "sector_name"),
}
DERIVED_UNVERIFIED = ("is_first_detection (SCANNER_ROW)", "first_strict_pass", "first_pass_basis", "trading_day",
                      "intraday", "scan_status", "scan_completed_at", "late_after_decision", "scanner_commit",
                      "threshold_hash", "capture_logic_hash", "capture_source", "created_at")


def _num(v) -> Optional[float]:
    """``snapshot._num`` 과 같은 정규화 (실수 변환, NaN·무한대·변환 불가 → None)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _parse_list(raw):
    """``snapshot._parse_list`` 와 같은 규칙."""
    if raw is None:
        return None
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return [str(raw)]
    return v if isinstance(v, list) else [str(v)]


def _derived(price, pivot, stop) -> tuple:
    """``snapshot._capture`` 와 같은 파생식 (판단가 대비 피벗 이격·손절폭, %)."""
    ext = (price / pivot - 1) * 100 if price and pivot and pivot > 0 else None
    stop_pct = (1 - stop / price) * 100 if price and price > 0 and stop and stop > 0 else None
    return ext, stop_pct


def _scanner_expected(raw: dict) -> dict:
    g = raw.get
    price, pivot, stop = _num(g("price")), _num(g("pivot_price")), _num(g("stop_loss"))
    ext, stop_pct = _derived(price, pivot, stop)
    sp = g("strict_filter_passed")
    return {"scan_result_id": g("id"),
            "signal_key": f"{g('market')}:{g('ticker')}:{g('signal_type')}:{g('signal_date')}",
            "market": g("market"), "ticker": g("ticker"), "name": g("name"), "signal_type": g("signal_type"),
            "signal_date": g("signal_date"), "first_detected_at": g("first_detected_at"),
            "strict_passed": None if sp is None else int(bool(sp)), "strict_reasons": g("filter_reasons"),
            "grade": g("grade"), "signal_quality": g("signal_quality"), "market_condition": g("market_condition"),
            "decision_price": price, "pivot_price": pivot, "stop_loss": stop, "ext_pct": ext, "stop_pct": stop_pct,
            "entry_warnings": json.dumps(_parse_list(g("entry_warnings")), ensure_ascii=False),
            "rs_value": _num(g("rs_value")), "volume_ratio": _num(g("volume_ratio")),
            "weekly_volume_ratio": _num(g("weekly_volume_ratio")), "sector_name": g("sector_name"),
            "sector_source": g("sector_source")}


def _p7_expected(raw: dict) -> dict:
    g = raw.get
    cid = g("cid")
    sfp = g("strict_filter_passed")
    return {"scan_result_id": int(cid) if isinstance(cid, (int, float)) and not isinstance(cid, bool) else cid,
            "signal_key": g("signal_key"), "market": "US", "ticker": g("ticker"), "name": None,
            "signal_type": g("signal_type"), "signal_date": g("signal_date"),
            "is_first_detection": int(bool(g("first_seen"))) if "first_seen" in raw else None,
            "strict_passed": None if sfp is None else int(bool(sfp)), "grade": g("grade"),
            "signal_quality": g("signal_quality"), "market_condition": g("market_condition"),
            "decision_price": g("close"), "pivot_price": None, "stop_loss": g("stop"), "ext_pct": g("ext_pct"),
            "stop_pct": g("stop_pct"), "entry_warnings": json.dumps(list(g("warnings") or []), ensure_ascii=False),
            "sector_name": g("sector")}


def _same(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b and type(a) is type(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)          # 같은 연산을 다시 했으므로 정확히 같아야 한다 (허용 오차 없음)
    return type(a) is type(b) and a == b


def row_issues(row) -> list:
    """스냅샷 1행: 원본 형식 판별 → 재현 가능한 필드 대조. 반환: 불일치 필드 이름 목록 (형식 오류는 ``RAW_*``)."""
    src = row["capture_source"]
    try:
        raw = json.loads(row["row_json"]) if isinstance(row["row_json"], str) else None
    except ValueError:
        raw = None
    if not isinstance(raw, dict):
        return ["RAW_UNPARSEABLE"]
    if src in SCANNER_SOURCES and "source" not in raw:
        profile, want = "SCANNER_ROW", _scanner_expected(raw)
    elif src in P7_SOURCES and raw.get("source") == P7_RAW_SOURCE:
        profile, want = "P7_RECONSTRUCTED", _p7_expected(raw)
    else:
        return [f"RAW_PROFILE_UNKNOWN:{src}"]
    keys = row.keys()
    return [f for f in VERIFIED_FIELDS[profile] if f in keys and not _same(want[f], row[f])]


def scan_issues(db, scan_log_id) -> dict:
    """결정 스캔 1회의 구성원별 불일치 {snapshot_id: [필드]}."""
    out = {}
    for r in db.execute("SELECT * FROM ab_signal_snapshots WHERE scan_log_id=? ORDER BY id", (scan_log_id,)):
        bad = row_issues(r)
        if bad:
            out[r["id"]] = bad
    return out


def journal_issues(db, limit: int = 20) -> list:
    """저널 전체 (모든 수집, 결정 여부 무관). Migration 사전 검사·전체 점검용. 문제 설명 목록 (최대 ``limit``건 + 합계)."""
    prev = db.row_factory
    import sqlite3
    db.row_factory = sqlite3.Row
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ab_signal_snapshots'").fetchone():
            return []
        bad, n = [], 0
        for r in db.execute("SELECT * FROM ab_signal_snapshots ORDER BY id"):
            fields = row_issues(r)
            if fields:
                n += 1
                if len(bad) < limit:
                    bad.append(f"ab_signal_snapshots #{r['id']} (scan #{r['scan_log_id']}): 원본 row_json 과 "
                               f"정규화 필드 불일치 {fields[:8]}")
        if n > len(bad):
            bad.append(f"... 원본 불일치 스냅샷 합계 {n}행")
        return bad
    finally:
        db.row_factory = prev
