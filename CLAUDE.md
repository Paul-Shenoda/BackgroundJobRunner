# CLAUDE.md

Constraints for this repo. Read before writing code. These are decided — implement them, don't relitigate them. The reasoning behind each lives in `DECISIONS.md`.

---

## What this is

A background job runner: submit a job, get a short handle back immediately, work runs in a pool under a concurrency limit, state survives restarts, lifecycle events stream to clients. Take-home assessment; the **orchestration engine is what's evaluated hardest**, followed by handle logic and durability.

Stack: Python 3.12 · FastAPI · Postgres (asyncpg, raw SQL) · React frontend · deployed to Azure Container Apps, single replica.

---

## Hard rules

These are architectural boundaries. Breaking one is a bug even if the code works.

1. **No web framework imports under `engine/`.** `grep -r "fastapi" engine/` must return nothing. The engine must be fully usable from a plain Python script.
2. **No database driver outside `engine/store.py`.** `grep -rl "asyncpg" engine/` must return exactly one file. All SQL lives there.
3. **API route handlers only call `engine.py`'s facade.** Three lines each: parse, delegate, serialise. No SQL, no queue manipulation, no handle logic in a route.
4. **The frontend is a pure HTTP consumer.** It never imports Python, never reads the database. The system must work fully with the frontend deleted.
5. **Handle uniqueness is enforced by the database**, via the partial unique index. Never by an application-level check-then-insert.
6. **API field names and shapes are fixed by the spec below.** Do not rename, add, or restructure fields in API responses.

---

## Layout

```
engine/
  models.py      Task, Job, WorkerResult, Status — plain dataclasses/enums
  store.py       ALL SQL. Nothing else touches the DB.
  handles.py     allocation + recycling
  registry.py    lane → worker mapping
  events.py      in-process pub/sub
  runner.py      worker pool, concurrency, lease heartbeat, reaper
  engine.py      facade: submit/get/list/collect/cancel/retry/subscribe
  workers/
    mock.py      the mock worker

api/
  main.py        app wiring, lifespan (starts/stops the engine)
  routes.py      six endpoints
  events.py      SSE endpoint
  auth.py        X-API-Key header → user_id
  schemas.py     pydantic request/response models

sql/
  schema.sql     tables + indexes
  seed.sql       demo data

tests/           pytest, async, against the API
frontend/        React, built last
```

Build order (each depends on the one before): `models` → `store` → `handles` → `registry` → `events` → `runner` → `engine` → `workers/mock` → API → tests → seed → frontend.

**Work one module at a time.** Do not generate multiple modules in a single pass. I need to read and understand each before moving on — I have to defend this design on camera.

---

## Concurrency gotchas — these are the ones that break the acceptance criteria

**Use `asyncio.sleep`, never `time.sleep`.** `time.sleep` blocks the event loop and is not cancellable. The entire cancellation requirement depends on the worker awaiting something interruptible.

**Re-raise `CancelledError` before any general exception handler.** This is the single most common way an asyncio job runner gets cancellation wrong — a broad `except Exception` swallows the cancellation and records a bogus failure, so a cancelled job lands in `failed` instead of `cancelled`.

```python
try:
    result = await worker(job)
except asyncio.CancelledError:
    raise                    # MUST come first
except Exception as e:
    ...                      # record as failed, retryable
```

**Claim jobs with `FOR UPDATE SKIP LOCKED`.** Without `SKIP LOCKED`, concurrent claimers serialise behind the same row and the pool stops being a pool.

**Heartbeat running leases.** A 30s lease on a 30s job gets reclaimed mid-run and executed twice. Extend every ~10s while running.

**The reaper runs on boot AND on a timer.** Boot-only recovery misses a worker that hung rather than died.

**Never mutate task state outside `store.py`.** All transitions go through it so `task_events` stays a complete audit trail.

---

## API contract — fixed, do not deviate

### Endpoints
```
POST   /tasks                    enqueue; returns task object immediately
GET    /tasks                    list; supports ?status= and ?lane=
GET    /tasks/{handle}           fetch one
GET    /tasks/{handle}/result    fetch result; sets collected = true
POST   /tasks/{handle}/cancel    cancel queued or running
GET    /events                   SSE stream
```

All authenticated via `X-API-Key`, scoped to that user's data. A user must never see another user's tasks.

### Task object
```json
{
  "handle": "scrape-1",
  "lane": "scrape",
  "params": {},
  "status": "queued",
  "result": null,
  "error": null,
  "created_at": "2026-05-30T18:00:00Z",
  "updated_at": "2026-05-30T18:00:00Z",
  "collected": false
}
```

- `status` ∈ `queued` | `running` | `ready` | `failed` | `cancelled` — **exactly these five, no additions**
- `result` populated only when `status == "ready"`
- `error` populated only when `status == "failed"`, shape `{ "reason": str, "retryable": bool }`
- `collected` flips true when the result is retrieved
- Timestamps are ISO 8601 UTC with `Z`

Extra fields (`attempts`, `seeded`, state history) may appear on the **detail** endpoint for the dashboard, but the six fields above keep these exact names and shapes everywhere.

### Events
```json
{ "type": "accepted",  "handle": "scrape-1", "lane": "scrape", "summary": "..." }
{ "type": "ready",     "handle": "scrape-1", "lane": "scrape", "summary": "..." }
{ "type": "failed",    "handle": "scrape-1", "lane": "scrape", "reason": "...", "retryable": true }
{ "type": "cancelled", "handle": "scrape-1", "lane": "scrape" }
```

Send `: ping` comment lines every 15s to keep the stream alive through proxies.

### Worker contract
```python
async def worker(job: Job) -> WorkerResult: ...
# Job:          handle, lane, params
# WorkerResult: status ("ready"|"failed"), result?, error? {reason, retryable}
```

Workers return `ready` or `failed` **only**. They know nothing about `queued`, `running`, or `cancelled` — the engine owns the lifecycle. No lane-specific logic may exist in the engine; the engine never inspects `params`.

---

## Behaviour rules

**Handles.** `lane-N`, numbered per lane per user. A number is held while the job is `queued`, `running`, or finished-but-uncollected. Released on collect (success *or* failure) or on cancel. Allocation: compute lowest free number → INSERT → on unique violation, recompute and retry (up to 5 times).

**Retries.** Max 3 attempts. Retryable failure with budget remaining → back to `queued` with `not_before = now + backoff` (exponential: 1s, 2s, 4s). Attempts exhausted → `failed` with `retryable: false`. Non-retryable failure → `failed` immediately, no retry. A lease-expiry requeue **does** consume an attempt (poison-job protection).

**Failure reasons must be specific.** `"upstream timed out (attempt 3 of 3, retries exhausted)"`, not `"failed"`. The brief explicitly asks for honest, distinct reasons.

**Cancellation.** Queued → straight to `cancelled`. Running → cancel the asyncio task so the worker actually stops; no later `ready` event may arrive for it.

**Backoff is not a status.** A job waiting to retry sits in `queued` with `not_before` set. `collected` is not a status either — it's a flag.

**Mock worker params.** `duration_ms` (default: random 3000–15000), `fail` → retryable error, `fail_permanent` → non-retryable error. Sleep happens *before* the failure check so retries are observable.

**Concurrency** comes from config (env var, default 2). Never more than N tasks in `running` at once.

---

## Testing

pytest + pytest-asyncio, **written against the HTTP API**, not against engine internals. One test named per acceptance criterion:

1. `test_enqueue_returns_handle_before_completion`
2. `test_lifecycle_to_ready_and_collect`
3. `test_numbering_is_per_lane`
4. `test_handle_recycles_after_collect_no_collision_while_active`
5. `test_failure_surfaces_and_is_not_auto_collected`
6. `test_cancel_running_and_queued`
7. `test_concurrency_limit_respected`
8. `test_concurrent_completions_all_land`
9. `test_state_survives_restart`

(4) and (9) are the two the reviewer scrutinises hardest — write them first and most carefully. (9) must genuinely kill and respawn the process; an in-process fake proves nothing.

---

## Do NOT build

- A real scraper or file converter. The brief says explicitly this is "not where the points are."
- Per-lane parameter schemas or validation.
- Signup, login, password reset, or key rotation. API keys are seeded at startup.
- Any sixth status, or extra fields in the core task object.
- Multi-replica coordination.
- A replay buffer for the event stream — clients refetch `GET /tasks` on reconnect.

## Ask before

- Adding any dependency not already in `requirements.txt`.
- Changing anything in `sql/schema.sql`.
- Refactoring across module boundaries.
- Anything that would alter the API contract above.

## Housekeeping

Append a dated entry to `DECISIONS.md` whenever a real fork is resolved — options, choice, why, tradeoff. Do not rewrite existing entries.
