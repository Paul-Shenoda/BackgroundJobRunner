"""App wiring and lifespan (starts/stops the engine).

Does NOT import from engine/ except engine.engine (the facade) and
engine.engine's re-exported domain exceptions — see the grep check in the
Phase 3 brief. api/auth.py's users lookup goes through
scripts/bootstrap.py's connection() helper instead, which is deliberately
outside engine/ (DECISIONS.md, "Engine as a standalone module").
"""

import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.auth import load_api_keys
from api.events import router as events_router
from api.routes import router
from engine.engine import (
    Engine,
    HandleAllocationError,
    TaskConflictError,
    TaskNotFoundError,
    UnknownLaneError,
)
from engine.workers.mock import mock_worker

load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    dsn = os.environ["DATABASE_URL"]
    engine = Engine(
        dsn,
        concurrency=int(os.environ.get("CONCURRENCY_LIMIT", 2)),
        lease_seconds=int(os.environ.get("LEASE_SECONDS", 30)),
        max_attempts=int(os.environ.get("MAX_ATTEMPTS", 3)),
    )
    engine.register_worker("scrape", mock_worker)
    engine.register_worker("report", mock_worker)

    # Store before pool: the pool can't claim anything against a connection
    # that doesn't exist yet (see engine.py's start()).
    await engine.start()
    # API keys are seeded, never rotated (CLAUDE.md) — load the cache once
    # here rather than round-tripping to Postgres per request.
    await load_api_keys(dsn)

    app.state.engine = engine
    try:
        yield
    finally:
        await engine.stop()


app = FastAPI(title="Background Job Runner", lifespan=lifespan)

_origins = ["http://localhost:5173"]
_deployed_origin = os.environ.get("FRONTEND_ORIGIN")
if _deployed_origin:
    _origins.append(_deployed_origin)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(UnknownLaneError)
async def _unknown_lane(request: Request, exc: UnknownLaneError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(TaskNotFoundError)
async def _task_not_found(request: Request, exc: TaskNotFoundError):
    return JSONResponse(status_code=404, content={"detail": f"no task {exc.args[0]!r}"})


@app.exception_handler(TaskConflictError)
async def _task_conflict(request: Request, exc: TaskConflictError):
    return JSONResponse(status_code=409, content={"detail": exc.reason})


@app.exception_handler(HandleAllocationError)
async def _handle_allocation_failed(request: Request, exc: HandleAllocationError):
    # Only reachable under contention this system isn't expected to see in
    # practice (see HandleAllocationError's docstring) — 503 tells the
    # client this is transient and worth retrying, not a client error.
    return JSONResponse(status_code=503, content={"detail": str(exc)})


app.include_router(router)
app.include_router(events_router)


@app.get("/health")
async def health():
    """Liveness only: is this process working, not how much work it's
    doing. engine.health() owns every decision here; this route is purely
    parse (nothing to parse), delegate, serialise."""
    return await app.state.engine.health()
