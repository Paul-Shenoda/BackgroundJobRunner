"""X-API-Key header -> user_id.

Keys are seeded at startup and never rotate (CLAUDE.md), so the whole
api_key -> user_id mapping is loaded into memory once via load_api_keys
(called from api/main.py's lifespan) rather than round-tripping to Postgres
on every request for a mapping that cannot change without a restart.

Deliberately does not touch engine/store.py: the engine orchestrates tasks,
it doesn't manage identity (DECISIONS.md, "Engine as a standalone module").
User lookups go through scripts/bootstrap.py's connection() helper, the same
one-shot asyncpg helper scripts/bootstrap.py itself uses to provision users.
"""

import uuid

from fastapi import Header, HTTPException

from scripts.bootstrap import connection

_api_keys: dict[str, uuid.UUID] = {}


async def load_api_keys(dsn: str) -> None:
    """Populate the in-memory api_key -> user_id cache. Call once at
    process startup, before the app starts serving requests."""
    async with connection(dsn) as conn:
        rows = await conn.fetch("SELECT api_key, id FROM users")
    _api_keys.clear()
    _api_keys.update({row["api_key"]: row["id"] for row in rows})


async def get_current_user_id(x_api_key: str | None = Header(default=None)) -> uuid.UUID:
    """Every route depends on this for its user_id. Missing header and
    unknown key get the IDENTICAL 401 — distinguishing them would let a
    caller learn which keys exist by probing."""
    if x_api_key is None or x_api_key not in _api_keys:
        raise HTTPException(status_code=401, detail="missing or invalid API key")
    return _api_keys[x_api_key]
