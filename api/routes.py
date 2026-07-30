"""The six task endpoints, plus GET /lanes for the frontend's dropdown.

Route handlers only call engine.py's facade: parse, delegate, serialise.
No SQL, no queue manipulation, no handle logic here. Failure-case HTTP
codes (404/400/409) come from exceptions the facade raises (see
api/main.py's exception handlers) — never from a handler re-deriving
*why* a call failed.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from api.auth import get_current_user_id
from api.schemas import SubmitRequest
from engine.engine import Engine
from engine.models import Status

router = APIRouter()


def get_engine(request: Request) -> Engine:
    """One Engine instance for the process, constructed in main.py's
    lifespan and stashed on app.state."""
    return request.app.state.engine


def _parse_status(value: str) -> Status:
    """Query param -> Status, or 400 naming the bad value — CLAUDE.md
    requires this be an honest 400, not a silent empty list from a status
    filter that quietly matched nothing."""
    try:
        return Status(value)
    except ValueError:
        valid = ", ".join(s.value for s in Status)
        raise HTTPException(
            status_code=400, detail=f"invalid status {value!r}; must be one of: {valid}"
        )


@router.post("/tasks", status_code=202)
async def submit_task(
    body: SubmitRequest,
    user_id: uuid.UUID = Depends(get_current_user_id),
    engine: Engine = Depends(get_engine),
):
    """202: accepted for async processing, MUST return before work starts
    (criterion 1) — engine.submit() only inserts a row, it never awaits the
    job itself. Unknown lane -> UnknownLaneError -> 400 (main.py)."""
    task = await engine.submit(user_id, body.lane, body.params)
    return task.to_api_dict()


_SORT_COLUMNS = {"created_at", "updated_at"}
_SORT_ORDERS = {"asc", "desc"}


@router.get("/tasks")
async def list_tasks(
    status: list[str] | None = Query(None),
    lane: str | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    sort_by: str = "created_at",
    sort_order: str = "desc",
    limit: int = 50,
    offset: int = 0,
    user_id: uuid.UUID = Depends(get_current_user_id),
    engine: Engine = Depends(get_engine),
):
    # Repeated ?status= params (e.g. ?status=queued&status=running), not a
    # new param name -- the dashboard's "Active"/"Completed" quick filters
    # are each an OR of statuses, and this is the standard REST convention
    # for a multi-value query filter. A single ?status=ready still works
    # unchanged (Query(None) collects it into a one-item list).
    status_list = [_parse_status(s) for s in status] if status else None
    if lane is not None and lane not in engine.lanes():
        raise HTTPException(status_code=400, detail=f"unknown lane {lane!r}")
    if sort_by not in _SORT_COLUMNS:
        raise HTTPException(
            status_code=400,
            detail=f"invalid sort_by {sort_by!r}; must be one of: {', '.join(_SORT_COLUMNS)}",
        )
    if sort_order not in _SORT_ORDERS:
        raise HTTPException(
            status_code=400,
            detail=f"invalid sort_order {sort_order!r}; must be one of: {', '.join(_SORT_ORDERS)}",
        )

    tasks = await engine.list_tasks(
        user_id,
        status=status_list,
        lane=lane,
        created_after=created_after,
        created_before=created_before,
        sort_by=sort_by,
        sort_order=sort_order,
        limit=limit,
        offset=offset,
    )
    # attempts/max_attempts/seeded are additive extras on top of the 9
    # fixed contract fields (the same extras the detail endpoint exposes)
    # — the dashboard list needs them for the attempt counter ("2/3") and
    # SEEDED badge. Nothing here renames or removes a contract field.
    return [
        {**t.to_api_dict(), "attempts": t.attempts, "max_attempts": t.max_attempts, "seeded": t.seeded}
        for t in tasks
    ]


@router.get("/tasks/{handle}")
async def get_task(
    handle: str,
    user_id: uuid.UUID = Depends(get_current_user_id),
    engine: Engine = Depends(get_engine),
):
    """The nine contract fields plus attempts/seeded/state_history —
    CLAUDE.md permits extra fields on this single-task fetch for the
    dashboard's detail view, unlike the list endpoint."""
    detail = await engine.get_detail(user_id, handle)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"no task {handle!r}")
    task, events = detail
    return task.to_detail_dict(events)


@router.get("/tasks/{handle}/result")
async def get_result(
    handle: str,
    user_id: uuid.UUID = Depends(get_current_user_id),
    engine: Engine = Depends(get_engine),
):
    """Works for 'ready' AND 'failed' — collection is the ACK that frees
    the handle either way. engine.collect() raises TaskNotFoundError (404)
    or TaskConflictError (409, still queued/running or already collected);
    see main.py's exception handlers."""
    task = await engine.collect(user_id, handle)
    return task.to_api_dict()


@router.post("/tasks/{handle}/cancel")
async def cancel_task(
    handle: str,
    user_id: uuid.UUID = Depends(get_current_user_id),
    engine: Engine = Depends(get_engine),
):
    """engine.cancel() raises TaskNotFoundError (404) or TaskConflictError
    (409, already terminal); see main.py's exception handlers."""
    task = await engine.cancel(user_id, handle)
    return task.to_api_dict()


@router.post("/tasks/{handle}/retry")
async def retry_task(
    handle: str,
    user_id: uuid.UUID = Depends(get_current_user_id),
    engine: Engine = Depends(get_engine),
):
    """Not in the brief's minimum list, but the dashboard needs operators to
    be able to retry a failed job. engine.retry() raises TaskNotFoundError
    (404) or TaskConflictError (409, not failed or already collected); see
    main.py's exception handlers."""
    task = await engine.retry(user_id, handle)
    return task.to_api_dict()


@router.get("/lanes")
async def list_lanes(
    _user_id: uuid.UUID = Depends(get_current_user_id),
    engine: Engine = Depends(get_engine),
):
    """So the frontend populates its lane dropdown from the engine's
    registered workers instead of hardcoding lane names."""
    return engine.lanes()
