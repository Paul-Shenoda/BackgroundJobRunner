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

### [2026-07-29] — `claim_next` is a global queue, deliberately not scoped per-pool or per-process
- **Context:** Found while building scripts/smoke.py's poison-job scenario (J). That scenario needs to run a second, temporary `WorkerPool` to force a controlled lease expiry, without it racing the main pool that's been running for the rest of the script. `claim_next` filters only on `status = 'queued' AND (not_before IS NULL OR not_before <= now())` — nothing identifies which pool, process, or replica "owns" a row before claiming it.
- **Options considered:** Add a pool/worker-scoping column or filter to `claim_next` / leave it global.
- **Chose:** Leave it global.
- **Why:** This is correct and deliberate for single-replica deployment (see "Single replica by design," below) — exactly one pool ever runs in production, so "global" and "this pool's work" are the same set by construction. It is also exactly the mechanism that makes horizontal scaling work for free if that limit were ever lifted: `FOR UPDATE SKIP LOCKED` already makes concurrent claimers from different processes or replicas safe and non-colliding — that is the entire reason that clause exists (see "Lease-based job claiming for restart durability"). Scoping `claim_next` per-pool would be actively wrong for that future: it would stop replicas from load-balancing the same queue, which is exactly the behavior multi-replica deployment would want.
- **Tradeoff / cost:** scripts/smoke.py's poison-job scenario has to explicitly stop the main pool before running its own temporary second pool, rather than being able to run both concurrently. A test-only inconvenience, not a production concern.
- **Revisit if:** Never, within this architecture. This is the answer to "what happens with two instances of the engine running?" — not a limitation waiting to be fixed.

### [2026-07-29] — Poison-job protection: documented as an invariant, not actually implemented until code review
- **Context:** CLAUDE.md stated from the very start of the build: "A lease-expiry requeue **does** consume an attempt (poison-job protection)." The code never enforced it. `reap_expired_leases` unconditionally reset an expired-lease row back to `'queued'`, with no check of `attempts` against `max_attempts` — a task whose lease kept expiring (crashed or hung worker) would cycle `running → queued → running` forever, `attempts` climbing unbounded, never reaching terminal `failed`. The only place `attempts >= max_attempts` was ever checked was runner.py's `_record_outcome`, reachable only when a worker's coroutine actually returns a `WorkerResult` — never on the reap path. scripts/smoke.py's original 9 scenarios never forced a lease to actually expire, so nothing caught the gap until an explicit `/code-review` pass, followed up by independent verification against the live code before trusting the finding.
- **Options considered:** N/A — this is a bug fix restoring documented behavior, not a design fork with real alternatives.
- **Chose:** `reap_expired_leases` now branches inside its single atomic `UPDATE` via `CASE WHEN attempts >= max_attempts`: exhausted rows go straight to `'failed'`, budget-remaining rows requeue exactly as before. Doing the branch inside one `UPDATE` (not a `SELECT`-then-branch) keeps the exhausted case atomic — it can never surface a dishonest intermediate `RUNNING → QUEUED` transition that a moment later gets papered over by `QUEUED → FAILED`.
- **Why:** The alternative (checking attempts only after a separate read) would let a caller observe or record a wrong intermediate state, and would require a second round trip for something that has to be decided at the same instant the row is claimed as expired.
- **Tradeoff / cost:** `store.py`'s SQL now embeds a retry-budget comparison (`attempts >= max_attempts`), which is a narrow, deliberate exception to this module's usual rule that retry policy lives upstream in runner.py/engine.py — forced by the atomicity requirement above, not an oversight. Documented inline in the method's docstring so a future reader doesn't "fix" it by moving the check out.
- **Revisit if:** N/A.
- **Note:** Logged honestly as found-in-review rather than glossed over, per CLAUDE.md's own bar for honest, distinct reasons and no silent failures — the gap existed in shipped, committed code for one full phase before being caught.

### [2026-07-29] — Reaper-path and worker-path exhaustion failures use deliberately different reason wording
- **Context:** Once the poison-job fix above lets `reap_expired_leases` terminate a task directly, its exhaustion reason needed its own wording — reusing the WorkerResult-path's format (built from `error.reason`, e.g. `"upstream connection reset (attempt 3 of 3, retries exhausted)"`) isn't available here, since a reaped task's worker never returned a `WorkerResult` at all.
- **Options considered:** Reuse a generic reason like `"retries exhausted"` for both paths / give the reap path its own distinct wording.
- **Chose:** The reap path's reason is `"worker died mid-execution (attempt N of M, retries exhausted)"` — distinct from any worker-reported reason.
- **Why:** These describe genuinely different failures. A worker-reported reason (e.g. "upstream connection reset") says the *work itself* failed while the process stayed alive to report it. The reap-path reason says the *process never got the chance to report anything* — its lease simply stopped being renewed. Collapsing these into one generic message would hide exactly the distinction an operator needs to diagnose the difference between "the target system is flaky" and "our own worker keeps dying or hanging." Matches CLAUDE.md's "distinct, honest reasons" requirement applied to a failure path the brief didn't explicitly anticipate.
- **Tradeoff / cost:** None meaningful — one more string constant.
- **Revisit if:** N/A.

### [2026-07-29] — The `_on_job_done` identity check doesn't prevent double execution, and doesn't need to
- **Context:** `_dispatch_loop` writes `self._running[task.id] = job_task` unconditionally. If the reaper reclaims a task's lease while its worker is genuinely still running (heartbeat trouble, or just slow to notice), the row can go back to `'queued'` and get re-claimed before the original job finishes — overwriting the dict entry and orphaning the original from `self._running`'s bookkeeping, which breaks `cancel_running()`'s accuracy and the concurrency gate's count.
- **Options considered:** Leave `_on_job_done`'s pop unconditional (buggy) / make it identity-checked, only popping the entry it actually owns / attempt to structurally prevent double execution entirely (lease fencing tokens).
- **Chose:** Identity-checked pop (`if self._running.get(task_id) is job_task: pop`). Explicitly did NOT attempt to structurally prevent double execution.
- **Why:** Traced the actual failure mode before fixing it: the identity check fixes *tracking* (so `self._running` and `cancel_running()` stay accurate for whichever execution is current), not *double execution* itself — a genuinely orphaned worker can still run to completion in parallel with a freshly re-claimed one. That's acceptable because `mark_ready`/`mark_failed`/`requeue_for_retry` all already guard on `WHERE status = 'running'`: whichever execution finishes first wins and records the real outcome; the second's identical call finds the row no longer `'running'`, returns `None`, and publishes nothing — the same "lost the race, no event" pattern used everywhere else in this codebase for cancel-vs-worker and reap-vs-worker races. Data integrity is never at risk; only the underlying work (the mock worker's sleep; a real worker's side effects) can run twice — which is exactly what "At-least-once execution, jobs re-run from the start" (below) already accepts, just triggered here by an in-process heartbeat failure racing the reaper instead of a full crash/restart. Lease fencing tokens would close this structurally, but that's out of scope for a single-replica system per that same entry's revisit condition.
- **Tradeoff / cost:** None beyond what "at-least-once" already costs. The companion fix to `_heartbeat_loop` (cancel the worker on ANY heartbeat trouble, not only an explicit "lease lost" report) shrinks the window this can occur in, without claiming to close it.
- **Revisit if:** Throughput demands exceed one process (same condition as "Single replica by design") — multi-replica coordination would make lease fencing worth the added complexity.

### [2026-07-29] — CLAUDE.md hard rule #2 reworded to scope asyncpg-confinement to `engine/` (retroactive entry)
- **Context:** Commit `2979fde` reworded hard rule #2 from "No database driver outside `engine/store.py`" (read as repo-wide) to "Within `engine/`, no database driver outside `engine/store.py`" — narrowing it explicitly to the engine's internal layering — in the same commit that added `scripts/bootstrap.py` and `scripts/smoke.py`, both of which use `asyncpg` directly. No dated entry was added at the time, which is itself a lapse: CLAUDE.md's own housekeeping rule requires one for exactly this kind of change ("Append a dated entry to DECISIONS.md whenever a real fork is resolved"), and a Hard Rule is explicitly the category the doc calls "an architectural boundary... a bug even if the code works" — narrowing one is a real fork, not a typo fix.
- **Options considered:** Leave the rule repo-wide and treat `scripts/` as a one-off, undocumented exception / reword the rule to state the boundary it was always actually protecting.
- **Chose:** Reword the rule.
- **Why:** The rule's actual purpose — verifiable in one command, `grep -rl asyncpg engine/` — was always about the engine's internal layering (no SQL leaking outside `store.py` inside the engine), not about the whole repository. Bootstrap/seed scripts and future tests sit outside that boundary by design; they aren't part of the engine and were never meant to be constrained by a rule about the engine's internal structure. The original repo-wide wording was simply broader than the rule's own justification supported.
- **Tradeoff / cost:** None — this is a clarification of intent that was always true in spirit, not a loosening of a real constraint. The verifiable check (`grep -rl asyncpg engine/`) is unchanged.
- **Revisit if:** N/A.
- **Note:** Logged after the fact, during a code-review pass that flagged the missing entry directly — the honest thing is to add it now rather than pretend it was never missing.

### [2026-07-29] — A handle identifies the active task, with fallback to most recent history
- **Context:** Discovered via scripts/smoke.py's first real run against Postgres — the first time store.py's SQL executed against a live database rather than a fake. Handle recycling means a handle string like `scrape-1` is not unique across time: once a task is collected or cancelled, its number can be reused by a new submission, leaving an old terminal row and a new active row that share the same `(user_id, lane, number)`. `get_task`'s query had no `ORDER BY`, so with two matching rows Postgres returned whichever its scan happened to find first — observed in practice as consistently the OLDER row (sequential scan order tends to favor earlier-inserted tuples). `GET /tasks/scrape-1` could silently return a stale collected/cancelled row while the real, live `scrape-1` was running or ready — a genuine defect sitting directly on criterion 4 (handle recycling), not a smoke-test artifact. The same audit found `reset_for_retry`'s `WHERE status = 'failed'` had the identical exposure: missing `AND collected = false`, so an operator retry could silently match an old collected 'failed' row instead of the live one.
- **Options considered:** (A) Make handle lookups 404 the instant a handle is reused elsewhere (reject ambiguity entirely). (B) Resolve to the active row if one exists, falling back to the most recent historical row otherwise. (C) Leave read paths ambiguous and only fix the mutating methods (which are already unambiguous by construction — see below).
- **Chose:** B, for `get_task` (the only method that needs a fallback). The mutating methods (`collect_result`, `cancel_task`, `reset_for_retry`) don't get a fallback — they must return `None` when there's no active row, never fall back to touching history.
- **Why:** A handle names a currently-active task first and foremost, so while one exists it must win unambiguously — that's B and C's shared premise. But a client that just collected `scrape-1` and immediately re-fetches it (a completely normal UI flow — confirm-then-refresh) should still see that task, not a 404 (rules out A) and not silence (rules out doing nothing). Falling back to the most recent historical row keeps that working while never letting a stale row outrank a live one. The ordering expression (`(status IN ('queued','running') OR (status IN ('ready','failed') AND collected=false)) DESC, created_at DESC`) is deterministic precisely because `uniq_active_handle` guarantees at most one row can ever evaluate the first clause `TRUE` — there's no scenario where two rows both claim to be "the active one."

  The mutating methods needed no such ordering trick, once actually audited: `cancel_task`'s `status IN ('queued','running')` and `collect_result`'s `status IN ('ready','failed') AND collected=false` are each already an exact subset of `uniq_active_handle`'s predicate, so the index itself guarantees at most one row can match — they were unambiguous by construction and needed no code change, only confirmation (now documented inline in each). `reset_for_retry` was the one exception: its `WHERE status = 'failed'` was missing `collected = false`, so it wasn't actually a subset of the active predicate — fixed by adding that clause, bringing it in line with the other two.
- **Tradeoff / cost:** `get_task`'s query is now a three-way `ORDER BY` instead of a flat `WHERE`, and every handle-scoped read carries that cost — negligible at this table size, and the index that makes it correct is already required for handle allocation.
- **Revisit if:** N/A — this is the correct permanent semantics for a recyclable-handle system, not a stopgap.

### [2026-07-29] — `Engine.list()` renamed to `list_tasks()`: it collided with the builtin
- **Context:** `Engine` defined a method named `list()`. Python class bodies execute as a namespace: once `list()` was defined, every subsequent statement in that class body — including type annotations evaluated at `def`-time, like `get_detail`'s `-> Optional[tuple[Task, list[TaskEvent]]]` — resolved `list` to that method object instead of the builtin, and crashed at class-definition time (`TypeError: 'function' object is not subscriptable`).
- **Options considered:** Work around it with `from __future__ import annotations` alone (defers annotation evaluation to strings, so it no longer breaks) / rename the method.
- **Chose:** Both — keep the `__future__` import as cheap insurance, but also rename `list()` to `list_tasks()`.
- **Why:** The `__future__` import only fixes annotations. It does nothing about a plain `list(...)` call appearing later in the same class body (a default value, a class-level constant, a decorator argument) — that would still resolve to the method and fail or misbehave silently depending on arity. Renaming removes the collision at its source instead of papering over one symptom of it. It also now matches `store.list_tasks`, so the same operation has the same name at both layers.
- **Tradeoff / cost:** None — `list_tasks` is arguably the better name anyway (`Engine.list` read ambiguously next to Python's own `list`).
- **Revisit if:** N/A.

### [2026-07-29] — No graceful-shutdown mode; the reaper is the only recovery path
- **Context:** `WorkerPool.stop()` was initially specced with a `graceful` flag, without defining what the non-graceful branch would skip.
- **Options considered:** Implement a real distinction (drain in-flight jobs vs abandon them) / keep the flag as a no-op / remove it.
- **Chose:** Remove it. `stop()` takes no flag.
- **Why:** Both branches would have done the same thing. Shutdown cancels in-flight asyncio tasks and leaves their rows `running` with live leases for the reaper to reclaim on next boot — there is no draining step for a "less graceful" mode to skip. More fundamentally, a SIGKILL runs no cleanup at all, so the reaper has to be correct without any shutdown hook. Once that's true, a shutdown handler is a second mechanism doing what the first already does. A parameter that doesn't change behaviour is worse than no parameter: it implies a distinction that doesn't exist.
- **Tradeoff / cost:** After an unclean kill, orphaned rows stay `running` for up to `lease_seconds` before recovery. Accepted deliberately — see the at-least-once entry below.
- **Revisit if:** Workers acquire external resources needing release on shutdown, at which point cleanup becomes the worker's responsibility, not the pool's.

### [2026-07-29] — A retry emits no lifecycle event, whether automatic or operator-triggered
- **Context:** When a retryable failure returns a job to `queued` for another attempt, should that publish to the event stream? The same question came up again for `Engine.retry()`, the operator-triggered path for a `failed` job — it was initially specced to publish `accepted`, on the reasoning that the job is "re-entering the system."
- **Options considered:** Emit a `retrying` event / emit `failed` then `accepted` / emit `accepted` only for the operator-triggered path (treating it as distinct from an automatic requeue) / emit nothing in either case.
- **Chose:** Emit nothing, for both the automatic requeue (`runner.py`) and the operator-triggered retry (`Engine.retry()`).
- **Why:** The API contract fixes exactly four event types — `accepted`, `ready`, `failed`, `cancelled` — and none of them honestly describes "an existing task returned to `queued`." A `retrying` type would violate the contract. Emitting `failed` for a job about to run again would mislead a consumer treating `failed` as terminal. And `accepted` specifically means "a new row was created" — a client that treats `accepted` as "append a row" would duplicate an existing task on every operator retry, since the handle, params, and history are all unchanged from before. The retry is still fully visible: recorded in `task_events`, with `attempts` reset to 0 and visible on the dashboard row, and the task itself shows back up in a `GET /tasks?status=queued` refetch.
- **Tradeoff / cost:** A client watching only the stream sees nothing when a retry happens, automatic or operator-triggered. Acceptable because the stream is a liveness hint, not the source of truth — a refetch shows current state either way.
- **Revisit if:** The contract's event types were ever extensible — at which point a `requeued` type covering both cases would be the natural addition, not two different ad-hoc signals for the two retry paths.

### [2026-07-29] — Unknown lanes are rejected at submission, not discovered at claim time
- **Context:** Nothing initially prevented submitting a task for a lane with no registered worker. Such a task would be created, sit in `queued`, get claimed, and then have nowhere to go — a silently stuck job.
- **Options considered:** Fail it at claim time / validate at submission via `registry.has(lane)`.
- **Chose:** Validate at submission, and *also* fail defensively at claim time.
- **Why:** A stuck job with no explanation is exactly the silent failure the brief's observability bar rules out. Rejecting at submission gives the caller an immediate, actionable error instead of a task that looks accepted and never moves. The claim-time check stays as a backstop for the case where a worker is unregistered after a task is already queued — it marks the task failed and non-retryable rather than leaving it `running` forever.
- **Tradeoff / cost:** Lane validity is coupled to registry state at submission time.
- **Revisit if:** Lanes ever need to be submittable before their workers are registered (e.g. deferred worker loading).

### [2026-07-29] — Bounded subscriber queues; drop events rather than block
- **Context:** The event bus fans out to one queue per subscriber. A slow or dead SSE client could otherwise grow its queue without limit or stall the publisher.
- **Options considered:** Unbounded queues / bounded with blocking publish / bounded with drop-on-full.
- **Chose:** Bounded (maxsize 100), dropping the event for any subscriber whose queue is full. `publish()` never blocks and never raises into its caller.
- **Why:** The worker pool must never be stalled by a client that stopped reading — a failure to *notify* must not fail a job that actually *completed*. Dropping is safe precisely because of the liveness-hint decision below: clients refetch `GET /tasks` on reconnect, so a dropped event self-heals. Unbounded queues would turn a dead browser tab into a memory leak.
- **Tradeoff / cost:** A very slow client can miss events. Recovered on its next refetch.
- **Revisit if:** Events ever carry information not reconstructible from task state.

### [2026-07-28] — `store.py` translates driver exceptions into domain exceptions
- **Context:** `handles.py` needs to detect a lost allocation race, which surfaces as `asyncpg.UniqueViolationError`. Catching it there would put a database driver import in a module that is otherwise pure policy.
- **Options considered:** Catch `UniqueViolationError` directly in `handles.py` (permitting one carve-out to the asyncpg rule) / have `store.py` catch it and re-raise a domain `HandleConflict`.
- **Chose:** Translation in `store.py`.
- **Why:** It preserves a claim that is checkable in one command — `grep -rl asyncpg engine/` returns exactly one file, with no exceptions to explain. Architecture claims are cheap; a falsifiable one that survives the check is worth far more. It's also better on the merits: translating a driver's vocabulary into the domain's is precisely what a data-access layer is for, and `store.py` already does the same for rows (raw record → `Task`). Leaving errors untranslated would be doing half the job.
- **Tradeoff / cost:** One extra exception class and a try/except in `insert_task`.
- **Revisit if:** N/A.
- **Note:** This buys layer hygiene, not database portability. The claim query depends on `SKIP LOCKED` and allocation depends on partial indexes, so swapping storage engines would mean rewriting `store.py`'s logic, not just its connection string. The honest claim is "the blast radius of a storage change is one file," not "the engine is database-agnostic."

### [2026-07-28] — `task_events` records creation, not just transitions
- **Context:** `insert_task` initially wrote no `task_events` row, so a task's state history began at `queued → running` with no record of its own creation.
- **Options considered:** Treat creation as not-a-transition / record it with `from_status = NULL`.
- **Chose:** Record it.
- **Why:** The brief requires the detail view to show "its state history with timestamps." A history missing its first entry is incomplete. `TaskEvent.from_status` is already `Optional[Status]` precisely so a null origin is representable.
- **Tradeoff / cost:** One extra row per task.
- **Revisit if:** N/A.

### [2026-07-28] — `TaskEvent` (persisted history) vs `LifecycleEvent` (live stream)
- **Context:** Two different concepts were converging on the same name: the `task_events` rows backing the detail view's state history, and the four-type notifications pushed to SSE clients.
- **Options considered:** Reuse one type for both / name them distinctly.
- **Chose:** `TaskEvent` for persisted state history, `LifecycleEvent` for stream notifications.
- **Why:** They differ in shape, audience, and lifetime. History rows are internal, exhaustive, and permanent — including transitions like `running → queued` on retry that deliberately never reach the stream. Lifecycle events are external, limited to four contract-fixed types, and ephemeral. Collapsing them would have forced one of the two to carry fields it doesn't need, and made every later conversation about "events" ambiguous.
- **Tradeoff / cost:** Two similar-sounding types to keep straight.
- **Revisit if:** N/A.

### [2026-07-28] — API serialisation derives `result`/`error` from `status` rather than trusting stored state
- **Context:** The contract states `result` is populated only when status is `ready`, and `error` only when `failed`.
- **Options considered:** Emit whatever the row holds / compute both from the current status at serialisation time.
- **Chose:** Compute from status.
- **Why:** It makes the contract hold structurally rather than by convention. Even if a bug left a stale `result` on a row that later failed, the serialised output stays conformant. The invariant is enforced at the single point where it's externally visible, rather than depending on every write path having cleaned up correctly.
- **Tradeoff / cost:** None meaningful.
- **Revisit if:** N/A.

### [2026-07-28] — Codespaces devcontainer adds an app service, not just Postgres
- **Context:** The initial scaffold could have extended the root compose file's Postgres service alone, leaving Codespaces with a database but no Python environment.
- **Options considered:** Postgres-only devcontainer / add an app service extending the root compose file.
- **Chose:** Add an app service (Python 3.12 image, repo mounted, waiting on the Postgres healthcheck).
- **Why:** The brief requires the project to run in GitHub Codespaces. A bare Postgres container satisfies the letter of "bring up the database" while leaving nowhere to actually run the engine. Extending the root compose file rather than duplicating it keeps local WSL and Codespaces on one definition — two divergent environment setups is a failure that surfaces at the worst possible moment.
- **Tradeoff / cost:** A second compose file to keep coherent, mitigated by `extends` rather than duplication.
- **Revisit if:** N/A.


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
- **Tradeoff / cost:** A crash at an unlucky moment can cause work to happen twice. Stated openly rather than left for a reviewer to discover. This acceptance also covers a narrower, in-process trigger found later (see "The `_on_job_done` identity check doesn't prevent double execution, and doesn't need to" below): a heartbeat failure racing the reaper can produce two live executions of the same task within a single still-running process, not just across a crash/restart. Same consequence (work happens twice), different mechanism — already covered by this entry, not a new exception to it.
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

---

## Smaller implementation calls
Real forks, resolved quickly — logged so they don't have to be re-reasoned later.

- **`Status` as a `str` subclass enum** — serialises as a plain string with no custom JSON encoder, and `Status.QUEUED == "queued"` holds when comparing against values read back from Postgres. A plain `Enum` would need `.value` access at every boundary.
- **`WorkerResult.status` typed `Literal["ready","failed"]`, not `Status`** — encodes the worker boundary in the type system. A worker literally cannot express `cancelled` or `running`; the engine owns the lifecycle.
- **`_iso8601` helper** — `datetime.isoformat()` on a tz-aware UTC value yields `+00:00`, not the `Z` the contract requires. One-line helper rather than a per-call fix. Note it is a no-op on naive datetimes; asyncpg returns `TIMESTAMPTZ` as tz-aware, so this holds in practice.
- **`TaskEvent` omits `task_id`** — history is always fetched scoped to its parent task, so re-embedding the parent id in every entry is redundant. Add it back only if a cross-task audit view appears.
- **`publish()` is synchronous, not `async def`** — it never awaits. Declaring it async would misrepresent its behaviour and force every caller into a pointless `await`.
- **`subscribe()` returns the raw bounded `Queue`** rather than a wrapped async generator — `api/events.py` needs to interleave `: ping` heartbeat lines with real events on its own schedule, which a yield-on-event-only generator would make awkward.
- **Duplicate worker registration raises** rather than silently overwriting. A silently replaced worker is a bug that costs an hour to find.
- **`collect_result` is a single UPDATE, not a transaction** — single statements are atomic in Postgres. It would need one only if it later also writes to `task_events`.
- **`get_task_events` is keyed by `task_id`, not `user_id`** — authorisation sits one layer up: `engine.py` is its only caller and always sequences `get_task(user_id, handle)` first. The boundary is deliberate, not absent.
- **Docker Desktop's containerd image store** mishandles range-requested layer downloads over WSL2, causing truncated pulls. Fix is to disable "Use containerd for pulling and storing images". Documented in the README's known issues since a reviewer on Windows could hit it.


## What I'd do next (if this were more than a week)
- **Multi-replica workers** with a cluster-wide concurrency budget rather than a per-process one. The lease design already supports it; what's missing is coordinating the limit across replicas.
- **Idempotency keys on the worker contract**, so at-least-once execution becomes safe for workers with real side effects.
- **A delta endpoint** (`GET /tasks?updated_since=`) so reconnects don't need a full list refetch as the task table grows.
- **Scheduled and recurring jobs** — the `not_before` column used for retry backoff is already most of the mechanism needed for "run this at 3am."
- **Per-lane parameter schemas** with server-side validation, which becomes worth building the moment there's a real worker whose params actually matter.
- **Structured metrics** — queue depth, time-to-claim, worker utilisation, retry rates — since the brief's emphasis on observability naturally extends past the dashboard into instrumentation.