import pytest

from trading.auto_buy_us.schema import upgrade_journal
from trading.auto_buy_us.store import LeaseLost, Store


@pytest.fixture
def store(tmp_path):
    p = str(tmp_path / "j.sqlite")
    upgrade_journal(p, allow_create=True)
    return Store(p)


def _events(store):
    with store.read() as db:
        return db.execute("SELECT * FROM abu_system_events").fetchall()


def test_connect_does_not_create_file(tmp_path):
    p = tmp_path / "none.sqlite"
    with pytest.raises(FileNotFoundError):
        Store(str(p)).connect()
    assert not p.exists()


def test_tx_commits_and_rolls_back_on_error(store):
    with store.tx() as db:
        store.event(db, "INFO", "ok", 1.0)
    with pytest.raises(RuntimeError):
        with store.tx() as db:
            store.event(db, "WARN", "boom", 2.0)
            raise RuntimeError("x")
    rows = _events(store)
    assert [r["kind"] for r in rows] == ["ok"]


def test_event_details_and_level_validation(store):
    with store.tx() as db:
        store.event(db, "URGENT", "k", 5.0, a="한글", b=1)
        with pytest.raises(ValueError):
            store.event(db, "DEBUG", "k", 5.0)
    r = _events(store)[0]
    assert r["details_json"] == '{"a": "한글", "b": 1}' and r["notified"] == 0


def test_lease_exclusive(store):
    assert store.acquire_lease("a", 100, 90) and not store.acquire_lease("b", 150, 90)
    assert not store.acquire_lease("b", 189, 90)
    assert store.acquire_lease("b", 190, 90)  # until 시각 정각은 만료
    assert store.acquire_lease("b", 191, 90)  # 같은 소유자 재획득


def test_renew_after_takeover_raises(store):
    assert store.acquire_lease("a", 100, 90)
    assert store.acquire_lease("b", 191, 90)
    with pytest.raises(LeaseLost):
        store.renew_lease("a", 192, 90)
    store.renew_lease("b", 200, 90)


def test_renew_expired_raises(store):
    store.acquire_lease("a", 100, 90)
    with pytest.raises(LeaseLost):
        store.renew_lease("a", 190, 90)


def test_release_only_owner(store):
    store.acquire_lease("a", 100, 90)
    store.release_lease("b")
    assert not store.acquire_lease("b", 110, 90)
    store.release_lease("a")
    assert store.acquire_lease("b", 110, 90)


def test_count_api_accumulates(store):
    with store.tx() as db:
        store.count_api(db, 1_700_000_000.0, "ka10001", "ok")
        store.count_api(db, 1_700_000_030.0, "ka10001", "ok")
        store.count_api(db, 1_700_000_030.0, "ka10001", "http_429", 3)
        with pytest.raises(ValueError):
            store.count_api(db, 1_700_000_030.0, "ka10001", "bad")
    with store.read() as db:
        rows = {(r["minute"], r["outcome"]): r["n"] for r in db.execute("SELECT * FROM abu_api_calls")}
    assert rows == {("2023-11-14T22:13", "ok"): 2, ("2023-11-14T22:13", "http_429"): 3}


def test_runtime_roundtrip(store):
    with store.tx() as db:
        assert store.get_runtime(db, "k") is None
        store.set_runtime(db, "k", {"x": 1})
        store.set_runtime(db, "k2", [1, 2])
    with store.read() as db:
        assert store.get_runtime(db, "k") == {"x": 1}
        assert store.get_runtime(db, "k2") == [1, 2]
    with store.tx() as db:
        store.set_runtime(db, "k", {"x": 2})
        assert store.get_runtime(db, "k") == {"x": 2}
