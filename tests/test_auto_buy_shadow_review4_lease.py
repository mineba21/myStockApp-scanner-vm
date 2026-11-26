"""PR #14 Codex 최종 재리뷰 반영 회귀 테스트 (2) — 재계산 실행권(lease)·heartbeat·fencing.

다른 작업자는 실제 별도 프로세스(subprocess)로 만든다: 관찰자(정리 판단), 재계산 중 SIGKILL 되는 작업자.
임시 SHADOW 저널만 쓰고 운영 DB·네트워크·주문 없음 (자식 프로세스는 config·운영 DB 모듈을 import 하지 않는다).
"""
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tests.test_auto_buy_shadow import D3, job, prod, shadow  # noqa: F401
from tests.test_auto_buy_shadow_integrity import evening, one, q
from tests.test_auto_buy_shadow_review3 import _dead_pid, _replay_ready, _set_lease
from trading.auto_buy.shadow import engine as EN
from trading.auto_buy.shadow import inputs as IN
from trading.auto_buy.shadow import report as RP
from trading.auto_buy.shadow.store import connect_rw, get_runtime, set_runtime, tx

REPO = str(Path(__file__).resolve().parents[1])

OBSERVER = """
import json, sys
from trading.auto_buy.shadow import inputs as IN
from trading.auto_buy.shadow.store import connect_rw
db = connect_rw(sys.argv[1])
now = float(sys.argv[2])
lease = IN.get_runtime(db, "replay_in_progress")
c = IN.classify_lease(lease, now)
reaped = IN.reap_abandoned_builds(db, now)
st = IN.replay_state(db, now)
print(json.dumps({"reaped": reaped, "lease_state": c["state"], "status": st and st["status"]}))
"""

WORKER = """
import sys, time
from trading.auto_buy import settings as S
from trading.auto_buy.shadow import inputs as IN
from trading.auto_buy.shadow.store import connect_rw
real = IN.process_day
def slow(db, run, inp, settings=None, **kw):
    r = real(db, run, inp, settings, **kw)
    print("DAY", run, inp.day, flush=True)
    time.sleep(120)
    return r
IN.process_day = slow
db = connect_rw(sys.argv[1])
IN.replay(db, lambda t, d: {x: None for x in t}, "BARS_RECOVERED", S.AutoBuySettings.from_env())
"""


def _py(code, *args, env=None):
    return subprocess.run([sys.executable, "-c", code, *map(str, args)], cwd=REPO, capture_output=True, text=True,
                          timeout=120, env=env or dict(os.environ))


def observe(shadow, now):
    r = _py(OBSERVER, shadow.journal_path, now)
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def _building(shadow, lease_overrides=None):
    """다른 작업자의 BUILDING run + 실행권 (기본: 살아 있는 다른 프로세스 = pytest 의 부모, 같은 호스트)."""
    db = connect_rw(shadow.journal_path)
    try:
        with tx(db):
            run = EN.create_run(db, "BARS_RECOVERED", EN.current_run(db), "BUILDING", shadow)
            now = time.time()
            lease = {"run_id": run, "reason": "BARS_RECOVERED", "at": now, "heartbeat_at": now,
                     "host": socket.gethostname(), "pid": os.getppid(), "boot_id": IN.boot_id(),
                     "proc_start": IN.proc_start_id(os.getppid()), "token": "other-worker"}
            lease.update(lease_overrides or {})
            set_runtime(db, "replay_in_progress", lease)
    finally:
        db.close()
    return run


# ── 901초 이상 정상 실행 중인 작업자 ──────────────────────────────────

def test_worker_running_over_901s_keeps_heartbeat_and_is_not_reaped(shadow, prod, monkeypatch):
    full = _replay_ready(shadow, prod)
    clock = {"t": 1_000_000.0}

    def fake_now():                       # 이 프로세스의 시계: 호출마다 200초씩 흐른다 (장시간 재계산)
        clock["t"] += 200.0
        return clock["t"]
    monkeypatch.setattr(IN, "_now", fake_now)
    seen = []
    real_build = IN.build_inputs

    def build_and_observe(db, run, day, bars, settings=None):
        lease = get_runtime(db, "replay_in_progress")
        seen.append({"elapsed": clock["t"] - lease["at"], "obs": observe(shadow, clock["t"]),
                     "hb_age": clock["t"] - lease["heartbeat_at"]})
        return real_build(db, run, day, bars, settings)
    monkeypatch.setattr(IN, "build_inputs", build_and_observe)
    db = connect_rw(shadow.journal_path)
    try:
        res = IN.replay(db, full, "BARS_RECOVERED", shadow)
    finally:
        db.close()
    last = seen[-1]
    assert last["elapsed"] > 901                                         # 901초 이상 실행
    assert all(s["obs"]["reaped"] == [] and s["obs"]["lease_state"] == "ALIVE" and s["obs"]["status"] == "RUNNING"
               for s in seen)
    assert all(s["hb_age"] <= IN.REPLAY_LEASE_STALE_SEC for s in seen)  # 거래일·계좌마다 갱신
    assert one(shadow, "SELECT status FROM ab_runs WHERE run_id=?", res["run"])["status"] == "CURRENT"


def test_live_but_unresponsive_worker_is_blocked_and_warned_not_taken_over(shadow, prod):
    full = _replay_ready(shadow, prod)
    run = _building(shadow, {"heartbeat_at": time.time() - IN.REPLAY_LEASE_STALE_SEC - 60})
    o = observe(shadow, time.time())
    assert o == {"reaped": [], "lease_state": "STALLED", "status": "STALLED"}
    out = job(shadow, prod, full, evening(D3))
    assert out["replay"] is None and "자동 탈취하지 않음" in out["waiting"] and out["days"] == []
    assert one(shadow, "SELECT status FROM ab_runs WHERE run_id=?", run)["status"] == "BUILDING"
    assert one(shadow, "SELECT COUNT(*) n FROM ab_events WHERE kind='replay_stalled' AND severity='WARN'")["n"] == 1
    s5 = RP.summary(shadow)["s5"]["profiles"]["P7C"]
    assert s5["safety_breakdown"]["operational"]["replay_stalled"] == 1 and not s5["safety_pass"]


# ── 사망 판정: pid 재사용·다른 호스트·시작 식별자 확인 불가·SIGKILL ─────────────

@pytest.mark.parametrize("case,state", [
    ("pid_reused_other_start", "DEAD"),       # 살아 있는 pid 이지만 시작 식별자가 다름 → 다른 프로세스
    ("pid_reused_self", "DEAD"),              # 이 프로세스가 같은 pid 를 다른 토큰으로 가짐
    ("rebooted", "DEAD"),
    ("gone", "DEAD"),
    ("remote_fresh", "REMOTE_ACTIVE"),
    ("remote_stale", "REMOTE_STALE"),
])
def test_lease_death_requires_evidence(shadow, prod, case, state):
    _replay_ready(shadow, prod)
    stale = time.time() - IN.REPLAY_LEASE_STALE_SEC - 60
    over = {"pid_reused_other_start": {"proc_start": "ps:Mon Jan  1 00:00:00 2001"},
            "pid_reused_self": {"pid": os.getpid()},
            "rebooted": {"boot_id": "linux:00000000-old-boot"},
            "gone": {"pid": _dead_pid()},
            "remote_fresh": {"host": "other-host.invalid", "pid": 1},
            "remote_stale": {"host": "other-host.invalid", "pid": 1, "heartbeat_at": stale}}[case]
    if case == "rebooted" and not IN.boot_id():
        pytest.skip("이 환경은 boot 식별자를 읽을 수 없음")
    if case == "pid_reused_other_start" and not IN.proc_start_id(os.getppid()):
        pytest.skip("이 환경은 프로세스 시작 식별자를 읽을 수 없음")
    run = _building(shadow, over)
    if case == "pid_reused_self":          # '같은 pid' 는 판정하는 프로세스 기준 — 이 프로세스에서 정리한다
        db = connect_rw(shadow.journal_path)
        try:
            c = IN.classify_lease(get_runtime(db, "replay_in_progress"))
            o = {"lease_state": c["state"], "reaped": IN.reap_abandoned_builds(db)}
        finally:
            db.close()
    else:
        o = observe(shadow, time.time())
    assert o["lease_state"] == state
    status = one(shadow, "SELECT status FROM ab_runs WHERE run_id=?", run)["status"]
    if state == "DEAD":
        assert o["reaped"] == [run] and status == "FAILED"
        assert json.loads(one(shadow, "SELECT summary FROM ab_runs WHERE run_id=?", run)["summary"])["death_evidence"]
    else:
        assert o["reaped"] == [] and status == "BUILDING"                 # 다른 호스트는 임의로 사망 처리하지 않음


def test_start_identity_unavailable_is_conservative(shadow, prod, monkeypatch):
    _replay_ready(shadow, prod)
    monkeypatch.setattr(IN, "proc_start_id", lambda pid: None)
    lease = {"run_id": 1, "host": socket.gethostname(), "pid": os.getppid(), "token": "x", "boot_id": IN.boot_id(),
             "heartbeat_at": time.time()}
    assert IN.classify_lease(lease)["state"] == "ALIVE_UNVERIFIED"
    assert IN.classify_lease({**lease, "heartbeat_at": time.time() - 2000})["state"] == "STALLED_UNVERIFIED"
    run = _building(shadow, {"proc_start": None, "heartbeat_at": time.time() - 2000})
    db = connect_rw(shadow.journal_path)
    try:
        assert IN.reap_abandoned_builds(db) == []                        # pid 재사용 판정 불가 → 정리하지 않음
    finally:
        db.close()
    assert one(shadow, "SELECT status FROM ab_runs WHERE run_id=?", run)["status"] == "BUILDING"


def test_sigkill_during_replay_is_detected_reaped_and_retried(shadow, prod):
    full = _replay_ready(shadow, prod)
    parent = one(shadow, "SELECT run_id FROM ab_runs WHERE status='CURRENT'")["run_id"]
    p = subprocess.Popen([sys.executable, "-c", WORKER, shadow.journal_path], cwd=REPO, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, env=dict(os.environ))
    try:
        line = p.stdout.readline()
        assert line.startswith("DAY"), p.stderr.read()[-2000:]
        run = int(line.split()[1])
        o = observe(shadow, time.time())
        assert o["lease_state"] == "ALIVE" and o["reaped"] == []         # 살아 있는 동안은 보호
        os.kill(p.pid, signal.SIGKILL)
        p.wait(timeout=30)
    finally:
        if p.poll() is None:
            p.kill()
    assert one(shadow, "SELECT status FROM ab_runs WHERE run_id=?", run)["status"] == "BUILDING"
    assert one(shadow, "SELECT run_id FROM ab_runs WHERE status='CURRENT'")["run_id"] == parent
    o = observe(shadow, time.time())
    assert o["lease_state"] == "DEAD" and o["reaped"] == [run]
    summ = json.loads(one(shadow, "SELECT summary FROM ab_runs WHERE run_id=?", run)["summary"])
    assert summ["reason"] == "ABANDONED_BUILD" and summ["death_evidence"] == "PROCESS_GONE"
    assert one(shadow, "SELECT COUNT(*) n FROM ab_daily WHERE run_id=?", run)["n"] > 0     # 감사 이력 보존
    out = job(shadow, prod, full, evening(D3))
    assert out["replay"] and one(shadow, "SELECT status FROM ab_runs WHERE run_id=?", out["replay"]["run"])["status"] \
        == "CURRENT"


# ── 실행권 상실 후 이전 작업자 재개 (fencing) ──────────────────────────────

def test_old_worker_resuming_after_lease_loss_cannot_write_or_become_current(shadow, prod, monkeypatch):
    full = _replay_ready(shadow, prod)
    real_build = IN.build_inputs
    st = {"n": 0, "b": None}

    def build(db, run, day, bars, settings=None):
        st["n"] += 1
        if st["n"] == 2:                  # A 가 멈춘 사이: 운영자가 실행권 해제 → B 가 재계산 완료
            other = connect_rw(shadow.journal_path)
            try:
                # A 를 '다른 호스트에서 응답 없는 작업자'로 만든다 (수동 해제가 허용되는 유일한 경우)
                with tx(other):
                    lease = get_runtime(other, "replay_in_progress")
                    lease.update(host="worker-a.invalid", token="worker-a", heartbeat_at=time.time() - 3600)
                    set_runtime(other, "replay_in_progress", lease)
                ev = {"host": "worker-a.invalid", "pid": lease["pid"], "proc_start": lease["proc_start"],
                      "confirmed_stopped_at": "2026-11-27T10:00:00Z", "method": "테스트: 원격 호스트 프로세스 목록 확인"}
                assert IN.release_stalled_lease(other, run=run, token="worker-a", operator="test-operator",
                                                reason="테스트: 작업자 응답 없음 확인 후 해제", evidence=ev)["released"]
                monkeypatch.setattr(IN, "build_inputs", real_build)
                st["b"] = IN.replay(other, full, "BARS_RECOVERED", shadow)["run"]
                monkeypatch.setattr(IN, "build_inputs", build)
            finally:
                other.close()
        return real_build(db, run, day, bars, settings)
    monkeypatch.setattr(IN, "build_inputs", build)
    db = connect_rw(shadow.journal_path)
    try:
        with pytest.raises(IN.ReplayLeaseLost):
            IN.replay(db, full, "BARS_RECOVERED", shadow)
        a = db.execute("SELECT run_id FROM ab_runs WHERE status='FAILED' ORDER BY run_id DESC").fetchone()[0]
        # 파이썬 확인을 건너뛰어도 DB fencing 이 막는다
        with pytest.raises(Exception, match="run fenced"):
            with tx(db):
                db.execute("INSERT INTO ab_days(run_id, day, status, decision_status, candidates, bars_requested, "
                           "bars_missing, notes, processed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                           (a, "2026-12-01", "COMPLETE", "FROZEN", 0, 0, 0, "[]", 0))
        with pytest.raises(Exception, match="run fenced"):
            with tx(db):
                db.execute("UPDATE ab_orders SET state='FILLED' WHERE run_id=?", (a,))
                db.execute("UPDATE ab_daily SET equity=0 WHERE run_id=?", (a,))
        with pytest.raises(Exception, match="terminal|only one CURRENT"):
            with tx(db):
                db.execute("UPDATE ab_runs SET status='CURRENT' WHERE run_id=?", (a,))
    finally:
        db.close()
    assert one(shadow, "SELECT run_id FROM ab_runs WHERE status='CURRENT'")["run_id"] == st["b"]
    assert json.loads(one(shadow, "SELECT summary FROM ab_runs WHERE run_id=?", a)["summary"])["reason"] \
        == "ABANDONED_MANUAL"
    assert one(shadow, "SELECT COUNT(*) n FROM ab_events WHERE kind='replay_lease_lost'")["n"] == 1


def test_concurrent_replays_only_one_becomes_current(shadow, prod):
    full = _replay_ready(shadow, prod)
    parent = one(shadow, "SELECT run_id FROM ab_runs WHERE status='CURRENT'")["run_id"]
    barrier, results = threading.Barrier(2), []

    def worker():
        db = connect_rw(shadow.journal_path)
        try:
            barrier.wait()
            res = IN.replay(db, full, "BARS_RECOVERED", shadow)
            results.append(("skipped", res["skipped"]) if res.get("skipped") else ("ok", res["run"]))
        except IN.ReplayBusy as exc:
            results.append(("busy", str(exc)))
        except Exception as exc:  # pragma: no cover - 실패 내용 확인용
            results.append(("error", repr(exc)))
        finally:
            db.close()
    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(120)
    kinds = sorted(r[0] for r in results)
    assert kinds in (["busy", "ok"], ["ok", "skipped"]), results      # 같은 요청은 한 번만 성공 (동시=busy, 순차=skipped)
    winner = next(r[1] for r in results if r[0] == "ok")
    runs = {r["run_id"]: r["status"] for r in q(shadow, "SELECT * FROM ab_runs")}
    assert [k for k, v in runs.items() if v == "CURRENT"] == [winner] and runs[parent] == "SUPERSEDED"
    assert "BUILDING" not in runs.values()
    db = connect_rw(shadow.journal_path)
    try:                                            # CURRENT 전환 경쟁: 두 번째 CURRENT 는 DB 가 거부
        with pytest.raises(Exception, match="only one CURRENT|terminal"):
            with tx(db):
                db.execute("UPDATE ab_runs SET status='CURRENT' WHERE run_id=?", (parent,))
    finally:
        db.close()


def test_heartbeat_write_failure_warns_and_continues(shadow, prod, monkeypatch, caplog):
    import contextlib
    import sqlite3
    full = _replay_ready(shadow, prod)
    real_tx, calls = IN.tx, {"n": 0}

    @contextlib.contextmanager
    def flaky_tx(db):
        calls["n"] += 1
        if calls["n"] == 2:                       # 두 번째 heartbeat 갱신: DB 잠김
            raise sqlite3.OperationalError("database is locked")
        with real_tx(db):
            yield db
    db = connect_rw(shadow.journal_path)
    try:
        orig = IN._heartbeat

        def hb(dbx, run):
            monkeypatch.setattr(IN, "tx", flaky_tx)
            try:
                orig(dbx, run)
            finally:
                monkeypatch.setattr(IN, "tx", real_tx)
        monkeypatch.setattr(IN, "_heartbeat", hb)
        with caplog.at_level("WARNING"):
            res = IN.replay(db, full, "BARS_RECOVERED", shadow)
    finally:
        db.close()
    assert "heartbeat 갱신 실패" in caplog.text
    assert one(shadow, "SELECT status FROM ab_runs WHERE run_id=?", res["run"])["status"] == "CURRENT"
