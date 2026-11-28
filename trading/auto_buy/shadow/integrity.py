"""동결 입력(결정 신호 집합) 공통 무결성 검증 — 적격 판정·후보 조회·신규 거래일·중단 후 재실행·결측 복구·replay·
CURRENT 전환 전 최종 검증이 모두 이 함수를 쓴다 (Codex 재리뷰 head a183b99 P1).

FROZEN 거래일 검증 항목 (하나라도 다르면 오류 — 다시 계산해 정상화하거나 신호를 빼지 않는다):
- 결정 스캔의 수집 기록: 같은 거래일·정규·DONE·complete=1·FULL·장 마감 후·확정 전
- 봉인(ab_capture_seals) 존재, 수집 건수 = 봉인 건수 = 실제 구성원 수 (전체·Strict)
- 구성원 소속: 모두 그 스캔·거래일·장 마감 후·DONE
- 구성원별 sha256(row_json) = row_sha256
- 저장된 동결 해시(v1) = 실제 집합으로 계산한 v1 해시
- SSV2 다이제스트(식별자·정규화·파생 필드 포함) = 봉인 다이제스트
- 저장된 적격 판정(현재 전략 버전) = 같은 스냅샷으로 다시 계산한 판정

불일치 시: 신규 가상 주문을 만들지 않고 처리를 멈춘다. 기존 CURRENT·원장은 그대로 두고 새 CURRENT 로 바꾸지 않는다.
ERROR 이벤트 ``signal_set_integrity`` 와 runtime ``signal_set_integrity`` (MONITOR 표시·S5 안전 위반)를 남긴다.
runtime 표시는 자동으로 지우지 않는다 (운영자 확인 후 수동 처리).
"""
from __future__ import annotations

import json
import time
from typing import Optional

from trading.auto_buy import settings as S
from trading.auto_buy.shadow.journal_migrations import ddl_v7
from trading.auto_buy.shadow.store import event, get_runtime, set_runtime, tx

RUNTIME_KEY = "signal_set_integrity"


class SignalSetIntegrityError(RuntimeError):
    def __init__(self, day, scan_log_id, issues, where):
        self.day, self.scan_log_id, self.issues, self.where = day, scan_log_id, list(issues), where
        self.recorded = False
        super().__init__(f"{day} 결정 스캔 #{scan_log_id} 신호 집합 무결성 오류 ({where}): " + "; ".join(self.issues[:3]))


def scan_set_issues(db, scan_log_id, day) -> list:
    """결정 스캔 1회의 수집 기록·봉인·건수·소속·row_sha256·SSV2 (동결 전후 공통)."""
    c = db.execute("SELECT * FROM ab_scan_captures WHERE scan_log_id=?", (scan_log_id,)).fetchone()
    if c is None:
        return ["CAPTURE_MISSING"]
    out = []
    want = (day, "DONE", 1, "FULL", 0, 0, ddl_v7.REGULAR_SCAN_TRIGGER)
    have = (c["trading_day"], c["scan_status"], c["complete"], c["recoverability"], c["intraday"],
            c["late_after_decision"], c["triggered_by"])
    if have != want:
        out.append(f"CAPTURE_NOT_DECISION:{have}")
    g = db.execute("SELECT * FROM ab_capture_seals WHERE scan_log_id=?", (scan_log_id,)).fetchone()
    members = db.execute("SELECT id, row_json, row_sha256, trading_day, intraday, scan_status, strict_passed "
                         "FROM ab_signal_snapshots WHERE scan_log_id=? ORDER BY id", (scan_log_id,)).fetchall()
    n, ns = len(members), sum(1 for m in members if m["strict_passed"] == 1)
    if (c["rows_captured"], c["strict_rows"]) != (n, ns):
        out.append(f"CAPTURE_COUNT_MISMATCH:{c['rows_captured']}/{c['strict_rows']}!={n}/{ns}")
    if g is None:
        out.append("SEAL_MISSING")
    elif (g["member_count"], g["strict_count"]) != (n, ns):
        out.append(f"SEAL_COUNT_MISMATCH:{g['member_count']}/{g['strict_count']}!={n}/{ns}")
    foreign = [m["id"] for m in members if (m["trading_day"], m["intraday"], m["scan_status"]) != (day, 0, "DONE")]
    if foreign:
        out.append(f"MEMBER_NOT_IN_DECISION_DAY:{foreign[:10]}")
    bad_sha = [m["id"] for m in members if not ddl_v7.row_sha_ok(m["row_json"], m["row_sha256"])]
    if bad_sha:
        out.append(f"ROW_SHA_MISMATCH:{bad_sha[:10]}")
    if g is not None and ddl_v7.member_digest(db, scan_log_id)[2] != g["member_digest"]:
        out.append("SSV2_DIGEST_MISMATCH")
    return out


def _eligibility_issues(db, scan_log_id, day_row) -> list:
    from trading.auto_buy.shadow import eligibility as EL
    from trading.auto_buy.shadow import engine as EN
    bad = []
    snaps = {r["id"]: r for r in db.execute("SELECT * FROM ab_signal_snapshots WHERE scan_log_id=?", (scan_log_id,))}
    for e in db.execute("SELECT e.* FROM ab_eligibility e JOIN ab_signal_snapshots s ON s.id=e.snapshot_id "
                        "WHERE s.scan_log_id=? AND e.strategy_version=?", (scan_log_id, S.STRATEGY_VERSION)):
        r = snaps[e["snapshot_id"]]
        snap = dict(r)
        snap["decision_role"] = EN.decision_role(r, day_row)
        ok, reasons = EL.evaluate(snap)[e["pool"]]
        try:
            stored = json.loads(e["reasons"])
        except ValueError:
            stored = None
        if (int(ok), list(reasons), snap["decision_role"]) != (e["eligible"], stored, e["decision_role"]):
            bad.append(f"{e['snapshot_id']}:{e['pool']}")
    return [f"ELIGIBILITY_MISMATCH:{bad[:10]}"] if bad else []


def decision_set_issues(db, day) -> list:
    row = db.execute("SELECT * FROM ab_day_inputs WHERE day=?", (day,)).fetchone()
    if row is None:
        return []
    if row["decision_status"] != "FROZEN":
        if row["decision_scan_log_id"] is not None or row["signal_set_hash"] is not None:
            return ["NO_DECISION_WITH_SCAN"]
        return []
    sid = row["decision_scan_log_id"]
    out = scan_set_issues(db, sid, day)
    if ddl_v7.signal_set_hash_v1(db, sid) != row["signal_set_hash"]:
        out.append("V1_HASH_MISMATCH")
    out += _eligibility_issues(db, sid, row)
    return out


def verify_decision_set(db, day, where: str) -> None:
    issues = decision_set_issues(db, day)
    if issues:
        row = db.execute("SELECT decision_scan_log_id FROM ab_day_inputs WHERE day=?", (day,)).fetchone()
        raise SignalSetIntegrityError(day, row[0] if row else None, issues, where)


def fingerprint(db, day) -> Optional[tuple]:
    """가벼운 현재 상태 지문 (계좌별 처리 트랜잭션 안에서 검증 시점과 같은지 확인): FROZEN 이면
    (결정 스캔, 봉인 다이제스트, 봉인 건수, 실제 구성원 수, 동결 해시), 아니면 None."""
    row = db.execute("SELECT decision_status, decision_scan_log_id, signal_set_hash FROM ab_day_inputs WHERE day=?",
                     (day,)).fetchone()
    if row is None or row["decision_status"] != "FROZEN":
        return None
    sid = row["decision_scan_log_id"]
    g = db.execute("SELECT member_digest, member_count FROM ab_capture_seals WHERE scan_log_id=?", (sid,)).fetchone()
    n = db.execute("SELECT COUNT(*) FROM ab_signal_snapshots WHERE scan_log_id=?", (sid,)).fetchone()[0]
    return (sid, g[0] if g else None, g[1] if g else None, n, row["signal_set_hash"])


def check_inputs_current(db, inp, where: str) -> None:
    """후보를 쓰는 트랜잭션 안에서: 검증 때의 지문과 지금이 같고, 후보가 모두 그날 결정 스캔 구성원인지."""
    fp = fingerprint(db, inp.day)
    issues = []
    if fp != getattr(inp, "set_fingerprint", None):
        issues.append("FINGERPRINT_CHANGED_SINCE_VERIFICATION")
    scans = {c.get("scan_log_id") for pool in (inp.cands or {}).values() for c in pool}
    if scans and (fp is None or scans != {fp[0]}):
        issues.append("CANDIDATE_NOT_IN_DECISION_SET")
    if issues:
        raise SignalSetIntegrityError(inp.day, fp[0] if fp else inp.decision_scan_log_id, issues, where)


def record_failure(db, exc: SignalSetIntegrityError, run: Optional[int] = None) -> None:
    """무결성 오류 감사 기록 (자체 트랜잭션 — 실패한 처리 트랜잭션은 이미 롤백됨). 같은 오류가 job 마다 반복되면 이벤트는
    한 번만 남기고 runtime 의 반복 횟수만 늘린다. runtime 표시는 자동으로 지우지 않는다."""
    if exc.recorded:
        return
    try:
        with tx(db):
            prev = get_runtime(db, RUNTIME_KEY) or {}
            sig = {"day": exc.day, "scan_log_id": exc.scan_log_id, "issues": exc.issues[:10]}
            same = {k: prev.get(k) for k in sig} == sig
            if not same:
                event(db, "ERROR", "signal_set_integrity", str(exc)[:500], day=exc.day, scan_log_id=exc.scan_log_id,
                      issues=exc.issues[:20], where=exc.where, run=run)
            set_runtime(db, RUNTIME_KEY, {**sig, "where": exc.where, "run": run, "at": time.time(),
                                          "first_at": prev.get("first_at") if same else time.time(),
                                          "repeats": int(prev.get("repeats") or 0) + 1 if same else 1,
                                          "message": str(exc)[:300]})
        exc.recorded = True
    except Exception:
        pass
