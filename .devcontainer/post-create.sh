#!/usr/bin/env bash
# Runs once, after the devcontainer is created. Idempotent: safe to re-run
# on a rebuild that reuses the existing `pgdata` volume (schema/seed are
# skipped if the `tasks` table already exists -- scripts/seed.py's bare
# run is NOT itself idempotent, it inserts a fresh batch of rows every
# call, so this script must not call it twice against the same data).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

pip install -r requirements.txt

if [ ! -f .env ]; then
  # .env.example's DATABASE_URL points at localhost:5432, correct for a
  # host-machine docker-compose setup where postgres's port is published
  # to the host. Inside the devcontainer network there is no host loopback
  # to share -- the postgres container is reachable by its compose service
  # name instead.
  sed 's#localhost:5432#postgres:5432#' .env.example > .env
  echo "Wrote .env (DATABASE_URL pointed at the 'postgres' compose service)."
fi

# docker-compose.yml's app service has `depends_on: postgres: condition:
# service_healthy`, which gates this container's own start on postgres's
# pg_isready healthcheck -- so postgres is already accepting connections
# by the time this line runs, no extra wait needed here.

TASKS_TABLE_EXISTS=$(python3 - <<'PYEOF'
import asyncio, os
import asyncpg
from dotenv import load_dotenv

load_dotenv()

async def main():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        exists = await conn.fetchval("SELECT to_regclass('public.tasks') IS NOT NULL")
        print("yes" if exists else "no")
    finally:
        await conn.close()

asyncio.run(main())
PYEOF
)

if [ "$TASKS_TABLE_EXISTS" = "no" ]; then
  echo "Fresh database -- applying sql/schema.sql and seeding demo data..."
  python3 - <<'PYEOF'
import asyncio, os
import asyncpg
from dotenv import load_dotenv

load_dotenv()

async def main():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        await conn.execute(open("sql/schema.sql").read())
    finally:
        await conn.close()

asyncio.run(main())
PYEOF
  python3 -m scripts.seed
else
  echo "tasks table already exists -- schema apply and seed skipped (rebuild against existing data, not a fresh volume)."
fi

if [ -d frontend ]; then
  echo "Installing frontend dependencies..."
  (cd frontend && npm install)
fi

echo "postCreateCommand done."
