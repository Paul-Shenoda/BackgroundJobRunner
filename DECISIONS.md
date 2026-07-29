# Decisions & Tradeoffs
> A running log of the real calls I made building this, and why. Most recent on top.
> Purpose: make my reasoning visible — the "how" matters as much as the "what."

---

## Scope — what's in, what's out (read this first)

**In scope (and why):**
- **The orchestration engine**, as a standalone Python module with no HTTP or framework dependencies — the brief weights it first and evaluates it hardest.
- **Handle assignment and recycling**, enforced as a database invariant rather than application logic. Called out twice in the brief as one of the two things verified most carefully.
- **Durability across restart** via lease-based job claiming. The other most-carefully-verified item.
- **Full REST API** to the specified contract — fixed field names, fixed event shapes, per-user API key scoping.
- **Automated tests against the API**, covering the nine success criteria. The brief states these are required, not a bonus.
- **Operations dashboard** — filters, sort, per-item detail, operator actions, live updates, mobile-friendly across every surface.
- **Two lanes** (`scrape`, `report`) backed by the mock worker.

**Deliberately OUT of scope (and why):**
- **A genuinely real worker** (actual scraping or file conversion) — the brief says explicitly this is "welcome but not required and not where the points are." Building one would consume time that belongs to engine correctness.
- **Per-lane parameter schemas** (e.g. a URL field for `scrape`, a target format for `convert`) — considered and rejected. The mock worker ignores them, so they'd be UI surface that does nothing. Adding a text box that has no effect puts a lie in the demo. Documented here rather than built.
- **Auth beyond API keys** — no signup, login, password reset, or key rotation. Keys are seeded at startup. The brief asks for "a per-user API key scoped to that user's data," which this satisfies exactly.
- **Multi-replica horizontal scaling** — deployed single-replica by design (see below). The engine is written so it *could* scale, but proving that is out of scope.
- **Exactly-once execution guarantees** — at-least-once is what this delivers, and the brief's "resumed or cleanly re-queued" permits it.

**Assumptions I made** (where the brief was open-ended):
- Assumed **thin-but-real API key auth** is the intent rather than a full user management system — asked in Slack, proceeding with this default.
- Assumed **my choice of host** unless told otherwise — asked in Slack, defaulting to Azure Container Apps.
- Assumed the mock worker's params may be **extended beyond `duration_ms` and `fail`** where needed to make a required behaviour demonstrable (see the permanent-failure decision below).

---

## Decision log

### [2026-07-28] — Event stream is a liveness hint, not a data channel
- **Context:** If the SSE connection drops and a job completes during the gap, that `ready` event is delivered to nobody. The client reconnects successfully and looks healthy while displaying a job stuck at `running` forever — silently wrong state, which the brief's "no silent failures" bar rules out.
- **Options considered:** (A) On every reconnect, refetch `GET /tasks` and replace client state wholesale. (B) Server-side replay buffer with sequence IDs and `Last-Event-ID` resumption.
- **Chose:** A.
- **Why:** B still requires A as a fallback for any disconnect longer than the buffer window, so B is strictly A plus extra machinery. A self-heals from *every* cause of staleness — dropped connection, dropped event, a bug in my emission logic, a tab restored from cache — where B only heals the specific case it was designed for. Reframing the stream as "something changed, go look" rather than "the only channel by which truth arrives" makes the whole system more forgiving.
- **Tradeoff / cost:** A brief flash of a full list refetch on reconnect, and slightly more load on `GET /tasks`. Negligible at this scale.
- **Revisit if:** The task list grows large enough that a full refetch is expensive, at which point a delta endpoint (`?updated_since=`) is the cheaper next step, not a replay buffer.

### [2026-07-28] — Retries capped at 3 attempts; `retryable` flips to `false` on exhaustion
- **Context:** The brief requires transient failures to auto-retry with backoff and permanent failures to be surfaced without auto-retry. It lists "repeated exhaustion of retries" as a *permanent* failure, so a job that has burned its attempts must not still advertise itself as retryable.
- **Options considered:** Keep `retryable: true` after exhaustion (treating the flag as a property of the error's nature, with a separate exhaustion signal) / flip to `false` (treating the flag as a statement about engine behaviour).
- **Chose:** Flip to `false`.
- **Why:** The brief's own taxonomy puts exhaustion in the permanent bucket, so `retryable: true` on an exhausted job would contradict it. Reading the flag as "will the engine retry this on its own?" also makes it directly useful to an API consumer: a polling script sees `retryable: false` and knows the job will not move again without human action. The information that would otherwise be lost lives in the reason string, which the brief already requires to be specific and honest.
- **Tradeoff / cost:** The flag no longer tells you whether *this class* of error is transient in principle. Recovered by writing reasons like `"upstream timed out (attempt 3 of 3, retries exhausted)"`.
- **Revisit if:** A client needs to distinguish "permanently failed because flaky" from "permanently failed because invalid" programmatically — then add a separate `error.kind` field rather than overloading `retryable`.
- **Note:** `retryable: false` does not mean un-retryable by a human. Manual operator retry remains available on every failed job, per the brief's "the operator decides when to retry."

### [2026-07-28] — Mock worker params extended with a permanent-failure trigger
- **Context:** The brief specifies `params.fail` returns *a retryable error*. That only exercises two of the three paths into `failed`: retry-with-budget, and retry-exhausted. A job marked non-retryable from the outset — which the brief explicitly names as a permanent failure — is unreachable with the given knobs.
- **Options considered:** Leave the mock worker exactly as specified and demo permanent failure only via exhaustion / add a second param.
- **Chose:** Add `params.fail_permanent`.
- **Why:** Without it a required behaviour of the engine cannot be demonstrated in the UI or in a test. The addition is additive — `params.fail` behaves exactly as the brief specifies — so nothing in the contract breaks.
- **Tradeoff / cost:** A small deviation from the letter of the brief, mitigated by being purely additive and documented here.
- **Revisit if:** N/A — the specified behaviour is preserved.

### [2026-07-28] — Failed jobs are collectable; collection is the ACK that frees a handle
- **Context:** A handle must be held while its job is "queued, running, or finished-but-uncollected." A `failed` job is finished-but-uncollected, so it holds its number — but if failure had no collection path, a failed job nobody retries would hold that number permanently and leak it.
- **Options considered:** Only `ready` jobs are collectable (accept the leak) / failed jobs are collectable too, acting as an acknowledgement.
- **Chose:** Failed jobs are collectable.
- **Why:** It closes the leak with no special-casing, and it matches how an operator actually works — you acknowledge a failure to clear it from your active view. `GET /tasks/{handle}/result` on a failed job returns the error object and flips `collected`, with `result` staying `null` per the fixed contract (result is populated only when status is `ready`).
- **Tradeoff / cost:** `/result` is no longer purely a success path, so its response shape varies by status. Documented in the README.
- **Revisit if:** N/A.
- **Note:** This is deliberate operator-initiated collection. Failed jobs are never *auto*-collected, per the brief's "the job is not silently collected."

### [2026-07-28] — Handle uniqueness enforced by a partial unique index, not application logic
- **Context:** Two simultaneous submits in the same lane will both read the table, both conclude the same number is free, and both try to claim it. Application-level checking is a race condition by construction.
- **Options considered:** Application-level check-then-insert / advisory lock or serialized allocation / partial unique index with insert-retry.
- **Chose:** Partial unique index on `(user_id, lane, number)`, restricted to rows where the handle is genuinely held.
- **Why:** It makes a collision *structurally impossible* rather than merely unlikely. Allocation becomes: compute the lowest free number, insert, and on unique violation recompute and retry. The loser of a race retries and correctly receives the next number. Correctness no longer depends on my code being careful, and the invariant is one readable line of DDL — far stronger evidence in a walkthrough than a paragraph of prose.
- **Tradeoff / cost:** Requires a retry loop on insert, and ties this design to Postgres (see below).
- **Revisit if:** Never, within this architecture. This is the load-bearing decision for one of the two most-scrutinised criteria.

### [2026-07-28] — Lease-based job claiming for restart durability
- **Context:** A row marked `running` carries no information about whether the process that claimed it is still alive. After a crash, orphaned rows are indistinguishable from legitimately in-flight ones.
- **Options considered:** (A) On boot, re-queue everything marked `running` (valid because single-replica). (B) Lease with expiry, heartbeat extension, and a periodic reaper.
- **Chose:** B.
- **Why:** A works today but only because I happen to run one replica — it's correct by accident of deployment rather than by design. B costs roughly twenty extra lines, converts "is that process alive?" into the trivially answerable "is `now()` past the expiry?", and gives an honest answer to "how would this behave with multiple workers?" A periodic reaper (not boot-only) also recovers a worker that *hung* rather than died, which boot-only recovery misses entirely.
- **Tradeoff / cost:** Requires heartbeating, since a lease shorter than a legitimately long job would be reclaimed mid-run and executed twice. Lease duration therefore expresses "how fast we detect death," not "how long jobs take."
- **Revisit if:** N/A.

### [2026-07-28] — A crash-requeue consumes an attempt
- **Context:** When a lease expires and a job is re-queued, should that count against its retry budget?
- **Options considered:** Don't count it (crashes aren't the job's fault) / count it.
- **Chose:** Count it.
- **Why:** Poison-job protection. A job that reliably kills the worker process would otherwise crash-loop forever, taking the whole system down with it repeatedly. Counting the attempt means it lands in `failed` after three tries and a human looks at it. The cost is that a blameless job unlucky enough to be running during a deploy loses one attempt of three — a far cheaper failure mode.
- **Tradeoff / cost:** Occasional unfair attempt consumption during planned restarts.
- **Revisit if:** Restarts become frequent enough that this misfires often, at which point distinguishing "clean shutdown" (don't count) from "lease expiry" (count) is the refinement.

### [2026-07-28] — At-least-once execution, jobs re-run from the start
- **Context:** An interrupted job has no checkpoint to resume from.
- **Options considered:** Checkpointing / idempotency keys / accept at-least-once.
- **Chose:** Accept at-least-once and re-run from the start.
- **Why:** The brief permits it — "in-flight work resumed **or cleanly re-queued**." The mock worker is a sleep, so there is nothing meaningful to checkpoint, and building a checkpointing protocol would be effort spent on a problem this system doesn't have.
- **Tradeoff / cost:** A crash at an unlucky moment can cause work to happen twice. Stated openly rather than left for a reviewer to discover.
- **Revisit if:** A real worker with side effects (sending email, charging a card) is added — then idempotency keys become mandatory.

### [2026-07-28] — Postgres over SQLite and MySQL
- **Context:** Needed persistent storage that survives container restarts and supports the concurrency primitives this design leans on.
- **Options considered:** SQLite / MySQL / Postgres.
- **Chose:** Postgres (managed, hosted outside the app container).
- **Why:** Three concrete reasons. (1) SQLite lives in the container's ephemeral filesystem and dies with the container; putting it on a network mount reintroduces well-known file-locking problems. (2) **MySQL does not support partial indexes**, and the handle-uniqueness invariant above is expressed as one — emulating it would need a nullable column plus triggers, which is fiddly and much harder to explain. (3) Keeping state outside the app container makes the restart-durability story trivially demonstrable: kill the container, the data is visibly untouched.
- **Tradeoff / cost:** A network round-trip per query, and a second piece of infrastructure to run locally (handled via docker-compose).
- **Revisit if:** N/A.

### [2026-07-28] — Raw SQL via asyncpg rather than an ORM
- **Context:** Needed a database access layer for the engine's store.
- **Options considered:** SQLAlchemy / an ORM with migrations / raw SQL in a single module.
- **Chose:** Raw SQL, confined entirely to `store.py`.
- **Why:** The SQL *is* the interesting part of this project. The partial unique index and the `FOR UPDATE SKIP LOCKED` claim are the two cleverest things in the codebase and the strongest material in the architectural walkthrough. Behind an ORM they become generated queries I'd be pointing at rather than four lines I wrote deliberately. Confining all SQL to one module keeps the boundary just as clean as an ORM would.
- **Tradeoff / cost:** No automatic migrations; schema managed as plain `.sql` files. Acceptable for a proof of concept.
- **Revisit if:** The schema starts changing often enough that hand-managed migrations become a burden.

### [2026-07-28] — Single replica by design
- **Context:** Azure Container Apps defaults toward HTTP-driven autoscaling and can scale to zero.
- **Options considered:** Leave autoscaling on / pin min and max replicas to 1.
- **Chose:** Pin to 1.
- **Why:** The engine runs an in-process worker pool. Two replicas means two pools claiming from the same queue — survivable thanks to `SKIP LOCKED` and the handle index, but it doubles the concurrency limit the brief asks me to respect, which would break criterion 7 outright. Scaling to zero would stop the worker pool entirely and leave queued jobs untouched until the next HTTP request. Pinning to one replica is the correct call for a system whose whole job is running background work under a stated concurrency limit.
- **Tradeoff / cost:** No horizontal scaling, and the deployment is a single point of failure. Appropriate for a proof of concept; the lease design means removing this limit is a config change plus a per-replica concurrency recalculation, not a rewrite.
- **Revisit if:** Throughput demands exceed one process — then concurrency becomes a cluster-wide budget rather than a per-process one.

### [2026-07-28] — Engine as a standalone module; API layer imports only its facade
- **Context:** The brief requires the engine to be usable with the frontend deleted, and grades the cleanliness of that boundary.
- **Options considered:** Route handlers talk to the store directly where convenient / all routes go through a single engine facade object.
- **Chose:** The facade. No `fastapi` import anywhere under `engine/`; no database driver import anywhere outside `engine/store.py`.
- **Why:** The brief's stated boundary is frontend↔API, which my architecture satisfies trivially since they're separate apps. This is the *second*, internal boundary — and it's the one that actually keeps the engine the single source of truth, because nothing stops a route handler from running its own UPDATE. Route handlers become three lines: parse, delegate, serialise. It also means a CLI client or a queue consumer could drive the same engine unchanged. Both rules are verifiable in seconds on camera, which makes them worth more than any amount of prose about clean architecture.
- **Tradeoff / cost:** Occasional facade methods that exist purely to expose something a route needs.
- **Revisit if:** N/A.

### [2026-07-28] — Python / FastAPI over Node
- **Context:** The brief allows either and asks that the engine stay in one of them.
- **Options considered:** Node/TypeScript / Python/FastAPI.
- **Chose:** Python with FastAPI.
- **Why:** It's the stack I move fastest in, and speed matters over a 4–5 day window. `asyncio` gives me exactly the primitives this design needs — cancellable tasks via `CancelledError`, a natural worker pool, and `asyncio.sleep` as a genuinely interruptible mock. FastAPI has first-class SSE support and generates API docs from the route definitions, which serves the brief's documentation requirement directly.
- **Tradeoff / cost:** The brief's worker contract is written in TypeScript, so I'm implementing its shape rather than the literal interface. Frontend and backend are different languages, so no shared types.
- **Revisit if:** N/A.

### [2026-07-28] — Diagrams as Mermaid in the repo, not exported images
- **Context:** The brief asks me to "sketch your approach in the architecture writeup."
- **Options considered:** Excalidraw / Lucidchart / draw.io exports / Mermaid.
- **Chose:** Mermaid, committed as text in the README.
- **Why:** It version-controls with the code, produces readable diffs when the design changes, renders natively on GitHub with no export step, and can't go stale as a forgotten PNG. Three diagrams: `stateDiagram-v2` for the lifecycle, `flowchart` for module boundaries, `sequenceDiagram` for crash-and-recover — the last of which *is* the durability sketch the brief asks for.
- **Tradeoff / cost:** Less layout control than a drawing tool.
- **Revisit if:** A diagram gets complex enough that Mermaid's auto-layout becomes unreadable.

### [2026-07-28] — Engine first, frontend last
- **Context:** Sequencing a 4–5 day build.
- **Options considered:** Build vertically (thin slice through every layer) / engine to completion, then API, then tests, then frontend.
- **Chose:** Engine → API → tests → seed data → frontend → deploy → recording.
- **Why:** The brief's evaluation order is explicit — "the engine first, and evaluated hardest" — and its quality bar requires the engine to be fully exercisable with the frontend deleted. Building the frontend early would tempt me to reach into engine internals for convenience, which is precisely the failure mode being graded. Tests before the frontend also means the nine success criteria are proven at the API level, where the brief asks for them.
- **Tradeoff / cost:** No visual progress to show until late in the build, which feels slower than it is. Mitigated by the mid-week Slack update covering engine progress rather than screenshots.
- **Revisit if:** N/A.

---

## What I'd do next (if this were more than a week)
- **Multi-replica workers** with a cluster-wide concurrency budget rather than a per-process one. The lease design already supports it; what's missing is coordinating the limit across replicas.
- **Idempotency keys on the worker contract**, so at-least-once execution becomes safe for workers with real side effects.
- **A delta endpoint** (`GET /tasks?updated_since=`) so reconnects don't need a full list refetch as the task table grows.
- **Scheduled and recurring jobs** — the `not_before` column used for retry backoff is already most of the mechanism needed for "run this at 3am."
- **Per-lane parameter schemas** with server-side validation, which becomes worth building the moment there's a real worker whose params actually matter.
- **Structured metrics** — queue depth, time-to-claim, worker utilisation, retry rates — since the brief's emphasis on observability naturally extends past the dashboard into instrumentation.
