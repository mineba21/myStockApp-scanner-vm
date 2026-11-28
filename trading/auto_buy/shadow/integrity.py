"""동결 입력(결정 신호 집합) 공통 무결성 검증 — 적격 판정·후보 조회·신규 거래일·중단 후 재실행·결측 복구·replay·
CURRENT 전환 전 최종 검증이 모두 이 함수를 쓴다 (Codex 재리뷰 head a183b99 P1).

FROZEN 거래일 검증 항목 (하나라도 다르면 오류 — 다시 계산해 정상화하거나 신호를 빼지 않는다):
- 결정 스캔의 수집 기록: 같은 거래일·정규·DONE·complete=1·FULL·장 마감 후·확정 전
- 봉인(ab_capture_seals) 존재, 수집 건수 = 봉인 건수 = 실제 구성원 수 (전체·Strict)
- 구성원 소속: 모두 그 스캔·거래일·장 마감 후·DONE
- 구성원별 sha256(row_json) = row_sha256
- 저장된 동결 해시(v1) = 실제 집합으로 계산한 v1 해시
- SSV2 다이제스트(식별자·정규화·파생 필드 포함) = 봉인 다이제스트
- 구성원별 원본 row_json 으로 재현 가능한 정규화 필드 = 저장된 정규화 값 (``raw_contract``, head d95beab P2-3)
- 적격 판정 집합 완전성: 구성원마다 현재 전략 버전 B00·B01 정확히 1개씩 (기대 집합 = 실제 집합, head d95beab P1).
  정상 미완료(아직 어떤 현재 버전 run 도 쓰지 않은 날의 '전부 없음')만 pending 으로 구분하고, 일부 누락·사용 후 누락·
  과거 버전만 남은 사용된 날은 오류다
- 저장된 적격 판정(현재 전략 버전) = 같은 스냅샷으로 다시 계산한 판정
- 계좌별 처리·일 기록 트랜잭션 안에서 위 전체를 다시 검증하고 실제 재계산 지문·후보 행 내용을 대조 (head d95beab P2-2)

불일치 시: 신규 가상 주문을 만들지 않고 처리를 멈춘다. 기존 CURRENT·원장은 그대로 두고 새 CURRENT 로 바꾸지 않는다.
ERROR 이벤트 ``signal_set_integrity`` 와 runtime ``signal_set_integrity`` (MONITOR 표시·S5 안전 위반)를 남긴다.
runtime 표시는 자동으로 지우지 않는다 (운영자 확인 후 수동 처리).
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Optional

from trading.auto_buy import settings as S
from trading.auto_buy.shadow import raw_contract as RC
from trading.auto_buy.shadow.journal_migrations import ddl_v7
from trading.auto_buy.shadow.store import event, get_runtime, set_runtime, tx

RUNTIME_KEY = "signal_set_integrity"
DAY_BLOCK_KEY = "integrity_day_block"


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
    raw = RC.scan_issues(db, scan_log_id)               # 원본 row_json ↔ 정규화 필드 (봉인 이전 오염 포함)
    if raw:
        out.append(f"RAW_FIELD_MISMATCH:{[f'{k}:{v[:4]}' for k, v in list(raw.items())[:10]]}")
    return out


def eligibility_used(db, scan_log_id, day) -> bool:
    """현재 전략 버전의 run(상태 무관: CURRENT·BUILDING·FAILED·SUPERSEDED)이 이 결정 스캔의 판정을 이미 썼는지.
    그날 일 기록(ab_days)·계좌 일 기록(ab_daily, 일부 계좌만 처리된 날 포함)·이 스캔 구성원에 대한 매수 판단(ab_decisions)
    중 하나라도 있으면 사용됨 → 판정 누락은 '아직 생성 전'이 아니라 확정 판정의 누락·훼손이다."""
    v = S.STRATEGY_VERSION
    return bool(db.execute(
        "SELECT EXISTS (SELECT 1 FROM ab_days d JOIN ab_runs r ON r.run_id=d.run_id WHERE d.day=? AND r.strategy_version=?) "
        "OR EXISTS (SELECT 1 FROM ab_daily d JOIN ab_runs r ON r.run_id=d.run_id WHERE d.day=? AND r.strategy_version=?) "
        "OR EXISTS (SELECT 1 FROM ab_decisions x JOIN ab_signal_snapshots s ON s.id=x.snapshot_id "
        "JOIN ab_runs r ON r.run_id=x.run_id WHERE s.scan_log_id=? AND r.strategy_version=?)",
        (day, v, day, v, scan_log_id, v)).fetchone()[0])


def eligibility_state(db, scan_log_id, day_row) -> tuple:
    """결정 스캔 1회의 현재 전략 버전 적격 판정 집합 대조 (Codex 재리뷰 head d95beab P1).

    기대 집합 = 구성원마다 (B00, B01) 정확히 1개씩. 실제 집합과 대조한다 (존재하는 판정만 재계산하지 않는다).
    반환 (issues, pending):
    - 완료: 기대 = 실제, 모든 판정이 같은 스냅샷 재계산과 일치 (eligible=0 정상 부적격 포함). 빈 스캔은 빈 집합 = 완료.
    - pending (정상 미완료): 현재 버전 판정이 **하나도 없고** 아직 어떤 현재 버전 run 도 이 판정을 쓰지 않았다 →
      최초 적격 판정 생성 대상 (``record_eligibility``). 과거 버전 판정만 있는 경우(버전 전환 직후)도 같다.
    - issues (무결성 오류): 일부만 있음(B00만·B01만·일부 구성원만), 판정을 쓴 뒤 전부 없음(과거 버전만 남은 경우 포함),
      저장 판정 ≠ 재계산. 자동 재생성으로 숨기지 않는다."""
    from trading.auto_buy.shadow import eligibility as EL
    from trading.auto_buy.shadow import engine as EN
    snaps = {r["id"]: r for r in db.execute("SELECT * FROM ab_signal_snapshots WHERE scan_log_id=? ORDER BY id",
                                            (scan_log_id,))}
    if not snaps:
        return [], 0
    rows = db.execute("SELECT e.* FROM ab_eligibility e JOIN ab_signal_snapshots s ON s.id=e.snapshot_id "
                      "WHERE s.scan_log_id=? AND e.strategy_version=?", (scan_log_id, S.STRATEGY_VERSION)).fetchall()
    have = {}
    for e in rows:
        have.setdefault((e["snapshot_id"], e["pool"]), []).append(e)
    expected = {(sid, p) for sid in snaps for p in EL.POOLS}
    if not rows:
        if not eligibility_used(db, scan_log_id, day_row["day"]):
            return [], len(snaps)
        old = db.execute("SELECT COUNT(*) FROM ab_eligibility e JOIN ab_signal_snapshots s ON s.id=e.snapshot_id "
                         "WHERE s.scan_log_id=? AND e.strategy_version!=?", (scan_log_id, S.STRATEGY_VERSION)).fetchone()[0]
        return [f"ELIGIBILITY_MISSING_AFTER_USE:{len(expected)}" + (f":OLD_VERSION_ONLY({old})" if old else "")], 0
    issues = []
    missing = sorted(expected - set(have))
    if missing:
        issues.append(f"ELIGIBILITY_INCOMPLETE:{[f'{s}:{p}' for s, p in missing[:10]]}")
    extra = sorted(k for k, v in have.items() if k not in expected or len(v) != 1)
    if extra:
        issues.append(f"ELIGIBILITY_UNEXPECTED:{[f'{s}:{p}' for s, p in extra[:10]]}")
    bad = []
    for (sid, pool), es in sorted(have.items()):
        if (sid, pool) not in expected:
            continue
        r = snaps[sid]
        snap = dict(r)
        snap["decision_role"] = EN.decision_role(r, day_row)
        ok, reasons = EL.evaluate(snap)[pool]
        for e in es:
            try:
                stored = json.loads(e["reasons"])
            except ValueError:
                stored = None
            if (int(ok), list(reasons), snap["decision_role"]) != (e["eligible"], stored, e["decision_role"]):
                bad.append(f"{sid}:{pool}")
    if bad:
        issues.append(f"ELIGIBILITY_MISMATCH:{bad[:10]}")
    return issues, 0


def decision_set_issues(db, day) -> list:
    """FROZEN 거래일의 전체 무결성 오류 목록 (수집·봉인·건수·소속·row_sha256·원본↔정규화·v1·SSV2·적격 판정 완전성·내용).
    적격 판정이 정상 미완료(pending)인 것은 오류가 아니다 (``eligibility_pending`` 이 따로 센다)."""
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
    out += eligibility_state(db, sid, row)[0]
    return out


def verify_decision_set(db, day, where: str) -> None:
    issues = decision_set_issues(db, day)
    if issues:
        row = db.execute("SELECT decision_scan_log_id FROM ab_day_inputs WHERE day=?", (day,)).fetchone()
        raise SignalSetIntegrityError(day, row[0] if row else None, issues, where)


def _eligibility_digest(db, scan_log_id) -> str:
    rows = [list(r) for r in db.execute(
        "SELECT e.snapshot_id, e.pool, e.eligible, e.reasons, e.decision_role, e.logic_hash FROM ab_eligibility e "
        "JOIN ab_signal_snapshots s ON s.id=e.snapshot_id WHERE s.scan_log_id=? AND e.strategy_version=? "
        "ORDER BY e.snapshot_id, e.pool", (scan_log_id, S.STRATEGY_VERSION))]
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def fingerprint(db, day) -> Optional[tuple]:
    """현재 상태 지문 — **실제 데이터로 다시 계산한** 값 (저장된 봉인 해시만 다시 읽지 않는다): FROZEN 이면
    (결정 스캔, 실제 SSV2 다이제스트, 봉인 다이제스트, 봉인 건수, 실제 구성원 수, 저장 v1 해시, 실제 v1 해시,
    현재 버전 적격 판정 다이제스트), 아니면 None. 지문이 같아도 무결성 검증(``decision_set_issues``)은 따로 한다."""
    row = db.execute("SELECT decision_status, decision_scan_log_id, signal_set_hash FROM ab_day_inputs WHERE day=?",
                     (day,)).fetchone()
    if row is None or row["decision_status"] != "FROZEN":
        return None
    sid = row["decision_scan_log_id"]
    g = db.execute("SELECT member_digest, member_count FROM ab_capture_seals WHERE scan_log_id=?", (sid,)).fetchone()
    n, _, actual = ddl_v7.member_digest(db, sid)
    return (sid, actual, g[0] if g else None, g[1] if g else None, n, row["signal_set_hash"],
            ddl_v7.signal_set_hash_v1(db, sid), _eligibility_digest(db, sid))


def _cand_key(pool_rows: dict) -> dict:
    return {p: [json.dumps(dict(c), ensure_ascii=False, sort_keys=True, default=str) for c in rows]
            for p, rows in (pool_rows or {}).items()}


def check_inputs_current(db, inp, where: str) -> None:
    """후보를 쓰는 **같은 쓰기 트랜잭션**(BEGIN IMMEDIATE) 안에서 전체 무결성을 다시 검증한다 (Codex 재리뷰 head d95beab P2-2).
    - 공통 무결성 검증 전체 (실제 SSV2 재계산·구성원 row_sha256·원본↔정규화·v1·적격 판정 완전성·재계산)
    - 검증 때의 지문(실제 재계산 값)과 지금이 같은지
    - build_inputs 이후 후보 집합(행 내용 전체)이 바뀌지 않았는지, 후보가 모두 그날 결정 스캔 구성원인지
    이 트랜잭션이 커밋될 때까지 다른 연결은 쓸 수 없으므로 검증과 계좌 원장 쓰기 사이에 경쟁이 없다."""
    from trading.auto_buy.shadow import engine as EN
    issues, found = [], list(decision_set_issues(db, inp.day))
    fp = fingerprint(db, inp.day)
    if fp != getattr(inp, "set_fingerprint", None):
        issues.append("FINGERPRINT_CHANGED_SINCE_VERIFICATION")
    scans = {c.get("scan_log_id") for pool in (inp.cands or {}).values() for c in pool}
    if scans and (fp is None or scans != {fp[0]}):
        issues.append("CANDIDATE_NOT_IN_DECISION_SET")
    elif not issues and not found:
        now = EN.candidate_rows(db, fp[0] if fp else None)
        if _cand_key(now) != _cand_key(inp.cands):
            issues.append("CANDIDATES_CHANGED_SINCE_BUILD")
    issues += found
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


def quarantine_day(db, run, day, exc: SignalSetIntegrityError) -> Optional[dict]:
    """계좌별 처리(검증 통과 이후) 중 무결성 오류 → 거래일 격리 (Codex 재리뷰 head d95beab P2-2 복구 정책).

    계좌 하루 처리는 계좌별 트랜잭션이다. 오류가 난 계좌는 롤백되지만 앞서 커밋된 계좌의 기록은 그대로 남는다
    (이미 커밋된 감사 원장·계좌 기록을 삭제해 복구하지 않는다). 그날은 ``ab_days`` 가 기록되지 않아 COMPLETE 가 아니며:
    - CURRENT run: runtime ``integrity_day_block`` (run·거래일·커밋된 계좌)을 남긴다. job 은 이 표시가 있는 동안 신규 거래일
      처리·재계산을 하지 않는다 (자동 해제 없음, 운영자 확인 후 수동 해제). 보고서는 그날 기록을 정상 성과에서 뺀다.
    - BUILDING run(재계산): replay 가 그 run 을 FAILED 로 남기고 기존 CURRENT 를 유지한다 (정상 승격 차단). 표시는 남기지 않는다.
    반환: 기록한 표시 (CURRENT 가 아니면 None)."""
    try:
        with tx(db):
            st = db.execute("SELECT status FROM ab_runs WHERE run_id=?", (run,)).fetchone()
            if st is None or st[0] != "CURRENT":
                return None
            accts = [r[0] for r in db.execute("SELECT account_id FROM ab_daily WHERE run_id=? AND day=? ORDER BY rowid",
                                              (run, day))]
            prev = get_runtime(db, DAY_BLOCK_KEY)
            if prev and prev.get("run") == run and prev.get("day") == day:
                info = {**prev, "accounts_committed": accts, "repeats": int(prev.get("repeats") or 1) + 1,
                        "last_at": time.time()}
            else:
                info = {"run": run, "day": day, "scan_log_id": exc.scan_log_id, "where": exc.where,
                        "issues": exc.issues[:10], "accounts_committed": accts, "at": time.time(), "repeats": 1,
                        "policy": "커밋된 계좌 기록 보존(삭제 없음), 그날 ab_days 미기록(COMPLETE 아님), 신규 거래일·재계산 "
                                  "차단, 운영자 확인 후 수동 해제"}
                event(db, "ERROR", "integrity_day_block", f"{day} run #{run} 계좌 처리 중 무결성 오류 — 커밋된 계좌 "
                      f"{len(accts)}개 보존, 그날 미완료 격리·신규 판단 차단", run=run, day=day, accounts=accts[:50],
                      issues=exc.issues[:10], where=exc.where)
            set_runtime(db, DAY_BLOCK_KEY, info)
        return info
    except Exception:
        return None
