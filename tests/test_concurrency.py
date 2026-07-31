"""Criteria 7 and 8: the concurrency limit holds under load, and multiple
near-simultaneous completions all land correctly."""

import asyncio
import time

from tests.helpers import submit, wait_for_status


async def test_concurrency_limit_respected(client_a):
    submitted = [await submit(client_a, "scrape", {"duration_ms": 1500}) for _ in range(5)]
    handles = {t["handle"] for t in submitted}

    max_running = 0
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        resp = await client_a.get("/tasks", params={"status": "running"})
        assert resp.status_code == 200
        running = resp.json()
        max_running = max(max_running, len(running))
        assert len(running) <= 2, f"observed {len(running)} tasks running simultaneously; limit is 2"

        all_resp = await client_a.get("/tasks")
        statuses = {t["handle"]: t["status"] for t in all_resp.json()}
        if all(statuses.get(h) in ("ready", "failed") for h in handles):
            break
        await asyncio.sleep(0.2)
    else:
        raise AssertionError("not all 5 tasks reached a terminal state within 20s")

    # >= 2, not >= 1: the limit must actually be REACHED, not merely never
    # exceeded — a pool hardcoded to concurrency 1 would sail through the
    # <= 2 assertions above and prove nothing about parallelism. With 5
    # queued 1.5s jobs and a 0.2s poll, both slots being full is observable
    # on many consecutive polls, so this is not a timing-lottery assertion.
    assert max_running >= 2, (
        f"never observed both slots in use (max seen: {max_running}) — the pool "
        f"is not actually running 2 jobs in parallel"
    )


async def test_concurrent_completions_all_land(client_a):
    # a and b start immediately (concurrency=2) and run long enough that c
    # is still queued behind them; c then claims a freed slot and finishes
    # shortly after b, clustering all three completions close together.
    a = await submit(client_a, "scrape", {"duration_ms": 800})
    b = await submit(client_a, "scrape", {"duration_ms": 900})
    c = await submit(client_a, "scrape", {"duration_ms": 300})

    results = await asyncio.gather(
        wait_for_status(client_a, a["handle"], {"ready", "failed"}, a["created_at"], timeout=15),
        wait_for_status(client_a, b["handle"], {"ready", "failed"}, b["created_at"], timeout=15),
        wait_for_status(client_a, c["handle"], {"ready", "failed"}, c["created_at"], timeout=15),
    )

    for task, final in zip((a, b, c), results):
        assert final["status"] == "ready", f"{task['handle']} ended {final['status']!r}, not ready"

    # None left stuck in running/queued afterward.
    resp = await client_a.get("/tasks")
    by_handle = {t["handle"]: t["status"] for t in resp.json()}
    for task in (a, b, c):
        assert by_handle[task["handle"]] == "ready"
