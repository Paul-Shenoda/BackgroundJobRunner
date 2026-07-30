"""GET /events: the SSE stream carries exactly the four contract event
types, scoped per user, and a retry (which never terminates) publishes
nothing until the job actually reaches a terminal state."""

import asyncio
import json

import pytest

from tests.helpers import submit, wait_for_status


async def _read_n_events(response, n: int, timeout: float = 10.0) -> list[dict]:
    """Collect the next n `data:` events from an open SSE response,
    ignoring blank lines and `: ping` comment lines."""
    events: list[dict] = []

    async def _read():
        async for line in response.aiter_lines():
            if not line.startswith("data: "):
                continue
            events.append(json.loads(line[len("data: "):]))
            if len(events) >= n:
                return

    await asyncio.wait_for(_read(), timeout=timeout)
    return events


async def test_stream_carries_accepted_and_ready(live_client_a):
    async with live_client_a.stream("GET", "/events") as response:
        assert response.status_code == 200
        # Give the SSE generator a moment to reach engine.subscribe() before
        # we act — subscribing only happens on the generator's first
        # iteration, which runs slightly after headers are flushed back to
        # us, not before.
        await asyncio.sleep(0.2)

        task = await submit(live_client_a, "scrape", {"duration_ms": 500})
        events = await _read_n_events(response, 2, timeout=10)

    assert events[0]["type"] == "accepted"
    assert events[0]["handle"] == task["handle"]
    assert events[0]["lane"] == "scrape"
    assert isinstance(events[0]["summary"], str) and events[0]["summary"]

    assert events[1]["type"] == "ready"
    assert events[1]["handle"] == task["handle"]
    assert events[1]["lane"] == "scrape"
    assert isinstance(events[1]["summary"], str) and events[1]["summary"]


async def test_stream_carries_failed_and_cancelled(live_client_a):
    async with live_client_a.stream("GET", "/events") as response:
        assert response.status_code == 200
        await asyncio.sleep(0.2)
        task = await submit(live_client_a, "scrape", {"fail_permanent": True, "duration_ms": 200})
        events = await _read_n_events(response, 2, timeout=10)

    assert events[0]["type"] == "accepted"
    assert events[1]["type"] == "failed"
    assert events[1]["handle"] == task["handle"]
    assert events[1]["reason"]
    assert events[1]["retryable"] is False

    async with live_client_a.stream("GET", "/events") as response:
        assert response.status_code == 200
        await asyncio.sleep(0.2)
        task2 = await submit(live_client_a, "scrape", {"duration_ms": 6000})
        await wait_for_status(live_client_a, task2["handle"], {"running"}, task2["created_at"], timeout=10)
        cancel_resp = await live_client_a.post(f"/tasks/{task2['handle']}/cancel")
        assert cancel_resp.status_code == 200
        events2 = await _read_n_events(response, 2, timeout=10)

    assert events2[0]["type"] == "accepted"
    assert events2[1]["type"] == "cancelled"
    assert events2[1]["handle"] == task2["handle"]


async def test_retry_emits_no_event(live_client_a):
    """A retryable failure requeues (and requeues again) before finally
    exhausting its attempt budget — per DECISIONS.md, none of those
    intermediate requeues is a lifecycle event. Only 'accepted' at
    submission and 'failed' at final exhaustion should ever reach the
    stream for this handle."""
    async with live_client_a.stream("GET", "/events") as response:
        assert response.status_code == 200
        await asyncio.sleep(0.2)

        task = await submit(live_client_a, "scrape", {"fail": True, "duration_ms": 200})
        events_task = asyncio.create_task(_read_n_events(response, 2, timeout=20))
        final = await wait_for_status(live_client_a, task["handle"], {"failed"}, task["created_at"], timeout=20)
        events = await events_task

    assert [e["type"] for e in events] == ["accepted", "failed"], (
        f"expected exactly accepted then failed with nothing in between — a 'retrying' "
        f"or duplicate event would mean an intermediate requeue wrongly published, "
        f"got {[e['type'] for e in events]}"
    )
    assert events[1]["reason"] == final["error"]["reason"]


async def test_events_are_user_scoped(live_client_a, live_client_b):
    async with live_client_a.stream("GET", "/events") as resp_a, live_client_b.stream("GET", "/events") as resp_b:
        assert resp_a.status_code == 200
        assert resp_b.status_code == 200
        await asyncio.sleep(0.2)

        task = await submit(live_client_a, "scrape", {"duration_ms": 300})

        a_events = await _read_n_events(resp_a, 1, timeout=10)
        assert a_events[0]["type"] == "accepted"
        assert a_events[0]["handle"] == task["handle"]

        # User B's stream must see nothing from user A's activity.
        with pytest.raises(asyncio.TimeoutError):
            await _read_n_events(resp_b, 1, timeout=2)
