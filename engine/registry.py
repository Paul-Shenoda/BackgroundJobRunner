"""Lane -> worker mapping.

This is the entire "pluggable workers" story, and it is deliberately small.
The engine treats workers as opaque callables: it hands a Job to whatever
is registered for that lane and gets back a WorkerResult. It never inspects
job params and never knows what a lane means — all lane-specific knowledge
lives inside the worker function itself, not here and not in the engine.
"""

from typing import Awaitable, Callable

from engine.models import Job, WorkerResult

Worker = Callable[[Job], Awaitable[WorkerResult]]


class UnknownLaneError(Exception):
    """Raised by WorkerRegistry.get() when no worker is registered for the
    requested lane."""


class WorkerRegistry:
    """Lane name -> Worker callable. No I/O, no SQL, no asyncio here —
    just a mapping. Registration happens once at startup; do not register
    any workers in this module."""

    def __init__(self) -> None:
        self._workers: dict[str, Worker] = {}

    def register(self, lane: str, worker: Worker) -> None:
        """Raises ValueError on duplicate registration for the same lane —
        silently overwriting a worker is the kind of bug that costs an
        hour to find."""
        if lane in self._workers:
            raise ValueError(f"a worker is already registered for lane {lane!r}")
        self._workers[lane] = worker

    def get(self, lane: str) -> Worker:
        if lane not in self._workers:
            raise UnknownLaneError(f"no worker registered for lane {lane!r}")
        return self._workers[lane]

    def has(self, lane: str) -> bool:
        """Used by engine.submit() to reject unknown lanes at submission
        time rather than letting the task sit queued forever."""
        return lane in self._workers

    def lanes(self) -> list[str]:
        """Registered lane names, so the API can expose them and the
        frontend can populate its lane dropdown from the engine rather
        than hardcoding it."""
        return list(self._workers.keys())
