"""Criterion 2: full lifecycle to ready, and collection semantics."""

from tests.helpers import cancel_and_confirm, submit, wait_for_status


async def test_lifecycle_to_ready_and_collect(client_a):
    task = await submit(client_a, "scrape", {"duration_ms": 1500})
    handle, created_at = task["handle"], task["created_at"]

    await wait_for_status(client_a, handle, {"running"}, created_at, timeout=10)
    ready = await wait_for_status(client_a, handle, {"ready"}, created_at, timeout=10)
    assert ready["result"] is not None
    assert ready["error"] is None
    assert ready["collected"] is False

    resp = await client_a.get(f"/tasks/{handle}/result")
    assert resp.status_code == 200
    body = resp.json()
    assert body["collected"] is True
    assert body["result"] is not None
    assert body["error"] is None
    assert body["status"] == "ready"


async def test_collect_twice_conflicts(client_a):
    task = await submit(client_a, "scrape", {"duration_ms": 500})
    handle, created_at = task["handle"], task["created_at"]
    await wait_for_status(client_a, handle, {"ready"}, created_at, timeout=10)

    first = await client_a.get(f"/tasks/{handle}/result")
    assert first.status_code == 200

    second = await client_a.get(f"/tasks/{handle}/result")
    assert second.status_code == 409


async def test_result_before_ready_conflicts(client_a):
    task = await submit(client_a, "scrape", {"duration_ms": 10000})
    handle, created_at = task["handle"], task["created_at"]

    # Still queued (or, briefly, running) — either way not collectable yet.
    resp = await client_a.get(f"/tasks/{handle}/result")
    assert resp.status_code == 409
    assert resp.json()["detail"]  # a specific reason, not a bare code

    await cancel_and_confirm(client_a, handle, created_at)
