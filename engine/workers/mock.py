"""The mock worker.

This is a real worker, not a placeholder to be swapped out later — the brief
states explicitly that a genuine scraper/converter is "welcome but not
required and not where the points are." This one function is also the
entire reproducibility mechanism for the acceptance criteria: every
observable behaviour (retry-with-backoff, permanent failure, cancellation
mid-run, concurrency under load) is driven purely through job.params, not
by branching on which lane the job belongs to.

A worker knows nothing about engine state: no store, no engine, no
registry. It reads a Job and returns a WorkerResult; the engine owns
queued/running/cancelled and everything else about the lifecycle.

No make_lane_worker(lane_name) factory here. It would only earn its keep if
different lanes needed different behaviour (different duration ranges,
different result shapes), and nothing in the design calls for that — lane
identity is just data this worker echoes back in its result, never a branch
it takes. A factory that always produces the same function isn't a factory,
it's indirection.
"""

import asyncio
import random

from engine.models import Job, TaskError, WorkerResult


async def mock_worker(job: Job) -> WorkerResult:
    """Sleep for duration_ms, then succeed or fail based on params.

    fail_permanent takes precedence over fail if both are set.
    """
    duration_ms = job.params.get("duration_ms", random.randint(3000, 15000))

    # asyncio.sleep, never time.sleep: time.sleep blocks the event loop and
    # cannot be cancelled. The entire cancellation requirement — a running
    # job actually stopping when cancelled, not just being abandoned by the
    # caller — depends on this specific line being interruptible. This is
    # the single most important line in the file.
    await asyncio.sleep(duration_ms / 1000)

    # Failure check comes AFTER the sleep, deliberately: a failing job still
    # takes its full duration, so retry-with-backoff is something you can
    # actually watch happen in the dashboard rather than something that
    # flashes past before anyone can see it.
    if job.params.get("fail_permanent"):
        return WorkerResult(
            status="failed",
            error=TaskError(
                reason="unsupported input format: expected JSON",
                retryable=False,
            ),
        )

    if job.params.get("fail"):
        return WorkerResult(
            status="failed",
            error=TaskError(
                reason="upstream connection reset",
                retryable=True,
            ),
        )

    return WorkerResult(
        status="ready",
        result={
            "lane": job.lane,
            "duration_ms": duration_ms,
            "bytes_processed": random.randint(1_000, 500_000),
            "records": random.randint(1, 500),
        },
    )
