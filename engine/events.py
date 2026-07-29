"""In-process pub/sub for lifecycle events.

This module publishes to subscribers and knows nothing beyond that — no SQL,
no framework, no HTTP, and it must not know SSE exists. Turning a subscriber's
queue into an SSE stream is api/events.py's job, not this module's.

Naming note: LifecycleEvent (this module) is unrelated to models.py's
TaskEvent. TaskEvent is a persisted state-history row (from_status,
to_status, reason, attempt) written to task_events. LifecycleEvent is a live,
ephemeral notification pushed to SSE clients and never stored. Two similar
names for genuinely different concepts would be a standing source of
confusion, so they are named to look nothing alike.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Literal, Optional

logger = logging.getLogger(__name__)

EventType = Literal["accepted", "ready", "failed", "cancelled"]


@dataclass
class LifecycleEvent:
    """Exactly the four types fixed by the API contract — no "running", no
    "retrying". user_id is present for EventBus routing only; it must never
    appear in to_dict() output.

    Prefer the accepted()/ready()/failed()/cancelled() classmethods over
    direct construction. __post_init__ validates the type/field combination
    either way, so a malformed event (e.g. a summary on a cancelled event)
    raises immediately instead of silently reaching a subscriber.
    """

    type: EventType
    handle: str
    lane: str
    user_id: uuid.UUID
    summary: Optional[str] = None
    reason: Optional[str] = None
    retryable: Optional[bool] = None

    def __post_init__(self) -> None:
        if self.type in ("accepted", "ready"):
            if self.summary is None:
                raise ValueError(f"{self.type} event requires summary")
            if self.reason is not None or self.retryable is not None:
                raise ValueError(f"{self.type} event must not set reason/retryable")
        elif self.type == "failed":
            if self.reason is None or self.retryable is None:
                raise ValueError("failed event requires reason and retryable")
            if self.summary is not None:
                raise ValueError("failed event must not set summary")
        elif self.type == "cancelled":
            if self.summary is not None or self.reason is not None or self.retryable is not None:
                raise ValueError("cancelled event must not set summary/reason/retryable")
        else:
            raise ValueError(f"unknown lifecycle event type: {self.type!r}")

    @classmethod
    def accepted(
        cls, user_id: uuid.UUID, handle: str, lane: str, summary: str
    ) -> "LifecycleEvent":
        return cls(type="accepted", handle=handle, lane=lane, user_id=user_id, summary=summary)

    @classmethod
    def ready(
        cls, user_id: uuid.UUID, handle: str, lane: str, summary: str
    ) -> "LifecycleEvent":
        return cls(type="ready", handle=handle, lane=lane, user_id=user_id, summary=summary)

    @classmethod
    def failed(
        cls, user_id: uuid.UUID, handle: str, lane: str, reason: str, retryable: bool
    ) -> "LifecycleEvent":
        return cls(
            type="failed",
            handle=handle,
            lane=lane,
            user_id=user_id,
            reason=reason,
            retryable=retryable,
        )

    @classmethod
    def cancelled(cls, user_id: uuid.UUID, handle: str, lane: str) -> "LifecycleEvent":
        return cls(type="cancelled", handle=handle, lane=lane, user_id=user_id)

    def to_dict(self) -> dict[str, Any]:
        """Emits only the contract keys for this event's type. user_id is
        deliberately excluded — it exists on this object for EventBus
        routing only, never for the wire."""
        data: dict[str, Any] = {"type": self.type, "handle": self.handle, "lane": self.lane}
        if self.type in ("accepted", "ready"):
            data["summary"] = self.summary
        elif self.type == "failed":
            data["reason"] = self.reason
            data["retryable"] = self.retryable
        return data


class EventBus:
    """In-process fan-out, scoped per user. A user must never see another
    user's events — routing is keyed on LifecycleEvent.user_id, which never
    reaches the wire (see to_dict())."""

    def __init__(self, maxsize: int = 100) -> None:
        self._maxsize = maxsize
        self._subscribers: dict[uuid.UUID, list["asyncio.Queue[LifecycleEvent]"]] = {}

    def subscribe(self, user_id: uuid.UUID) -> "asyncio.Queue[LifecycleEvent]":
        """Returns the bounded queue directly (not a wrapped async
        generator) so api/events.py retains control over how it waits on
        it — specifically, asyncio.wait_for with a timeout, needed to send
        `: ping` comment lines on a schedule independent of event arrival.
        This module must not know SSE exists, so that waiting strategy
        lives entirely in api/events.py, not here.
        """
        queue: "asyncio.Queue[LifecycleEvent]" = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.setdefault(user_id, []).append(queue)
        return queue

    def unsubscribe(self, user_id: uuid.UUID, queue: "asyncio.Queue[LifecycleEvent]") -> None:
        """Must be called on client disconnect. Leaking subscriber queues
        on every dropped SSE connection is a slow memory leak."""
        queues = self._subscribers.get(user_id)
        if queues is None:
            return
        try:
            queues.remove(queue)
        except ValueError:
            return
        if not queues:
            del self._subscribers[user_id]

    def publish(self, event: LifecycleEvent) -> None:
        """Fan out to event.user_id's subscribers only. Synchronous, not
        async def: put_nowait never awaits, so this genuinely cannot block
        — the signature itself is the guarantee, not just a docstring
        promise.

        Never raises into its caller. A failure to notify must not fail a
        job that actually completed, so both the expected case (a full
        queue) and anything unexpected are logged and swallowed here.

        Dropping on a full queue is safe because the event stream is a
        liveness hint, not the source of truth (see DECISIONS.md, "Event
        stream is a liveness hint, not a data channel"): clients refetch
        GET /tasks on reconnect, so a dropped event is recoverable state,
        not lost state. A slow or dead client must not be able to stall
        the worker pool that's calling publish().
        """
        try:
            for queue in self._subscribers.get(event.user_id, ()):
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    logger.warning(
                        "dropping %s event for handle=%s: subscriber queue full "
                        "(user_id=%s) — client will self-heal via GET /tasks on reconnect",
                        event.type,
                        event.handle,
                        event.user_id,
                    )
        except Exception:
            logger.exception(
                "unexpected error publishing %s event for handle=%s",
                event.type,
                event.handle,
            )
