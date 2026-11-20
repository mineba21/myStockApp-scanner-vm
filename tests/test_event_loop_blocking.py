"""Blocking broker/market I/O must not run on the FastAPI event loop."""
import asyncio
import inspect
import time

import httpx
import pytest


@pytest.fixture()
def webapp(monkeypatch):
    import database.models as dbm
    import scheduler as sched

    monkeypatch.setattr(dbm, "init_db", lambda: None)
    monkeypatch.setattr(sched, "start_scheduler", lambda: None)
    monkeypatch.setattr(sched, "stop_scheduler", lambda: None)
    monkeypatch.setattr(sched, "get_next_run_times", lambda: {})
    import web.app as app_module

    monkeypatch.setattr(app_module, "SITES_API_KEY", "test-key")
    monkeypatch.setattr(app_module, "KIWOOM_WEB_ENABLED", True)
    return app_module


def test_slow_broker_call_does_not_stall_health_check(webapp, monkeypatch):
    def slow_summaries(**kwargs):
        time.sleep(0.6)  # stands in for several sequential broker HTTP calls
        return []

    monkeypatch.setattr(webapp, "get_kiwoom_account_summaries", slow_summaries)

    async def scenario():
        transport = httpx.ASGITransport(app=webapp.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                     headers={"Authorization": "Bearer test-key"}) as client:
            slow = asyncio.create_task(client.get("/api/portfolio-summary"))
            started = time.monotonic()
            await asyncio.sleep(0.1)  # a free loop wakes after ~0.1s; a blocked one after ~0.6s
            loop_delay = time.monotonic() - started
            health = await client.get("/api/health")
            await slow
            return health.status_code, loop_delay

    status, loop_delay = asyncio.run(scenario())
    assert status == 200
    assert loop_delay < 0.3, f"event loop was blocked for {loop_delay:.2f}s by a broker call"


@pytest.mark.parametrize("module,name", [
    ("web.app", "get_results"), ("web.app", "list_holdings"), ("web.app", "portfolio_summary"),
    ("web.app", "get_chart_ohlcv"), ("web.app", "get_exchange_rate"), ("web.app", "get_market_status"),
    ("web.kiwoom_order_api", "quote_sell"), ("web.kiwoom_order_api", "quote_buy"),
])
def test_blocking_read_handlers_are_sync_so_they_run_in_the_threadpool(webapp, module, name):
    import importlib

    handler = getattr(importlib.import_module(module), name)
    assert not inspect.iscoroutinefunction(handler)
