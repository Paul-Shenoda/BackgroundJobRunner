"""Criterion 1: submitting a job returns a handle before the job runs."""

import time

from tests.helpers import cancel_and_confirm, submit


async def test_enqueue_returns_handle_before_completion(client_a):
    start = time.monotonic()
    task = await submit(client_a, "scrape", {"duration_ms": 10000})
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, f"POST /tasks took {elapsed:.3f}s, expected well under 1s"
    assert task["status"] == "queued"
    assert task["handle"] == "scrape-1"

    await cancel_and_confirm(client_a, task["handle"], task["created_at"])
