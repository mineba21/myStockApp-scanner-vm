"""PR #14 Codex 재리뷰(head a183b99) 반영 (3) — Yahoo 429 집계 원자성 (P2).

``_record_rate_limit`` 는 이전 상태 조회 → 증가/초기화 → 경고 임계값 판단 → 감사 이벤트 → runtime 갱신을 한 쓰기
트랜잭션(BEGIN IMMEDIATE)에서 한다. 두 독립 연결이 동시에 RATE_LIMITED 를 기록해도 증가분이 유실되지 않는다.
경쟁은 조회 직후 두 작업자가 만나도록 장벽(barrier)으로 강제한다 (잠금을 잡은 쪽은 장벽 시간 초과 후 진행).
"""
import threading

import pytest

from tests.test_auto_buy_shadow import shadow  # noqa: F401
from trading.auto_buy.shadow import runner as RN
from trading.auto_buy.shadow.store import connect_rw, get_runtime, set_runtime, tx

LIMITED = {"requests": 1, "halted": "RATE_LIMITED", "contract_ok": 0}
CONFIRMED = {"requests": 2, "halted": None, "contract_ok": 1}


def seed(path, n, warned=False):
    db = connect_rw(path)
    try:
        with tx(db):
            set_runtime(db, "yahoo_rate_limit", {"consecutive_jobs": n, "last_limited_at": 1.0, "warned": warned})
    finally:
        db.close()


def read(path):
    db = connect_rw(path)
    try:
        return (get_runtime(db, "yahoo_rate_limit"),
                db.execute("SELECT COUNT(*) FROM ab_events WHERE kind='yahoo_rate_limited_repeated'").fetchone()[0])
    finally:
        db.close()


def race(path, monkeypatch, fetches, job_ids=None):
    """각 작업자가 상태를 읽은 직후 장벽에서 만나게 한 뒤 기록한다."""
    barrier = threading.Barrier(len(fetches), timeout=1.0)
    real = RN.get_runtime

    def meet(db, key, *a, **k):
        v = real(db, key, *a, **k)
        if key == "yahoo_rate_limit":
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
        return v
    monkeypatch.setattr(RN, "get_runtime", meet)
    errs = []

    def work(i, f):
        db = connect_rw(path)
        try:
            RN._record_rate_limit(db, f, *([job_ids[i]] if job_ids else []))
        except Exception as exc:          # noqa: BLE001
            errs.append(repr(exc))
        finally:
            db.close()
    ts = [threading.Thread(target=work, args=(i, f)) for i, f in enumerate(fetches)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    monkeypatch.setattr(RN, "get_runtime", real)
    assert not errs, errs


def test_two_connections_rate_limited_concurrently_lose_nothing(shadow, monkeypatch):
    seed(shadow.journal_path, 1)
    race(shadow.journal_path, monkeypatch, [LIMITED, LIMITED])
    st, warns = read(shadow.journal_path)
    assert st["consecutive_jobs"] == 3 and st["warned"] is True and warns == 1


@pytest.mark.parametrize("rounds", [3])
def test_repeated_concurrent_increments(shadow, monkeypatch, rounds):
    seed(shadow.journal_path, 0)
    for _ in range(rounds):
        race(shadow.journal_path, monkeypatch, [LIMITED, LIMITED])
    st, warns = read(shadow.journal_path)
    assert st["consecutive_jobs"] == 2 * rounds and warns == 1


def test_confirmed_and_limited_race_is_serializable(shadow, monkeypatch):
    seed(shadow.journal_path, 2)
    race(shadow.journal_path, monkeypatch, [LIMITED, CONFIRMED])
    st, warns = read(shadow.journal_path)
    # 두 순서 중 하나의 결과여야 한다 (섞인 값 없음):
    #   제한 → 정상: 3 (경고 1건) 후 초기화 → (0, 경고 1) / 정상 → 제한: 0 후 1 → (1, 경고 0)
    assert (st["consecutive_jobs"], st["warned"], warns) in ((0, False, 1), (1, False, 0))


def test_same_job_recorded_twice_counts_once_distinct_jobs_count_each(shadow):
    seed(shadow.journal_path, 0)
    db = connect_rw(shadow.journal_path)
    try:
        RN._record_rate_limit(db, LIMITED, "job-a")
        RN._record_rate_limit(db, LIMITED, "job-a")          # 같은 job 재기록
        assert get_runtime(db, "yahoo_rate_limit")["consecutive_jobs"] == 1
        RN._record_rate_limit(db, LIMITED, "job-b")
        RN._record_rate_limit(db, LIMITED, "job-c")
        st = get_runtime(db, "yahoo_rate_limit")
        assert st["consecutive_jobs"] == 3 and st["warned"] is True
        RN._record_rate_limit(db, CONFIRMED, "job-d")
        assert get_runtime(db, "yahoo_rate_limit")["consecutive_jobs"] == 0
    finally:
        db.close()


def test_failure_inside_transaction_rolls_back_and_rerun_counts_once(shadow, monkeypatch):
    seed(shadow.journal_path, 2)
    real = RN.event

    def boom(*a, **k):
        raise RuntimeError("event write failed")
    monkeypatch.setattr(RN, "event", boom)
    db = connect_rw(shadow.journal_path)
    try:
        with pytest.raises(RuntimeError):
            RN._record_rate_limit(db, LIMITED, "job-x")
        assert not db.in_transaction
    finally:
        db.close()
    st, warns = read(shadow.journal_path)
    assert st["consecutive_jobs"] == 2 and warns == 0                    # 증가·경고 모두 롤백
    monkeypatch.setattr(RN, "event", real)
    db = connect_rw(shadow.journal_path)
    try:
        RN._record_rate_limit(db, LIMITED, "job-x")                       # 같은 job 재실행: 1회만
        RN._record_rate_limit(db, LIMITED, "job-x")
    finally:
        db.close()
    st, warns = read(shadow.journal_path)
    assert st["consecutive_jobs"] == 3 and warns == 1


@pytest.mark.parametrize("fetch", [{}, {"requests": 0, "contract_ok": 0, "halted": None},
                                   {"requests": 3, "contract_ok": 0, "halted": None},
                                   {"requests": 5, "contract_ok": 0, "halted": "CIRCUIT_OPEN"}])
def test_existing_policies_kept(shadow, fetch):
    seed(shadow.journal_path, 2, warned=True)
    before = read(shadow.journal_path)
    db = connect_rw(shadow.journal_path)
    try:
        RN._record_rate_limit(db, fetch, "job-z")
    finally:
        db.close()
    assert read(shadow.journal_path) == before
