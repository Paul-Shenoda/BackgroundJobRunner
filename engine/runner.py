"""The worker pool: claims tasks, runs them under a concurrency limit,
heartbeats their leases, and reacts to cancellation and crash recovery.

This is the only module that owns asyncio.Task objects for in-flight jobs.
Everything it does to task state goes through store.py — it never touches
SQL directly, and it never inspects job params (that would be lane-specific
knowledge, which belongs in the worker, not here).
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from engine.events import EventBus, LifecycleEvent
from engine.models import Job, Status, Task, TaskError, WorkerResult
from engine.registry import Worker, WorkerRegistry
from engine.store import TRANSIENT_DB_ERRORS, Store

logger = logging.getLogger(__name__)

# Backoff for the background loops when the database is briefly unreachable.
_DB_RETRY_INITIAL = 1.0
_DB_RETRY_MAX = 30.0


class WorkerPool:
    """Runs claimed tasks under a concurrency limit until stop() is called.

    Lifecycle: start() launches the dispatch loop and the reaper loop as
    background asyncio tasks and returns immediately. stop() tears both
    down along with any in-flight job execution.
    """

    def __init__(
        self,
        store: Store,
        registry: WorkerRegistry,
        event_bus: EventBus,
        concurrency: int = 2,
        lease_seconds: int = 50,
        heartbeat_interval: int = 10,
        max_attempts: int = 3,
        poll_interval: float = 0.5,
    ) -> None:
        # Mirrors Engine.MIN_LEASE_TO_HEARTBEAT_RATIO's default ratio (5x)
        # for consistency's sake, but the enforced invariant itself lives
        # only on Engine.__init__ — this class is internal-only (see
        # engine.py's module docstring: Engine is the sole public entry
        # point under engine/), so it's Engine's job to validate, not this
        # one's to duplicate that check.
        self._store = store
        self._registry = registry
        self._event_bus = event_bus
        self._concurrency = concurrency
        self._lease_seconds = lease_seconds
        self._heartbeat_interval = heartbeat_interval
        # max_attempts is accepted for signature completeness (matches the
        # originally-specified constructor) but deliberately not stored:
        # task.max_attempts (the per-row DB column, set at submission time
        # by engine.py) is what _record_outcome actually checks, and
        # nothing in this class ever reads a pool-level copy.
        self._poll_interval = poll_interval
        self._worker_id = str(uuid.uuid4())

        self._shutdown = asyncio.Event()
        self._running: dict[uuid.UUID, asyncio.Task] = {}
        self._dispatch_task: asyncio.Task | None = None
        self._reaper_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Run boot recovery once, synchronously, then launch the loops.

        Recovering expired leases BEFORE the dispatch loop starts is what
        makes restart recovery correct: any task a previous process left
        'running' (killed, crashed, or SIGKILLed) with an expired lease is
        requeued before this process claims anything new. Running it again
        on a timer (the reaper loop) covers a worker that hangs rather than
        dies — boot-only recovery would miss that case entirely.
        """
        recovered = await self._store.reap_expired_leases()
        if recovered:
            failed = [t for t in recovered if t.status == Status.FAILED]
            logger.info(
                "startup recovery: reclaimed %d expired lease(s) "
                "(%d requeued, %d exhausted -> failed): %s",
                len(recovered),
                len(recovered) - len(failed),
                len(failed),
                [t.handle for t in recovered],
            )
            self._publish_reap_failures(recovered)
        self._shutdown.clear()
        self._dispatch_task = asyncio.create_task(self._dispatch_loop())
        self._reaper_task = asyncio.create_task(self._reaper_loop())

    async def stop(self) -> None:
        """Signal shutdown, cancel everything in-flight, and wait for it to
        actually stop. Runs on SIGTERM, so it must complete quickly rather
        than waiting on anything unbounded.

        No graceful/hard-stop distinction: a SIGKILL never reaches this
        method at all, so the reaper has to be correct with no shutdown
        hook regardless (see the tradeoff note in _reaper_loop's
        docstring). A flag that would make both branches behave the same
        is worse than no flag — see DECISIONS.md, "No graceful-shutdown
        mode; the reaper is the only recovery path" (2026-07-29).

        In-flight worker tasks are cancelled but their DB rows are left
        'running' with a live lease — we do not race to mark them on the
        way out. See _reaper_loop's docstring for why that's the right
        tradeoff rather than a shortcut.
        """
        self._shutdown.set()

        pending: list[asyncio.Task] = []
        for loop_task in (self._dispatch_task, self._reaper_task):
            if loop_task is not None:
                loop_task.cancel()
                pending.append(loop_task)
        for job_task in self._running.values():
            job_task.cancel()
            pending.append(job_task)

        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        # Explicit, not relied-upon-by-inference: every job_task's
        # done-callback (_on_job_done) already pops its own entry, and
        # asyncio's callback-ordering guarantee means that's already
        # happened by the time gather() above returns. Clearing here turns
        # that implementation detail into an enforced invariant instead of
        # an assumption a future refactor could quietly break.
        self._running.clear()
        self._dispatch_task = None
        self._reaper_task = None

    async def _dispatch_loop(self) -> None:
        """Claim and spawn jobs until shutdown.

        len(self._running) is the concurrency gate. This coroutine is the
        only writer of self._running entries (done-callbacks only remove),
        so there's no race to guard against between the length check and
        the eventual insert below — it's all sequential within one
        coroutine.

        Wrapped in a broad except that logs before re-raising: without it,
        an exception here (e.g. a SQL error in claim_next) kills this loop
        silently — WorkerPool.stop()'s `asyncio.gather(..., return_exceptions=True)`
        retrieves the exception without ever printing it, so the pool would
        quietly stop claiming any work with zero trace. That's exactly the
        silent-failure mode the brief rules out; logger.exception() here is
        what makes it loud instead.

        Transient database outages (TRANSIENT_DB_ERRORS, e.g. Postgres restarting
        for maintenance) are NOT crashes: the loop logs a warning, backs off
        (1s doubling to 30s) and keeps claiming once the database is back.
        Before this, a nightly Postgres restart killed the loop for good while
        the process kept serving HTTP, so new jobs queued forever.
        """
        backoff = _DB_RETRY_INITIAL
        try:
            while not self._shutdown.is_set():
                if len(self._running) >= self._concurrency:
                    await asyncio.sleep(self._poll_interval)
                    continue

                try:
                    task = await self._store.claim_next(self._worker_id, self._lease_seconds)
                except TRANSIENT_DB_ERRORS as exc:
                    logger.warning(
                        "dispatch: database unavailable (%s: %s); retrying in %.0fs",
                        type(exc).__name__, exc, backoff,
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, _DB_RETRY_MAX)
                    continue
                backoff = _DB_RETRY_INITIAL

                if task is None:
                    await asyncio.sleep(self._poll_interval)
                    continue

                logger.info(
                    "dispatch: claimed task %s (lane=%s, attempt=%d)",
                    task.handle, task.lane, task.attempts,
                )

                if not self._registry.has(task.lane):
                    await self._fail_unknown_lane(task)
                    continue

                worker = self._registry.get(task.lane)
                job_task = asyncio.create_task(self._execute_job(task, worker))
                self._running[task.id] = job_task
                # Default-arg capture: task.id/task.handle must be bound at
                # closure-creation time, not looked up when the callback
                # fires — the loop variable would otherwise have moved on
                # to a different task.
                job_task.add_done_callback(
                    lambda t, task_id=task.id, handle=task.handle: self._on_job_done(t, task_id, handle)
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("dispatch loop crashed; the pool will stop claiming new work")
            raise

    def _on_job_done(self, job_task: asyncio.Task, task_id: uuid.UUID, handle: str) -> None:
        """Done-callback for a per-job execution task: always removes it
        from self._running, and — this is the part that matters — surfaces
        any exception that _execute_job let escape.

        _execute_job's own try/except already converts a worker crash into
        a WorkerResult, so under normal operation nothing should ever
        reach here as an unhandled exception. But _record_outcome's store
        calls are NOT wrapped, so a bug there (or a dropped DB connection
        mid-job) would otherwise die exactly the same silent way the
        dispatch loop did: parked on the task object, never logged, and
        eventually discarded by stop()'s `gather(..., return_exceptions=True)`.
        Calling .exception() on a cancelled task is unsafe — it re-raises
        CancelledError — so that case is checked and skipped first.
        Cancellation is an expected, already-handled outcome, not a crash.

        The pop is identity-checked, not unconditional: if the reaper
        reclaims this task's lease while its worker is genuinely still
        running (heartbeat trouble, or just slow to notice), the row can go
        back to 'queued' and get re-claimed by _dispatch_loop before this
        (the ORIGINAL) job finishes — which overwrites self._running[task_id]
        with the NEW job_task. When the original then finally completes and
        this callback fires, popping unconditionally would remove the NEW
        job's entry, not the (already-gone) original's — silently
        orphaning a task that is, in fact, still actively running from
        self._running's bookkeeping. Comparing identity before popping
        means this callback only ever removes the entry it actually put
        there.
        """
        if self._running.get(task_id) is job_task:
            self._running.pop(task_id, None)
        if job_task.cancelled():
            return
        exc = job_task.exception()
        if exc is not None:
            logger.error(
                "job execution for handle=%s (task_id=%s) ended with an unhandled exception",
                handle,
                task_id,
                exc_info=exc,
            )

    async def _fail_unknown_lane(self, task: Task) -> None:
        """A claimed task whose lane has no registered worker must not be
        left stuck in 'running' — that would hold its lease and its handle
        forever. Fail it immediately, non-retryable: no worker is ever
        going to show up for a lane that doesn't exist."""
        reason = f"no worker registered for lane {task.lane!r}"
        logger.error("task %s claimed for unknown lane: %s", task.handle, reason)
        updated = await self._store.mark_failed(task.id, TaskError(reason=reason, retryable=False))
        if updated is None:
            # Lost the race (e.g. cancelled between claim and this check).
            # Nothing to report — a transition that didn't happen gets no event.
            return
        self._event_bus.publish(
            LifecycleEvent.failed(
                user_id=task.user_id,
                handle=task.handle,
                lane=task.lane,
                reason=reason,
                retryable=False,
            )
        )

    async def _execute_job(self, task: Task, worker: Worker) -> None:
        """Run one claimed job to a terminal outcome, or let cancellation
        propagate untouched.

        Runs the worker as its own asyncio.Task (not just `await worker(job)`
        inline) specifically so _heartbeat_loop can cancel it directly when
        the lease is lost — cancelling THIS coroutine wouldn't be enough on
        its own if the heartbeat loop were the thing that noticed the
        problem, since it isn't the one blocked on the worker call.

        Exception handling order is the single most load-bearing thing in
        this file: CancelledError MUST be re-raised before the general
        `except Exception` runs, or a cancelled job gets recorded as a
        bogus failure instead of staying cancelled. On Python 3.12
        CancelledError inherits from BaseException, so `except Exception`
        wouldn't actually catch it here regardless — the explicit re-raise
        is written anyway because it documents the intent and protects
        against anyone later widening that clause to `except BaseException`.
        """
        job = Job(handle=task.handle, lane=task.lane, params=task.params)
        worker_task: asyncio.Task = asyncio.create_task(worker(job))
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(task.id, worker_task))

        try:
            result = await worker_task
        except asyncio.CancelledError:
            raise                       # MUST come first — see docstring
        except Exception as e:
            reason = f"unexpected error: {type(e).__name__}: {e}"
            logger.exception("worker crashed for task %s", task.handle)
            result = WorkerResult(status="failed", error=TaskError(reason=reason, retryable=True))
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            except Exception:
                # A bug in the heartbeat loop itself must not masquerade as
                # (or clobber) the job's real outcome — log and move on.
                logger.exception("heartbeat loop raised for task %s", task.handle)

        await self._record_outcome(task, result)

    async def _heartbeat_loop(self, task_id: uuid.UUID, worker_task: asyncio.Task) -> None:
        """Extend the lease every heartbeat_interval while the worker runs.

        If store.heartbeat() reports the row is no longer 'running' — it
        was cancelled, or a previous reaper pass already reclaimed a lease
        we thought we still held — the worker no longer has anything valid
        to finish into. Cancel it and stop; _execute_job's `await
        worker_task` will raise CancelledError and take the
        record-nothing/publish-nothing path.

        Any OTHER exception here (e.g. a transient DB error on the
        heartbeat call itself) gets the same treatment, not just a log: we
        can no longer vouch for owning the lease either way, so cancelling
        the worker is the correct response to "heartbeat trouble", not only
        to "heartbeat explicitly says we lost it". Without this, an
        unguarded exception would kill this loop silently — the worker
        would keep running, unaware, while its lease quietly expired, and
        the reaper could reclaim and re-dispatch the same task while the
        original was still genuinely alive. No re-raise after cancelling:
        the cancellation IS the fix, and _execute_job's own finally block
        already awaits this task and would just log the same exception a
        second time for no benefit.
        """
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval)
                still_ours = await self._store.heartbeat(task_id, self._lease_seconds)
                if not still_ours:
                    logger.warning(
                        "lease no longer held for task_id=%s; cancelling worker", task_id
                    )
                    worker_task.cancel()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "heartbeat failed for task_id=%s; can no longer guarantee lease "
                "ownership, cancelling worker",
                task_id,
            )
            worker_task.cancel()

    async def _record_outcome(self, task: Task, result: WorkerResult) -> None:
        """Translate a terminal WorkerResult into store calls and lifecycle
        events.

        Every store call here is conditional on the row still being
        'running' (see mark_ready/mark_failed/requeue_for_retry in
        store.py). If we lost that race — cancelled or reaped between the
        worker finishing and this method running — the store call returns
        None. A transition that didn't happen gets no event.
        """
        if result.status == "ready":
            updated = await self._store.mark_ready(task.id, result.result)
            if updated is None:
                return
            self._event_bus.publish(
                LifecycleEvent.ready(
                    user_id=task.user_id,
                    handle=task.handle,
                    lane=task.lane,
                    summary=f"{task.handle} completed",
                )
            )
            return

        error = result.error
        assert error is not None  # WorkerResult contract: status=="failed" implies error is set

        if not error.retryable:
            updated = await self._store.mark_failed(task.id, error)
            if updated is None:
                return
            self._event_bus.publish(
                LifecycleEvent.failed(
                    user_id=task.user_id,
                    handle=task.handle,
                    lane=task.lane,
                    reason=error.reason,
                    retryable=False,
                )
            )
            return

        if task.attempts >= task.max_attempts:
            reason = (
                f"{error.reason} "
                f"(attempt {task.attempts} of {task.max_attempts}, retries exhausted)"
            )
            updated = await self._store.mark_failed(task.id, TaskError(reason=reason, retryable=False))
            if updated is None:
                return
            self._event_bus.publish(
                LifecycleEvent.failed(
                    user_id=task.user_id,
                    handle=task.handle,
                    lane=task.lane,
                    reason=reason,
                    retryable=False,
                )
            )
            return

        # Retryable, budget remaining: back to queued with exponential
        # backoff (1s, 2s, 4s, ... = 2**(attempts-1)). A retry is not a
        # lifecycle event — only terminal failure is, so nothing is
        # published here.
        backoff = timedelta(seconds=2 ** (task.attempts - 1))
        not_before = datetime.now(timezone.utc) + backoff
        await self._store.requeue_for_retry(task.id, not_before, error.reason)

    async def cancel_running(self, task_id: uuid.UUID) -> bool:
        """Cancel the in-flight asyncio task for task_id, if this process
        holds it. Returns whether it did.

        Caller contract: engine.cancel() marks the DB row 'cancelled'
        BEFORE calling this — never after. That ordering is what prevents
        the worker from winning a race and marking itself 'ready' in the
        gap between "decided to cancel" and "actually cancelled the task".

        A False return is a normal outcome, not an error: it means the
        task was 'queued', not 'running' — engine.cancel()'s DB update was
        the only thing that needed to happen.
        """
        job_task = self._running.get(task_id)
        if job_task is None:
            return False
        job_task.cancel()
        return True

    def is_running(self) -> bool:
        """True if both background loops are still alive. A process-local
        liveness signal for GET /health: the dispatch and reaper loops now
        log loudly instead of dying silently on an unhandled exception (see
        DECISIONS.md), but "logged" doesn't mean "the loop is still there"
        — if either task has actually ended, the process keeps accepting
        HTTP requests while quietly no longer claiming or reaping work,
        which is exactly the degraded state /health needs to be able to
        tell apart from a healthy one."""
        return (
            self._dispatch_task is not None
            and not self._dispatch_task.done()
            and self._reaper_task is not None
            and not self._reaper_task.done()
        )

    def running_count(self) -> int:
        """len(self._running) — jobs this process currently has in flight.
        Process-local by construction, no DB round trip: it answers "is
        this process doing anything," not "how much work is queued
        cluster-wide" (that's queue depth, a different and DB-backed
        question — see DECISIONS.md on why GET /health doesn't answer it)."""
        return len(self._running)

    async def _reaper_loop(self) -> None:
        """The periodic half of restart recovery. start() already runs
        reap_expired_leases() once at boot; this covers the case boot-only
        recovery misses — a worker that hangs rather than dies, whose
        lease quietly expires while the process is still very much alive.

        Does not explicitly exclude self._running's own tasks, and doesn't
        need to: reap_expired_leases() only touches rows whose lease has
        actually expired, and this process is heartbeating (extending
        lease_expires_at) every heartbeat_interval for everything it
        holds. A task in self._running only ever has an expired lease if
        this process already failed to heartbeat it — in which case
        reaping it is the correct outcome, not a bug to guard against.

        Shutdown tradeoff: on an unclean kill (SIGKILL), nothing in this
        class runs at all — no cleanup code can execute after a SIGKILL
        no matter where it's placed. That means this loop (and the
        identical boot-time call in start()) is the ONLY mechanism that
        can ever be correct for recovering a dead worker's in-flight jobs;
        a shutdown hook that tries to mark rows on the way down would be
        redundant machinery layered on a guarantee that already has to
        hold unconditionally. That's why stop() doesn't try to mark
        in-flight rows before cancelling them — it cancels and leaves
        their leases to expire, accepting up to lease_seconds of delay
        before the next reap (boot-time or periodic) reclaims them.

        Like the dispatch loop, a transient database outage is logged and
        retried on the next tick (every lease_seconds / 2) instead of ending
        the loop.
        """
        try:
            while not self._shutdown.is_set():
                await asyncio.sleep(self._lease_seconds / 2)
                try:
                    recovered = await self._store.reap_expired_leases()
                except TRANSIENT_DB_ERRORS as exc:
                    logger.warning(
                        "reaper: database unavailable (%s: %s); retrying next tick",
                        type(exc).__name__, exc,
                    )
                    continue
                if recovered:
                    failed = [t for t in recovered if t.status == Status.FAILED]
                    logger.info(
                        "reaper: reclaimed %d expired lease(s) "
                        "(%d requeued, %d exhausted -> failed): %s",
                        len(recovered),
                        len(recovered) - len(failed),
                        len(failed),
                        [t.handle for t in recovered],
                    )
                    for task in failed:
                        logger.warning(
                            "task %s exhausted its retry budget via repeated lease "
                            "expiry (attempt %d of %d); marked failed",
                            task.handle, task.attempts, task.max_attempts,
                        )
                    self._publish_reap_failures(recovered)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("reaper loop crashed; expired leases will no longer be reclaimed")
            raise

    def _publish_reap_failures(self, recovered: list[Task]) -> None:
        """Publish a 'failed' LifecycleEvent for every row reap_expired_leases
        just terminated outright (attempts exhausted while its lease kept
        expiring) — mirrors _record_outcome's publish for the WorkerResult
        exhaustion path. Rows it merely requeued publish nothing, per
        DECISIONS.md, "A retry emits no lifecycle event, whether automatic
        or operator-triggered" — a reap-triggered requeue is exactly that
        kind of automatic retry.
        """
        for task in recovered:
            if task.status != Status.FAILED:
                continue
            assert task.error is not None  # set by reap_expired_leases's CASE branch
            self._event_bus.publish(
                LifecycleEvent.failed(
                    user_id=task.user_id,
                    handle=task.handle,
                    lane=task.lane,
                    reason=task.error.reason,
                    retryable=False,
                )
            )
