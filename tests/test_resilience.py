"""The worker pool survives a temporary database outage, and /health reports a stopped engine."""

import asyncio

import asyncpg

from tests.helpers import submit, wait_for_status


async def test_dispatch_survives_transient_db_outage(app, client_a, monkeypatch):
    """Postgres restarting for maintenance used to kill the dispatch loop for good.
    Two 'database is shutting down' errors in a row must only delay claiming."""
    store = app.state.engine._pool._store
    real_claim_next = store.claim_next
    failures = {"left": 2}

    async def flaky_claim_next(*args, **kwargs):
        if failures["left"] > 0:
            failures["left"] -= 1
            raise asyncpg.exceptions.CannotConnectNowError("the database system is shutting down")
        return await real_claim_next(*args, **kwargs)

    monkeypatch.setattr(store, "claim_next", flaky_claim_next)

    task = await submit(client_a, "scrape", {"duration_ms": 200})
    ready = await wait_for_status(client_a, task["handle"], {"ready"}, task["created_at"], timeout=20)
    assert ready["status"] == "ready"
    assert failures["left"] == 0  # the outage really happened

    health = await client_a.get("/health")
    assert health.status_code == 200
    assert health.json()["engine"] == "running"


async def test_reaper_survives_transient_db_outage(app, monkeypatch):
    store = app.state.engine._pool._store
    real_reap = store.reap_expired_leases
    calls = {"failed": 0, "ok": 0}

    async def flaky_reap(*args, **kwargs):
        if calls["failed"] == 0:
            calls["failed"] += 1
            raise ConnectionRefusedError("connection refused")
        calls["ok"] += 1
        return await real_reap(*args, **kwargs)

    monkeypatch.setattr(store, "reap_expired_leases", flaky_reap)

    # The reaper ticks every LEASE_SECONDS / 2 (5s in tests): wait for a failed tick and a good one.
    for _ in range(150):
        if calls["ok"] >= 1:
            break
        await asyncio.sleep(0.1)
    assert calls["failed"] == 1 and calls["ok"] >= 1
    assert app.state.engine._pool.is_running()


async def test_health_is_503_when_engine_stopped(app, anon_client, monkeypatch):
    monkeypatch.setattr(app.state.engine._pool, "is_running", lambda: False)
    resp = await anon_client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["engine"] == "stopped"
    # The dashboard reads heartbeat_interval from this body even on 503.
    assert "heartbeat_interval" in resp.json()
