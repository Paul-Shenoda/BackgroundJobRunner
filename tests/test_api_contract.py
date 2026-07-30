"""The fixed API contract: exact field set, timestamp format, auth
behaviour, and honest filter validation."""

import asyncio
import re
import time
from datetime import datetime, timezone

from tests.helpers import CONTRACT_KEYS, cancel_and_confirm, submit, wait_for_status

_ISO_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


async def test_task_object_has_exactly_the_contract_keys(client_a):
    task = await submit(client_a, "scrape", {"duration_ms": 5000})
    assert set(task.keys()) == CONTRACT_KEYS, (
        f"unexpected key set: extra={set(task.keys()) - CONTRACT_KEYS}, "
        f"missing={CONTRACT_KEYS - set(task.keys())}"
    )

    # GET /tasks/{handle} is the detail endpoint — CLAUDE.md explicitly
    # allows extras here (attempts, seeded, state history) for the
    # dashboard, as long as the nine core fields keep their exact shape.
    # So: contract keys are a subset, not the full set, and the specific
    # extras this endpoint adds are named explicitly rather than just
    # tolerated.
    fetched_keys = set((await client_a.get(f"/tasks/{task['handle']}")).json().keys())
    assert CONTRACT_KEYS <= fetched_keys, (
        f"detail response missing contract keys: {CONTRACT_KEYS - fetched_keys}"
    )
    assert fetched_keys - CONTRACT_KEYS == {"attempts", "max_attempts", "seeded", "state_history"}

    await cancel_and_confirm(client_a, task["handle"], task["created_at"])


async def test_timestamps_are_iso8601_utc_with_z(client_a):
    task = await submit(client_a, "scrape", {"duration_ms": 5000})

    for field in ("created_at", "updated_at"):
        value = task[field]
        assert _ISO_Z_RE.match(value), f"{field}={value!r} is not ISO 8601 UTC with a Z suffix"
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        assert parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(None)

    await cancel_and_confirm(client_a, task["handle"], task["created_at"])


async def test_unauthenticated_requests_rejected(anon_client, client_a):
    missing = await anon_client.get("/tasks")
    assert missing.status_code == 401

    bad_key = await client_a.get("/tasks", headers={"X-API-Key": "not-a-real-key"})
    assert bad_key.status_code == 401

    assert missing.json() == bad_key.json(), (
        "missing and invalid keys must give the identical response — a distinct "
        "message for one would let a caller learn which keys exist by probing"
    )


async def test_invalid_filters_rejected(client_a):
    resp = await client_a.get("/tasks", params={"status": "bogus"})
    assert resp.status_code == 400, "an unrecognised status filter must 400, not silently return []"
    assert resp.json()["detail"]


async def test_status_filter_accepts_multiple_values(client_a):
    """?status=queued&status=running (repeated params, the dashboard's
    "Active" quick filter) must return the OR of both statuses -- and only
    those -- via one server-side query, not a client-side merge of two
    separate calls."""
    # Deliberately a different lane from q1/q2/q3 below: handle numbers
    # recycle per (user, lane) as soon as a task goes cancelled/collected
    # (CLAUDE.md), so cancelled_task's number would otherwise be legitimately
    # free for one of q1/q2/q3 to reuse the instant it's cancelled -- real,
    # correct recycling, but it would make "assert this handle is absent"
    # ambiguous (present-but-different-row vs actually absent). A separate
    # lane keeps the two groups' numbering namespaces disjoint so handle
    # absence is unambiguous.
    ready_task = await submit(client_a, "report", {"duration_ms": 300})
    await wait_for_status(client_a, ready_task["handle"], {"ready"}, ready_task["created_at"])

    failed_task = await submit(client_a, "report", {"fail_permanent": True, "duration_ms": 300})
    await wait_for_status(client_a, failed_task["handle"], {"failed"}, failed_task["created_at"])

    cancelled_task = await submit(client_a, "report", {"duration_ms": 4000})
    await cancel_and_confirm(client_a, cancelled_task["handle"], cancelled_task["created_at"])

    # concurrency=2 (see conftest.py): submitting three more long-running
    # tasks after the ones above are already terminal means exactly two of
    # these three claim the pool's two slots and the third sits queued
    # behind them -- a real, simultaneous queued+running pair to filter for,
    # not a synthetic one.
    q1 = await submit(client_a, "scrape", {"duration_ms": 4000})
    q2 = await submit(client_a, "scrape", {"duration_ms": 4000})
    q3 = await submit(client_a, "scrape", {"duration_ms": 4000})
    active_handles = {q1["handle"], q2["handle"], q3["handle"]}

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        resp = await client_a.get("/tasks", params={"status": "running"})
        running = {t["handle"] for t in resp.json()} & active_handles
        if len(running) == 2:
            break
        await asyncio.sleep(0.2)
    else:
        raise AssertionError(f"never observed 2 of {active_handles} running simultaneously")

    try:
        resp = await client_a.get(
            "/tasks", params={"status": ["queued", "running"], "limit": 100}
        )
        assert resp.status_code == 200
        body = resp.json()
        by_handle = {t["handle"]: t["status"] for t in body}

        assert active_handles <= by_handle.keys(), (
            f"expected all of {active_handles} in the queued+running filter, got {by_handle}"
        )
        assert set(by_handle.values()) <= {"queued", "running"}, (
            f"filter returned a status outside {{queued, running}}: {by_handle}"
        )
        # None of the ready/failed/cancelled tasks share a handle with
        # q1/q2/q3 (all submitted fresh, nothing collected/recycled yet in
        # this test), so their absence here is unambiguous -- not just
        # "absent under their old status," genuinely not present at all.
        for other in (ready_task, failed_task, cancelled_task):
            assert other["handle"] not in by_handle, (
                f"{other['handle']} (ready/failed/cancelled) must not appear in a "
                f"queued+running filter, but was returned with status "
                f"{by_handle.get(other['handle'])!r}"
            )
    finally:
        # Discipline this suite already follows elsewhere (test_concurrency.py):
        # nothing may be left running/queued in the shared pool for the next
        # test to inherit.
        for handle, created_at in (
            (q1["handle"], q1["created_at"]),
            (q2["handle"], q2["created_at"]),
            (q3["handle"], q3["created_at"]),
        ):
            await cancel_and_confirm(client_a, handle, created_at)


async def test_status_filter_single_value_unchanged(client_a):
    """A single ?status=ready must behave exactly as before the multi-value
    change -- backward compatibility, not just "the new code path works"."""
    ready_task = await submit(client_a, "scrape", {"duration_ms": 300})
    await wait_for_status(client_a, ready_task["handle"], {"ready"}, ready_task["created_at"])

    failed_task = await submit(client_a, "scrape", {"fail_permanent": True, "duration_ms": 300})
    await wait_for_status(client_a, failed_task["handle"], {"failed"}, failed_task["created_at"])

    resp = await client_a.get("/tasks", params={"status": "ready", "limit": 100})
    assert resp.status_code == 200
    by_handle = {t["handle"]: t["status"] for t in resp.json()}

    assert by_handle.get(ready_task["handle"]) == "ready"
    assert failed_task["handle"] not in by_handle
    assert set(by_handle.values()) == {"ready"}


async def test_invalid_status_in_list_rejected(client_a):
    """A bad value hidden inside an otherwise-valid list must still 400 --
    the exact case a naive "parse and skip failures" implementation would
    let through as a silent partial filter."""
    resp = await client_a.get("/tasks", params={"status": ["queued", "bogus"]})
    assert resp.status_code == 400, (
        f"an invalid status inside a valid list must 400, not silently drop it "
        f"or partially filter: got {resp.status_code} {resp.text}"
    )
    assert resp.json()["detail"]
