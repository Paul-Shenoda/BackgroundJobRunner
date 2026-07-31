"""SSE endpoint (GET /events).

This file is the adapter: it turns engine.subscribe(user_id)'s bounded
asyncio.Queue into an HTTP text/event-stream. The engine (engine/events.py)
knows nothing about SSE, HTTP, or framing — that boundary is deliberate and
stays intact here: this module reads from the queue and yields formatted
lines, nothing else.
"""

import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from api.auth import get_current_user_id
from engine.engine import Engine

logger = logging.getLogger(__name__)

router = APIRouter()

_PING_INTERVAL_SECONDS = 15.0


def get_engine(request: Request) -> Engine:
    """One Engine instance for the process, set on app.state by main.py's
    lifespan. Duplicated from api/routes.py rather than shared — this pass
    touches api/events.py only."""
    return request.app.state.engine


async def _event_stream(engine: Engine, user_id: uuid.UUID):
    """One loop, one timeout — asyncio.wait_for(queue.get(), timeout=15)
    races the queue against the clock directly, rather than a separate
    ping task racing the reader. A second task would need its own
    cancellation and coordination with this one for no benefit: a single
    wait_for already blocks for at most 15s either way.

    No history replay on connect: DECISIONS.md ("Event stream is a
    liveness hint, not a data channel") — a client reconnecting refetches
    GET /tasks for ground truth. This stream only ever tells it "something
    changed just now," never "here's what you missed."
    """
    queue = engine.subscribe(user_id)
    logger.info("SSE subscribe user_id=%s", user_id)
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=_PING_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                # Comment line — valid SSE, ignored by clients. Keeps the
                # connection alive through proxies with an idle-request
                # timeout (Azure Container Apps included); without this an
                # idle stream gets silently killed, not just delayed.
                yield ": ping\n\n"
                continue
            yield f"data: {json.dumps(event.to_dict())}\n\n"
    except asyncio.CancelledError:
        # A client disconnect surfaces here, at whichever await this
        # generator was suspended on. Re-raise after cleanup — swallowing
        # it would mean the finally block below never actually reflects a
        # real disconnect to whatever is watching the underlying task.
        raise
    finally:
        # Always runs — normal exit (there isn't one; this loop is
        # infinite), an exception, or cancellation all pass through here.
        # Skipping this on a dropped connection would leak the queue:
        # EventBus.publish() would keep fanning out into it forever, for a
        # client that no longer exists. Browsers drop SSE connections
        # constantly (tab close, sleep, network change), so this leak
        # would be steady, not rare.
        engine.unsubscribe(user_id, queue)
        logger.info("SSE unsubscribe user_id=%s", user_id)


@router.get("/events")
async def stream_events(
    user_id: uuid.UUID = Depends(get_current_user_id),
    engine: Engine = Depends(get_engine),
):
    """Same X-API-Key scoping as every other endpoint — a user's stream
    carries only their own events (EventBus routes by user_id; see
    engine/events.py). Cache-Control and X-Accel-Buffering both exist to
    stop an intermediary proxy from buffering the response, which would
    turn a live stream into one that only flushes once a buffer fills —
    silently breaking liveness without erroring."""
    return StreamingResponse(
        _event_stream(engine, user_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
