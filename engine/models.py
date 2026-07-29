"""Plain data structures for the job runner engine.

Pure data: dataclasses and enums only. No dependencies on storage,
transport, or any specific lane. This module must be importable with
nothing else in the project working — standard library only.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional


def _iso8601(dt: datetime) -> str:
    """Format a UTC datetime as ISO 8601 ending in "Z" instead of "+00:00"."""
    return dt.isoformat().replace("+00:00", "Z")


class Status(str, Enum):
    """Task lifecycle state. Exactly these five — no sixth status, ever."""

    QUEUED = "queued"
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TaskError:
    """Error shape fixed by the API contract: { "reason": ..., "retryable": ... }."""

    reason: str
    retryable: bool


@dataclass
class Job:
    """What gets handed to a worker. Nothing else — a worker must not be
    able to see or touch engine state."""

    handle: str
    lane: str
    params: dict[str, Any]


@dataclass
class WorkerResult:
    """What a worker returns. Workers only ever return ready or failed —
    they know nothing about queued/running/cancelled; the engine owns
    the lifecycle."""

    status: Literal["ready", "failed"]
    result: Optional[Any] = None
    error: Optional[TaskError] = None


@dataclass
class Task:
    """The full internal record, mirroring the tasks table."""

    id: uuid.UUID
    user_id: uuid.UUID
    lane: str
    number: int
    handle: str
    params: dict[str, Any]
    status: Status
    result: Optional[Any]
    error: Optional[TaskError]
    collected: bool
    attempts: int
    max_attempts: int
    not_before: Optional[datetime]
    worker_id: Optional[str]
    lease_expires_at: Optional[datetime]
    seeded: bool
    created_at: datetime
    updated_at: datetime

    def to_api_dict(self) -> dict[str, Any]:
        """Serialise to exactly the nine fields fixed by the API contract.

        Internal fields (id, user_id, number, attempts, lease, worker_id,
        seeded) never appear here — that's the point of this method.
        """
        return {
            "handle": self.handle,
            "lane": self.lane,
            "params": self.params,
            "status": self.status.value,
            "result": self.result if self.status == Status.READY else None,
            "error": (
                {"reason": self.error.reason, "retryable": self.error.retryable}
                if self.status == Status.FAILED and self.error is not None
                else None
            ),
            "created_at": _iso8601(self.created_at),
            "updated_at": _iso8601(self.updated_at),
            "collected": self.collected,
        }


@dataclass
class TaskEvent:
    """A single state-history transition. Backs the detail view's
    state history."""

    from_status: Optional[Status]
    to_status: Status
    reason: Optional[str]
    attempt: Optional[int]
    created_at: datetime
