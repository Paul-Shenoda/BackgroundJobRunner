"""The mock worker.

Params: duration_ms (default: random 3000-15000), fail (retryable error),
fail_permanent (non-retryable error). Sleep happens before the failure
check so retries are observable.
"""
