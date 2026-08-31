import json
import os
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def client(monkeypatch, tmp_path):
    import database.models as dbm
    import scheduler as sched

    monkeypatch.setattr(dbm, "init_db", lambda: None)
    monkeypatch.setattr(sched, "start_scheduler", lambda: None)
    monkeypatch.setattr(sched, "stop_scheduler", lambda: None)
    monkeypatch.setattr(sched, "get_next_run_times", lambda: {})

    import web.app as webapp
    import web.asset_allocation_api as allocation_api

    monkeypatch.setattr(webapp, "SITES_API_KEY", "test-key")
    monkeypatch.setattr(allocation_api, "ASSET_ALLOCATION_DIR", tmp_path)
    monkeypatch.setattr(allocation_api, "ASSET_ALLOCATION_CACHE_DIR", tmp_path / "cache")
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").write_text("python", encoding="utf-8")
    (tmp_path / "asset_allocation.py").write_text("# test", encoding="utf-8")
    allocation_api._allocation_runtime.clear()

    with TestClient(webapp.app, headers={"Authorization": "Bearer test-key"}) as test_client:
        yield test_client, allocation_api
    allocation_api._allocation_runtime.clear()


def test_get_returns_empty_before_first_calculation(client):
    test_client, _ = client
    response = test_client.get("/api/asset-allocation?profile=easy&as_of=2026-08-31")
    assert response.status_code == 200
    assert response.json()["status"] == "empty"
    assert response.json()["report"] is None


@pytest.mark.parametrize("query", [
    "profile=bad&as_of=2026-08-31",
    "profile=easy&as_of=2026-08-30",
    "profile=easy&as_of=not-a-date",
])
def test_invalid_parameters_are_rejected(client, query):
    test_client, _ = client
    response = test_client.get(f"/api/asset-allocation?{query}")
    assert response.status_code == 422


def test_refresh_executes_fixed_command_and_persists_cache(client, monkeypatch):
    test_client, webapp = client
    report = {
        "requested_as_of": "2026-08-31",
        "status": "PREVIEW",
        "combined_allocations": {"IEMG": 1 / 3, "EFA": 1 / 3},
    }
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(report), stderr="")

    monkeypatch.setattr(webapp.subprocess, "run", fake_run)
    response = test_client.post(
        "/api/asset-allocation/refresh?profile=easy&as_of=2026-08-31"
    )
    assert response.status_code == 202

    ready = test_client.get(
        "/api/asset-allocation?profile=easy&as_of=2026-08-31"
    ).json()
    assert ready["status"] == "ready"
    assert ready["report"] == report
    assert calls[0][0][-1] == "--json"
    assert calls[0][1]["check"] is False
    assert calls[0][1]["timeout"] == webapp.ASSET_ALLOCATION_TIMEOUT_SECONDS
