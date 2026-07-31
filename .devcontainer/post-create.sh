#!/usr/bin/env bash
# Runs once, after the devcontainer is created. Idempotent: safe to re-run
# on a rebuild that reuses the existing `pgdata` volume (schema/seed are
# skipped if the `tasks` table already exists -- scripts/seed.py's bare
# run is NOT itself idempotent, it inserts a fresh batch of rows every
# call, so this script must not call it twice against the same data).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# Numbered banners + an ERR trap naming the step: a 20-line Python
# traceback with no other context is exactly the silent-failure problem
# this project is otherwise careful about (CLAUDE.md's honest-failure-
# reasons rule, applied to setup instead of task failures).
TOTAL_STEPS=6
STEP=0
CURRENT_STEP="(not started)"

step() {
  STEP=$((STEP + 1))
  CURRENT_STEP="$1"
  echo ""
  echo "[$STEP/$TOTAL_STEPS] $CURRENT_STEP"
}

on_error() {
  echo "" >&2
  echo "postCreateCommand FAILED at step [$STEP/$TOTAL_STEPS]: $CURRENT_STEP" >&2
}
trap on_error ERR

step "Installing Python dependencies"
pip install -r requirements.txt

step "Writing .env"
if [ ! -f .env ]; then
  # .env.example's DATABASE_URL points at localhost:5432, correct for a
  # host-machine docker-compose setup where postgres's port is published
  # to the host. Inside the devcontainer network there is no host loopback
  # to share -- the postgres container is reachable by its compose service
  # name instead.
  sed 's#localhost:5432#postgres:5432#' .env.example > .env
  echo "Wrote .env (DATABASE_URL pointed at the 'postgres' compose service)."
else
  echo ".env already exists -- leaving it alone."
fi

# The python snippets below run via `python3 - <<EOF` (stdin, no calling
# frame). python-dotenv's find_dotenv() walks the CALLER's stack to locate
# .env and raises `AssertionError: frame.f_back is not None` when there
# isn't one -- nothing to do with whether .env exists (it does, by this
# point). Read DATABASE_URL out of the .env we just confirmed exists
# directly in bash instead of relying on load_dotenv() inside those
# snippets (see DECISIONS.md, "post-create.sh died at step 1: load_dotenv()
# has no stack to walk under a stdin-piped python").
export DATABASE_URL="$(grep -m1 '^DATABASE_URL=' .env | cut -d= -f2-)"

# docker-compose.yml's app service has `depends_on: postgres: condition:
# service_healthy`, which gates this container's own start on postgres's
# pg_isready healthcheck -- so postgres is already accepting connections
# by the time this line runs, no extra wait needed here.

step "Checking whether the schema is already applied"
TASKS_TABLE_EXISTS=$(python3 - <<'PYEOF'
import asyncio, os
import asyncpg

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
  step "Applying sql/schema.sql"
  python3 - <<'PYEOF'
import asyncio, os
import asyncpg

async def main():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        await conn.execute(open("sql/schema.sql").read())
    finally:
        await conn.close()

asyncio.run(main())
PYEOF

  step "Seeding demo data"
  python3 -m scripts.seed
else
  step "Applying sql/schema.sql -- skipped, tasks table already exists"
  step "Seeding demo data -- skipped, rebuild against existing data, not a fresh volume"
fi

step "Installing frontend dependencies"
if [ -d frontend ]; then
  (cd frontend && npm install)
else
  echo "No frontend/ directory found, skipping."
fi

echo ""
echo "postCreateCommand done."
