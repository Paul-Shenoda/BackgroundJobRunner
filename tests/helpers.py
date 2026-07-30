"""Shared polling/assertion helpers for the API test suite.

Plain functions, not fixtures — imported directly by test modules. Every
helper here talks HTTP only.
"""

import asyncio
import time
from typing import Any, Iterable

import httpx

CONTRACT_KEYS = {
    "handle",
    "lane",
    "params",
    "status",
    "result",
    "error",
    "created_at",
    "updated_at",
    "collected",
}


async def submit(
    client: httpx.AsyncClient, lane: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    resp = await client.post("/tasks", json={"lane": lane, "params": params or {}})
    assert resp.status_code == 202, f"submit failed: {resp.status_code} {resp.text}"
    return resp.json()


async def wait_for_status(
    client: httpx.AsyncClient,
    handle: str,
    statuses: Iterable[str],
    created_at: str,
    timeout: float = 15.0,
    interval: float = 0.2,
) -> dict[str, Any]:
    """Poll GET /tasks/{handle} until it reaches one of `statuses`.

    The API contract never exposes an internal row id (CLAUDE.md fixes the
    task object to exactly nine fields, none of them `id`), so there is no
    HTTP-visible equivalent of the id-based guard scripts/smoke.py uses for
    the identical problem. `created_at` stands in for it here: it's part of
    the public contract, assigned once at INSERT and never updated
    afterward, so it's unique per row for as long as that row exists. A
    handle recycling mid-poll serves a NEW row with a NEW created_at, so
    comparing it on every poll catches exactly the bug an id comparison
    would — a stale reference silently watching a different task under the
    same handle string.
    """
    wanted = set(statuses)
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        resp = await client.get(f"/tasks/{handle}")
        assert resp.status_code == 200, f"GET {handle} failed: {resp.status_code} {resp.text}"
        body = resp.json()
        assert body["created_at"] == created_at, (
            f"{handle}: created_at changed from {created_at!r} to {body['created_at']!r} "
            f"mid-poll — the handle was recycled to a different task while this helper "
            f"was still watching it; the caller needs to track the NEW submission instead"
        )
        last = body
        if body["status"] in wanted:
            return body
        await asyncio.sleep(interval)
    all_resp = await client.get("/tasks")
    health_resp = await client.get("/health")
    raise AssertionError(
        f"{handle}: timed out after {timeout}s waiting for status in {wanted}; "
        f"last seen: {last}; full list for this user: {all_resp.json()}; "
        f"health: {health_resp.json()}"
    )


async def cancel_and_confirm(
    client: httpx.AsyncClient, handle: str, created_at: str, timeout: float = 10.0
) -> dict[str, Any]:
    """Cancel `handle` and don't return until GET confirms it actually
    reached 'cancelled' — used by every test that saturates the shared
    worker pool, so the next test never starts against a cancel that was
    merely requested rather than completed (a cancelled-but-still-running
    asyncio task would occupy a concurrency slot the next test needs)."""
    resp = await client.post(f"/tasks/{handle}/cancel")
    assert resp.status_code == 200, f"cancel {handle} failed: {resp.status_code} {resp.text}"
    return await wait_for_status(client, handle, {"cancelled"}, created_at, timeout=timeout)
