"""Criterion 6: cancellation actually stops the worker, for both queued and
running jobs."""

import asyncio

from tests.helpers import cancel_and_confirm, submit, wait_for_status


async def test_cancel_running_task(client_a):
    # The brief's scenario uses duration_ms=30000; scaled down to 6000 here
    # so this test finishes in single-digit seconds rather than tying up the
    # suite for over half a minute — the property under test (no late
    # 'ready' after the job's would-be completion time) doesn't depend on
    # the literal duration, only on waiting past it.
    task = await submit(client_a, "scrape", {"duration_ms": 6000})
    handle, created_at = task["handle"], task["created_at"]

    await wait_for_status(client_a, handle, {"running"}, created_at, timeout=10)

    resp = await client_a.post(f"/tasks/{handle}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    # Wait past when the job would have finished if it hadn't actually
    # stopped, then confirm it's still cancelled, not ready.
    await asyncio.sleep(7.0)
    final = await client_a.get(f"/tasks/{handle}")
    assert final.status_code == 200
    body = final.json()
    assert body["created_at"] == created_at
    assert body["status"] == "cancelled", (
        f"expected the cancelled job to stay cancelled, got {body['status']!r} — "
        f"the worker did not actually stop"
    )
    assert body["result"] is None


async def test_cancel_queued_task(client_a):
    filler_a = await submit(client_a, "scrape", {"duration_ms": 8000})
    filler_b = await submit(client_a, "scrape", {"duration_ms": 8000})
    await wait_for_status(client_a, filler_a["handle"], {"running"}, filler_a["created_at"], timeout=10)
    await wait_for_status(client_a, filler_b["handle"], {"running"}, filler_b["created_at"], timeout=10)

    extra = await submit(client_a, "scrape", {"duration_ms": 500})
    # Pool is saturated (concurrency=2, both fillers running) — this must
    # stay queued rather than being claimed.
    still_queued = await client_a.get(f"/tasks/{extra['handle']}")
    assert still_queued.status_code == 200
    body = still_queued.json()
    assert body["created_at"] == extra["created_at"]
    assert body["status"] == "queued", f"expected {extra['handle']} to stay queued, got {body['status']!r}"

    await cancel_and_confirm(client_a, extra["handle"], extra["created_at"])

    await cancel_and_confirm(client_a, filler_a["handle"], filler_a["created_at"])
    await cancel_and_confirm(client_a, filler_b["handle"], filler_b["created_at"])


async def test_cancel_terminal_task_conflicts(client_a):
    task = await submit(client_a, "scrape", {"duration_ms": 300})
    await wait_for_status(client_a, task["handle"], {"ready"}, task["created_at"], timeout=10)

    resp = await client_a.post(f"/tasks/{task['handle']}/cancel")
    assert resp.status_code == 409
