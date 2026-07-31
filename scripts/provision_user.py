"""Provision a real user's API key.

There is deliberately no signup flow -- no registration form, no email
verification, no key rotation (see DECISIONS.md, "Auth beyond API
keys... no signup, login, password reset, or key rotation"). The brief
asks for "a per-user API key scoped to that user's data," not a user
management system, so keys are provisioned by an operator, out of band.
This is the CLI wrapper around the one function every other caller
(scripts/seed.py's demo users, the API's own seeded users at startup, the
pytest fixtures) already goes through: scripts.bootstrap.ensure_user(dsn,
name, api_key).

Run as:
    python -m scripts.provision_user "Carol"
    python -m scripts.provision_user "Carol" --key carol-prod-key

With no --key, a fresh one is generated from the name plus a short random
suffix -- the point for a new tester is a working key handed back
immediately, not having to invent a string of their own. Passing --key
explicitly is idempotent, same as ensure_user's own contract (INSERT ...
ON CONFLICT (api_key) DO UPDATE) -- re-running with the same key is safe
and just confirms the same user. Omitting --key is NOT idempotent by
design: each run mints a genuinely new identity, since "give me a new
key" is the whole point of running this without one.

Reads DATABASE_URL from .env (or the environment), same as every other
script in scripts/.

Boundary note: this script is outside engine/, so CLAUDE.md's "asyncpg
confined to store.py" rule doesn't bind it -- same reasoning as
scripts/bootstrap.py's own docstring, which this script is a thin CLI
wrapper around.
"""

import argparse
import asyncio
import os
import re
import secrets

from dotenv import load_dotenv

from scripts.bootstrap import ensure_user

load_dotenv()

DSN = os.environ["DATABASE_URL"]


def _slug(name: str) -> str:
    """Lowercase, dash-separated, alnum-only -- matches the existing demo
    key style (alice-demo-key, bob-demo-key) rather than embedding
    whatever casing/punctuation the operator typed as the display name."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "user"


def _generate_key(name: str) -> str:
    return f"{_slug(name)}-{secrets.token_hex(4)}"


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("name", help='display name for the new user, e.g. "Carol"')
    parser.add_argument(
        "--key",
        default=None,
        help=(
            "use this exact API key instead of generating one (idempotent: re-running with "
            "the same key just confirms the same user rather than minting a new one)"
        ),
    )
    args = parser.parse_args()

    api_key = args.key or _generate_key(args.name)
    user_id = await ensure_user(DSN, args.name, api_key)

    print(f"user_id={user_id}")
    print(f"name={args.name}")
    print(f"api_key={api_key}")
    print()
    print(f"Hand this key to {args.name}; send it as the X-API-Key header on every request.")
    print(
        "The API's api_key -> user_id cache loads once at process startup (api/auth.py) -- "
        "restart the API (or, in production, the next natural redeploy) before this key is "
        "recognized."
    )


if __name__ == "__main__":
    asyncio.run(main())
