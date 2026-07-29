"""User provisioning: create or reuse a user and its API key.

Deliberately NOT part of engine/. The engine is a task orchestrator — it
accepts a user_id and scopes everything to it, but it has no opinion about
where that user_id comes from. Creating users and minting API keys is
authentication setup, the same category of concern as running schema.sql,
not orchestration. Putting this on the Engine facade would widen what the
engine claims to be for one script's convenience; importing store.py
directly from a script would breach the exact "engine.engine is the only
import" boundary scripts/smoke.py exists to demonstrate is real.

Uses asyncpg directly. CLAUDE.md's "asyncpg confined to one file" rule
scopes to engine/ (see hard rule #2) — this module is outside the engine,
so it isn't bound by the engine's internal layering rule any more than a
schema migration would be.

Shared, not a one-off: scripts/smoke.py's demo user (Phase 2), seeded API
keys at API startup (Phase 3), the pytest fixture user (Phase 4), and
sql/seed.sql's demo data (Phase 5) all provision users through this one
function rather than each growing its own copy.
"""

import uuid

import asyncpg


async def ensure_user(dsn: str, name: str, api_key: str) -> uuid.UUID:
    """Idempotent: creates the user if api_key is new, returns its id
    either way. A single INSERT ... ON CONFLICT DO UPDATE ... RETURNING is
    atomic — there's no separate check-then-insert here to lose a race on.

    Opens and closes its own connection rather than a pool: this runs once
    per invocation (script startup, API startup, test fixture setup), not
    in a hot path — a pool would be machinery this doesn't need.
    """
    conn = await asyncpg.connect(dsn)
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO users (name, api_key)
            VALUES ($1, $2)
            ON CONFLICT (api_key) DO UPDATE SET name = EXCLUDED.name
            RETURNING id
            """,
            name,
            api_key,
        )
        return row["id"]
    finally:
        await conn.close()
