"""End-to-end smoke test: exercises the real Engine against a real Postgres,
with zero HTTP involved.

This is the executable version of the brief's "delete the frontend and the
engine still works" claim. It's also the first point in the build where the
real modules — store, handles, registry, events, runner, engine, the mock
worker — run together against a real database rather than fakes.

Boundary note: this script talks to Engine and nothing else in engine/, with
one deliberate exception — user provisioning and one-shot DB access
(scripts/bootstrap.py's ensure_user/connection) are setup, not orchestration,
and live outside the engine on purpose (see that module's docstring).
Task-row cleanup at the end of this script uses that same shared connection()
helper for the same reason: it isn't engine business logic, just this script
tidying up after itself, and CLAUDE.md's asyncpg-confinement rule scopes to
engine/, not the whole repo.

Run as:
    python -m scripts.smoke [--keep]

Reads DATABASE_URL from .env (or the environment). --keep skips the
cleanup step so the created rows can be inspected afterward.
"""

import argparse
import asyncio
import contextlib
import logging
import os
import sys
import time

from dotenv import load_dotenv

from engine.engine import Engine
from engine.models import Status
from engine.workers.mock import mock_worker
from scripts.bootstrap import connection, ensure_user

DEMO_USER_NAME = "Smoke Test"
DEMO_USER_API_KEY = "smoke-test-key"

event_logger = logging.getLogger("scripts.smoke.events")


class ElapsedTimeFilter(logging.Filter):
    """Injects record.elapsed = seconds since script start, computed via
    time.monotonic() at the moment each record is emitted.

    Not derived from the record's own timestamp: logging.LogRecord.created
    is wall-clock (time.time())-based, so subtracting a wall-clock start
    reference wouldn't actually restore monotonicity if a clock jump
    happened mid-run — the subtraction would just carry the jump's error
    into the result. Calling time.monotonic() fresh here, at emit time, is
    what makes this genuinely immune to it, the same guarantee print_events
    already relies on for the event stream.
    """

    def __init__(self, start_time: float) -> None:
        super().__init__()
        self._start_time = start_time

    def filter(self, record: logging.LogRecord) -> bool:
        record.elapsed = time.monotonic() - self._start_time
        return True


def configure_logging(start_time: float) -> None:
    """One destination (stdout), one format, one clock basis for
    everything this script emits while running: runner.py's background-task
    logs (dispatch/reaper/heartbeat) AND the lifecycle event stream (via
    print_events using event_logger below), instead of two independent
    streams — print() to stdout with UTC wall-clock, logging to stderr with
    local wall-clock. Independently harmless, but interleaved output from
    two unsynchronized clocks and two different destinations reads as a
    defect to anyone watching a recording, even when nothing is actually
    wrong. Elapsed monotonic time instead of UTC for the same reason
    DECISIONS.md's "handle resolution ordering uses a monotonic sequence,
    not a timestamp" entry gives: wall-clock time, UTC or not, is exactly
    what a WSL2 clock jump can corrupt — monotonic elapsed time can't be.
    """
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.addFilter(ElapsedTimeFilter(start_time))
    handler.setFormatter(logging.Formatter("[+%(elapsed)7.3fs] %(levelname)-7s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])


async def wait_for_task(engine, user_id, handle, task_id, statuses, timeout=15.0, interval=0.25):
    """Poll GET /tasks/{handle} until the row matching task_id reaches one
    of `statuses`.

    A handle is a CURRENTLY-active identifier, not a permanent one —
    numbers get recycled by design the moment a task is collected or
    cancelled. Polling by handle string alone risks silently watching a
    DIFFERENT, later task that happens to have inherited the same handle.
    This helper guards against exactly that: every poll checks the
    returned task's id against the one actually being tracked, and fails
    loudly and specifically the moment they disagree, rather than
    reporting a stranger's status as if it were the task under test.
    """
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = await engine.get(user_id, handle)
        if last is not None:
            if last.id != task_id:
                raise AssertionError(
                    f"{handle}: expected to observe task_id={task_id}, but GET returned a "
                    f"DIFFERENT task (id={last.id}, status={last.status.value}) under this handle. "
                    f"Either the handle has genuinely been recycled to a new submission, or an "
                    f"older row sharing this handle string was returned instead of the tracked one."
                )
            if last.status in statuses:
                return last
        await asyncio.sleep(interval)
    seen = f"status={last.status.value}" if last else "no task found for this handle"
    wanted = ", ".join(s.value for s in statuses)
    raise AssertionError(f"{handle} (id={task_id}): timed out after {timeout}s waiting for [{wanted}]; last seen: {seen}")


async def cancel_and_confirm(engine, user_id, task, timeout=10.0):
    """Cancel `task` and don't return until GET confirms — by id, not just
    handle — that it actually reached 'cancelled'. Used everywhere a
    scenario tidies up after itself, so the next scenario never starts
    against a cancel that was merely requested rather than completed."""
    cancelled = await engine.cancel(user_id, task.handle)
    assert cancelled is not None and cancelled.id == task.id, (
        f"cancel() did not return the task we asked to cancel ({task.handle}, id={task.id})"
    )
    return await wait_for_task(engine, user_id, task.handle, task.id, {Status.CANCELLED}, timeout=timeout)


async def print_events(queue, event_log):
    """Background printer: logs every LifecycleEvent as it arrives, and
    appends it to event_log so scenarios can assert on exactly what a real
    SSE client would have seen.

    Goes through event_logger (see configure_logging) rather than a bare
    print() — same handler, same format, same monotonic-elapsed clock
    basis as runner.py's own background-task logs, so this and the
    dispatch/reaper/heartbeat logs interleave as one coherent stream
    instead of two that visually disagree.
    """
    while True:
        event = await queue.get()
        event_log.append(event)
        event_logger.info("EVENT %s", event.to_dict())


# --- Scenarios ---------------------------------------------------------
#
# Each scenario is self-contained: it submits whatever it needs and, before
# returning, brings its own tasks to a CONFIRMED terminal state (via
# wait_for_task/cancel_and_confirm, never a fire-and-forget cancel()) so
# the pool is idle and every handle it touched is settled before the next
# scenario starts. Two failure modes this avoids: backlog from an earlier
# scenario sitting queued behind later scenarios' jobs under the
# concurrency=2 limit, and a later scenario's fresh submission recycling a
# handle number that an earlier scenario is still in the middle of
# resolving.

async def scenario_a(engine, user_id, track, event_log, dsn):
    start = time.monotonic()
    task = await engine.submit(user_id, "scrape", {"duration_ms": 10000})
    elapsed = time.monotonic() - start
    track(task)

    assert elapsed < 1.0, f"submit() took {elapsed:.3f}s, expected well under 1s"
    assert task.status == Status.QUEUED, f"expected queued immediately after submit, got {task.status}"
    print(f"  submit() returned in {elapsed * 1000:.1f}ms, status={task.status.value}, handle={task.handle}")

    # The point made, not the full 10s job — cancel rather than leave it as
    # backlog for the next scenario. Confirmed (not just requested) before
    # returning, so scenario B starts against a genuinely idle pool.
    await cancel_and_confirm(engine, user_id, task)


async def scenario_b(engine, user_id, track, event_log, dsn):
    scrape_task = await engine.submit(user_id, "scrape", {"duration_ms": 5000})
    report_task = await engine.submit(user_id, "report", {"duration_ms": 5000})
    track(scrape_task)
    track(report_task)

    assert report_task.handle == "report-1", (
        f"expected the first report submission to be report-1, got {report_task.handle} "
        f"(a previous --keep run may have left rows behind for this demo user)"
    )
    assert scrape_task.lane == "scrape" and report_task.lane == "report"
    print(f"  {scrape_task.handle} and {report_task.handle} — independent per-lane counters, not one shared sequence")

    await cancel_and_confirm(engine, user_id, scrape_task)
    await cancel_and_confirm(engine, user_id, report_task)


async def scenario_c(engine, user_id, track, event_log, dsn):
    tasks = [track(await engine.submit(user_id, "scrape", {"duration_ms": 3000})) for _ in range(5)]

    max_running = 0
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        running = await engine.list_tasks(user_id, status=Status.RUNNING)
        max_running = max(max_running, len(running))
        assert len(running) <= 2, f"observed {len(running)} tasks running simultaneously; concurrency limit is 2"
        await asyncio.sleep(0.25)

    print(f"  observed max concurrent running = {max_running} (limit 2, never exceeded)")

    # Drain the backlog before returning so scenario D starts against an
    # idle pool. Tracked by each task's own id — with 5 scrape submissions
    # in a row, at least one of these numbers is highly likely to be a
    # reused one (freed by an earlier scenario's cleanup), so a stale
    # cancelled/ready row can share this handle string too.
    for t in tasks:
        await wait_for_task(engine, user_id, t.handle, t.id, {Status.READY, Status.FAILED}, timeout=10)


async def scenario_d(engine, user_id, track, event_log, dsn):
    task = await engine.submit(user_id, "scrape", {"duration_ms": 30000})
    track(task)
    await wait_for_task(engine, user_id, task.handle, task.id, {Status.RUNNING}, timeout=10)

    cancelled = await engine.cancel(user_id, task.handle)
    assert cancelled is not None and cancelled.id == task.id, "cancel() did not return the task we asked to cancel"
    assert cancelled.status == Status.CANCELLED, f"expected cancelled immediately, got {cancelled.status}"

    # Give the cancelled asyncio task a moment to actually unwind, then
    # re-check: a late 'ready' event here would mean the worker kept running
    # after we believed we'd stopped it. Events are checked before this
    # scenario has moved on, so no later scenario has had a chance to
    # resubmit into this handle yet — filtering event_log by handle alone
    # is safe at this specific point, unlike polling GET over time.
    await asyncio.sleep(4)

    handle_events = [e for e in event_log if e.handle == task.handle]
    assert any(e.type == "cancelled" for e in handle_events), f"no 'cancelled' event observed for {task.handle}"
    assert not any(e.type == "ready" for e in handle_events), (
        f"a 'ready' event arrived for cancelled task {task.handle} — the worker did not actually stop"
    )

    final = await wait_for_task(engine, user_id, task.handle, task.id, {Status.CANCELLED}, timeout=1)
    print(f"  {task.handle} cancelled mid-run; no late 'ready' event after a 4s grace period")


async def scenario_e(engine, user_id, track, event_log, dsn):
    filler_a = track(await engine.submit(user_id, "scrape", {"duration_ms": 20000}))
    filler_b = track(await engine.submit(user_id, "scrape", {"duration_ms": 20000}))
    await wait_for_task(engine, user_id, filler_a.handle, filler_a.id, {Status.RUNNING}, timeout=10)
    await wait_for_task(engine, user_id, filler_b.handle, filler_b.id, {Status.RUNNING}, timeout=10)

    extra = await engine.submit(user_id, "scrape", {"duration_ms": 5000})
    track(extra)
    await asyncio.sleep(0.5)  # let the dispatch loop have a chance to (wrongly) claim it, if it were going to
    still_queued = await engine.get(user_id, extra.handle)
    assert still_queued is not None and still_queued.id == extra.id, (
        f"GET {extra.handle} did not return the task we just submitted (id={extra.id})"
    )
    assert still_queued.status == Status.QUEUED, (
        f"expected {extra.handle} to stay queued under a saturated pool, got {still_queued.status}"
    )

    await cancel_and_confirm(engine, user_id, extra)
    print(f"  {extra.handle} stayed queued while the pool was saturated (2/2 running), then cancelled cleanly")

    # Free the pool rather than waiting 20s for the fillers to finish.
    await cancel_and_confirm(engine, user_id, filler_a)
    await cancel_and_confirm(engine, user_id, filler_b)


async def scenario_f(engine, user_id, track, event_log, dsn):
    task = await engine.submit(user_id, "scrape", {"fail": True, "duration_ms": 500})
    track(task)

    final = await wait_for_task(engine, user_id, task.handle, task.id, {Status.FAILED}, timeout=30)
    assert final.attempts == 3, f"expected 3 attempts, got {final.attempts}"
    assert final.error is not None and final.error.retryable is False, "exhausted retry must end non-retryable"
    assert "exhaust" in final.error.reason.lower(), f"reason does not name exhaustion: {final.error.reason!r}"

    failed_events = [e for e in event_log if e.handle == task.handle and e.type == "failed"]
    assert len(failed_events) == 1, f"expected exactly 1 'failed' event (retries publish nothing), got {len(failed_events)}"
    print(f"  {task.handle} failed after {final.attempts} attempts: {final.error.reason!r}; exactly 1 'failed' event")


async def scenario_g(engine, user_id, track, event_log, dsn):
    task = await engine.submit(user_id, "scrape", {"fail_permanent": True, "duration_ms": 300})
    track(task)

    final = await wait_for_task(engine, user_id, task.handle, task.id, {Status.FAILED}, timeout=10)
    assert final.attempts == 1, f"expected exactly 1 attempt for a permanent failure, got {final.attempts}"
    assert final.error is not None and final.error.retryable is False
    print(f"  {task.handle} failed permanently after 1 attempt: {final.error.reason!r}")


async def scenario_h(engine, user_id, track, event_log, dsn):
    # Part 1: ready -> collect -> the freed number gets reused.
    task = await engine.submit(user_id, "scrape", {"duration_ms": 500})
    track(task)
    await wait_for_task(engine, user_id, task.handle, task.id, {Status.READY}, timeout=10)
    collected = await engine.collect(user_id, task.handle)
    assert collected is not None and collected.id == task.id and collected.collected is True

    recycled = await engine.submit(user_id, "scrape", {"duration_ms": 20000})
    track(recycled)
    assert recycled.number == task.number, (
        f"expected the freed number {task.number} to be reused, got {recycled.number} "
        f"({task.handle} -> {recycled.handle})"
    )
    # From here, `recycled.handle` is very likely the SAME STRING as
    # `task.handle` (the number was just reused) — two physical rows now
    # share one handle: task (terminal, collected) and recycled (active).
    # Every wait/read below is keyed on recycled.id specifically, not the
    # handle string alone, so it can't be confused with task's stale row.
    print(f"  {task.handle} collected; its number reused as {recycled.handle} (id={recycled.id})")

    # Part 2: while `recycled` is still active (uncollected), a fresh
    # submission must NOT reuse its number.
    while_active = await engine.submit(user_id, "scrape", {"duration_ms": 500})
    track(while_active)
    assert while_active.number != recycled.number, (
        f"got number {while_active.number} while {recycled.handle} was still active — handle collision risk"
    )
    print(f"  while {recycled.handle} was still active, a new submission got {while_active.handle} instead")

    # Clean up: recycled has a long duration and would otherwise occupy a
    # concurrency slot for the rest of the script. Cancel it (not wait for
    # 'ready') — it was never asserted to reach ready, only to hold its
    # number while active, which Part 2 already proved.
    await cancel_and_confirm(engine, user_id, recycled)
    await wait_for_task(engine, user_id, while_active.handle, while_active.id, {Status.READY, Status.FAILED}, timeout=10)


async def scenario_i(engine, user_id, track, event_log, dsn):
    task = await engine.submit(user_id, "scrape", {"fail_permanent": True, "duration_ms": 300})
    track(task)
    failed = await wait_for_task(engine, user_id, task.handle, task.id, {Status.FAILED}, timeout=10)
    assert failed.collected is False

    collected = await engine.collect(user_id, task.handle)
    assert collected is not None and collected.id == task.id and collected.collected is True

    reused = await engine.submit(user_id, "scrape", {"duration_ms": 500})
    track(reused)
    assert reused.number == task.number, (
        f"collecting a failed task should free its number; expected {task.number}, got {reused.number}"
    )
    print(f"  {task.handle} (failed) collected; its number reused as {reused.handle} (id={reused.id})")
    await wait_for_task(engine, user_id, reused.handle, reused.id, {Status.READY, Status.FAILED}, timeout=10)


async def scenario_j_poison_job(engine, user_id, track, event_log, dsn):
    """Forces a lease to expire twice before any heartbeat can renew it,
    proving reap_expired_leases's poison-job protection actually terminates
    the task (both the requeue AND the exhaustion branch) instead of
    cycling it forever. This was the exact gap code review caught — nothing
    else in this script exercises the reap-to-failed path.

    max_attempts=2 (not 1): with 1, the very first lease expiry is already
    exhaustion, so the CASE WHEN's requeue branch never runs — the scenario
    would still pass even if requeuing were completely broken. With 2, the
    first expiry requeues (attempts=1<2) and the second exhausts
    (attempts=2>=2), proving both branches in one run.

    concurrency=2 (not 1) so the second reclaim doesn't have to wait for
    the first attempt's worker to naturally finish before it can even be
    claimed — keeps this fast (~3-4s) regardless of duration_ms.

    duration_ms=10000 is deliberately far longer than this scenario's own
    runtime, not arbitrary: heartbeat_interval=100 guarantees neither
    attempt's worker ever notices its lease was reclaimed, so both keep
    sleeping in the background the whole time. If either woke up and called
    mark_ready while the row happened to be 'running' again (a fresh
    claim), its stale WHERE status='running' could match and corrupt the
    result. Making duration_ms far outlast the scenario means neither
    worker wakes up during the test at all — the row's terminal 'failed'
    state comes entirely from the reaper's own direct UPDATE, never from a
    worker's outcome. Both orphaned workers get cleanly cancelled by
    poison.stop() once the assertions are done.

    claim_next has no per-pool scoping (see DECISIONS.md) — it claims the
    oldest eligible 'queued' row globally, regardless of which WorkerPool
    asks. Running this second, temporary pool concurrently with the main
    one would risk each claiming the other's in-flight work, so the main
    pool is stopped for the duration of this scenario and restarted after.
    """
    await engine.stop()
    try:
        poison = Engine(dsn=dsn, concurrency=2, lease_seconds=1,
                         heartbeat_interval=100, max_attempts=2)
        poison.register_worker("scrape", mock_worker)
        await poison.start()
        try:
            task = await poison.submit(user_id, "scrape", {"duration_ms": 10000})
            track(task)
            final = await wait_for_task(poison, user_id, task.handle, task.id,
                                         {Status.FAILED}, timeout=15)
            assert final.attempts == 2, f"expected 2 attempts (max_attempts=2), got {final.attempts}"
            assert final.error is not None and final.error.retryable is False
            # Distinguishing check: this reason must come from the reap
            # path, never a worker-reported one — no ordinary WorkerResult
            # error (e.g. mock_worker's "upstream connection reset" or
            # "unsupported input format: expected JSON") ever starts with
            # "worker died mid-execution".
            assert final.error.reason.startswith("worker died mid-execution"), (
                f"expected the reap path's distinct reason (never a worker-reported "
                f"one), got: {final.error.reason!r}"
            )
            assert "attempt 2 of 2" in final.error.reason and "exhausted" in final.error.reason
            print(f"  {task.handle} poison-job protection: requeued once, then "
                  f"exhausted and failed, reason={final.error.reason!r}")
        finally:
            await poison.stop()
    finally:
        await engine.start()


SCENARIOS = [
    ("A. INSTANT HANDLE", scenario_a),
    ("B. PER-LANE NUMBERING", scenario_b),
    ("C. CONCURRENCY", scenario_c),
    ("D. CANCEL RUNNING", scenario_d),
    ("E. CANCEL QUEUED", scenario_e),
    ("F. RETRY THEN EXHAUSTION", scenario_f),
    ("G. PERMANENT FAILURE", scenario_g),
    ("H. RECYCLING", scenario_h),
    ("I. COLLECT A FAILURE", scenario_i),
    ("J. POISON-JOB PROTECTION", scenario_j_poison_job),
]


async def cleanup(dsn, task_ids):
    """Delete exactly the rows this run created, by explicit id list, so
    the script is re-runnable without accumulating rows across recordings.
    Uses the shared connection() helper — this is script housekeeping, not
    engine business logic, and CLAUDE.md's asyncpg-confinement rule scopes
    to engine/, not the whole repo.
    """
    if not task_ids:
        return
    async with connection(dsn) as conn:
        await conn.execute("DELETE FROM tasks WHERE id = ANY($1::uuid[])", task_ids)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="skip cleanup; leave created rows in place for inspection")
    args = parser.parse_args()

    # Captured before configure_logging (and before anything else that
    # might log) so every timestamp this run ever prints — engine logs and
    # events alike — measures from the exact same zero point.
    start_time = time.monotonic()

    # Without this, background-task exceptions in runner.py (dispatch loop,
    # reaper loop, per-job execution) log through Python's silent default
    # "last resort" handler, which is easy to miss. Explicit config makes
    # the engine's internal state loud in a script meant to be watched, and
    # unifies it with the event stream's own logging (see print_events).
    configure_logging(start_time)

    load_dotenv()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set (checked .env and the environment)", file=sys.stderr)
        return 1

    user_id = await ensure_user(dsn, name=DEMO_USER_NAME, api_key=DEMO_USER_API_KEY)
    print(f"demo user: {user_id}")

    engine = Engine(dsn=dsn, concurrency=2, lease_seconds=10, heartbeat_interval=3, max_attempts=3)
    engine.register_worker("scrape", mock_worker)
    engine.register_worker("report", mock_worker)
    await engine.start()
    print("engine started (concurrency=2, lease_seconds=10, heartbeat_interval=3, max_attempts=3)")

    event_log = []
    queue = engine.subscribe(user_id)
    printer = asyncio.create_task(print_events(queue, event_log))

    created_task_ids = []

    def track(task):
        created_task_ids.append(task.id)
        return task

    results = []
    try:
        for name, scenario in SCENARIOS:
            print(f"\n{'=' * 72}\nSCENARIO {name}\n{'=' * 72}")
            try:
                await scenario(engine, user_id, track, event_log, dsn)
            except Exception as e:  # noqa: BLE001 - a scenario failing must not abort the rest
                results.append((name, False, str(e)))
                print(f"  RESULT: FAIL — {e}")
            else:
                results.append((name, True, ""))
                print("  RESULT: PASS")
    finally:
        printer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await printer
        engine.unsubscribe(user_id, queue)
        await engine.stop()

        if args.keep:
            print(f"\n--keep set: leaving {len(created_task_ids)} row(s) in place for inspection")
        else:
            await cleanup(dsn, created_task_ids)
            print(f"\ncleaned up {len(created_task_ids)} row(s)")

    print(f"\n{'=' * 72}\nSUMMARY\n{'=' * 72}")
    all_passed = True
    for name, passed, detail in results:
        status = "PASS" if passed else "FAIL"
        all_passed = all_passed and passed
        line = f"  [{status}] {name}"
        if detail:
            line += f" — {detail}"
        print(line)

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
