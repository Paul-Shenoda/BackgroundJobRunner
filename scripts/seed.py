"""Synthetic demo data: two named users, tasks in every status, plausible
state history for each.

Run as:
    python -m scripts.seed [--reset | --clear | --wipe] [--purge-debug-users]

--reset, --clear, and --wipe are mutually exclusive -- each ends the seed
data in a different state (reseeded / empty / gone-with-the-users), so
combining them would just mean one silently overriding another. See each
flag's --help text below for the exact distinction, particularly between
--reset (clear then reseed) and --clear (clear and stop -- no reseed).

A bare run only ever touches Alice's and Bob's own rows -- it never deletes
another user. --purge-debug-users is the one opt-in exception: it previews
which non-demo, non-test-fixture users exist and asks for confirmation
before deleting them and their tasks. See PROTECTED_KEYS below.

Reads DATABASE_URL from .env (or the environment), same as every other
script in scripts/.

Rows are inserted directly via SQL (through the same one-shot `connection()`
helper scripts/bootstrap.py already shares with scripts/smoke.py and the
pytest fixtures), never through Engine. Going through engine.submit() would
mean every seeded task starts 'queued' and gets claimed by the live worker
pool the moment the app boots — the opposite of what demo data needs. Direct
inserts are also the only way to backdate created_at/updated_at and write a
task_events history that looks like it happened over the last two weeks
rather than in the last second.

Every row here gets seeded=true (sql/schema.sql's column for exactly this
purpose) so the API's detail response and the future dashboard can visibly
mark it as demo data rather than something a real run produced.

Boundary note: this script is outside engine/, so CLAUDE.md's "asyncpg
confined to store.py" rule doesn't bind it, same reasoning as
scripts/bootstrap.py's own docstring.
"""

import argparse
import asyncio
import json
import os
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import asyncpg
from dotenv import load_dotenv

from scripts.bootstrap import connection, ensure_user

load_dotenv()

DSN = os.environ["DATABASE_URL"]

ALICE_NAME = "Alice"
ALICE_KEY = "alice-demo-key"
BOB_NAME = "Bob"
BOB_KEY = "bob-demo-key"

# Users that must survive a seed run even though they aren't Alice/Bob: the
# pytest fixture users from tests/conftest.py and tests/test_restart.py
# (USER_A_KEY/USER_B_KEY/RESTART_USER_KEY). Duplicated as literals rather
# than imported — scripts/ has no business depending on tests/ — but these
# three keys are effectively frozen API surface for the test suite, so
# anything else found in the users table is fair game as leftover ad-hoc
# debug data from manual poking (e.g. a `psql` session or a one-off repro).
PROTECTED_KEYS = {
    ALICE_KEY,
    BOB_KEY,
    "phase4-test-key-a",
    "phase4-test-key-b",
    "phase4-test-key-restart",
}

# Sentinel far-future timestamp for the one seeded 'queued' row that must
# never be picked up by the live system on boot: the dispatch loop only
# claims queued rows with not_before <= now(), and this date is comfortably
# past that check for the life of this demo, without inventing a "scheduled
# job" feature the engine doesn't actually have.
#
# There is deliberately no seeded 'running' row (and, as of the inertness
# review below, only ONE seeded 'queued' row, not several) — see
# DECISIONS.md, "seeded non-terminal rows read as 'the system is stuck'".
# A permanently-running job reads as a hung worker, more alarming than a
# permanently-queued one, and 'running' is trivially demonstrated by
# submitting a real job — seeding one bought nothing and cost credibility.
# A single queued row is still enough to demonstrate `status=queued`
# filtering; more would just be more rows to explain away as inert.
FAR_FUTURE = datetime(2099, 1, 1, tzinfo=timezone.utc)

# Distinct, honest failure reasons (CLAUDE.md: "do not use the same reason
# string on every failed row"). Sized to exactly match how many exhausted
# vs. permanent failures the spec table below uses, so every failed seed row
# gets a different one.
RETRYABLE_REASONS = [
    "upstream timed out",
    "upstream connection reset",
    "rate limited by target host (429 Too Many Requests)",
    "DNS resolution failed for target host",
    "target host returned 503 Service Unavailable",
    "connection pool exhausted talking to upstream",
]

PERMANENT_REASONS = [
    "unsupported input format: expected JSON",
    "malformed request payload: missing required field 'url'",
    "target returned 410 Gone -- resource permanently removed",
    "authentication rejected by upstream: credentials expired",
    "target host returned 403 Forbidden -- access denied",
]

# Reasons narrated on the transient running->queued hop inside a "succeeded
# on retry" history. Not a failed row's terminal reason, so CLAUDE.md's
# uniqueness rule doesn't bind these the same way, but varying them keeps
# the state-history panel from looking templated.
TRANSIENT_RETRY_REASONS = [
    "upstream connection reset",
    "upstream timed out",
    "rate limited by target host (429 Too Many Requests)",
    "DNS resolution failed for target host",
]


@dataclass
class Spec:
    """One seed task, before it's been assigned a handle number or turned
    into DB rows."""

    user_id: uuid.UUID
    lane: str
    kind: str  # ready1 | ready2 | exhausted | permanent | cancel_queued | cancel_running | queued_future
    days_ago: float
    duration_ms: int
    collected: bool = False
    reason: Optional[str] = None  # exhausted/permanent: the base reason
    retry_reason: Optional[str] = None  # ready2: the transient mid-history reason


def build_specs(alice_id: uuid.UUID, bob_id: uuid.UUID) -> list[Spec]:
    """Hand-authored rather than randomly generated: the brief wants
    realistic, varied, screenshot-worthy demo data, which is easier to get
    right by choosing each row deliberately than by tuning a randomizer.
    `random` is still used downstream for result payload numbers, where
    the exact value genuinely doesn't matter.

    Uses all 6 RETRYABLE_REASONS and all 5 PERMANENT_REASONS exactly once
    across both users, so every failed row's reason is distinct.
    """
    r = iter(RETRYABLE_REASONS)
    p = iter(PERMANENT_REASONS)
    t = iter(TRANSIENT_RETRY_REASONS)

    alice = [
        Spec(alice_id, "scrape", "ready1", 13.7, 6200, collected=True),
        Spec(alice_id, "report", "ready1", 13.2, 9100, collected=True),
        Spec(alice_id, "scrape", "exhausted", 12.6, 4800, collected=True, reason=next(r)),
        Spec(alice_id, "report", "permanent", 12.0, 3100, collected=True, reason=next(p)),
        Spec(alice_id, "scrape", "cancel_queued", 11.4, 7000),
        Spec(alice_id, "report", "ready1", 10.8, 5400),
        Spec(alice_id, "scrape", "ready2", 10.1, 6100, collected=True, retry_reason=next(t)),
        Spec(alice_id, "report", "cancel_running", 9.5, 11000),
        Spec(alice_id, "scrape", "permanent", 8.9, 4300, collected=True, reason=next(p)),
        Spec(alice_id, "report", "exhausted", 8.3, 5200, reason=next(r)),
        Spec(alice_id, "scrape", "ready1", 7.7, 8300, collected=True),
        Spec(alice_id, "report", "ready1", 7.1, 3900),
        Spec(alice_id, "scrape", "cancel_queued", 6.5, 6600),
        Spec(alice_id, "report", "ready2", 5.9, 7200, collected=True, retry_reason=next(t)),
        Spec(alice_id, "scrape", "exhausted", 5.3, 5600, collected=True, reason=next(r)),
        Spec(alice_id, "report", "permanent", 4.7, 4100, reason=next(p)),
        Spec(alice_id, "scrape", "ready1", 4.1, 9800),
        Spec(alice_id, "report", "cancel_running", 3.5, 13500),
        Spec(alice_id, "scrape", "permanent", 2.9, 3600, collected=True, reason=next(p)),
        Spec(alice_id, "report", "ready1", 2.3, 6700, collected=True),
        Spec(alice_id, "scrape", "exhausted", 1.8, 4900, reason=next(r)),
        Spec(alice_id, "report", "ready1", 1.3, 8100),
        Spec(alice_id, "scrape", "exhausted", 0.95, 5600, collected=True, reason=next(r)),
        Spec(alice_id, "report", "ready1", 0.6, 7600),
        Spec(alice_id, "scrape", "ready1", 0.35, 4400, collected=True),
        Spec(alice_id, "report", "ready1", 0.12, 8800),
        # The ONE seeded queued row, system-wide -- enough to demonstrate
        # `status=queued` filtering without needing more than one inert row
        # to explain (see FAR_FUTURE's comment above).
        Spec(alice_id, "scrape", "queued_future", 0.06, 5900),
        Spec(alice_id, "report", "cancel_running", 0.02, 12000),
    ]

    bob = [
        Spec(bob_id, "scrape", "ready1", 6.5, 5200, collected=True),
        Spec(bob_id, "report", "ready1", 4.2, 7600),
        Spec(bob_id, "scrape", "ready2", 3.0, 6100, collected=True, retry_reason=next(t)),
        Spec(bob_id, "report", "exhausted", 1.8, 4900, collected=True, reason=next(r)),
        Spec(bob_id, "scrape", "permanent", 1.1, 3600, reason=next(p)),
        Spec(bob_id, "report", "cancel_queued", 0.6, 8200),
        Spec(bob_id, "scrape", "ready1", 0.1, 6600),
    ]

    # Every reason pool must be used exactly once -- if this ever fires, the
    # spec table above and the pool sizes have drifted out of sync.
    assert next(r, "exhausted") == "exhausted", "RETRYABLE_REASONS not fully consumed"
    assert next(p, "exhausted") == "exhausted", "PERMANENT_REASONS not fully consumed"

    return alice + bob


def plausible_result(lane: str, duration_ms: int) -> dict[str, Any]:
    """Same shape mock_worker actually returns (see engine/workers/mock.py)
    so a seeded 'ready' row looks like a real one, not an obviously
    different fixture shape."""
    if lane == "scrape":
        bytes_processed = random.randint(20_000, 480_000)
        records = random.randint(5, 400)
    else:
        bytes_processed = random.randint(5_000, 150_000)
        records = random.randint(1, 60)
    return {
        "lane": lane,
        "duration_ms": duration_ms,
        "bytes_processed": bytes_processed,
        "records": records,
    }


def take_number(active: dict[tuple[uuid.UUID, str], set[int]], user_id: uuid.UUID, lane: str) -> int:
    """Lowest free positive integer for (user_id, lane), mirroring
    store.lowest_free_number so seeded handles follow the exact same rule
    the live system uses. Every spec takes a number here as if it were
    momentarily active (every real task starts 'queued'), and kinds that
    end up terminal-and-inactive (cancelled, or collected ready/failed)
    free it again right after -- see materialize(). Processing specs in
    chronological order (oldest created_at first) is what makes this
    faithfully replay a believable allocation history instead of just
    happening to avoid collisions.
    """
    used = active.setdefault((user_id, lane), set())
    n = 1
    while n in used:
        n += 1
    used.add(n)
    return n


def free_number(active: dict[tuple[uuid.UUID, str], set[int]], user_id: uuid.UUID, lane: str, number: int) -> None:
    active[(user_id, lane)].discard(number)


def materialize(now: datetime, number: int, spec: Spec) -> tuple[dict[str, Any], list[tuple]]:
    """Turn one Spec into a tasks row (dict of column -> value) and its
    task_events history (list of (from_status, to_status, reason, attempt,
    created_at) tuples), with timestamps consistent with status and
    attempts per the brief.
    """
    created_at = now - timedelta(days=spec.days_ago)
    events: list[tuple] = [(None, "queued", None, 0, created_at)]
    row: dict[str, Any] = {
        "lane": spec.lane,
        "number": number,
        "params": {"duration_ms": spec.duration_ms},
        "collected": False,
        "max_attempts": 3,
        "not_before": None,
        "worker_id": None,
        "lease_expires_at": None,
        "result": None,
        "error": None,
        "created_at": created_at,
    }

    if spec.kind == "ready1":
        t_run = created_at + timedelta(seconds=2)
        t_ready = t_run + timedelta(milliseconds=spec.duration_ms)
        events += [("queued", "running", None, 1, t_run), ("running", "ready", None, 1, t_ready)]
        row.update(
            status="ready",
            attempts=1,
            result=plausible_result(spec.lane, spec.duration_ms),
            updated_at=t_ready,
        )

    elif spec.kind == "ready2":
        t_run1 = created_at + timedelta(seconds=2)
        t_fail1 = t_run1 + timedelta(milliseconds=spec.duration_ms)
        not_before1 = t_fail1 + timedelta(seconds=1)  # backoff: 2**(1-1)
        t_run2 = not_before1 + timedelta(seconds=2)
        t_ready = t_run2 + timedelta(milliseconds=spec.duration_ms)
        events += [
            ("queued", "running", None, 1, t_run1),
            ("running", "queued", spec.retry_reason, 1, t_fail1),
            ("queued", "running", None, 2, t_run2),
            ("running", "ready", None, 2, t_ready),
        ]
        row.update(
            status="ready",
            attempts=2,
            result=plausible_result(spec.lane, spec.duration_ms),
            updated_at=t_ready,
        )

    elif spec.kind == "exhausted":
        t_run1 = created_at + timedelta(seconds=2)
        t_fail1 = t_run1 + timedelta(milliseconds=spec.duration_ms)
        nb1 = t_fail1 + timedelta(seconds=1)  # 2**(1-1)
        t_run2 = nb1 + timedelta(seconds=2)
        t_fail2 = t_run2 + timedelta(milliseconds=spec.duration_ms)
        nb2 = t_fail2 + timedelta(seconds=2)  # 2**(2-1)
        t_run3 = nb2 + timedelta(seconds=2)
        t_fail3 = t_run3 + timedelta(milliseconds=spec.duration_ms)
        # Matches runner.py's _record_outcome exhaustion wording exactly:
        # f"{error.reason} (attempt {attempts} of {max_attempts}, retries exhausted)"
        final_reason = f"{spec.reason} (attempt 3 of 3, retries exhausted)"
        events += [
            ("queued", "running", None, 1, t_run1),
            ("running", "queued", spec.reason, 1, t_fail1),
            ("queued", "running", None, 2, t_run2),
            ("running", "queued", spec.reason, 2, t_fail2),
            ("queued", "running", None, 3, t_run3),
            ("running", "failed", final_reason, 3, t_fail3),
        ]
        row["params"]["fail"] = True
        row.update(
            status="failed",
            attempts=3,
            error={"reason": final_reason, "retryable": False},
            updated_at=t_fail3,
        )

    elif spec.kind == "permanent":
        t_run1 = created_at + timedelta(seconds=2)
        t_fail1 = t_run1 + timedelta(milliseconds=spec.duration_ms)
        events += [("queued", "running", None, 1, t_run1), ("running", "failed", spec.reason, 1, t_fail1)]
        row["params"]["fail_permanent"] = True
        row.update(
            status="failed",
            attempts=1,
            error={"reason": spec.reason, "retryable": False},
            updated_at=t_fail1,
        )

    elif spec.kind == "cancel_queued":
        t_cancel = created_at + timedelta(seconds=45)
        events += [("queued", "cancelled", None, 0, t_cancel)]
        row.update(status="cancelled", attempts=0, updated_at=t_cancel)

    elif spec.kind == "cancel_running":
        t_run1 = created_at + timedelta(seconds=2)
        t_cancel = t_run1 + timedelta(milliseconds=int(spec.duration_ms * 0.4))
        events += [("queued", "running", None, 1, t_run1), ("running", "cancelled", None, 1, t_cancel)]
        row.update(status="cancelled", attempts=1, updated_at=t_cancel)

    elif spec.kind == "queued_future":
        # Deliberately unclaimable: not_before is far beyond "now", so the
        # dispatch loop's `not_before IS NULL OR not_before <= now()` never
        # matches this row. Without this, the pool would claim and actually
        # run it within a second of the app starting.
        row.update(status="queued", attempts=0, not_before=FAR_FUTURE, updated_at=created_at)

    else:
        raise ValueError(f"unknown spec kind: {spec.kind!r}")

    if spec.collected and spec.kind in ("ready1", "ready2", "exhausted", "permanent"):
        row["collected"] = True
        # A real collect happens some time after the row went terminal, not
        # in the same instant -- bump updated_at like collect_result() does.
        row["updated_at"] = row["updated_at"] + timedelta(minutes=45)

    return row, events


def to_jsonb(value: Optional[dict]) -> Optional[str]:
    return None if value is None else json.dumps(value)


async def find_debug_users(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """Users that aren't Alice/Bob or a known pytest fixture user --
    candidates for purge_debug_users, along with how many task rows
    deleting each one would take with it. Read-only: never gated, since
    reporting what a purge WOULD touch is always safe to compute."""
    rows = await conn.fetch(
        """
        SELECT u.id, u.name, u.api_key, count(t.id) AS task_count
        FROM users u LEFT JOIN tasks t ON t.user_id = u.id
        GROUP BY u.id, u.name, u.api_key
        """
    )
    return [dict(row) for row in rows if row["api_key"] not in PROTECTED_KEYS]


async def purge_debug_users(conn: asyncpg.Connection, candidates: list[dict[str, Any]]) -> None:
    """Delete exactly the given users and their tasks. Tasks are deleted
    first: tasks.user_id has no ON DELETE CASCADE (unlike task_events ->
    tasks), so the user row can't go first.

    Deliberately takes an explicit candidate list rather than re-querying
    PROTECTED_KEYS itself -- this is the only place in the script that
    deletes another user's real data, not just seed rows, so the caller is
    required to have already shown the operator exactly what this will
    touch (see main()) before this runs.
    """
    for row in candidates:
        await conn.execute("DELETE FROM tasks WHERE user_id = $1", row["id"])
        await conn.execute("DELETE FROM users WHERE id = $1", row["id"])


async def insert_tasks(conn: asyncpg.Connection, alice_id: uuid.UUID, bob_id: uuid.UUID) -> None:
    now = datetime.now(timezone.utc)
    specs = build_specs(alice_id, bob_id)
    # Oldest first: take_number's "lowest free number" allocation only
    # produces a believable history (and, for the active rows left at the
    # end, numbers matching uniq_active_handle's at-most-one-active-row
    # guarantee) if specs are processed in the order they'd actually have
    # been submitted.
    specs.sort(key=lambda s: s.days_ago, reverse=True)

    active: dict[tuple[uuid.UUID, str], set[int]] = {}
    for spec in specs:
        number = take_number(active, spec.user_id, spec.lane)
        row, events = materialize(now, number, spec)
        if row["status"] in ("cancelled",) or (row["status"] in ("ready", "failed") and row["collected"]):
            free_number(active, spec.user_id, spec.lane, number)

        task_id = await conn.fetchval(
            """
            INSERT INTO tasks (
                user_id, lane, number, params, status, result, error, collected,
                attempts, max_attempts, not_before, worker_id, lease_expires_at,
                seeded, created_at, updated_at
            ) VALUES (
                $1, $2, $3, $4::jsonb, $5::task_status, $6::jsonb, $7::jsonb, $8,
                $9, $10, $11, $12, $13,
                true, $14, $15
            )
            RETURNING id
            """,
            spec.user_id,
            row["lane"],
            row["number"],
            to_jsonb(row["params"]),
            row["status"],
            to_jsonb(row["result"]),
            to_jsonb(row["error"]),
            row["collected"],
            row["attempts"],
            row["max_attempts"],
            row["not_before"],
            row["worker_id"],
            row["lease_expires_at"],
            row["created_at"],
            row["updated_at"],
        )

        for from_status, to_status, reason, attempt, created_at in events:
            await conn.execute(
                """
                INSERT INTO task_events (task_id, from_status, to_status, reason, attempt, created_at)
                VALUES ($1, $2::task_status, $3::task_status, $4, $5, $6)
                """,
                task_id,
                from_status,
                to_status,
                reason,
                attempt,
                created_at,
            )


async def print_summary(conn: asyncpg.Connection) -> None:
    rows = await conn.fetch(
        """
        SELECT u.name, t.status, count(*) AS n
        FROM tasks t JOIN users u ON u.id = t.user_id
        WHERE t.seeded = true
        GROUP BY u.name, t.status
        ORDER BY u.name, t.status
        """
    )
    print("\nSeeded task counts:")
    by_user: dict[str, list[str]] = {}
    for row in rows:
        by_user.setdefault(row["name"], []).append(f"{row['status']}={row['n']}")
    for name, parts in by_user.items():
        print(f"  {name}: {', '.join(parts)}")

    print("\nDemo API keys:")
    print(f"  {ALICE_NAME}: {ALICE_KEY}")
    print(f"  {BOB_NAME}:   {BOB_KEY}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    # Mutually exclusive: each leaves seeded data in a different end state,
    # so combining them would just mean one silently winning over another
    # (an argparse error here is better than that silent ambiguity).
    end_state = parser.add_mutually_exclusive_group()
    end_state.add_argument(
        "--reset", action="store_true", help="delete all seeded=true rows, then reseed fresh ones"
    )
    end_state.add_argument(
        "--clear",
        action="store_true",
        help="delete all seeded=true rows and stop -- does NOT reseed afterward",
    )
    end_state.add_argument(
        "--wipe",
        action="store_true",
        help="delete ALL rows (tasks and users), not just seed data, then reseed Alice/Bob",
    )
    parser.add_argument(
        "--purge-debug-users",
        action="store_true",
        help=(
            "delete users that aren't Alice/Bob or a pytest fixture user, along with their "
            "tasks. Never runs implicitly -- shows exactly who would be deleted and asks "
            "for confirmation first."
        ),
    )
    args = parser.parse_args()

    if args.wipe:
        confirm = input(
            "--wipe deletes EVERY row in tasks and users, seeded or not. "
            "Type 'yes' to continue: "
        )
        if confirm.strip().lower() != "yes":
            print("Aborted.")
            return
        async with connection(DSN) as conn:
            await conn.execute("DELETE FROM tasks")
            await conn.execute("DELETE FROM users")
        print("Wiped all tasks and users.")

    elif args.reset:
        async with connection(DSN) as conn:
            result = await conn.execute("DELETE FROM tasks WHERE seeded = true")
        print(f"Reset: {result}")

    elif args.clear:
        async with connection(DSN) as conn:
            result = await conn.execute("DELETE FROM tasks WHERE seeded = true")
        print(f"Cleared: {result}")

    # Opt-in only, and only when --wipe hasn't already deleted every user
    # (asking to confirm a purge of zero remaining users would be noise).
    # This deletes OTHER users' real, non-seed task data via their user_id
    # -- unlike --reset/--wipe above, there is no "seeded = true" or
    # blanket-delete framing to fall back on, so a bare `python -m
    # scripts.seed` must never reach this path, and even an explicit
    # --purge-debug-users run confirms against a concrete, named preview
    # rather than deleting on a promise of what it will find.
    if args.purge_debug_users and not args.wipe:
        async with connection(DSN) as conn:
            candidates = await find_debug_users(conn)
        if not candidates:
            print("No ad-hoc debug users found.")
        else:
            print("The following users are not Alice/Bob or a pytest fixture user:")
            for row in candidates:
                print(f"  {row['name']!r} ({row['api_key']}) -- {row['task_count']} task(s)")
            confirm = input("Type 'yes' to delete them and their tasks: ")
            if confirm.strip().lower() != "yes":
                print("Skipped debug-user purge.")
            else:
                async with connection(DSN) as conn:
                    await purge_debug_users(conn, candidates)
                for row in candidates:
                    print(f"Removed {row['name']!r} ({row['api_key']})")

    # --clear's whole point is to leave seed data absent -- unlike --reset
    # and --wipe, which both fall through to the reseed below, --clear
    # stops here. Placed after the --purge-debug-users block (not before
    # it) so `--clear --purge-debug-users` together still runs both
    # cleanup steps rather than the second one being silently skipped.
    if args.clear:
        print("Seeded rows removed. Run again without --clear to reseed.")
        return

    alice_id = await ensure_user(DSN, ALICE_NAME, ALICE_KEY)
    bob_id = await ensure_user(DSN, BOB_NAME, BOB_KEY)

    async with connection(DSN) as conn:
        await insert_tasks(conn, alice_id, bob_id)
        await print_summary(conn)


if __name__ == "__main__":
    asyncio.run(main())
