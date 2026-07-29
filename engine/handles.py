"""Handle allocation and recycling (lane-N, numbered per lane per user).

Uniqueness is enforced by the database's partial unique index, not by
application-level check-then-insert logic here. This module contains no
SQL — it calls store.py and reacts to what comes back.
"""

import uuid
from typing import Any

from engine.models import Task
from engine.store import HandleConflict, Store


class HandleAllocationError(Exception):
    """Raised when allocate() exhausts max_retries without landing a row.
    Under realistic contention this should never happen — it exists as a
    bound on worst-case retry storms, not a path expected to trigger."""


async def allocate(
    store: Store,
    user_id: uuid.UUID,
    lane: str,
    params: dict[str, Any],
    max_attempts: int = 3,
    seeded: bool = False,
    max_retries: int = 5,
) -> Task:
    """Compute the lowest free number and insert, retrying on conflict.

    max_attempts here is the retry BUDGET written onto the new row (passed
    straight through to store.insert_task) — unrelated to max_retries
    below, which bounds this function's own handle-collision retry loop.
    Two different "how many times" knobs for two different problems.

    This loop does not prevent a race between two concurrent allocators
    reading the same "lowest free number" — it can't, and isn't trying to.
    What makes it correct is uniq_active_handle (sql/schema.sql), which
    makes a duplicate (user_id, lane, number) row structurally impossible
    at the database level. The loser of a race gets HandleConflict instead
    of a duplicate handle, catches it, and recomputes the lowest free
    number from scratch — never number + 1, since recycled numbers mean
    the next-lowest-free isn't necessarily adjacent. Correctness comes from
    the constraint; this loop's only job is to react to losing.
    """
    for _ in range(max_retries):
        number = await store.lowest_free_number(user_id, lane)
        try:
            return await store.insert_task(
                user_id, lane, number, params, max_attempts=max_attempts, seeded=seeded
            )
        except HandleConflict:
            continue
    raise HandleAllocationError(
        f"could not allocate a handle in lane {lane!r} for user {user_id} "
        f"after {max_retries} attempts"
    )


def parse_handle(handle: str) -> tuple[str, int]:
    """"scrape-1" -> ("scrape", 1). Splits on the LAST hyphen, since lanes
    may themselves contain hyphens (e.g. "web-scrape-3" -> ("web-scrape", 3)).
    Raises ValueError on malformed input."""
    lane, sep, number_str = handle.rpartition("-")
    if not sep or not lane or not number_str.isdigit():
        raise ValueError(f"malformed handle: {handle!r}")
    return lane, int(number_str)
