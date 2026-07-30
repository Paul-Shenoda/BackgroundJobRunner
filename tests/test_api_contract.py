"""The fixed API contract: exact field set, timestamp format, auth
behaviour, and honest filter validation."""

import re
from datetime import datetime, timezone

from tests.helpers import CONTRACT_KEYS, cancel_and_confirm, submit

_ISO_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


async def test_task_object_has_exactly_the_contract_keys(client_a):
    task = await submit(client_a, "scrape", {"duration_ms": 5000})
    assert set(task.keys()) == CONTRACT_KEYS, (
        f"unexpected key set: extra={set(task.keys()) - CONTRACT_KEYS}, "
        f"missing={CONTRACT_KEYS - set(task.keys())}"
    )

    fetched = await client_a.get(f"/tasks/{task['handle']}")
    assert set(fetched.json().keys()) == CONTRACT_KEYS

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
