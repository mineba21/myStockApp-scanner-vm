"""자동매수 SHADOW 저널 스키마 v7 (리비전 ab0007): **신호 집합·관계 단위 보호**와 집합 무결성 근거(봉인).

ab0001~ab0006 은 공개됐으므로 바꾸지 않는다. ab0006 의 표별 계약은 **개별 행** 불변성이었다. 행 하나하나는 바꿀 수 없어도
확정된 결정 스캔에 **새 스냅샷 행을 추가**하면 동결된 신호 집합이 바뀌었다 (Codex 재리뷰 head a183b99 P1). 이 리비전은
집합 구성원·참조 관계·수집 건수·해시 정합성을 DB 와 엔진 양쪽에서 보호한다.

1. **수집 봉인** (``ab_capture_seals``, 완전 불변 입력). 스캔 1회 수집(스냅샷 INSERT → 수집 건수 기록)과 같은 트랜잭션
   끝에 봉인 1행을 남긴다: 구성원 수·Strict 수·구성원 다이제스트(``SSV2``). 봉인된 스캔에는 스냅샷을 더할 수 없다.
   봉인 INSERT 는 수집 건수·실제 스냅샷 수가 모두 같을 때만 허용된다.
2. **동결 집합.** ``ab_day_inputs`` 에서 FROZEN 으로 참조된 결정 스캔에는 스냅샷 INSERT 를 거부한다 (일반·OR IGNORE·
   REPLACE·UPSERT 모두 — ``RAISE(ABORT)`` 는 충돌 해소 방식보다 우선한다). FROZEN 입력 INSERT 는 결정 스캔이 같은 거래일·
   정규·DONE·완전 수집(FULL)·장 마감 후·확정 전 스캔이고, 봉인돼 있고, 봉인·수집 건수·실제 스냅샷 수가 모두 같으며,
   구성원이 모두 그 스캔·거래일 소속일 때만 허용한다. 정상 빈 스캔(0건)도 유효한 동결 집합이다.
   정규 결정 이후의 **다른 scan_log_id** 재스캔은 자기 수집·봉인으로 정상 저장된다 (LATE_SCAN_AFTER_DECISION 유지).
3. **참조 관계.** 스냅샷은 자기 수집 기록이 있어야 하고 거래일·장중 여부·스캔 상태가 수집 기록과 같아야 한다.
   적격 판정은 존재하는 스냅샷에만 기록된다. (기본 sqlite3 연결은 foreign_keys 가 꺼져 있어 FK 만으로는 막히지 않는다.)
4. **수집 건수.** 봉인 후에는 수집 건수를 바꿀 수 없다 (ab0006 은 (0,0) 에서 1회 허용이었다).

집합 무결성 근거 ``SSV2`` (기존 동결 해시 ``signal_set_hash`` 는 그대로 두고 덮어쓰지 않는다):
- 기존 해시(v1)는 구성원 ``row_sha256``(원본 ``row_json`` 의 SHA-256)의 정렬 목록 해시다. 원본 JSON 은 보호하지만,
  엔진이 실제로 쓰는 정규화·파생 필드(판단가·손절가·최초 Strict 통과·최초 탐지·추격 경고·섹터 등)와 스냅샷 식별자는
  포함하지 않는다. 저장된 해시 문자열만 대조하면 이 필드의 불일치를 놓칠 수 있다.
- ``SSV2`` 는 구성원 전체 컬럼(``row_json`` 은 ``row_sha256`` 으로 대신)과 id 를 고정 순서로 직렬화한 SHA-256 이다.
  엔진 검증(``integrity.py``)은 v1 해시·SSV2·구성원별 ``sha256(row_json) = row_sha256``·건수·소속·적격 판정 재계산을 모두
  대조한다. 규칙은 이 파일에 고정한다 (바꾸려면 새 버전 ``SSV3``).

Migration 사전 검사(``precheck``): 기존 데이터가 맞지 않으면 중단하고 원본은 바꾸지 않는다 (자동 수정·재계산 없음).
보호 범위 밖 (기존과 같음): DB 관리자·파일 소유자의 임의 DDL 과 파일 직접 변경 — 엔진 검증이 결과 사용을 막는다.
"""
from __future__ import annotations

import hashlib
import json

from trading.auto_buy.shadow.journal_migrations import ddl_v6
from trading.auto_buy.shadow.journal_migrations.ddl_v6 import FROZEN_INPUT, _t

BASIS_VERSION = "SSV2"
REGULAR_SCAN_TRIGGER = "scheduler"          # settings.REGULAR_SCAN_TRIGGER 와 같은 값 (리비전에 고정)

# SSV2 직렬화 컬럼 (ab0006 기준 ab_signal_snapshots 의 모든 컬럼, row_json 은 row_sha256 으로 대신). 순서 고정.
SSV2_COLUMNS = (
    "id", "scan_log_id", "scan_result_id", "signal_key", "market", "ticker", "name", "signal_type", "signal_date",
    "trading_day", "intraday", "scan_status", "scan_completed_at", "late_after_decision", "first_detected_at",
    "is_first_detection", "strict_passed", "strict_reasons", "first_strict_pass", "first_pass_basis", "grade",
    "signal_quality", "market_condition", "decision_price", "pivot_price", "stop_loss", "ext_pct", "stop_pct",
    "entry_warnings", "rs_value", "volume_ratio", "weekly_volume_ratio", "sector_name", "sector_source",
    "scanner_commit", "threshold_hash", "capture_logic_hash", "row_sha256", "capture_source", "created_at")


def signal_set_hash_v1(db, scan_log_id) -> str:
    """기존 동결 해시 규칙 (snapshot.capture_scan_log 와 같음): 구성원 row_sha256 정렬 목록의 SHA-256 앞 16자."""
    return hashlib.sha256(json.dumps(sorted(
        r[0] for r in db.execute("SELECT row_sha256 FROM ab_signal_snapshots WHERE scan_log_id=?",
                                 (scan_log_id,)))).encode()).hexdigest()[:16]


def member_digest(db, scan_log_id) -> tuple:
    """SSV2: (구성원 수, Strict 수, 다이제스트)."""
    rows = [list(r) for r in db.execute(f"SELECT {', '.join(SSV2_COLUMNS)} FROM ab_signal_snapshots "
                                        f"WHERE scan_log_id=? ORDER BY id", (scan_log_id,))]
    strict = sum(1 for r in rows if r[SSV2_COLUMNS.index("strict_passed")] == 1)
    blob = json.dumps({"basis": BASIS_VERSION, "columns": SSV2_COLUMNS, "rows": rows},
                      ensure_ascii=False, separators=(",", ":"), allow_nan=True)
    return len(rows), strict, hashlib.sha256(blob.encode()).hexdigest()


def row_sha_ok(row_json, row_sha256) -> bool:
    return isinstance(row_json, str) and hashlib.sha256(row_json.encode()).hexdigest() == row_sha256


# ── 계약 (ab0006 계약 + 봉인 표 + 집합·관계 단위 보호) ────────────────────────────────────

CONTRACT = dict(ddl_v6.CONTRACT)
CONTRACT["ab_capture_seals"] = _t(
    FROZEN_INPUT, "정수 PK scan_log_id (= rowid)", [(("scan_log_id",), None)],
    "scan_log_id basis_version member_count strict_count member_digest source sealed_at",
    insert="수집 트랜잭션 끝에 1회. 수집 기록이 있고 수집 건수 = 실제 스냅샷 수 = 봉인 건수일 때만 (ab0007)",
    conflict=ddl_v6._KEY_CONFLICT)
COVERED_ELSEWHERE = ddl_v6.COVERED_ELSEWHERE
PROTECTED = tuple(t for t, c in CONTRACT.items() if c["category"] != ddl_v6.RUNTIME)

# 집합·관계 단위 계약 (개별 행 계약 위에 더한다). 테스트가 각 항목의 트리거·검증 경로를 확인한다.
SET_CONTRACT = {
    "ab_signal_snapshots": {
        "set": "scan_log_id 별 구성원 집합. 수집 봉인 후·FROZEN 결정 스캔은 구성원 추가 불가 (삭제·변경은 ab0004·ab0006)",
        "relation": "수집 기록(ab_scan_captures) 필수, trading_day·intraday·scan_status 가 수집 기록과 같음",
        "freeze": "결정 입력 동결 전: 그 스캔의 수집 트랜잭션 안에서만 추가. 동결 후: 추가·변경·삭제 불가"},
    "ab_scan_captures": {
        "set": "rows_captured·strict_rows = 실제 구성원 수·Strict 수. 봉인 후 변경 불가",
        "relation": "봉인(ab_capture_seals) 1:1",
        "freeze": "FROZEN 결정 스캔은 같은 거래일·정규·DONE·complete=1·FULL·장 마감 후·확정 전"},
    "ab_capture_seals": {
        "set": "구성원 수·Strict 수·SSV2 다이제스트 (수집 시점 근거)",
        "relation": "수집 기록 1:1, 건수 일치",
        "freeze": "FROZEN 입력 INSERT 전에 존재해야 함"},
    "ab_day_inputs": {
        "set": "FROZEN = 봉인된 결정 스캔 집합 + 기존 v1 해시 (덮어쓰지 않음)",
        "relation": "decision_scan_log_id → 봉인된 수집 기록 (거래일 일치)",
        "freeze": "동결 INSERT 시 수집·봉인·건수·소속을 DB 가 확인. 해시·SSV2·적격 재계산은 엔진 공통 검증"},
    "ab_eligibility": {
        "set": "결정 스캔 구성원별 판정. 엔진 검증이 저장 판정 = 재계산 판정 대조",
        "relation": "존재하는 스냅샷에만",
        "freeze": "추가 전용 (같은 판단만 멱등, ab0006)"},
    "ab_runtime": {
        "set": "-",
        "relation": "-",
        "freeze": "yahoo_rate_limit: 이전 상태 조회·증가/초기화·경고·이벤트·갱신을 한 쓰기 트랜잭션(BEGIN IMMEDIATE)에서"},
}

_SET = "SELECT COUNT(*) FROM ab_signal_snapshots s WHERE s.scan_log_id = {sid}"


def _rowid_triggers(t):
    """ab0006 과 같은 숨은 rowid·허용 목록 보호를 새 표에 적용한다 (이 표는 변경 허용 컬럼 없음)."""
    cols = CONTRACT[t]["columns"]
    changed = " OR ".join([f"NEW.{x} IS NOT OLD.{x}" for x in cols] + ["NEW.rowid IS NOT OLD.rowid"])
    ident = "SELECT RAISE(ABORT, 'ledger immutable: row identity (rowid) conflict or change not allowed')"
    return [
        f"""CREATE TRIGGER {t}_v7_rowid_insert BEFORE INSERT ON {t}
        WHEN EXISTS (SELECT 1 FROM {t} x WHERE x.rowid = NEW.rowid)
        BEGIN {ident}; END""",
        f"""CREATE TRIGGER {t}_v7_rowid_positive AFTER INSERT ON {t}
        WHEN NEW.rowid <= 0
        BEGIN {ident}; END""",
        f"""CREATE TRIGGER {t}_v7_allowlist BEFORE UPDATE ON {t}
        WHEN {changed}
        BEGIN SELECT RAISE(ABORT, 'contract immutable: column is not in the update allow-list'); END""",
        f"""CREATE TRIGGER {t}_v7_no_delete BEFORE DELETE ON {t}
        BEGIN SELECT RAISE(ABORT, 'ledger immutable: seal rows cannot be deleted'); END""",
    ]


def _ddl():
    out = ["""CREATE TABLE ab_capture_seals (
        scan_log_id INTEGER PRIMARY KEY REFERENCES ab_scan_captures(scan_log_id),
        basis_version TEXT NOT NULL CHECK(basis_version = 'SSV2'),
        member_count INTEGER NOT NULL CHECK(member_count >= 0),
        strict_count INTEGER NOT NULL CHECK(strict_count >= 0),
        member_digest TEXT NOT NULL CHECK(length(member_digest) = 64),
        source TEXT NOT NULL CHECK(source IN ('CAPTURE','AB0007_BACKFILL')),
        sealed_at REAL NOT NULL)"""]
    sid_new = "NEW.scan_log_id"
    # 봉인 INSERT: 수집 기록 있음, 수집 건수 = 실제 = 봉인 건수
    out.append(f"""CREATE TRIGGER ab_capture_seals_v7_insert BEFORE INSERT ON ab_capture_seals
        WHEN NOT COALESCE(EXISTS (SELECT 1 FROM ab_scan_captures c WHERE c.scan_log_id = {sid_new}
                  AND c.rows_captured = NEW.member_count AND c.strict_rows = NEW.strict_count
                  AND NEW.member_count = ({_SET.format(sid=sid_new)})
                  AND NEW.strict_count = ({_SET.format(sid=sid_new)} AND s.strict_passed = 1)), 0)
        BEGIN SELECT RAISE(ABORT, 'contract set: seal counts must match the capture and its snapshots'); END""")
    # 봉인 표 행 보호 (숨은 rowid·허용 목록·삭제) — 위 집합 트리거보다 나중에 만들어 먼저 동작한다
    out += _rowid_triggers("ab_capture_seals")
    # 스냅샷: 관계(수집 기록 필수·소속 일치), 봉인 후 추가 불가, FROZEN 결정 스캔 추가 불가
    out.append(f"""CREATE TRIGGER ab_signal_snapshots_v7_relation BEFORE INSERT ON ab_signal_snapshots
        WHEN NOT COALESCE(EXISTS (SELECT 1 FROM ab_scan_captures c WHERE c.scan_log_id = {sid_new}
                  AND c.trading_day IS NEW.trading_day AND c.intraday IS NEW.intraday
                  AND c.scan_status IS NEW.scan_status), 0)
        BEGIN SELECT RAISE(ABORT, 'contract relation: snapshot must belong to its capture (day, intraday, status)'); END""")
    out.append(f"""CREATE TRIGGER ab_signal_snapshots_v7_sealed BEFORE INSERT ON ab_signal_snapshots
        WHEN EXISTS (SELECT 1 FROM ab_capture_seals g WHERE g.scan_log_id = {sid_new})
        BEGIN SELECT RAISE(ABORT, 'contract set: capture is sealed, snapshot cannot be added'); END""")
    out.append(f"""CREATE TRIGGER ab_signal_snapshots_v7_frozen BEFORE INSERT ON ab_signal_snapshots
        WHEN EXISTS (SELECT 1 FROM ab_day_inputs d WHERE d.decision_status = 'FROZEN'
                     AND d.decision_scan_log_id = {sid_new})
        BEGIN SELECT RAISE(ABORT, 'contract set: frozen decision signal set, snapshot cannot be added'); END""")
    # 수집 건수: 봉인 후 변경 불가
    out.append("""CREATE TRIGGER ab_scan_captures_v7_counts_sealed BEFORE UPDATE ON ab_scan_captures
        WHEN (NEW.rows_captured IS NOT OLD.rows_captured OR NEW.strict_rows IS NOT OLD.strict_rows)
         AND EXISTS (SELECT 1 FROM ab_capture_seals g WHERE g.scan_log_id = OLD.scan_log_id)
        BEGIN SELECT RAISE(ABORT, 'contract set: capture counts are sealed'); END""")
    # 적격 판정: 존재하는 스냅샷에만
    out.append("""CREATE TRIGGER ab_eligibility_v7_relation BEFORE INSERT ON ab_eligibility
        WHEN NOT EXISTS (SELECT 1 FROM ab_signal_snapshots s WHERE s.id = NEW.snapshot_id)
        BEGIN SELECT RAISE(ABORT, 'contract relation: eligibility requires an existing snapshot'); END""")
    # FROZEN 입력: 봉인된 완전 정규 결정 스캔, 건수·소속 일치
    sid = "NEW.decision_scan_log_id"
    out.append(f"""CREATE TRIGGER ab_day_inputs_v7_frozen_set BEFORE INSERT ON ab_day_inputs
        WHEN NEW.decision_status IS 'FROZEN' AND NOT COALESCE(
               length(NEW.signal_set_hash) = 16
               AND EXISTS (SELECT 1 FROM ab_scan_captures c JOIN ab_capture_seals g ON g.scan_log_id = c.scan_log_id
                   WHERE c.scan_log_id = {sid} AND c.trading_day IS NEW.day AND c.scan_status = 'DONE'
                     AND c.complete = 1 AND c.recoverability = 'FULL' AND c.intraday = 0
                     AND c.late_after_decision = 0 AND c.triggered_by IS '{REGULAR_SCAN_TRIGGER}'
                     AND c.rows_captured = g.member_count AND c.strict_rows = g.strict_count
                     AND g.member_count = ({_SET.format(sid=sid)})
                     AND g.strict_count = ({_SET.format(sid=sid)} AND s.strict_passed = 1))
               AND NOT EXISTS (SELECT 1 FROM ab_signal_snapshots s WHERE s.scan_log_id = {sid}
                   AND (s.trading_day IS NOT NEW.day OR s.intraday != 0 OR s.scan_status != 'DONE')), 0)
        BEGIN SELECT RAISE(ABORT, 'contract insert: frozen decision scan must be a sealed complete regular capture of the day with matching counts'); END""")
    return tuple(out)


DDL_V7 = _ddl()


# ── Migration 사전 검사·봉인 기록 ───────────────────────────────────────────────

def precheck(db) -> list:
    """ab0007 적용 전 기존 저널 검사. 맞지 않으면 문제 목록 (Migration 중단, 원본 무변경). 자동 수정하지 않는다.

    - 모든 수집 기록: rows_captured = 실제 스냅샷 수, strict_rows = 실제 Strict 수 ((0,0) 도 예외 없음).
      불완전 수집(ERROR·ABANDONED·PARTIAL·UNRECOVERABLE)도 같은 트랜잭션에서 건수를 기록하므로 같이 본다 — 상태는 그대로 둔다.
    - 스냅샷: 수집 기록 소속·거래일·장중 여부·상태 일치, sha256(row_json) = row_sha256.
    - FROZEN 거래일: 결정 스캔이 같은 거래일·정규·DONE·complete=1·FULL·장 마감 후·확정 전 스캔이고 v1 해시가 실제 집합과 일치.
    - 불완전 수집은 FROZEN 결정 스캔으로 참조될 수 없다 (위 조건)."""
    bad = []
    rows = db.execute("""SELECT c.scan_log_id, c.scan_status, c.complete, c.recoverability, c.rows_captured, c.strict_rows,
        (SELECT COUNT(*) FROM ab_signal_snapshots s WHERE s.scan_log_id = c.scan_log_id),
        (SELECT COUNT(*) FROM ab_signal_snapshots s WHERE s.scan_log_id = c.scan_log_id AND s.strict_passed = 1)
        FROM ab_scan_captures c ORDER BY c.scan_log_id""").fetchall()
    for sid, status, complete, recov, rc, sr, n, ns in rows:
        if (rc, sr) != (n, ns):
            kind = "정상 완료" if (status == "DONE" and complete == 1 and recov == "FULL") else f"불완전({status}/{recov})"
            bad.append(f"ab_scan_captures #{sid} {kind}: 기록 {rc}/{sr}건 ≠ 실제 {n}/{ns}건 (전체/Strict)")
    for sid, in db.execute("""SELECT DISTINCT s.scan_log_id FROM ab_signal_snapshots s
            LEFT JOIN ab_scan_captures c ON c.scan_log_id = s.scan_log_id
            WHERE c.scan_log_id IS NULL OR s.trading_day IS NOT c.trading_day OR s.intraday IS NOT c.intraday
               OR s.scan_status IS NOT c.scan_status"""):
        bad.append(f"ab_signal_snapshots scan #{sid}: 수집 기록이 없거나 거래일·장중·상태가 다름")
    n = sum(1 for rj, sha in db.execute("SELECT row_json, row_sha256 FROM ab_signal_snapshots")
            if not row_sha_ok(rj, sha))
    if n:
        bad.append(f"ab_signal_snapshots: row_sha256 이 row_json 과 다름 {n}행")
    for day, sid, h in db.execute("SELECT day, decision_scan_log_id, signal_set_hash FROM ab_day_inputs "
                                  "WHERE decision_status='FROZEN' ORDER BY day"):
        c = db.execute("SELECT trading_day, scan_status, complete, recoverability, intraday, late_after_decision, "
                       "triggered_by FROM ab_scan_captures WHERE scan_log_id=?", (sid,)).fetchone()
        if c is None or tuple(c) != (day, "DONE", 1, "FULL", 0, 0, REGULAR_SCAN_TRIGGER):
            bad.append(f"ab_day_inputs {day}: 결정 스캔 #{sid} 가 그날의 정상 완료 정규 수집이 아님 ({tuple(c) if c else None})")
        if signal_set_hash_v1(db, sid) != h:
            bad.append(f"ab_day_inputs {day}: 동결 해시가 결정 스캔 #{sid} 의 실제 신호 집합과 다름")
    return bad


def backfill_seal_rows(db, now: float) -> list:
    """사전 검사를 통과한 기존 수집 기록마다 봉인 행 값을 만든다 (기존 행·해시는 바꾸지 않는다)."""
    out = []
    for (sid,) in db.execute("SELECT scan_log_id FROM ab_scan_captures ORDER BY scan_log_id").fetchall():
        n, ns, dg = member_digest(db, sid)
        out.append({"sid": sid, "n": n, "ns": ns, "dg": dg, "now": now})
    return out


SEAL_INSERT = ("INSERT INTO ab_capture_seals(scan_log_id, basis_version, member_count, strict_count, member_digest, "
               "source, sealed_at) VALUES (:sid, 'SSV2', :n, :ns, :dg, :source, :now)")
