"""Facade: submit / get / list / collect / cancel / retry / subscribe.

This is the ONLY module under engine/ that any consumer outside engine/ may
import — the FastAPI layer, a CLI, a test, a future queue consumer. No
consumer should import store.py, runner.py, handles.py, registry.py, or
events.py directly; doing so would bypass the ordering and authorisation
guarantees this class exists to enforce (e.g. cancel's mark-then-stop
ordering, get_detail's authorisation-via-sequencing). Route handlers built
on top of this become three lines each: parse, delegate, serialise.

Domain exceptions (UnknownLaneError, HandleAllocationError) are re-exported
from here so the API layer can map them to HTTP status codes without
reaching past this module either.
"""

from __future__ import annotations
# ^ Originally required because a method named list() shadowed the builtin
# for the rest of this class body, breaking every list[...]/tuple[...]
# annotation after it (see DECISIONS.md, "list() collides with the
# builtin"). That method is now list_tasks(), which removes the specific
# trap. Kept as cheap insurance against the same class of bug reappearing
# if a future method is ever named after a builtin.

import uuid
from datetime import datetime
from typing import Optional

from engine.events import EventBus, LifecycleEvent
from engine.handles import HandleAllocationError, allocate
from engine.models import Status, Task, TaskEvent
from engine.registry import UnknownLaneError, Worker, WorkerRegistry
from engine.runner import WorkerPool
from engine.store import Store

__all__ = ["Engine", "UnknownLaneError", "HandleAllocationError"]


class Engine:
    """Composes Store, WorkerRegistry, EventBus, and WorkerPool. Callers
    configure it once and never touch those four objects directly."""

    def __init__(
        self,
        dsn: str,
        concurrency: int = 2,
        lease_seconds: int = 30,
        heartbeat_interval: int = 10,
        max_attempts: int = 3,
        poll_interval: float = 0.5,
    ) -> None:
        self._dsn = dsn
        self._max_attempts = max_attempts
        self._store = Store()
        self._registry = WorkerRegistry()
        self._event_bus = EventBus()
        self._pool = WorkerPool(
            self._store,
            self._registry,
            self._event_bus,
            concurrency=concurrency,
            lease_seconds=lease_seconds,
            heartbeat_interval=heartbeat_interval,
            max_attempts=max_attempts,
            poll_interval=poll_interval,
        )

    async def start(self) -> None:
        """Connect the store, then start the pool — the pool can't claim
        anything against a connection that doesn't exist yet."""
        await self._store.connect(self._dsn)
        await self._pool.start()

    async def stop(self) -> None:
        """Reverse order from start(): stop the pool before closing the
        store, so in-flight store calls made by the pool's own shutdown
        path (cancelling tasks doesn't call the store, but a future change
        might) never run against a closed connection pool."""
        await self._pool.stop()
        await self._store.close()

    def register_worker(self, lane: str, worker: Worker) -> None:
        """Delegates to the registry. Must be called before start() —
        workers are wired once at process startup, not dynamically."""
        self._registry.register(lane, worker)

    def lanes(self) -> list[str]:
        """Registered lane names, so the API can expose them and the
        frontend can populate its lane dropdown from the engine instead of
        hardcoding it."""
        return self._registry.lanes()

    async def submit(
        self,
        user_id: uuid.UUID,
        lane: str,
        params: dict,
        seeded: bool = False,
    ) -> Task:
        """Enqueue a task and return immediately — MUST return before any
        work runs. Nothing job-related is awaited here: allocate() only
        computes a handle and inserts a row, and the pool's own dispatch
        loop is what claims and runs it later, on its own poll cycle. This
        is what criterion 1 depends on (POST returns in well under a
        second while the job itself may take 10s).

        Rejects an unknown lane BEFORE allocating a handle — a task queued
        for a lane with no worker would sit stuck forever with no
        explanation, which is exactly the silent failure the brief rules
        out. handles.allocate() would happily insert such a row; this
        check is what stops it from getting that far.
        """
        if not self._registry.has(lane):
            raise UnknownLaneError(f"no worker registered for lane {lane!r}")

        task = await allocate(
            self._store,
            user_id,
            lane,
            params,
            max_attempts=self._max_attempts,
            seeded=seeded,
        )

        # Publish AFTER the row is committed, never before — allocate()
        # either returns a task whose INSERT has already committed, or
        # raises (HandleAllocationError) without returning at all. There
        # is no path here where a task object exists but wasn't actually
        # persisted, so there's no way to publish for a task that isn't
        # real.
        self._event_bus.publish(
            LifecycleEvent.accepted(
                user_id=user_id,
                handle=task.handle,
                lane=task.lane,
                summary=f"{task.handle} accepted",
            )
        )
        return task

    async def get(self, user_id: uuid.UUID, handle: str) -> Optional[Task]:
        return await self._store.get_task(user_id, handle)

    async def list_tasks(
        self,
        user_id: uuid.UUID,
        status: Optional[Status] = None,
        lane: Optional[str] = None,
        created_after: Optional[datetime] = None,
        created_before: Optional[datetime] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Task]:
        return await self._store.list_tasks(
            user_id,
            status=status,
            lane=lane,
            created_after=created_after,
            created_before=created_before,
            limit=limit,
            offset=offset,
        )

    async def get_detail(
        self, user_id: uuid.UUID, handle: str
    ) -> Optional[tuple[Task, list[TaskEvent]]]:
        """Task plus its full state history, for the dashboard detail view.

        get_task_events() is not itself user-scoped (see store.py) — the
        authorisation boundary is enforced HERE, by the sequencing: it is
        only ever called with a task.id obtained from get_task(user_id,
        handle), which already proved this user_id owns the handle. There
        is no path in this method that reaches get_task_events with an id
        this user_id hasn't been checked against.
        """
        task = await self._store.get_task(user_id, handle)
        if task is None:
            return None
        events = await self._store.get_task_events(task.id)
        return task, events

    async def collect(self, user_id: uuid.UUID, handle: str) -> Optional[Task]:
        """Works for 'ready' AND 'failed' — collection is the ACK that
        releases the handle for reuse either way, not just on success.
        Returns None if the task isn't collectable: wrong status, or
        already collected."""
        return await self._store.collect_result(user_id, handle)

    async def cancel(self, user_id: uuid.UUID, handle: str) -> Optional[Task]:
        """Cancel a queued or running task.

        Ordering is load-bearing, not a style choice: mark the DB row
        cancelled FIRST, and only THEN stop the asyncio task. If this were
        reversed — stop the asyncio task, then mark the row — a worker
        that was already mid-`await store.mark_ready(...)` could commit a
        'ready' row before we ever touch the DB, and our cancel would then
        overwrite a real success with 'cancelled', or (worse, depending on
        timing) simply lose the race silently. Marking first closes that
        window: mark_ready/mark_failed are conditional on status='running'
        (see store.py), so once the row says 'cancelled', a worker that
        finishes afterward finds its own conditional UPDATE affects zero
        rows and correctly records nothing. Criterion 6 depends on exactly
        this — no late 'ready' event may ever arrive for a cancelled job.
        """
        task = await self._store.cancel_task(user_id, handle)
        if task is None:
            return None
        await self._pool.cancel_running(task.id)
        self._event_bus.publish(
            LifecycleEvent.cancelled(user_id=user_id, handle=task.handle, lane=task.lane)
        )
        return task

    async def retry(self, user_id: uuid.UUID, handle: str) -> Optional[Task]:
        """Operator-triggered retry of a failed job: failed -> queued,
        attempts reset to 0, error cleared (store.reset_for_retry).

        Publishes NO lifecycle event, deliberately: this is the same
        handle re-entering 'queued', not a new task, so 'accepted' would
        be wrong — a client treating 'accepted' as "add a row" would
        duplicate an existing one. Same reasoning as the automatic
        retry-requeue path in runner.py: only terminal failure is a
        lifecycle event; every other return to 'queued' is not (see
        DECISIONS.md, "A retry emits no lifecycle event"). The transition
        is still fully recorded in task_events via reset_for_retry.

        Returns None if the task isn't in 'failed' — there is nothing to
        retry.
        """
        return await self._store.reset_for_retry(user_id, handle)

    def subscribe(self, user_id: uuid.UUID):
        """Returns the raw bounded queue from EventBus. The engine knows
        nothing about SSE — that translation is api/events.py's job."""
        return self._event_bus.subscribe(user_id)

    def unsubscribe(self, user_id: uuid.UUID, queue) -> None:
        self._event_bus.unsubscribe(user_id, queue)
