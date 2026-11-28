"""PR #14 Codex 재리뷰(head 9a315db) 반영 (2) — 재계산 멱등성·수동 실행권 해제·정체 이력·종료 run 원장 불변성(ab0004).

원칙: 동일 요청을 중복 처리하지 않는다 / 실패한 재계산은 기존 CURRENT 를 훼손하지 않는다 / 종료 run 과 감사 이력을
변경하지 않는다 / 오류 복구 후에도 관찰 기간 오류 이력을 보존한다. 임시 SHADOW 저널만 쓴다.
"""
import json
import threading
import time

import pytest

from tests.test_auto_buy_shadow import D3, job, prod, shadow  # noqa: F401
from tests.test_auto_buy_shadow_integrity import evening, one, q
from tests.test_auto_buy_shadow_review3 import _dead_pid, _replay_ready, request_replay
from tests.test_auto_buy_shadow_review4_lease import _building
from trading.auto_buy.shadow import inputs as IN
from trading.auto_buy.shadow import report as RP
from trading.auto_buy.shadow.store import connect_rw, get_runtime, set_runtime, tx


def _db(shadow):
    return connect_rw(shadow.journal_path)


def runs(shadow):
    return {r["run_id"]: r["status"] for r in q(shadow, "SELECT run_id, status FROM ab_runs")}


def replay(shadow, full, **kw):
    db = _db(shadow)
    try:
        return IN.replay(db, full, "BARS_RECOVERED", shadow, **kw)
    finally:
        db.close()


# ── 멱등성 ───────────────────────────────────────────────────────────

def test_sequential_duplicate_request_runs_once(shadow, prod):
    full = _replay_ready(shadow, prod)
    first = replay(shadow, full)
    before = runs(shadow)
    second = replay(shadow, full)
    assert first["run"] and second == {"run": None, "parent": first["run"], "skipped": "NOT_NEEDED",
                                       "done_generation": first["generation"]}
    assert runs(shadow) == before                                         # 새 run 없음


def test_already_applied_generation_and_parent_change_are_skipped(shadow, prod):
    full = _replay_ready(shadow, prod)
    g1 = one(shadow, "SELECT value FROM ab_runtime WHERE key='replay_generation'")["value"]
    first = replay(shadow, full, expected_generation=int(g1))
    g2 = request_replay(shadow)                                           # 새 요청
    assert replay(shadow, full, expected_generation=int(g1))["skipped"] == "ALREADY_APPLIED"
    assert replay(shadow, full, expected_parent=first["run"] + 100)["skipped"] == "PARENT_CHANGED"
    db = _db(shadow)
    try:
        assert IN.replay_request(db)["generation"] == g2                  # 건너뛰어도 요청은 남는다
    finally:
        db.close()
    second = replay(shadow, full, expected_parent=first["run"], expected_generation=g2)
    assert second["run"] and second["parent"] == first["run"] and second["generation"] == g2


def test_new_request_during_replay_is_not_lost(shadow, prod, monkeypatch):
    full = _replay_ready(shadow, prod)
    real, st = IN.build_inputs, {"n": 0, "g": None}

    def build(db, run, day, bars, settings=None):
        st["n"] += 1
        if st["n"] == 1:                                                  # 재계산 도중 새 데이터 복구 요청
            st["g"] = request_replay(shadow, (("2026-11-25", "CCC"),))
        return real(db, run, day, bars, settings)
    monkeypatch.setattr(IN, "build_inputs", build)
    res = replay(shadow, full)
    assert res["pending_newer_request"] is True
    db = _db(shadow)
    try:
        need = IN.replay_request(db)
        assert need and need["generation"] == st["g"] > res["generation"]  # 이전 완료가 새 요청을 지우지 않음
    finally:
        db.close()
    assert one(shadow, "SELECT COUNT(*) n FROM ab_events WHERE kind='replay_request_pending'")["n"] == 1
    monkeypatch.setattr(IN, "build_inputs", real)
    again = replay(shadow, full)
    assert again["run"] and again["parent"] == res["run"] and not again["pending_newer_request"]
    assert replay(shadow, full)["skipped"] == "NOT_NEEDED"


def test_failed_replay_keeps_current_and_request_then_retries_once(shadow, prod, monkeypatch):
    full = _replay_ready(shadow, prod)
    parent = one(shadow, "SELECT run_id FROM ab_runs WHERE status='CURRENT'")["run_id"]
    real = IN.build_inputs
    monkeypatch.setattr(IN, "build_inputs", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        replay(shadow, full)
    monkeypatch.setattr(IN, "build_inputs", real)
    r = runs(shadow)
    assert r[parent] == "CURRENT" and list(r.values()).count("FAILED") == 1
    ok = replay(shadow, full)
    assert ok["parent"] == parent and runs(shadow)[ok["run"]] == "CURRENT"
    assert replay(shadow, full)["skipped"] == "NOT_NEEDED"


def test_process_termination_keeps_request_then_single_retry(shadow, prod, monkeypatch):
    full = _replay_ready(shadow, prod)
    parent = one(shadow, "SELECT run_id FROM ab_runs WHERE status='CURRENT'")["run_id"]
    real = IN.build_inputs
    monkeypatch.setattr(IN, "build_inputs", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):                                # 프로세스 종료 모사 (정리 코드 미실행)
        replay(shadow, full)
    monkeypatch.setattr(IN, "build_inputs", real)
    from tests.test_auto_buy_shadow_review3 import _set_lease
    _set_lease(shadow, token="killed", pid=_dead_pid())
    out = job(shadow, prod, full, evening(D3))                            # 사망 확인 → FAILED → 요청 그대로 → 1회 재계산
    assert len(out["abandoned_builds"]) == 1 and out["replay"]["parent"] == parent
    assert list(runs(shadow).values()).count("CURRENT") == 1
    assert job(shadow, prod, full, evening(D3)).get("replay") is None    # 두 번째 job: 새 run 없음


@pytest.mark.parametrize("rep", range(5))
def test_concurrent_same_request_exactly_one_new_run(shadow, prod, rep):
    """동시성 반복: 같은 요청을 4개 스레드가 동시에 처리하려 해도 새 run 은 정확히 1개."""
    full = _replay_ready(shadow, prod)
    before = set(runs(shadow))
    barrier, results = threading.Barrier(4), []

    def worker():
        db = _db(shadow)
        try:
            barrier.wait()
            res = IN.replay(db, full, "BARS_RECOVERED", shadow)
            results.append("skipped" if res.get("skipped") else "ok")
        except IN.ReplayBusy:
            results.append("busy")
        except Exception as exc:  # pragma: no cover
            results.append(repr(exc))
        finally:
            db.close()
    ts = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in ts]
    [t.join(120) for t in ts]
    assert results.count("ok") == 1 and set(results) <= {"ok", "busy", "skipped"}, results
    assert len(set(runs(shadow)) - before) == 1 and list(runs(shadow).values()).count("CURRENT") == 1


# ── 수동 실행권 해제 ───────────────────────────────────────────────────

STALE = time.time() - IN.REPLAY_LEASE_STALE_SEC - 600


def _remote_stale(shadow, prod):
    _replay_ready(shadow, prod)
    run = _building(shadow, {"host": "worker-b.invalid", "pid": 4242, "proc_start": "ps:remote", "token": "tok-b",
                             "heartbeat_at": STALE})
    ev = {"host": "worker-b.invalid", "pid": 4242, "proc_start": "ps:remote",
          "confirmed_stopped_at": "2026-11-27T09:00:00Z", "method": "원격 호스트에서 프로세스 목록 확인"}
    return run, ev


def _release(shadow, **kw):
    db = _db(shadow)
    try:
        return IN.release_stalled_lease(db, **kw)
    finally:
        db.close()


def _lease(shadow):
    db = _db(shadow)
    try:
        return get_runtime(db, "replay_in_progress")
    finally:
        db.close()


@pytest.mark.parametrize("change,match", [
    ({"operator": ""}, "운영자"),
    ({"reason": " "}, "사유"),
    ({"evidence": {}}, "근거"),
    ({"token": "wrong"}, "토큰"),
    ({"run": 999}, "run #999"),
    ({"evidence_host": "other.invalid"}, "host"),
])
def test_manual_release_refuses_bad_requests_and_keeps_lease(shadow, prod, change, match):
    run, ev = _remote_stale(shadow, prod)
    kw = {"run": run, "token": "tok-b", "operator": "op", "reason": "응답 없음 확인", "evidence": dict(ev)}
    if "evidence_host" in change:
        kw["evidence"]["host"] = change.pop("evidence_host")
    kw.update(change)
    before = _lease(shadow)
    with pytest.raises(IN.ManualReleaseRefused, match=match):
        _release(shadow, **kw)
    assert _lease(shadow) == before and runs(shadow)[run] == "BUILDING"


@pytest.mark.parametrize("lease,state", [
    ({"heartbeat_at": time.time()}, "ALIVE"),                                       # 정상 heartbeat (같은 호스트)
    ({"heartbeat_at": STALE}, "STALLED"),                                           # 같은 호스트, 살아 있음
    ({"pid": "DEAD"}, "DEAD"),
    ({"host": "worker-b.invalid", "heartbeat_at": time.time()}, "REMOTE_ACTIVE"),
])
def test_manual_release_refuses_unless_remote_stale(shadow, prod, lease, state):
    _replay_ready(shadow, prod)
    if lease.get("pid") == "DEAD":
        lease = {"pid": _dead_pid()}
    run = _building(shadow, lease)
    lz = _lease(shadow)
    assert IN.classify_lease(lz)["state"] == state
    ev = {"host": lz["host"], "pid": lz["pid"], "proc_start": lz["proc_start"], "confirmed_stopped_at": "x",
          "method": "y"}
    with pytest.raises(IN.ManualReleaseRefused, match=state):
        _release(shadow, run=run, token=lz["token"], operator="op", reason="r", evidence=ev)
    assert _lease(shadow) == lz and runs(shadow)[run] == "BUILDING"


def test_manual_release_requires_exactly_one_building_row(shadow, prod):
    _replay_ready(shadow, prod)
    cur = one(shadow, "SELECT run_id FROM ab_runs WHERE status='CURRENT'")["run_id"]
    db = _db(shadow)
    try:
        with tx(db):                                       # 실행권이 BUILDING 이 아닌 run 을 가리키는 비정상 상태
            set_runtime(db, "replay_in_progress", {"run_id": cur, "host": "worker-b.invalid", "pid": 1,
                                                   "proc_start": "p", "token": "t", "heartbeat_at": STALE})
    finally:
        db.close()
    ev = {"host": "worker-b.invalid", "pid": 1, "proc_start": "p", "confirmed_stopped_at": "x", "method": "y"}
    with pytest.raises(IN.ManualReleaseRefused, match="갱신 0행"):
        _release(shadow, run=cur, token="t", operator="op", reason="r", evidence=ev)
    assert _lease(shadow)["run_id"] == cur and runs(shadow)[cur] == "CURRENT"        # 롤백: 실행권 유지


def test_manual_release_success_is_audited_and_old_worker_fenced(shadow, prod):
    run, ev = _remote_stale(shadow, prod)
    res = _release(shadow, run=run, token="tok-b", operator="op-1", reason="원격 작업자 중지 확인", evidence=ev)
    assert res["released"] and _lease(shadow) is None and runs(shadow)[run] == "FAILED"
    summ = json.loads(one(shadow, "SELECT summary FROM ab_runs WHERE run_id=?", run)["summary"])
    assert summ["reason"] == "ABANDONED_MANUAL" and summ["operator"] == "op-1" and summ["evidence"] == ev
    e = one(shadow, "SELECT * FROM ab_events WHERE kind='replay_released_manually'")
    assert e["severity"] == "ERROR" and json.loads(e["details"])["operator"] == "op-1"
    db = _db(shadow)
    try:
        with pytest.raises(Exception, match="run fenced"):
            with tx(db):
                db.execute("INSERT INTO ab_days(run_id, day, status, decision_status, candidates, bars_requested, "
                           "bars_missing, notes, processed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                           (run, "2026-12-01", "COMPLETE", "FROZEN", 0, 0, 0, "[]", 0))
    finally:
        db.close()


# ── 정체 이력 (S5) ───────────────────────────────────────────────────

def _reap(shadow):
    db = _db(shadow)
    try:
        return IN.reap_abandoned_builds(db)
    finally:
        db.close()


def _set_hb(shadow, t):
    db = _db(shadow)
    try:
        with tx(db):
            lz = get_runtime(db, "replay_in_progress")
            lz["heartbeat_at"] = t
            set_runtime(db, "replay_in_progress", lz)
    finally:
        db.close()


def test_stall_history_survives_recovery_and_is_not_double_counted(shadow, prod):
    _replay_ready(shadow, prod)
    _building(shadow, {"heartbeat_at": STALE})                          # 같은 호스트, 살아 있으나 응답 없음
    for _ in range(3):                                                  # 같은 장애를 여러 job 이 관찰
        assert _reap(shadow) == []
    s = RP.summary(shadow)["s5"]["profiles"]["P7C"]["safety_breakdown"]
    assert s["operational"]["replay_stalled"] == 1 and s["current_unresolved"]["replay_stalled"] == 1
    _set_hb(shadow, time.time())                                        # STALLED → RUNNING 복구
    _reap(shadow)
    s = RP.summary(shadow)["s5"]["profiles"]["P7C"]["safety_breakdown"]
    assert s["operational"]["replay_stalled"] == 1 and s["current_unresolved"]["replay_stalled"] == 0
    assert s["recovered"]["replay_stalled"] == 1 and not s["pass"]       # 누적 위반은 줄지 않음
    assert one(shadow, "SELECT COUNT(*) n FROM ab_events WHERE kind='replay_stall_recovered'")["n"] == 1
    _set_hb(shadow, STALE)                                              # 두 번째 정체 = 새 에피소드
    _reap(shadow)
    assert RP.summary(shadow)["s5"]["profiles"]["P7C"]["safety_breakdown"]["operational"]["replay_stalled"] == 2


# ── 종료 run 원장 불변성 (ab0004) ──────────────────────────────────────

def test_terminal_run_ledger_is_immutable_but_normal_replay_still_works(shadow, prod):
    full = _replay_ready(shadow, prod)
    res = replay(shadow, full)
    old, cur = res["parent"], res["run"]
    order = one(shadow, "SELECT o.id FROM ab_orders o JOIN ab_fill_ledger f ON f.order_id=o.id WHERE o.run_id=?",
                old)["id"]
    snapshot = {t: q(shadow, f"SELECT * FROM {t} WHERE run_id=?", old) for t in ("ab_orders", "ab_daily", "ab_days")}
    db = _db(shadow)
    blocked = {
        "delete_terminal_rows": "DELETE FROM ab_orders WHERE run_id=%d" % old,
        "delete_current_rows": "DELETE FROM ab_daily WHERE run_id=%d" % cur,
        "move_terminal_to_current": "UPDATE ab_orders SET run_id=%d WHERE run_id=%d" % (cur, old),
        "move_current_to_terminal": "UPDATE ab_daily SET run_id=%d WHERE run_id=%d" % (old, cur),
        "update_terminal_row": "UPDATE ab_days SET status='COMPLETE' WHERE run_id=%d" % old,
        "fill_update": "UPDATE ab_fill_ledger SET price=price+1 WHERE order_id=%d" % order,
        "fill_move": "UPDATE ab_fill_ledger SET order_id=(SELECT MAX(id) FROM ab_orders) WHERE order_id=%d" % order,
        "fill_delete": "DELETE FROM ab_fill_ledger WHERE order_id=%d" % order,
        "fill_unknown_order": "INSERT INTO ab_fill_ledger(order_id, cum_filled_qty, applied_delta, price, day, basis, "
                              "applied_at) VALUES (999999, 1, 1, 1.0, '2026-11-24', 'T', 0)",
        "row_unknown_run": "INSERT INTO ab_days(run_id, day, status, decision_status, candidates, bars_requested, "
                           "bars_missing, notes, processed_at) VALUES (999999,'2026-12-01','COMPLETE','FROZEN',0,0,0,'[]',0)",
        "run_delete": "DELETE FROM ab_runs WHERE run_id=%d" % old,
        "run_audit_change": "UPDATE ab_runs SET summary='{}' WHERE run_id=%d" % old,
        "run_reopen": "UPDATE ab_runs SET status='CURRENT' WHERE run_id=%d" % old,
        "run_id_change": "UPDATE ab_runs SET run_id=777 WHERE run_id=%d" % cur,
        "event_update": "UPDATE ab_events SET message='x'",
        "event_delete": "DELETE FROM ab_events",
    }
    try:
        for name, sql in blocked.items():
            with pytest.raises(Exception, match="fenced|immutable|terminal|contract"):
                with tx(db):
                    db.execute(sql)
    finally:
        db.close()
    assert {t: q(shadow, f"SELECT * FROM {t} WHERE run_id=?", old) for t in snapshot} == snapshot
    request_replay(shadow)                                              # 정상 재계산·CURRENT 전환은 그대로 동작
    nxt = replay(shadow, full)
    assert nxt["parent"] == cur and runs(shadow)[nxt["run"]] == "CURRENT" and runs(shadow)[cur] == "SUPERSEDED"
    assert job(shadow, prod, full, evening(D3)).get("error") is None
