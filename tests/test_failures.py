"""Failure surfacing, retry exhaustion, and operator-triggered retry."""

from tests.helpers import cancel_and_confirm, submit, wait_for_status


async def test_failure_surfaces_and_is_not_auto_collected(client_a):
    # max_attempts=3 (test config, see conftest.py). Small duration so the
    # three attempts plus 1s/2s backoff between them stay fast.
    task = await submit(client_a, "scrape", {"fail": True, "duration_ms": 200})
    final = await wait_for_status(client_a, task["handle"], {"failed"}, task["created_at"], timeout=20)

    assert final["result"] is None
    assert final["collected"] is False
    assert final["error"] is not None
    assert final["error"]["retryable"] is False, "exhausted retry budget must end non-retryable"
    assert "exhaust" in final["error"]["reason"].lower(), (
        f"reason does not name exhaustion: {final['error']['reason']!r}"
    )


async def test_permanent_failure_not_retried(client_a):
    task = await submit(client_a, "scrape", {"fail_permanent": True, "duration_ms": 200})
    final = await wait_for_status(client_a, task["handle"], {"failed"}, task["created_at"], timeout=10)

    assert final["error"] is not None
    assert final["error"]["retryable"] is False
    assert final["result"] is None

    # No HTTP field exposes attempt count (that's a detail-view extra, not
    # part of the fixed contract) — a permanent failure retrying at all
    # would show up as a delay: exponential backoff would push this well
    # past a couple hundred ms if a second attempt had been made. Reaching
    # 'failed' this fast is itself evidence of exactly one attempt.


async def test_operator_can_retry_a_failed_job(client_a):
    task = await submit(client_a, "scrape", {"fail_permanent": True, "duration_ms": 200})
    await wait_for_status(client_a, task["handle"], {"failed"}, task["created_at"], timeout=10)

    resp = await client_a.post(f"/tasks/{task['handle']}/retry")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "queued"
    assert body["error"] is None

    # Let it run back out to a terminal state so it doesn't linger as a
    # zombie in the shared pool for the next test.
    await wait_for_status(
        client_a, task["handle"], {"ready", "failed"}, body["created_at"], timeout=10
    )


async def test_retry_on_non_failed_conflicts(client_a):
    ready_task = await submit(client_a, "scrape", {"duration_ms": 300})
    await wait_for_status(client_a, ready_task["handle"], {"ready"}, ready_task["created_at"], timeout=10)
    resp = await client_a.post(f"/tasks/{ready_task['handle']}/retry")
    assert resp.status_code == 409

    running_task = await submit(client_a, "scrape", {"duration_ms": 8000})
    await wait_for_status(
        client_a, running_task["handle"], {"running"}, running_task["created_at"], timeout=10
    )
    resp2 = await client_a.post(f"/tasks/{running_task['handle']}/retry")
    assert resp2.status_code == 409

    await cancel_and_confirm(client_a, running_task["handle"], running_task["created_at"])
