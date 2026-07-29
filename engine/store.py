"""ALL SQL lives here. Nothing else touches the database.

This is the only file under engine/ (or anywhere in the codebase) permitted
to import asyncpg.

This module is a data-access layer: it owns persistence and atomicity, not
policy. Business decisions (retry budgets, backoff maths, what a lane means)
live upstream in engine.py and runner.py.
"""

import json
import uuid
from datetime import datetime
from typing import Any

import asyncpg

from engine.models import Status, Task, TaskError, TaskEvent


class HandleConflict(Exception):
    """Raised by insert_task when the (user_id, lane, number) it tried to
    write is already held. Translates asyncpg.UniqueViolationError into a
    domain exception so no module outside store.py ever needs to import
    asyncpg — handles.py's retry loop catches this, not the driver error."""


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Register the jsonb codec on every pooled connection.

    Without this, asyncpg hands back jsonb columns (params/result/error) as
    raw JSON text, not Python objects — a silent, wrong-shape bug rather than
    a loud one. format='text' is required for jsonb specifically: its default
    binary wire format carries a leading version byte that json.loads can't
    parse.
    """
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
        format="text",
    )


def _row_to_task(row: asyncpg.Record) -> Task:
    """Every read path goes through this. Coerces types, doesn't just splat
    the row — see module docstring on the jsonb codec and status column."""
    return Task(
        id=row["id"],
        user_id=row["user_id"],
        lane=row["lane"],
        number=row["number"],
        handle=row["handle"],
        params=row["params"],
        status=Status(row["status"]),
        result=row["result"],
        error=TaskError(**row["error"]) if row["error"] is not None else None,
        collected=row["collected"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        not_before=row["not_before"],
        worker_id=row["worker_id"],
        lease_expires_at=row["lease_expires_at"],
        seeded=row["seeded"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        seq=row["seq"],
    )


def _row_to_task_event(row: asyncpg.Record) -> TaskEvent:
    """Same coercion principle as _row_to_task: from_status is nullable
    (the creation event has no prior state), to_status is not."""
    return TaskEvent(
        from_status=Status(row["from_status"]) if row["from_status"] is not None else None,
        to_status=Status(row["to_status"]),
        reason=row["reason"],
        attempt=row["attempt"],
        created_at=row["created_at"],
    )


async def _record_transition(
    conn: asyncpg.Connection,
    task_id: uuid.UUID,
    from_status: Status | None,
    to_status: Status,
    reason: str | None,
    attempt: int | None,
) -> None:
    """INSERT into task_events. Takes an explicit connection so it can join
    a caller's transaction — see the rule in Store: every tasks UPDATE and
    its task_events INSERT happen inside one transaction, or the detail
    view's state history is a lie."""
    await conn.execute(
        """
        INSERT INTO task_events (task_id, from_status, to_status, reason, attempt)
        VALUES ($1, $2, $3, $4, $5)
        """,
        task_id,
        from_status.value if from_status is not None else None,
        to_status.value,
        reason,
        attempt,
    )


class Store:
    """Owns the connection pool. Every query method below hangs off this.

    Rule for every state-changing method below: the tasks UPDATE and the
    task_events INSERT (via _record_transition) happen inside ONE
    transaction, acquired from the pool and shared by both statements, and
    updated_at is bumped to now() in the UPDATE. If a transition is visible
    in tasks but missing from task_events, the detail view's state history
    is a lie. Never write one without the other.
    """

    def __init__(self) -> None:
        self._pool: asyncpg.Pool | None = None

    async def connect(self, dsn: str) -> None:
        self._pool = await asyncpg.create_pool(dsn=dsn, init=_init_connection)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()

    async def insert_task(
        self,
        user_id: uuid.UUID,
        lane: str,
        number: int,
        params: dict,
        max_attempts: int = 3,
        seeded: bool = False,
    ) -> Task:
        """INSERT ... RETURNING *, plus the NULL -> queued creation event.

        max_attempts defaults to 3, matching sql/schema.sql's column
        default, so a direct caller that doesn't care still gets the same
        behaviour as before this parameter existed. engine.submit() passes
        its configured pool-level default explicitly rather than relying
        on this fallback (see engine.py).

        Raises HandleConflict (not asyncpg.UniqueViolationError) when the
        (user_id, lane, number) is already held by another row — the
        translation happens here so handles.py's retry loop never needs to
        import asyncpg. Running the INSERT inside a transaction doesn't
        change the propagation: an uncaught/re-raised exception still rolls
        the transaction back before it reaches the caller.

        Records a from_status=NULL, to_status='queued' task_events row so
        the detail view's state history has a beginning, not just whatever
        transition happens to occur first.
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                try:
                    row = await conn.fetchrow(
                        """
                        INSERT INTO tasks (user_id, lane, number, params, max_attempts, seeded)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        RETURNING *
                        """,
                        user_id,
                        lane,
                        number,
                        params,
                        max_attempts,
                        seeded,
                    )
                except asyncpg.UniqueViolationError as e:
                    raise HandleConflict(
                        f"handle {lane}-{number} already held for user {user_id}"
                    ) from e
                task = _row_to_task(row)
                await _record_transition(
                    conn, task.id, None, Status.QUEUED, None, task.attempts
                )
                return task

    async def get_task(self, user_id: uuid.UUID, handle: str) -> Task | None:
        """A handle string is not unique across all time — recycling means
        an old, terminal row and a new, active row can share the same
        (user_id, lane, number) once the old one is collected or
        cancelled. Without disambiguation this query is genuinely
        ambiguous whenever that happens: uniq_active_handle guarantees at
        most ONE row can ever be active for a given handle, but says
        nothing about how many terminal rows accumulate under it over
        time, and a plain WHERE with no ORDER BY returns Postgres's scan
        order, not any meaningful one.

        Resolution order: the active row wins if one exists (a handle
        names a currently-active task first and foremost); otherwise the
        most recent historical row, so a client that just collected
        scrape-1 and immediately re-fetches it still sees it instead of a
        404. The boolean ORDER BY expression mirrors uniq_active_handle's
        predicate exactly — TRUE (active) sorts before FALSE, and at most
        one row can ever be TRUE, so that part is deterministic no matter
        what.

        The historical-fallback tie-break is `seq DESC`, not `created_at
        DESC` — this used to be created_at, and a live run caught the gap:
        a system clock jump (NTP correction, VM suspend/resume, container
        migration) can make Postgres's own now() briefly disagree with true
        insertion order, so two historical rows' created_at values can come
        back in the wrong order even though nothing about the query logic
        is wrong. seq (BIGSERIAL) is assigned by a database sequence, not
        the wall clock, so it's monotonic by construction regardless of
        what the system clock does — and since it's a sequence, it's also
        always unique, so no further tie-break is needed after it. See
        DECISIONS.md, "handle resolution ordering uses a monotonic
        sequence, not a timestamp". created_at remains the user-facing
        timestamp in the API contract; it's just no longer load-bearing for
        this correctness question.
        """
        row = await self._pool.fetchrow(
            """
            SELECT * FROM tasks
             WHERE user_id = $1 AND handle = $2
             ORDER BY (
                 status IN ('queued', 'running')
                 OR (status IN ('ready', 'failed') AND collected = false)
             ) DESC, seq DESC
             LIMIT 1
            """,
            user_id,
            handle,
        )
        return _row_to_task(row) if row is not None else None

    async def list_tasks(
        self,
        user_id: uuid.UUID,
        status: Status | None = None,
        lane: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Task]:
        """Every user-facing read filters on user_id — a user must never see
        another user's tasks, no exceptions. Filter values are always bound
        as $N placeholders; only fixed column names/operators are
        interpolated into the query text."""
        conditions = ["user_id = $1"]
        params: list[Any] = [user_id]

        if status is not None:
            params.append(status.value)
            conditions.append(f"status = ${len(params)}")
        if lane is not None:
            params.append(lane)
            conditions.append(f"lane = ${len(params)}")
        if created_after is not None:
            params.append(created_after)
            conditions.append(f"created_at >= ${len(params)}")
        if created_before is not None:
            params.append(created_before)
            conditions.append(f"created_at <= ${len(params)}")

        params.append(limit)
        limit_idx = len(params)
        params.append(offset)
        offset_idx = len(params)

        query = f"""
            SELECT * FROM tasks
            WHERE {' AND '.join(conditions)}
            ORDER BY created_at DESC
            LIMIT ${limit_idx} OFFSET ${offset_idx}
        """
        rows = await self._pool.fetch(query, *params)
        return [_row_to_task(row) for row in rows]

    async def get_task_events(self, task_id: uuid.UUID) -> list[TaskEvent]:
        """Ordered ascending — chronological state history.

        Takes task_id, not user_id: the ownership check must already have
        happened upstream via get_task before a caller obtains this id.
        """
        rows = await self._pool.fetch(
            "SELECT * FROM task_events WHERE task_id = $1 ORDER BY created_at ASC",
            task_id,
        )
        return [_row_to_task_event(row) for row in rows]

    async def lowest_free_number(self, user_id: uuid.UUID, lane: str) -> int:
        """Lowest positive integer not currently held for (user_id, lane).

        This is SQL, so it lives here even though the allocation *policy*
        lives in handles.py. handles.py calls this, then calls insert_task,
        and retries on UniqueViolationError.

        The "held" predicate below MUST match uniq_active_handle in
        sql/schema.sql exactly. If they ever diverge, allocation degenerates
        into a retry loop that always loses.
        """
        return await self._pool.fetchval(
            """
            WITH held AS (
                SELECT number FROM tasks
                WHERE user_id = $1 AND lane = $2
                  AND (
                      status IN ('queued','running')
                      OR (status IN ('ready','failed') AND collected = false)
                  )
            )
            SELECT MIN(gs.n)
            FROM generate_series(1, (SELECT COALESCE(MAX(number), 0) FROM held) + 1) AS gs(n)
            WHERE gs.n NOT IN (SELECT number FROM held)
            """,
            user_id,
            lane,
        )

    async def claim_next(self, worker_id: str, lease_seconds: int) -> Task | None:
        """Claim the oldest eligible queued task for this worker.

        This claim is NOT user-scoped — the pool serves all users.

        FOR UPDATE SKIP LOCKED is load-bearing: without it, concurrent
        claimers would serialise behind a lock on the same candidate row
        instead of each landing on a distinct one, and the worker pool
        would stop being a pool.
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    UPDATE tasks
                       SET status = 'running',
                           worker_id = $1,
                           lease_expires_at = now() + make_interval(secs => $2),
                           attempts = attempts + 1,
                           updated_at = now()
                     WHERE id = (
                           SELECT id FROM tasks
                            WHERE status = 'queued'
                              AND (not_before IS NULL OR not_before <= now())
                            ORDER BY created_at
                              FOR UPDATE SKIP LOCKED
                            LIMIT 1)
                    RETURNING *
                    """,
                    worker_id,
                    lease_seconds,
                )
                if row is None:
                    return None
                task = _row_to_task(row)
                await _record_transition(
                    conn,
                    task.id,
                    Status.QUEUED,
                    Status.RUNNING,
                    None,
                    task.attempts,
                )
                return task

    async def heartbeat(self, task_id: uuid.UUID, lease_seconds: int) -> bool:
        """Extend the lease on a running task. Returns whether a row was
        updated — False means the job was cancelled or reclaimed
        underneath us, which the runner needs to know."""
        result = await self._pool.execute(
            """
            UPDATE tasks
               SET lease_expires_at = now() + make_interval(secs => $2)
             WHERE id = $1 AND status = 'running'
            """,
            task_id,
            lease_seconds,
        )
        return result == "UPDATE 1"

    async def reap_expired_leases(self) -> list[Task]:
        """Requeue tasks whose lease expired without a heartbeat — or, if
        the task has now burned its whole attempt budget doing so, fail it
        outright instead.

        Every claim increments attempts (see claim_next), including a claim
        that never gets a heartbeat — see DECISIONS.md, "A crash-requeue
        consumes an attempt" (poison-job protection). This method is the
        ONLY place that decision is actually enforced on the reap path: a
        task whose worker never returns a WorkerResult (crashed outright,
        or hung and got cancelled by _heartbeat_loop) never reaches
        runner.py's _record_outcome, which is the only other place attempts
        is compared against max_attempts. Skipping the check here would let
        such a task reclaim and requeue forever, attempts climbing
        unbounded, never reaching a terminal state.

        The attempts >= max_attempts comparison is embedded directly in the
        UPDATE's CASE, not resolved via a separate SELECT-then-branch step,
        so the exhausted case can transition RUNNING -> FAILED directly and
        atomically — never surfacing a dishonest intermediate RUNNING ->
        QUEUED. This is a deliberate, narrow exception to this module's
        usual rule that retry-budget policy lives upstream (see module
        docstring): runner.py only ever observes the row after this
        statement has already committed, by which point a wrongly-taken
        QUEUED transition could not be undone without a second, misleading
        event.

        The exhaustion reason is deliberately worded differently from a
        worker-reported exhaustion reason (runner.py's _record_outcome):
        "worker died mid-execution" is honest about what actually happened
        here — the worker never returned anything, the lease just stopped
        being renewed — versus a worker-reported reason like "upstream
        connection reset", which describes the work itself failing. Same
        exhaustion outcome, different honest cause (CLAUDE.md requires
        distinct, honest reasons).

        Returns the full recovered Task rows (not just handles), coerced
        via _row_to_task, so the caller can tell the two outcomes apart via
        .status and, for the failed ones, construct a 'failed'
        LifecycleEvent — mirroring how _record_outcome does the same for
        the WorkerResult exhaustion path.
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    """
                    UPDATE tasks
                       SET status = (CASE WHEN attempts >= max_attempts
                                           THEN 'failed' ELSE 'queued' END)::task_status,
                           worker_id = NULL,
                           lease_expires_at = NULL,
                           error = CASE WHEN attempts >= max_attempts THEN
                                       jsonb_build_object(
                                           'reason',
                                           'worker died mid-execution (attempt ' || attempts
                                           || ' of ' || max_attempts || ', retries exhausted)',
                                           'retryable', false
                                       )
                                   ELSE error END,
                           updated_at = now()
                     WHERE status = 'running' AND lease_expires_at < now()
                    RETURNING *
                    """
                )
                recovered = [_row_to_task(row) for row in rows]
                for task in recovered:
                    if task.status == Status.FAILED:
                        assert task.error is not None  # set by the CASE branch above
                        await _record_transition(
                            conn, task.id, Status.RUNNING, Status.FAILED,
                            task.error.reason, task.attempts,
                        )
                    else:
                        await _record_transition(
                            conn, task.id, Status.RUNNING, Status.QUEUED,
                            "lease expired; reclaimed by reaper", task.attempts,
                        )
                return recovered

    async def mark_ready(self, task_id: uuid.UUID, result: Any) -> Task | None:
        """Conditional: only succeeds from 'running'. Returns None if the
        row wasn't in that state — a lost race (already cancelled/reclaimed)
        must be visible to the caller, never silently ignored.

        Clears worker_id/lease_expires_at: once a task leaves 'running',
        those fields no longer mean anything and shouldn't linger.
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    UPDATE tasks
                       SET status = 'ready',
                           result = $2,
                           worker_id = NULL,
                           lease_expires_at = NULL,
                           updated_at = now()
                     WHERE id = $1 AND status = 'running'
                    RETURNING *
                    """,
                    task_id,
                    result,
                )
                if row is None:
                    return None
                task = _row_to_task(row)
                await _record_transition(
                    conn, task.id, Status.RUNNING, Status.READY, None, task.attempts
                )
                return task

    async def mark_failed(self, task_id: uuid.UUID, error: TaskError) -> Task | None:
        """Conditional: only succeeds from 'running'. See mark_ready for the
        lost-race and lease-clearing rationale."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    UPDATE tasks
                       SET status = 'failed',
                           error = $2,
                           worker_id = NULL,
                           lease_expires_at = NULL,
                           updated_at = now()
                     WHERE id = $1 AND status = 'running'
                    RETURNING *
                    """,
                    task_id,
                    {"reason": error.reason, "retryable": error.retryable},
                )
                if row is None:
                    return None
                task = _row_to_task(row)
                await _record_transition(
                    conn, task.id, Status.RUNNING, Status.FAILED, error.reason, task.attempts
                )
                return task

    async def requeue_for_retry(
        self, task_id: uuid.UUID, not_before: datetime, reason: str
    ) -> Task | None:
        """running -> queued, sets not_before to the backoff deadline the
        caller already computed. Does NOT touch attempts — claim_next
        already incremented it; incrementing again here would double-count
        a single attempt."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    UPDATE tasks
                       SET status = 'queued',
                           not_before = $2,
                           worker_id = NULL,
                           lease_expires_at = NULL,
                           updated_at = now()
                     WHERE id = $1 AND status = 'running'
                    RETURNING *
                    """,
                    task_id,
                    not_before,
                )
                if row is None:
                    return None
                task = _row_to_task(row)
                await _record_transition(
                    conn, task.id, Status.RUNNING, Status.QUEUED, reason, task.attempts
                )
                return task

    async def cancel_task(self, user_id: uuid.UUID, handle: str) -> Task | None:
        """Allowed from 'queued' OR 'running' only. For a running task this
        only marks the row — actually stopping the asyncio task is
        runner.py's job.

        Unlike mark_ready/mark_failed/requeue_for_retry, the prior status
        isn't a fixed constant here, so it's captured via SELECT ... FOR
        UPDATE before the UPDATE, with the row lock held across both
        statements to keep the read-then-write atomic.

        Unambiguous by construction, unlike get_task: `status IN
        ('queued', 'running')` is a strict subset of uniq_active_handle's
        predicate, and that index guarantees at most one row per
        (user_id, lane, number) can ever satisfy it. A handle recycled
        many times over can accumulate any number of terminal rows, but
        this WHERE clause can only ever match the current active one (or
        nothing) — never a historical row, and never more than one match.
        No ORDER BY/LIMIT needed to make this safe; the index already did.
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                current = await conn.fetchrow(
                    """
                    SELECT id, status FROM tasks
                     WHERE user_id = $1 AND handle = $2
                       AND status IN ('queued', 'running')
                     FOR UPDATE
                    """,
                    user_id,
                    handle,
                )
                if current is None:
                    return None
                prior_status = Status(current["status"])
                row = await conn.fetchrow(
                    """
                    UPDATE tasks
                       SET status = 'cancelled',
                           worker_id = NULL,
                           lease_expires_at = NULL,
                           updated_at = now()
                     WHERE id = $1
                    RETURNING *
                    """,
                    current["id"],
                )
                task = _row_to_task(row)
                await _record_transition(
                    conn, task.id, prior_status, Status.CANCELLED, None, task.attempts
                )
                return task

    async def collect_result(self, user_id: uuid.UUID, handle: str) -> Task | None:
        """Sets collected = true — this is what RELEASES the handle for
        reuse, for failures as well as successes: uniq_active_handle in
        sql/schema.sql excludes (ready|failed) rows once collected is true,
        freeing (lane, number) for lowest_free_number to hand out again.

        No transition recorded: collected is a flag, not a status (per
        CLAUDE.md), so there's nothing for task_events to capture here.

        Unambiguous by construction: `status IN ('ready', 'failed') AND
        collected = false` is exactly uniq_active_handle's second branch,
        so it can match at most one row — the active one — no matter how
        many already-collected historical rows share this handle from
        past cycles. No ORDER BY/LIMIT needed.
        """
        row = await self._pool.fetchrow(
            """
            UPDATE tasks
               SET collected = true,
                   updated_at = now()
             WHERE user_id = $1 AND handle = $2
               AND status IN ('ready', 'failed') AND collected = false
            RETURNING *
            """,
            user_id,
            handle,
        )
        return _row_to_task(row) if row is not None else None

    async def reset_for_retry(self, user_id: uuid.UUID, handle: str) -> Task | None:
        """Operator-triggered retry of a failed job: failed -> queued,
        clears error, resets attempts to 0, clears not_before. Distinct from
        requeue_for_retry (the automatic engine-driven retry): this is a
        fresh attempt budget authorized by a human, not another try against
        the same one.

        WHERE clause bug fixed here: `status = 'failed'` alone is NOT a
        subset of uniq_active_handle's predicate — it also matches
        already-collected historical rows, which the index does not keep
        unique (collected = true falls outside it entirely). A handle
        retried after being collected and reused could therefore have
        matched an old, dead 'failed' row instead of (or ambiguously
        alongside) the live one. Adding `AND collected = false` makes this
        exactly the index's second branch, restoring the same
        at-most-one-row guarantee collect_result already relies on.
        Retrying a collected historical row would be nonsense — collected
        is supposed to be a one-way flag, not something a retry silently
        reopens.
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    UPDATE tasks
                       SET status = 'queued',
                           error = NULL,
                           attempts = 0,
                           not_before = NULL,
                           updated_at = now()
                     WHERE user_id = $1 AND handle = $2
                       AND status = 'failed' AND collected = false
                    RETURNING *
                    """,
                    user_id,
                    handle,
                )
                if row is None:
                    return None
                task = _row_to_task(row)
                await _record_transition(
                    conn,
                    task.id,
                    Status.FAILED,
                    Status.QUEUED,
                    "operator-triggered retry",
                    task.attempts,
                )
                return task
