# Background Job Runner

## Overview

## Architecture

### 1. Job lifecycle

The five statuses are fixed by the API contract. Every transition below is one the engine actually performs.

```mermaid
stateDiagram-v2
    [*] --> queued: POST /tasks

    queued --> running: claimed by worker<br/>(lease stamped, attempts++)
    queued --> cancelled: cancelled while waiting

    running --> ready: worker returned ready
    running --> failed: non-retryable error<br/>or attempts exhausted
    running --> queued: retryable error<br/>(not_before = now + backoff)
    running --> queued: lease expired<br/>(crash recovery, attempts++)
    running --> cancelled: cancelled mid-run<br/>(CancelledError raised in worker)

    failed --> queued: operator-triggered retry

    ready --> [*]: collected (handle released)
    failed --> [*]: collected (handle released)
    cancelled --> [*]: handle released immediately

    note right of running
        Two distinct paths back to queued:
        - the worker spoke (retryable failure)
        - the worker vanished (lease expiry)
    end note

    note left of ready
        `collected` is a boolean flag, NOT a status.
        A collected job stays `ready` or `failed`.
        Backoff is also not a status — a job waiting
        to retry sits in `queued` with `not_before` set.
    end note
```

### 2. Module boundaries

The two rules this diagram encodes: nothing under `engine/` imports a web framework, and nothing outside `store.py` touches the database.

```mermaid
flowchart TB
    subgraph clients["Clients — pure API consumers"]
        UI["React dashboard"]
        SCRIPT["curl / external scripts"]
    end

    UI -->|"HTTP + X-API-Key"| API
    SCRIPT -->|"HTTP + X-API-Key"| API

    subgraph API["api/ — FastAPI (thin adapter)"]
        ROUTES["routes.py<br/><i>parse → delegate → serialise</i>"]
        SSEEP["/events (SSE + heartbeat)"]
        AUTH["auth.py — API key → user_id"]
    end

    ROUTES --> FACADE
    SSEEP --> FACADE
    AUTH --> ROUTES

    subgraph ENGINE["engine/ — standalone, zero HTTP imports"]
        FACADE["engine.py<br/><b>the only public surface</b><br/>submit · get · list · collect · cancel · retry · subscribe"]
        HANDLES["handles.py<br/>allocation + recycling"]
        RUNNER["runner.py<br/>worker pool · concurrency<br/>lease loop · reaper"]
        REGISTRY["registry.py<br/>lane → worker"]
        EVENTS["events.py<br/>in-process pub/sub"]
        STORE["store.py<br/><b>only module with SQL</b>"]
    end

    FACADE --> HANDLES
    FACADE --> RUNNER
    FACADE --> EVENTS
    FACADE --> STORE
    RUNNER --> REGISTRY
    RUNNER --> STORE
    RUNNER --> EVENTS
    HANDLES --> STORE

    REGISTRY --> WORKERS["workers/mock.py<br/><i>Job → WorkerResult</i>"]
    STORE --> DB[("Postgres")]

    style ENGINE fill:#f0f7ff,stroke:#4a7fb5
    style API fill:#fff8f0,stroke:#b5844a
    style clients fill:#f7f7f7,stroke:#999
```

**Verifiable in seconds:** `grep -r "fastapi" engine/` returns nothing. `grep -rl "asyncpg" engine/` returns exactly one file.

### 3. Durability — crash and recovery

This is the "sketch your approach" the brief asks for in the durability section.

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant A as API
    participant E as Engine
    participant DB as Postgres
    participant W as Worker

    C->>A: POST /tasks {lane: scrape, duration_ms: 30000}
    A->>E: engine.submit(...)
    E->>DB: allocate lowest free number + INSERT (queued)
    Note over DB: partial unique index rejects<br/>any collision with an active handle
    E-->>A: Task{handle: scrape-1, status: queued}
    A-->>C: 202 — returned in well under 1s

    E->>DB: claim: UPDATE ... FOR UPDATE SKIP LOCKED<br/>status=running, lease=now()+30s, attempts=1
    E->>W: worker(job)
    E-->>C: SSE: accepted

    loop every 10s while running
        E->>DB: heartbeat — extend lease
    end

    rect rgb(255, 235, 235)
        Note over E,W: 💥 process killed mid-run
        Note over DB: row still says `running`,<br/>lease now ticking down unrenewed
    end

    Note over E: process restarts

    E->>DB: reaper: UPDATE tasks SET status='queued'<br/>WHERE status='running' AND lease_expires_at < now()
    Note over DB: orphan reclaimed — no other process<br/>could have been holding it

    E->>DB: claim again (attempts=2)
    E->>W: worker(job) — re-runs from the start
    W-->>E: WorkerResult{status: ready}
    E->>DB: UPDATE status=ready, result=...
    E-->>C: SSE: ready

    Note over C: client also refetches GET /tasks on<br/>reconnect — stream is a liveness hint,<br/>not the only path to truth
```

The reaper runs on boot **and** on a timer — boot-only recovery would miss a worker that hung rather than died.

## Setup & Running Locally

### Database schema

`sql/schema.sql` is the single source of truth for the database, applied by
hand — there's no migration tooling and no `docker-entrypoint-initdb.d` init
script wired into `docker-compose.yml` (see DECISIONS.md, "Raw SQL via
asyncpg rather than an ORM": migrations were a deliberate non-goal for this
proof of concept). After `docker compose up -d`, apply it once:

```
docker compose exec postgres psql -U jobrunner -d jobrunner -f - < sql/schema.sql
```

**If you already have a running database from before `tasks.seq` was added**
(the monotonic ordering column — see DECISIONS.md, "Handle resolution
ordering uses a monotonic sequence, not a timestamp"), a fresh `schema.sql`
apply won't retroactively add it to an existing table. Run the one statement
by hand instead:

```
docker compose exec postgres psql -U jobrunner -d jobrunner -c "ALTER TABLE tasks ADD COLUMN seq BIGSERIAL;"
```

Same end state either way — a fresh clone gets `seq` from `schema.sql`
directly; an existing database gets there via this one `ALTER TABLE`. Worth
keeping in mind if `schema.sql` picks up further changes later: without
migration tooling, every existing database needs its own hand-applied catch-up
statement, not just new ones.

### Seed data

`scripts/seed.py` creates two named demo users and a realistic spread of
tasks across every status, so the dashboard's filtering, sorting, and detail
views have something meaningful to show without needing to manually drive
jobs through the API first. Run it once the schema is applied and the API's
dependencies are installed:

```
python -m scripts.seed
```

This creates:

- **Two users** — `Alice` (`X-API-Key: alice-demo-key`) and `Bob`
  (`X-API-Key: bob-demo-key`). Any other user already in the `users` table
  (leftover ad-hoc keys from manual testing) is removed, except the pytest
  fixture users tests/conftest.py and tests/test_restart.py provision —
  those are left alone so the test suite keeps working against the same
  database.
- **~28 tasks for Alice, ~7 for Bob** — every status represented
  (`queued`, `running`, `ready`, `failed`, `cancelled`), both lanes
  (`scrape`, `report`), a mix of collected/uncollected terminal tasks, and
  `created_at` spread across the last two weeks so date-range filtering has
  something to filter. Every failed row has a distinct, honest reason
  (some exhausted-retry wording, some permanent), and every task has a
  plausible `task_events` history backing its detail view.
- Every seeded row has `seeded: true` (visible on `GET /tasks/{handle}`,
  never on the core `GET /tasks` list shape) so it's never mistaken for
  real activity.

Rows are inserted directly via SQL, not through `engine.submit()` — a
seeded `queued` row would otherwise get claimed and actually "run" the
moment the app starts. The couple of seeded `queued` rows carry a
`not_before` decades in the future for the same reason, and the one seeded
`running` row carries a `lease_expires_at` decades in the future so the
reaper doesn't reclaim it on boot.

Flags:

```
python -m scripts.seed --reset   # delete existing seeded rows first, then reseed
python -m scripts.seed --wipe    # delete ALL rows (tasks + users), asks for confirmation
```

### Known issues

**Docker Desktop + WSL2: pulls fail with truncated layers.** Docker Desktop's containerd image store mishandles range-requested downloads over WSL2, causing `httpReadSeeker: failed open ... EOF` mid-pull. Affects both Windows 10 and 11. Fix: in Docker Desktop, **Settings → General**, disable **"Use containerd for pulling and storing images"**, then Apply & Restart.

## API Reference

## Testing

## Deployment

## Decisions & Tradeoffs

See `DECISIONS.md` for the full log of forks resolved during the build, with options considered and why.

## Demo

## What I'd Do Next
