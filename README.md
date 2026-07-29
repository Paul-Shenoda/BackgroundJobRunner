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

### Known issues

**Docker Desktop + WSL2: pulls fail with truncated layers.** Docker Desktop's containerd image store mishandles range-requested downloads over WSL2, causing `httpReadSeeker: failed open ... EOF` mid-pull. Affects both Windows 10 and 11. Fix: in Docker Desktop, **Settings → General**, disable **"Use containerd for pulling and storing images"**, then Apply & Restart.

## API Reference

## Testing

## Deployment

## Decisions & Tradeoffs

See `DECISIONS.md` for the full log of forks resolved during the build, with options considered and why.

## Demo

## What I'd Do Next
