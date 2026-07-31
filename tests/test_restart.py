"""Criterion 9: state survives a restart — and one of the two the reviewer
checks hardest (CLAUDE.md), alongside handle recycling.

This is the one test in the suite that cannot use the in-process app fixture
in tests/conftest.py. That fixture proves the ENGINE'S OWN OBJECT survives —
which it trivially does, since it just sits there in Python memory for the
whole test session. It says nothing about whether task state actually lives
in Postgres, independent of any particular process holding it. The only
honest way to test that is to run a real `uvicorn api.main:app` as a real OS
subprocess, SIGKILL it — not SIGTERM, which would run api/main.py's lifespan
shutdown path and test a gentler case than the one this system actually has
to survive (DECISIONS.md: "No graceful-shutdown mode; the reaper is the only
recovery path" — a SIGKILL runs no cleanup at all, by design) — and start a
fresh process against the same database.

Uses its own dedicated user/API key (RESTART_USER_KEY) rather than
tests/conftest.py's client_a/client_b, so a subprocess's own load_api_keys()
cache and this file's task rows can never collide with the rest of the suite
sharing the same Postgres instance.
"""

import asyncio
import contextlib
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import AsyncIterator

import httpx
import pytest
import pytest_asyncio

from scripts.bootstrap import connection, ensure_user
from tests.conftest import DSN
from tests.helpers import submit, wait_for_status

# Every test here is a real process kill/restart — seconds, not milliseconds.
# Marked so a reader can `pytest -m "not slow"` for a fast inner loop, but
# nothing in this repo's config deselects it by default (see pytest.ini):
# criterion 9 must run in a plain `pytest` invocation or it proves nothing.
pytestmark = pytest.mark.slow

REPO_ROOT = Path(__file__).resolve().parent.parent
RESTART_USER_NAME = "Phase 4 Restart Test User"
RESTART_USER_KEY = "phase4-test-key-restart"


def _free_port() -> int:
    """Ask the OS for an unused port rather than hardcoding one — two
    consecutive subprocesses in the same test must not fight over a port
    still draining from the previous one's death."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class APIProcess:
    """One real `uvicorn api.main:app` OS process, on its own loopback port,
    talking to the real Postgres database given by `dsn`. Everything this
    class does is real: a real fork/exec, a real bound socket, a real
    SIGKILL. An in-process fake would prove nothing about durability."""

    def __init__(self, dsn: str) -> None:
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        env = os.environ.copy()
        env.update(
            DATABASE_URL=dsn,
            CONCURRENCY_LIMIT="2",
            LEASE_SECONDS="10",
            HEARTBEAT_INTERVAL="2",
            MAX_ATTEMPTS="3",
        )
        # A real file, not subprocess.PIPE: uvicorn's own logging would
        # eventually fill an unread pipe buffer and deadlock the child. Only
        # ever read back for a failure message, never during normal running.
        self._log = tempfile.NamedTemporaryFile(
            prefix="restart-test-uvicorn-", suffix=".log", delete=False
        )
        self.process = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn", "api.main:app",
                "--host", "127.0.0.1", "--port", str(self.port),
                "--log-level", "warning",
            ],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )

    def _log_tail(self, n: int = 4000) -> str:
        self._log.flush()
        with open(self._log.name, "r", errors="replace") as f:
            return f.read()[-n:]

    async def wait_ready(self, timeout: float = 20.0) -> None:
        """Poll GET /health until it responds — never a fixed sleep. Also
        the honest place to fail loudly (with the child's own log) if the
        process died on startup instead of merely being slow."""
        deadline = time.monotonic() + timeout
        async with httpx.AsyncClient(base_url=self.base_url, timeout=2.0) as client:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError(
                        f"uvicorn subprocess exited early (code={self.process.returncode}) "
                        f"before becoming healthy; log tail:\n{self._log_tail()}"
                    )
                try:
                    resp = await client.get("/health")
                    if resp.status_code == 200:
                        return
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.15)
        raise AssertionError(
            f"server on {self.base_url} did not become healthy within {timeout}s; "
            f"log tail:\n{self._log_tail()}"
        )

    def sigkill(self, timeout: float = 10.0) -> None:
        """SIGKILL, not SIGTERM/terminate() — runs no cleanup at all, which
        is exactly the case CLAUDE.md's reaper design has to be correct
        under (see module docstring). .wait() blocks this thread briefly,
        but a killed process dies at the OS level in microseconds, so this
        is a negligible, bounded pause, not a real event-loop stall."""
        self.process.kill()
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise AssertionError(
                f"subprocess (pid={self.process.pid}) did not die within {timeout}s after SIGKILL"
            )

    def terminate_if_alive(self) -> None:
        """Best-effort teardown for a `finally` block: must never raise,
        since it commonly runs while another exception (a failed assertion)
        is already propagating. A leaked uvicorn holding a port would break
        every subsequent run of this file, so this always runs regardless
        of how the test using it exits."""
        with contextlib.suppress(Exception):
            if self.process.poll() is None:
                self.process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self.process.wait(timeout=10)
        with contextlib.suppress(OSError):
            self._log.close()
        with contextlib.suppress(OSError):
            os.unlink(self._log.name)


@pytest_asyncio.fixture(scope="module")
async def restart_user_id() -> uuid.UUID:
    return await ensure_user(DSN, RESTART_USER_NAME, RESTART_USER_KEY)


@pytest_asyncio.fixture(autouse=True)
async def _clean_slate(restart_user_id: uuid.UUID):
    """This file's own clean-slate fixture, scoped to RESTART_USER_KEY only
    — tests/conftest.py's autouse _clean_slate cleans user_a/user_b and
    never touches this file's dedicated user, by design (separate users
    exist precisely so these two cleanup fixtures never need to know about
    each other)."""
    async def _delete() -> None:
        async with connection(DSN) as conn:
            await conn.execute("DELETE FROM tasks WHERE user_id = $1", restart_user_id)

    await _delete()
    yield
    await _delete()


@pytest_asyncio.fixture(autouse=True)
async def _pause_shared_engine(app):
    """tests/conftest.py's session-scoped `app` fixture keeps its Engine's
    dispatch loop running for the WHOLE pytest session — including while
    THIS file's subprocesses are up. `claim_next` has no per-process
    scoping, by design (DECISIONS.md, "claim_next is a global queue,
    deliberately not scoped per-pool or per-process" — the exact mechanism
    that makes horizontal scaling free, since SKIP LOCKED already makes
    concurrent claimers from different processes safe and non-colliding).
    That's correct for production, but it means the shared in-process
    engine and this file's own subprocess engines would otherwise RACE to
    claim the very rows these tests submit: both register workers for the
    same "scrape"/"report" lanes against the same database, and a
    dedicated user (RESTART_USER_KEY) provides no protection here — claim
    isn't user-scoped either.

    Found by hand: test_state_survives_restart intermittently observed
    only 1 of its 5 submitted jobs reach 'running' (not 2) — NOT the
    lease-ratio bug this file's other fixes address, and reproducible only
    when running the full suite (never in 15+ isolated reruns of this file
    alone), which is exactly the signature of a leftover engine from an
    EARLIER test file still claiming this file's rows out from under it.

    Stopped for the duration of each test here and restarted after —
    the identical pattern scripts/smoke.py's poison-job scenario already
    uses for this same class of problem (see that function's own
    docstring). Depending on `app` here forces tests/conftest.py's heavy
    fixture to start even when this file runs in isolation; a real but
    minor cost for a test that would otherwise be correct only sometimes,
    depending on what ran before it.
    """
    engine = app.state.engine
    await engine.stop()
    yield
    await engine.start()


async def _live_client(base_url: str) -> httpx.AsyncClient:
    # Explicit, generous timeout: httpx.AsyncClient defaults to 5s on every
    # operation, which is too tight once real restart/recovery latency
    # (stale-lease reclaim, backoff windows) is in play (see
    # tests/conftest.py's _CLIENT_TIMEOUT, added for the identical reason).
    return httpx.AsyncClient(
        base_url=base_url, headers={"X-API-Key": RESTART_USER_KEY}, timeout=60.0
    )


async def test_state_survives_restart(restart_user_id: uuid.UUID) -> None:
    proc1 = APIProcess(DSN)
    handles: list[str] = []
    created_ats: dict[str, str] = {}
    try:
        await proc1.wait_ready()
        async with await _live_client(proc1.base_url) as client:
            submitted = [await submit(client, "scrape", {"duration_ms": 8000}) for _ in range(5)]
            handles = [t["handle"] for t in submitted]
            created_ats = {t["handle"]: t["created_at"] for t in submitted}

            # concurrency=2: expect exactly 2 running, 3 queued, and wait for
            # the dispatch loop to actually reach that steady state rather
            # than assuming it's instantaneous.
            deadline = time.monotonic() + 10.0
            running: list[dict] = []
            queued: list[dict] = []
            while time.monotonic() < deadline:
                resp = await client.get("/tasks")
                rows = resp.json()
                running = [t for t in rows if t["status"] == "running"]
                queued = [t for t in rows if t["status"] == "queued"]
                if len(running) == 2 and len(queued) == 3:
                    break
                await asyncio.sleep(0.2)
            assert len(running) == 2, f"expected 2 running before kill, got {len(running)}"
            assert len(queued) == 3, f"expected 3 queued before kill, got {len(queued)}"

        proc1.sigkill()
    finally:
        proc1.terminate_if_alive()

    proc2 = APIProcess(DSN)
    try:
        await proc2.wait_ready()
        async with await _live_client(proc2.base_url) as client:
            resp = await client.get("/tasks")
            assert resp.status_code == 200
            rows = resp.json()

            # Nothing lost, nothing spuriously created, handles unchanged.
            assert {t["handle"] for t in rows} == set(handles), (
                f"expected all 5 handles to survive, got {[t['handle'] for t in rows]}"
            )
            by_handle = {t["handle"]: t for t in rows}
            for handle in handles:
                assert by_handle[handle]["created_at"] == created_ats[handle], (
                    f"{handle}: created_at changed across restart — this is a different "
                    f"row, not the same task surviving"
                )
                # Not lost, not corrupted into some other status. A
                # previously-running row may still legitimately read
                # 'running' here even though the process that held it is
                # dead — its lease simply hasn't expired yet, and only the
                # (fresh) reaper in proc2 can reclaim it once it does. That
                # eventual reclaim-and-resume is proven below, not here.
                assert by_handle[handle]["status"] in ("queued", "running"), (
                    f"{handle} ended up {by_handle[handle]['status']!r} across a restart "
                    f"it should only have paused, not concluded"
                )

            # The real proof of "resumed or cleanly re-queued": progress
            # actually continues after the restart. A row stuck forever
            # with a dead lease (reaper broken) would time out right here.
            #
            # This must be a STRICT "ready", not "ready or failed": with a
            # sane lease-to-heartbeat ratio (see Engine.MIN_LEASE_TO_HEARTBEAT_RATIO
            # and DECISIONS.md, "lease-to-heartbeat ratio must leave room for
            # several missed renewals"), nothing here has any legitimate
            # reason to fail — these jobs never set fail/fail_permanent, so
            # a 'failed' outcome would mean a real regression, not an
            # accepted tradeoff. An earlier version of this test tolerated
            # an occasional 'failed' here; that was masking a genuine
            # misconfiguration (lease_seconds=5, heartbeat_interval=2 — only
            # 1s of real margin), not a property of the system worth
            # accepting. Tolerating it again would hide the next real bug
            # that produces the identical symptom.
            results = await asyncio.gather(
                *[
                    wait_for_status(client, h, {"ready", "failed"}, created_ats[h], timeout=40)
                    for h in handles
                ]
            )
            for handle, final in zip(handles, results):
                assert final["status"] == "ready", (
                    f"{handle} ended {final['status']!r} instead of ready after restart; "
                    f"error={final['error']!r}"
                )
    finally:
        proc2.terminate_if_alive()


async def test_backoff_survives_restart(restart_user_id: uuid.UUID) -> None:
    """The non-obvious half of criterion 9: a QUEUED row with a future
    not_before (retry backoff) must not be claimed early just because the
    process restarted — durability of *scheduled* state, not just of rows.

    Times this against the SECOND failure's backoff (2^(2-1) = 2s, per
    runner.py's exponential formula) rather than the first (1s): a fresh
    subprocess's own startup (fork/exec, imports, a real asyncpg connect)
    costs real wall-clock time too, and 1s leaves too little margin to
    distinguish "correctly waited" from "restart overhead alone ate the
    window" on a loaded machine. 2s isn't a lot more margin, so the
    assertion below uses a deliberately forgiving floor (1.2s, not the full
    2s) — tight enough to catch a real "ignore not_before on boot" bug
    (which would show elapsed time near zero), loose enough to tolerate
    ordinary process-restart jitter.
    """
    proc1 = APIProcess(DSN)
    handle = created_at = ""
    t_backoff_start = 0.0
    try:
        await proc1.wait_ready()
        async with await _live_client(proc1.base_url) as client:
            task = await submit(client, "scrape", {"fail": True, "duration_ms": 300})
            handle, created_at = task["handle"], task["created_at"]

            # Attempt 1: queued -> running -> failed(retryable) -> queued (~1s backoff).
            await wait_for_status(client, handle, {"running"}, created_at, timeout=10)
            await wait_for_status(client, handle, {"queued"}, created_at, timeout=10)
            # Attempt 2: same cycle -> queued again (~2s backoff). Timed from
            # the moment we OBSERVE this second queued state.
            await wait_for_status(client, handle, {"running"}, created_at, timeout=10)
            t_backoff_start = time.monotonic()
            await wait_for_status(client, handle, {"queued"}, created_at, timeout=10)

        proc1.sigkill()
    finally:
        proc1.terminate_if_alive()

    proc2 = APIProcess(DSN)
    try:
        await proc2.wait_ready()
        async with await _live_client(proc2.base_url) as client:
            t_resumed = None
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                resp = await client.get(f"/tasks/{handle}")
                body = resp.json()
                assert body["created_at"] == created_at
                if body["status"] != "queued":
                    t_resumed = time.monotonic()
                    break
                await asyncio.sleep(0.1)
            assert t_resumed is not None, f"{handle} never left 'queued' after restart"

            elapsed = t_resumed - t_backoff_start
            assert elapsed >= 1.2, (
                f"{handle} left its backoff window only {elapsed:.2f}s after entering it "
                f"(expected ~2s) — it was claimed too early on restart, ignoring not_before"
            )

            # It must still make progress: attempt 3 (of max_attempts=3)
            # runs and, since fail=True always fails, exhausts to 'failed'.
            final = await wait_for_status(client, handle, {"failed"}, created_at, timeout=20)
            assert final["error"] is not None
            assert final["error"]["retryable"] is False
            assert "exhaust" in final["error"]["reason"].lower()
    finally:
        proc2.terminate_if_alive()


async def test_no_duplicate_execution_after_restart(restart_user_id: uuid.UUID) -> None:
    """A task killed mid-run must resume to exactly one terminal outcome,
    never two. GET /tasks/{handle} does expose `attempts` (a documented
    detail-endpoint extra on top of the nine contract fields), but an
    attempts count can't distinguish "claimed twice, resolved once"
    (correct crash recovery) from "resolved twice" (the actual bug) — both
    read 2. What CAN distinguish them, and is the externally-visible
    symptom a genuine duplicate-execution bug would produce, is a result
    flip-flopping or a status regressing after first reaching terminal:
    mark_ready/mark_failed's `WHERE status = 'running'` guard (store.py)
    means only the FIRST writer of a race can ever stick, so a second,
    orphaned execution finishing later has no way to overwrite it — this
    test proves that from outside the process, not from re-reading the
    guarantee's source.
    """
    proc1 = APIProcess(DSN)
    handle = created_at = ""
    try:
        await proc1.wait_ready()
        async with await _live_client(proc1.base_url) as client:
            task = await submit(client, "scrape", {"duration_ms": 6000})
            handle, created_at = task["handle"], task["created_at"]
            await wait_for_status(client, handle, {"running"}, created_at, timeout=10)

        proc1.sigkill()
    finally:
        proc1.terminate_if_alive()

    proc2 = APIProcess(DSN)
    try:
        await proc2.wait_ready()
        async with await _live_client(proc2.base_url) as client:
            # Generous timeout: the stale lease (up to 10s) plus the periodic
            # reaper's own cadence (lease_seconds/2 = 5s) plus the job's
            # own 6s duration can add up before it resumes and completes.
            final = await wait_for_status(client, handle, {"ready", "failed"}, created_at, timeout=40)
            first_status = final["status"]
            first_result = final["result"]
            first_error = final["error"]

            # Hold and re-check: a second, orphaned execution racing in
            # behind the one that just "won" would show up here as a status
            # flip or a changed result/error, not as an exception.
            for _ in range(6):
                await asyncio.sleep(0.5)
                resp = await client.get(f"/tasks/{handle}")
                body = resp.json()
                assert body["created_at"] == created_at
                assert body["status"] == first_status, (
                    f"{handle} moved from {first_status!r} to {body['status']!r} after "
                    f"reaching terminal — evidence of a second execution overwriting the first"
                )
                assert body["result"] == first_result
                assert body["error"] == first_error
    finally:
        proc2.terminate_if_alive()
