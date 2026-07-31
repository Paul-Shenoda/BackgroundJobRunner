"""App wiring and lifespan (starts/stops the engine).

Does NOT import from engine/ except engine.engine (the facade) and
engine.engine's re-exported domain exceptions — see the grep check in the
Phase 3 brief. api/auth.py's users lookup goes through
scripts/bootstrap.py's connection() helper instead, which is deliberately
outside engine/ (DECISIONS.md, "Engine as a standalone module").
"""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

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

# uvicorn configures its own "uvicorn"/"uvicorn.access"/"uvicorn.error"
# loggers (each with propagate=False and its own handler) before this module
# is even imported -- Config.__init__ calls configure_logging() ahead of
# load_app(). Its default LOGGING_CONFIG never touches the root logger or
# sets disable_existing_loggers=True, so configuring the root logger here
# doesn't fight it or get clobbered by it: engine/ and api/'s loggers (all
# plain logging.getLogger(__name__), propagate=True, no level of their own)
# inherit whatever level the root logger has. Without this call the root
# logger sits at the logging-module default of WARNING with no handler, so
# every logger.info/debug in engine/runner.py and api/events.py -- the
# reaper's reclaim line, dispatch claims, SSE subscribe/unsubscribe -- is
# computed and then silently discarded.
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    dsn = os.environ["DATABASE_URL"]
    engine = Engine(
        dsn,
        concurrency=int(os.environ.get("CONCURRENCY_LIMIT", 2)),
        lease_seconds=int(os.environ.get("LEASE_SECONDS", 30)),
        heartbeat_interval=int(os.environ.get("HEARTBEAT_INTERVAL", 10)),
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


# Production: one container, one process. `npm run build` emits
# frontend/dist; FastAPI serves it directly instead of needing a second
# static-file server or a reverse proxy in front. Registered last so
# every API route above (matched by exact/parameterised path) always
# wins before this catch-all — Starlette tries routes in registration
# order, not by specificity.
#
# In dev, frontend/dist doesn't exist (Vite's own dev server serves the
# frontend directly, proxying API calls back to this process — see
# frontend/vite.config.js) so this block is skipped entirely.
_FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"

if _FRONTEND_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=_FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        """Any path that isn't a real built asset resolves to index.html —
        the SPA has no server-rendered routes of its own to 404 on."""
        candidate = _FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_FRONTEND_DIST / "index.html")
