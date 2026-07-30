"""Shared fixtures for the API test suite.

Everything here talks to the running system over HTTP (an in-process ASGI
transport, no real socket) — never by importing Store or calling engine.*
directly. The one exception is fixture housekeeping that isn't part of any
test's assertions: provisioning the two test users and deleting their task
rows between tests. Both go through scripts/bootstrap.py's shared
connection() helper, exactly the way scripts/smoke.py's cleanup() does —
CLAUDE.md's asyncpg-confinement rule scopes to engine/, not to tests/, and
tidying up rows is fixture plumbing, not a behavioural assertion.

One app for the whole session, not one per test: starting Engine means
opening an asyncpg pool and launching the dispatch/reaper background tasks,
and restarting that per test would be slow and would risk racing a previous
test's in-flight asyncio job against a freshly-started pool. A single
long-lived app matches production shape better anyway (one process, one
pool) and is why the event loop itself is pinned to session scope in
pytest.ini — a session-scoped asyncpg pool and worker pool would otherwise
outlive the per-test event loop that created them.
"""

import asyncio
import os
import uuid
from typing import AsyncIterator

import httpx
import pytest_asyncio
import uvicorn
from asgi_lifespan import LifespanManager
from dotenv import load_dotenv

from scripts.bootstrap import connection, ensure_user

load_dotenv()

DSN = os.environ["DATABASE_URL"]

# Set BEFORE api.main is ever imported (done lazily, inside the `app` fixture
# below) so api.main's own load_dotenv() — which does not override
# already-set variables — leaves these test values in place instead of the
# looser defaults in .env. Short values so timing tests don't crawl, but
# LEASE_SECONDS is 5x HEARTBEAT_INTERVAL, not 2.5x — a thinner ratio here
# originally caused test_restart.py's test_state_survives_restart to fail
# intermittently (~1 in 10-15 runs): with only ~1s of real margin before a
# late heartbeat renewal looks identical to a dead worker, a freshly
# restarted process under this suite's own concurrent polling load could
# occasionally miss one, spuriously reclaiming a perfectly live job. That
# was a genuine config bug, not a test artifact — Engine's constructor now
# enforces a 3x floor structurally (see DECISIONS.md, "lease-to-heartbeat
# ratio must leave room for several missed renewals"), and 5x here matches
# the documented 4-5x rule of thumb rather than merely clearing the floor.
os.environ["CONCURRENCY_LIMIT"] = "2"
os.environ["LEASE_SECONDS"] = "10"
os.environ["HEARTBEAT_INTERVAL"] = "2"
os.environ["MAX_ATTEMPTS"] = "3"

# httpx.AsyncClient defaults to a 5s timeout on every operation (connect,
# read, write, pool). Far too short here: a retry-backoff cycle can leave an
# open SSE stream genuinely silent for several seconds between events (no
# intermediate event is published for a requeue — see DECISIONS.md, "A retry
# emits no lifecycle event"), and a plain 5s default turned that into a real,
# intermittent httpx.ReadTimeout on the stream reader, not a clean test
# failure. Every client fixture below sets this explicitly instead.
_CLIENT_TIMEOUT = httpx.Timeout(60.0)

USER_A_NAME = "Phase 4 Test User A"
USER_A_KEY = "phase4-test-key-a"
USER_B_NAME = "Phase 4 Test User B"
USER_B_KEY = "phase4-test-key-b"


@pytest_asyncio.fixture(scope="session")
async def user_a_id() -> uuid.UUID:
    return await ensure_user(DSN, USER_A_NAME, USER_A_KEY)


@pytest_asyncio.fixture(scope="session")
async def user_b_id() -> uuid.UUID:
    return await ensure_user(DSN, USER_B_NAME, USER_B_KEY)


@pytest_asyncio.fixture(scope="session")
async def app(user_a_id: uuid.UUID, user_b_id: uuid.UUID):
    """The real FastAPI app, lifespan and all.

    Depends on both user fixtures so they exist BEFORE startup — api/auth.py's
    load_api_keys() runs once during the lifespan and caches api_key -> user_id
    in memory; a user created after that point would 401 on every request for
    the rest of the session.
    """
    from api.main import app as fastapi_app

    async with LifespanManager(fastapi_app):
        yield fastapi_app


@pytest_asyncio.fixture(scope="session")
async def _transport(app) -> httpx.ASGITransport:
    return httpx.ASGITransport(app=app)


@pytest_asyncio.fixture
async def client_a(_transport: httpx.ASGITransport) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=_transport, base_url="http://test", headers={"X-API-Key": USER_A_KEY},
        timeout=_CLIENT_TIMEOUT,
    ) as client:
        yield client


@pytest_asyncio.fixture
async def client_b(_transport: httpx.ASGITransport) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=_transport, base_url="http://test", headers={"X-API-Key": USER_B_KEY},
        timeout=_CLIENT_TIMEOUT,
    ) as client:
        yield client


@pytest_asyncio.fixture
async def anon_client(_transport: httpx.ASGITransport) -> AsyncIterator[httpx.AsyncClient]:
    """No X-API-Key header at all — for the unauthenticated-request tests.
    A separate client rather than client_a with a header stripped per-call:
    httpx merges call-level headers into the client's defaults, so a client
    fixture that never set the header in the first place is the only clean
    way to represent "the header is simply absent"."""
    async with httpx.AsyncClient(
        transport=_transport, base_url="http://test", timeout=_CLIENT_TIMEOUT
    ) as client:
        yield client


@pytest_asyncio.fixture(scope="session")
async def live_base_url(app) -> AsyncIterator[str]:
    """A real uvicorn server, on loopback, serving the SAME `app` object the
    `app` fixture above already started via LifespanManager.

    Exists only for the SSE tests (test_events.py). httpx.ASGITransport does
    not truly stream: handle_async_request() awaits the whole ASGI app call
    to completion before returning any Response at all (see
    httpx/_transports/asgi.py — `await self.app(scope, receive, send)` with
    no incremental handoff). GET /events runs forever by design, so
    ASGITransport hangs on it indefinitely — confirmed by hand while writing
    this suite, not a hypothetical. A real socket has no such limitation.

    lifespan="off" is load-bearing, not incidental: without it uvicorn would
    call the app's own startup/shutdown a second time, starting a SECOND
    Engine/WorkerPool against the same tasks table alongside the one the
    `app` fixture already started — doubling effective concurrency and
    silently breaking the concurrency-limit tests. One Engine, started
    once; uvicorn here is purely a second network entry point onto it.
    """
    config = uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="off", log_level="warning")
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await serve_task


@pytest_asyncio.fixture
async def live_client_a(live_base_url: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=live_base_url, headers={"X-API-Key": USER_A_KEY}, timeout=_CLIENT_TIMEOUT
    ) as client:
        yield client


@pytest_asyncio.fixture
async def live_client_b(live_base_url: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=live_base_url, headers={"X-API-Key": USER_B_KEY}, timeout=_CLIENT_TIMEOUT
    ) as client:
        yield client


async def _delete_user_tasks(user_id: uuid.UUID) -> None:
    async with connection(DSN) as conn:
        await conn.execute("DELETE FROM tasks WHERE user_id = $1", user_id)


@pytest_asyncio.fixture(autouse=True)
async def _clean_slate(user_a_id: uuid.UUID, user_b_id: uuid.UUID):
    """Delete both test users' task rows before AND after every test.

    Before: guarantees each test starts numbering at lane-1 regardless of
    what a previous test left behind. After: stops a test that errors out
    mid-way (without reaching its own cleanup) from poisoning the next one.

    A literal DELETE, not merely cancelling/collecting via HTTP — deleting
    is fixture housekeeping, not something a test is asserting on, so
    reaching past the API here doesn't compromise "tests prove behaviour
    against the API" (see module docstring). Tests themselves still must
    bring anything they leave 'running' to a confirmed terminal state
    before returning (mirroring scripts/smoke.py's discipline) — this
    fixture cleans up rows, but it does not cancel a live asyncio job still
    executing in the shared worker pool, and a zombie job left sleeping
    would occupy a concurrency slot for whatever test runs next.
    """
    await _delete_user_tasks(user_a_id)
    await _delete_user_tasks(user_b_id)
    yield
    await _delete_user_tasks(user_a_id)
    await _delete_user_tasks(user_b_id)
