# Background Job Runner

A background job runner: submit a job, get a handle back immediately, work runs in a concurrency-limited pool, state survives restarts, lifecycle events stream live to clients.

## Overview

Submit a job over a REST API and get a short handle back before it runs; work executes in a Postgres-backed pool under a concurrency limit, survives process restarts via lease-based claiming and a reaper, and streams lifecycle events to clients over SSE. Two demo lanes (`scrape`, `report`) run against a mock worker, with a React dashboard for filtering, sorting, and operating on tasks live.

**Live:** https://job-runner-app.mangobush-0fc2a290.eastus.azurecontainerapps.io
**Demo keys:** `X-API-Key: alice-demo-key` / `X-API-Key: bob-demo-key`
**Want your own key instead of sharing the demo ones?** `python -m scripts.provision_user "Your Name"` — see [Provisioning a real user's API key](#provisioning-a-real-users-api-key).

**Contents:** [Codespaces](#quickstart--github-codespaces) · [Local setup](#quickstart--local) · [API Reference](#api-reference) · [Tests](#running-the-tests) · [Seeding](#seeding) · [Frontend](#frontend) · [Architecture](#architecture) · [Deployment](#deployment) · [Decisions & Tradeoffs](#decisions--tradeoffs) · [What I'd Do Next](#what-id-do-next)

## Quickstart — GitHub Codespaces

The shortest path to a running app — **no manual setup required** beyond starting the two dev servers; everything else is automatic.

1. **Open in a Codespace** (Code → Codespaces → Create codespace on main). `.devcontainer/` brings up two containers — `app` (Python 3.12 + a Node 20 feature) and `postgres` — on the same compose network.
2. **Wait for `postCreateCommand`.** It runs automatically, no input needed: installs `requirements.txt`, writes `.env` with `DATABASE_URL` pointed at the `postgres` service (not `localhost` — there's no host loopback to share between containers), applies `sql/schema.sql`, seeds Alice/Bob via `scripts/seed.py`, and runs `npm install` in `frontend/`.
3. **Start the backend**, in one terminal:
   ```
   uvicorn api.main:app --reload --host 0.0.0.0
   ```
4. **Start the frontend**, in another terminal:
   ```
   cd frontend && npm run dev
   ```
5. **Open the forwarded ports.** VS Code's Ports tab prompts to open them in a browser once both processes are up — `8000` (API) and `5173` (frontend) are both declared in `devcontainer.json`'s `forwardPorts`. A forwarded port is private by default (requires your GitHub login through the proxy); switch to Public from the Ports tab only if you need to hand the URL to someone without repo access.
6. **Sign in** with `alice-demo-key` or `bob-demo-key` (shown as hints on the login card too).

`--host 0.0.0.0` matters here specifically, unlike plain local dev: Codespaces forwards ports out of the container by listening address.

**Idempotent on rebuild.** If a Codespace rebuild reuses the existing `pgdata` volume, `post-create.sh` detects it (`to_regclass('public.tasks')`) and skips the schema-apply-and-seed step entirely, rather than erroring on "already exists" or quietly doubling the seeded rows — `scripts/seed.py`'s bare run isn't itself idempotent, so this script must not call it twice against the same data.

Two setup-time failures were found and fixed while hardening this path (a compose `extends` quirk that silently dropped the whole devcontainer, and a `load_dotenv()` crash specific to stdin-piped Python). Both root-cause write-ups live in `DECISIONS.md` ("Codespaces landed on a bare recovery container" and "post-create.sh died at step 1").

## Quickstart — Local

1. **Start Postgres:**
   ```
   docker compose up -d
   ```
2. **Configure environment.** Copy `.env.example` to `.env` — its defaults already match the `docker-compose.yml` Postgres above. All variables are read via `python-dotenv`:

   | Variable            | Default | Purpose                                                        |
   |---------------------|---------|-----------------------------------------------------------------|
   | `DATABASE_URL`      | —       | Postgres DSN, `postgresql://user:pass@host:port/db`             |
   | `CONCURRENCY_LIMIT`  | `2`     | Max tasks running at once, this process                        |
   | `LEASE_SECONDS`      | `30`    | How long a claimed task's lease lives before the reaper reclaims it |
   | `HEARTBEAT_INTERVAL` | `10`    | How often a running task's lease is renewed                    |
   | `MAX_ATTEMPTS`       | `3`     | Attempts (including the first) before a retryable failure gives up |
   | `LOG_LEVEL`          | `INFO`  | Root logger level — `DEBUG` for verbose per-poll dispatch noise, `INFO` to see claims, retries, cancellations, and reaper reclaims without the noise |
3. **Install Python dependencies** (inside a virtualenv of your choice):
   ```
   pip install -r requirements.txt
   ```
4. **Apply the schema.** `sql/schema.sql` is the single source of truth for the database, applied by hand — there's no migration tooling and no `docker-entrypoint-initdb.d` init script wired into `docker-compose.yml` (see DECISIONS.md, "Raw SQL via asyncpg rather than an ORM": migrations were a deliberate non-goal for this proof of concept). Apply it once:
   ```
   docker compose exec postgres psql -U jobrunner -d jobrunner -f - < sql/schema.sql
   ```

   **If you already have a running database from before `tasks.seq` was added** (the monotonic ordering column — see DECISIONS.md, "Handle resolution ordering uses a monotonic sequence, not a timestamp"), a fresh `schema.sql` apply won't retroactively add it to an existing table. Run the one statement by hand instead:
   ```
   docker compose exec postgres psql -U jobrunner -d jobrunner -c "ALTER TABLE tasks ADD COLUMN seq BIGSERIAL;"
   ```
   Same end state either way — a fresh clone gets `seq` from `schema.sql` directly; an existing database gets there via this one `ALTER TABLE`. Worth keeping in mind if `schema.sql` picks up further changes later: without migration tooling, every existing database needs its own hand-applied catch-up statement, not just new ones.
5. **Seed demo data:**
   ```
   python -m scripts.seed
   ```
   Creates Alice and Bob with a realistic spread of tasks across every status. See [Seeding](#seeding) below for exactly what this creates and the available flags.
6. **Start the backend:**
   ```
   uvicorn api.main:app --reload
   ```
   **If you're about to test restart survival (criterion 9) by killing this process by hand, read this first:** `--reload` spawns a supervisor process plus a separate worker child (via Python's `multiprocessing`) that share the listening socket — the worker's own command line has nothing resembling "uvicorn" in it. `pkill -f "uvicorn api.main:app"`, or Ctrl+C in some terminals, kills only the supervisor; the worker survives as an orphan, keeps the port, and keeps serving requests exactly as before. It will look like the durability feature is broken (or like a restart never happened) when in fact nothing died at all. Kill by port instead, which gets both: `fuser -k 8000/tcp` (or drop `--reload` for this test — a plain `uvicorn api.main:app` has no supervisor/worker split, so any kill by name works correctly). See DECISIONS.md, "`--reload`'s orphaned worker survives a name-based kill."
7. **Start the frontend:**
   ```
   cd frontend
   npm install
   npm run dev
   ```
   Opens on `http://localhost:5173`. The Vite dev server proxies `/tasks`, `/lanes`, `/events`, and `/health` to the FastAPI backend on `http://localhost:8000` (see `frontend/vite.config.js`) — the backend from step 6 needs to already be running.

   **Also relevant to testing criterion 9 through this dev setup:** Vite's proxy doesn't propagate a mid-stream backend death to the browser — kill the backend (correctly, per the note above) while watching the dashboard through `localhost:5173`, and the connection indicator can sit on "Live" indefinitely even though the backend is genuinely gone, because the proxied `fetch()` to `/events` never sees the underlying connection close. This is a dev-proxy-only artifact: the same test against `localhost:8000` directly — either the built production frontend (`npm run build`, then reload `localhost:8000`; see [Building for production](#building-for-production)) or curl/`GET /tasks` polling — detects the disconnect in under a second and self-heals within a couple more, exactly as the deployed instance does (no proxy in production — see [Deployment](#deployment)). If you want to *see* the reconnect behavior work in the browser rather than just prove state survived via the API, use the production build for that specific check.

   On first load you'll land on a card asking for an API key — use the seeded demo keys (`alice-demo-key` / `bob-demo-key`, shown as hints on the card itself).

### Known issues

**Docker Desktop + WSL2: pulls fail with truncated layers.** Docker Desktop's containerd image store mishandles range-requested downloads over WSL2, causing `httpReadSeeker: failed open ... EOF` mid-pull. Affects both Windows 10 and 11. Fix: in Docker Desktop, **Settings → General**, disable **"Use containerd for pulling and storing images"**, then Apply & Restart.

## API Reference

Every endpoint requires `X-API-Key: <key>` and is scoped to that key's own user — a user never sees another user's tasks. Missing and invalid keys get the identical `401` (distinguishing them would let a caller learn which keys exist by probing).

### Endpoints

| Method | Path | Notes |
|---|---|---|
| `POST` | `/tasks` | Enqueue. Body: `{"lane": str, "params": {}}`. Returns the task object immediately (`202`), well before the job runs. |
| `GET` | `/tasks` | List. Query params: `status` (repeatable, e.g. `?status=queued&status=running`), `lane`, `created_after`/`created_before` (ISO 8601), `sort_by` (`created_at` \| `updated_at`), `sort_order` (`asc` \| `desc`), `limit`, `offset`. |
| `GET` | `/tasks/{handle}` | Fetch one. Includes the detail-only extras below. `404` if unknown. |
| `GET` | `/tasks/{handle}/result` | Fetch the result; sets `collected = true`. Works for both `ready` and `failed` — collection is the ACK that releases the handle either way. `409` if still `queued`/`running` or already collected. |
| `POST` | `/tasks/{handle}/cancel` | Cancel a `queued` or `running` task. `409` if already terminal. |
| `POST` | `/tasks/{handle}/retry` | Retry a `failed` task (not in the brief's minimum list — added so the dashboard can offer it as an operator action). `409` if not `failed` or already collected. |
| `GET` | `/lanes` | The registered lane names (`["scrape", "report"]`), so the frontend's lane dropdown isn't hardcoded. |
| `GET` | `/events` | SSE stream of this user's lifecycle events (see below). |
| `GET` | `/health` | Liveness: engine/database status, current concurrency, in-flight count, configured heartbeat interval. Not user-scoped. |

Every `/tasks/{handle}...` endpoint raises `404` via `TaskNotFoundError` if the handle doesn't exist for the calling user.

### Mock worker parameters

Every acceptance scenario is reproducible through three `params` keys the mock worker reads (`engine/workers/mock.py`). The engine itself never inspects `params` — they pass through opaquely, and there is deliberately no per-lane validation (see DECISIONS.md):

| Param | Type | Effect |
|---|---|---|
| `duration_ms` | int | How long the worker runs (sleeps) before finishing. Default: random 3000–15000. |
| `fail` | bool | Retryable failure *after* the sleep — watch it requeue with 1s/2s/4s backoff, then exhaust to `failed` with `retryable: false` after `MAX_ATTEMPTS` (default 3). |
| `fail_permanent` | bool | Non-retryable failure — lands in `failed` immediately, no retry. Takes precedence over `fail` if both are set. |

### Calling it with curl

```bash
BASE=https://job-runner-app.mangobush-0fc2a290.eastus.azurecontainerapps.io   # or http://localhost:8000
KEY="X-API-Key: alice-demo-key"

# Submit a 10s scrape job -- the handle comes back immediately, long before the work finishes
curl -s -X POST "$BASE/tasks" -H "$KEY" -H "Content-Type: application/json" \
  -d '{"lane": "scrape", "params": {"duration_ms": 10000}}'

# Poll it (substitute the handle the submit returned)
curl -s "$BASE/tasks/scrape-4" -H "$KEY"

# Collect the result -- flips `collected`, which is what releases the handle for reuse
curl -s "$BASE/tasks/scrape-4/result" -H "$KEY"

# Cancel a queued or running task
curl -s -X POST "$BASE/tasks/scrape-4/cancel" -H "$KEY"

# A retryable failure -- fails after 2s, requeues with backoff, exhausts to `failed`
curl -s -X POST "$BASE/tasks" -H "$KEY" -H "Content-Type: application/json" \
  -d '{"lane": "report", "params": {"duration_ms": 2000, "fail": true}}'

# Watch the live event stream (leave it running in a second terminal)
curl -N "$BASE/events" -H "$KEY"
```

Why `scrape-4` in the examples and not `scrape-1`: with fresh seed data, Alice's `scrape-1`/`scrape-2` are seeded *finished-but-uncollected* rows and `scrape-3` is the inert queued row — all three hold their numbers, so the first real submission gets the lowest free number, `scrape-4`. That's the no-collision rule doing its job, not a numbering bug; collect the seeded rows and the low numbers free up.

### Task object

The nine fields below are fixed by the contract — exact names, exact shapes, everywhere they appear:

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

- `status` ∈ `queued` | `running` | `ready` | `failed` | `cancelled` — exactly these five.
- `result` is populated only when `status == "ready"`.
- `error` is populated only when `status == "failed"`, shaped `{ "reason": str, "retryable": bool }`.
- `collected` flips to `true` once the result has been retrieved via `GET /tasks/{handle}/result`. It's a flag, not a status — a collected task keeps its terminal `ready`/`failed` status.
- Timestamps are ISO 8601 UTC with a trailing `Z`.

`GET /tasks` and `GET /tasks/{handle}` both add extra fields on top of the nine — additive only, never a rename or a reshape: `attempts`, `max_attempts`, `seeded`. `GET /tasks/{handle}` alone also adds `state_history`, the full list of state transitions (`from_status`, `to_status`, `reason`, `attempt`, `created_at`) backing the dashboard's detail view.

### Events (SSE)

`GET /events` streams one JSON object per line, prefixed `data: `, plus `: ping\n\n` comment lines every 15 seconds to hold the connection open through proxies and load balancers with an idle-request timeout (this is what keeps the stream alive on Azure Container Apps — see [Deployment](#deployment)). No history replay on reconnect: the stream is a liveness hint, not a data channel — a client reconnecting refetches `GET /tasks` for ground truth.

```json
{ "type": "accepted",  "handle": "scrape-1", "lane": "scrape", "summary": "..." }
{ "type": "ready",     "handle": "scrape-1", "lane": "scrape", "summary": "..." }
{ "type": "failed",    "handle": "scrape-1", "lane": "scrape", "reason": "...", "retryable": true }
{ "type": "cancelled", "handle": "scrape-1", "lane": "scrape" }
```

Collecting a result and retrying a failed task deliberately emit no event — `collected` is a flag, not a lifecycle transition, and a retry has no honest event type in this fixed set.

## Running the Tests

```
pytest
```

**Stop any locally running API first** (the `uvicorn` from the quickstart). The tests and a dev server share the same `DATABASE_URL`, and the engine's claim queue is deliberately not scoped per process — so a second running engine is a second worker pool on the same queue, silently doubling effective concurrency and stealing the suite's own jobs. The visible symptom is flaky failures in exactly the two most scrutinised tests (`test_concurrency_limit_respected`, `test_state_survives_restart`) while everything else passes.

Async, written against the live HTTP API (not engine internals), one test named per acceptance criterion — see `tests/`. `pytest.ini` pins a session-scoped event loop, since the tests share one session-scoped app/engine/db-pool (`tests/conftest.py`). `tests/test_restart.py` genuinely kills and respawns the API process to prove durability (criterion 9) — it's marked `slow` (informational only, not deselected by default): `pytest -m "not slow"` skips it for a quick loop, but a plain `pytest` run always includes it, since criterion 9 is the one most scrutinized and an easy-to-skip marker that silently excluded it would prove nothing.

## Seeding

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
  (`X-API-Key: bob-demo-key`). A bare run never touches any other user —
  it only ever creates/updates these two and inserts their tasks.
- **~28 tasks for Alice, ~7 for Bob** — `ready`, `failed`, and `cancelled`
  well represented, plus exactly one `queued` row (see below on why not
  more, and why never `running`), both lanes (`scrape`, `report`), a mix
  of collected/uncollected terminal tasks, and `created_at` spread across
  the last two weeks so date-range filtering has something to filter.
  The uncollected ones hold their handle numbers (that's what they're
  there to demonstrate), which is why a fresh scrape submission starts
  at `scrape-4`, not `scrape-1` — see the note under
  [Calling it with curl](#calling-it-with-curl).
  Every failed row has a distinct, honest reason (some exhausted-retry
  wording, some permanent), and every task has a plausible `task_events`
  history backing its detail view.
- Every seeded row has `seeded: true` (returned by both `GET /tasks` and
  `GET /tasks/{handle}` — additive extras on top of the 9 fixed contract
  fields, see DECISIONS.md) so it's never mistaken for real activity.

Rows are inserted directly via SQL, not through `engine.submit()`. Two
reasons, not one: first, a seeded `queued`/`running` row would otherwise get
claimed and actually run the moment the app starts, so nothing seeded
would still be in that state a minute later — the whole point of seed
data is to have something to look at, and a live worker pool would erase
it almost immediately (this is also why the filters need dedicated,
artificially-held rows at all: real submissions would all reach `ready`
within about a minute at this project's `duration_ms` range, leaving
`status=queued`/`status=running` nothing to filter on). Second, direct
inserts are the only way to backdate `created_at`/`updated_at` and write a
`task_events` history that looks like it happened over the last two weeks
rather than in the last second.

**Exactly one seeded row is ever non-terminal, and it's `queued`, never
`running`.** It carries a `not_before` decades in the future, so the
dispatch loop's `claim_next` will never select it — it sits in `queued`
forever, not just until the next poll. One row is enough to demonstrate
`status=queued` filtering; more would just be more rows to explain away
as inert for no added coverage. There's deliberately no seeded `running`
row at all: a permanently-running job reads as a hung worker, which is
more alarming than a permanently-queued one, and `running` is trivially
demonstrated by submitting a real job — seeding one bought nothing and
cost credibility (see DECISIONS.md, "seeded non-terminal rows read as
'the system is stuck'").

An inert seeded row still reads as *stuck*, not *demo data*, if all you
see is a `SEEDED` badge next to `queued` — the badge says where the row
came from, not that it will never move. The dashboard marks it more
plainly: the list badge reads **SEEDED · INERT** (with a tooltip) and the
detail panel states outright, "Seeded demo row: held in queued by a
future `not_before` so the worker pool never claims it. Real submissions
run normally." Terminal seeded rows (`ready`/`failed`/`cancelled`) get
neither — they look finished because they are, nothing to explain.

Flags:

```
python -m scripts.seed --reset               # delete existing seeded rows, then reseed fresh ones
python -m scripts.seed --clear               # delete existing seeded rows and STOP -- no reseed
python -m scripts.seed --wipe                # delete ALL rows (tasks + users), asks for confirmation, then reseeds
python -m scripts.seed --purge-debug-users   # delete non-demo, non-test-fixture users; previews and asks first
```

`--reset` and `--clear` both delete every `seeded = true` row — the
difference is only what happens next. `--reset` reseeds immediately
afterward, so you end up back where you started with a fresh batch (new
timestamps, same shape). `--clear` stops right there, so you end up with
*no* seed data at all — useful for a demo/recording where you want to
show the dashboard against a clean slate, or against only your own real
traffic, before seeding it back in later with a bare `python -m
scripts.seed`. `--reset`, `--clear`, and `--wipe` are mutually exclusive
(the script rejects combining them, rather than one silently winning).

`--purge-debug-users` exists for cleaning up ad-hoc users left behind by
manual testing (e.g. a one-off key created by hand while poking at the API,
or via `scripts/provision_user.py`). It is opt-in only — a bare run, and
even `--reset`/`--clear`, never delete another user — and it always prints
exactly who it found and how many tasks they have before asking for
confirmation, since deleting a user cascades to their real (non-seed) task
data too. Used alone (or with `--clear`), it's cleanup-only and stops
there — it does **not** fall through to a reseed, since `insert_tasks`
isn't idempotent (see above) and would collide with Alice/Bob's own
already-active rows on anything but an empty database. Combined with
`--reset` or `--wipe`, both still run in the same order — the purge, then
the reseed — since those two already clear (or wipe) the slate first.

### Provisioning a real user's API key

There is deliberately no signup flow — no registration form, no email
verification, no key rotation (see DECISIONS.md, "Auth beyond API
keys... no signup, login, password reset, or key rotation"). The brief
asks for "a per-user API key scoped to that user's data," not a user
management system, so keys are provisioned by an operator, out of band.

Every current caller that needs a user — `scripts/seed.py`'s demo users,
the API's own seeded users at startup, the pytest fixtures — goes through
the same idempotent function: `scripts.bootstrap.ensure_user(dsn, name,
api_key)`. It's `INSERT ... ON CONFLICT (api_key) DO UPDATE ... RETURNING
id` — safe to run again with the same key, and the only place a user row
gets created. `scripts/provision_user.py` is the CLI wrapper around it:

```
python -m scripts.provision_user "Carol"
```

```
user_id=c62bd5f5-fe0e-4f30-99fb-96adaaca78f3
name=Carol
api_key=carol-a099d7c3

Hand this key to Carol; send it as the X-API-Key header on every request.
The API's api_key -> user_id cache loads once at process startup (api/auth.py) --
restart the API (or, in production, the next natural redeploy) before this key is recognized.
```

With no `--key`, a fresh key is generated from the name plus a short
random suffix — the point is a working key handed back immediately, not
having to invent a string of your own. Pass one explicitly
(`--key carol-prod-key`) if you need a specific value; re-running with
the same `--key` is safe and idempotent (confirms the same user rather
than minting a new one). Omitting `--key` is deliberately **not**
idempotent — each run mints a genuinely new identity, since "give me a
new key" is the whole point of running it without one.

Nothing else to configure once you have a key — the in-memory
`api_key -> user_id` cache (`api/auth.py`) is loaded once at process
startup, so a newly provisioned key needs the API process restarted (or,
in production, the next natural redeploy) before it's recognized.

## Frontend

`frontend/` is a Vite + React app -- a pure HTTP consumer of the API above.
It imports nothing from `engine/`, never touches Postgres, and the system
works fully with `frontend/` deleted (CLAUDE.md's frontend boundary rule).
See [Quickstart — Local](#quickstart--local) and
[Quickstart — GitHub Codespaces](#quickstart--github-codespaces) above for
running it in dev.

### Theme

Dark is the default (an ops console reads as dark by default), but light
mode is fully supported: it follows the OS/browser's `prefers-color-scheme`
automatically, and Settings has an explicit System/Light/Dark toggle. The
toggle is session-only React state, not persisted -- the brief reserves
`localStorage` for the API key alone, so a forced theme resets to
"System" on next load rather than being written anywhere.

### Building for production

```
cd frontend
npm run build
```

Emits `frontend/dist`. **How it's served:** `api/main.py` mounts
`frontend/dist/assets` as static files and serves `frontend/dist/index.html`
for every other path that isn't a recognised API route (registered last,
after every real endpoint, so `/tasks`, `/events`, etc. always take
precedence over the SPA fallback). One container, one process, no CORS
needed in production since the frontend and API share an origin. In dev,
`frontend/dist` doesn't exist yet, so FastAPI skips that block entirely and
the Vite dev server serves the frontend instead.

The existing CORS config (`http://localhost:5173`, plus `FRONTEND_ORIGIN`
if set) stays in place for local dev, where the frontend and API run as
two separate processes on two different ports.

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

## Deployment

**Live URL:** https://job-runner-app.mangobush-0fc2a290.eastus.azurecontainerapps.io

Demo keys (same as local dev): `X-API-Key: alice-demo-key` / `X-API-Key: bob-demo-key`.

### Architecture

One container, one process: `uvicorn` serves both the API and the built
frontend (`api/main.py`'s `StaticFiles` mount + SPA fallback — see
"Building for production" above). Single Azure Container Apps replica
(`min-replicas 1, max-replicas 1` — see CLAUDE.md and DECISIONS.md,
"single-replica by design": two replicas would mean two worker pools
sharing one queue, silently doubling the effective concurrency limit and
breaking the concurrency-limit acceptance criterion; scale-to-zero would
stop the pool entirely between requests). Backed by a single Azure
Database for PostgreSQL flexible server (Burstable B1ms).

### Image

Built by the root `Dockerfile`, a two-stage build:

1. `node:20-alpine` — `npm ci` + `npm run build` against `frontend/`, producing `frontend/dist`.
2. `python:3.12-slim` — installs `requirements.txt`, copies `engine/`, `api/`, `scripts/`, `sql/`, and the built `frontend/dist` from stage 1.

The frontend is built **inside** the image on every build, never pushed
from a local `npm run build` — otherwise the served bundle could silently
go stale relative to the source it was supposedly built from. `CMD` runs
`uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000}`; Azure
Container Apps' ingress targets port 8000.

### Azure resources

| Resource | Name | Notes |
|---|---|---|
| Resource group | `rg-job-runner` | `eastus` |
| Postgres flexible server | `jobrunner-pg-c1ae5c` | Burstable B1ms, Postgres 16, `canadacentral` (the subscription's `eastus` quota rejected this resource type — see below) |
| Database | `jobrunner` | schema applied by hand from `sql/schema.sql`, then seeded via `scripts/seed.py` |
| Container registry | `tradingbotregistry9212.azurecr.io` | pre-existing registry, reused rather than creating a second one |
| Container Apps environment | `job-runner-env` | `eastus` |
| Container App | `job-runner-app` | `min-replicas 1, max-replicas 1`, external ingress, target port 8000 |

**Env vars** (`DATABASE_URL` is a Container Apps *secret*, referenced via
`secretRef`, never a plain env var — matches CLAUDE.md's requirement):

| Variable | Value |
|---|---|
| `DATABASE_URL` | secret `database-url`, the Postgres flexible server's connection string (`sslmode=require`) |
| `CONCURRENCY_LIMIT` | `2` |
| `LEASE_SECONDS` | `50` |
| `HEARTBEAT_INTERVAL` | `10` |
| `MAX_ATTEMPTS` | `3` |
| `LOG_LEVEL` | `INFO` |

### Deploying (reproducing this from scratch)

```
# 1. build & push
docker build -t tradingbotregistry9212.azurecr.io/job-runner:<tag> .
az acr login -n tradingbotregistry9212
docker push tradingbotregistry9212.azurecr.io/job-runner:<tag>

# 2. Postgres (one-time)
az group create -n rg-job-runner -l eastus
az postgres flexible-server create -g rg-job-runner -n <server-name> \
  --location canadacentral --admin-user <user> --admin-password <pass> \
  --sku-name Standard_B1ms --tier Burstable --storage-size 32 --version 16 \
  --public-access 0.0.0.0
# schema.sql uses pgcrypto (gen_random_uuid()) -- Azure Postgres requires
# extensions to be allow-listed before CREATE EXTENSION will succeed:
az postgres flexible-server parameter set -g rg-job-runner -s <server-name> \
  --name azure.extensions --value PGCRYPTO
# temporarily open the firewall to your own IP, apply sql/schema.sql and
# run `python -m scripts.seed` against the deployed DATABASE_URL, then
# remove that firewall rule again -- the "AllowAllAzureServicesAndResourcesWithinAzureIps"
# rule from --public-access 0.0.0.0 is what the running Container App
# actually connects through.

# 3. Container Apps
az containerapp env create -g rg-job-runner -n job-runner-env --location eastus
az containerapp create -g rg-job-runner -n job-runner-app \
  --environment job-runner-env \
  --image tradingbotregistry9212.azurecr.io/job-runner:<tag> \
  --target-port 8000 --ingress external \
  --registry-server tradingbotregistry9212.azurecr.io \
  --registry-username <acr-user> --registry-password <acr-pass> \
  --min-replicas 1 --max-replicas 1 \
  --secrets database-url=<dsn> \
  --env-vars DATABASE_URL=secretref:database-url CONCURRENCY_LIMIT=2 \
    LEASE_SECONDS=50 HEARTBEAT_INTERVAL=10 MAX_ATTEMPTS=3 LOG_LEVEL=INFO
```

### Verified against the deployed URL

- Frontend loads at `/` (not a 404); direct navigation to a client-side
  route (e.g. `/some/client/route`) also serves `index.html` via the SPA
  fallback, not a 404.
- `alice-demo-key` / `bob-demo-key` authenticate and return each user's
  own seeded tasks only.
- `POST /tasks` returns `202` with a `queued` task well before the job
  finishes; polling the handle shows it progress to `running` then
  `ready` with a populated `result` and full `state_history`.
- `POST /tasks/{handle}/cancel` on a `running` task returns `cancelled`
  immediately, no later `ready` event follows.
- `GET /events` streams `: ping` comment lines every 15s and was held
  open past ACA's idle-connection window (multi-minute idle check) with
  no drop — this is the one most likely to fail *only* in production
  (proxies/load balancers between the client and the app, not present in
  local dev), so it was checked directly against the deployed URL rather
  than assumed from the local pass.

## Decisions & Tradeoffs

See `DECISIONS.md` for the full log of forks resolved during the build, with options considered and why.

## What I'd Do Next

In rough priority order (each expanded in `DECISIONS.md`, "What I'd do next"):

- **Multi-replica workers** with a cluster-wide concurrency budget rather than a per-process one. The lease design already supports multiple claimers safely (`FOR UPDATE SKIP LOCKED`); what's missing is coordinating the *limit* across replicas.
- **Idempotency keys on the worker contract**, so at-least-once execution becomes safe for workers with real side effects.
- **Atomic handle allocation** — collapse compute-lowest-free-number + INSERT into one statement, replacing the retry-with-jittered-backoff loop entirely rather than bounding it (see DECISIONS.md, "Handle-allocation retry budget 5 → 25").
- **A delta endpoint** (`GET /tasks?updated_since=`) so reconnects don't need a full list refetch as the task table grows.
- **Scheduled and recurring jobs** — the `not_before` column used for retry backoff is already most of the mechanism needed for "run this at 3am."
- **Per-lane parameter schemas** with server-side validation, worth building the moment there's a real worker whose params actually matter. Until then params are opaque by design — which also means `duration_ms: 10**18` is accepted and will occupy a concurrency slot until cancelled; a documented limitation, not an oversight.
- **Structured metrics** — queue depth, time-to-claim, worker utilisation, retry rates — the brief's emphasis on observability naturally extends past the dashboard into instrumentation.
