-- Tables + indexes for the background job runner.
-- Ask before changing anything in this file (see CLAUDE.md).

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE users (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    api_key     TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TYPE task_status AS ENUM ('queued','running','ready','failed','cancelled');

CREATE TABLE tasks (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           UUID NOT NULL REFERENCES users(id),
    lane              TEXT NOT NULL,
    number            INT  NOT NULL,
    handle            TEXT GENERATED ALWAYS AS (lane || '-' || number) STORED,

    params            JSONB NOT NULL DEFAULT '{}',
    status            task_status NOT NULL DEFAULT 'queued',
    result            JSONB,
    error             JSONB,              -- { reason, retryable }
    collected         BOOLEAN NOT NULL DEFAULT false,

    attempts          INT NOT NULL DEFAULT 0,
    max_attempts      INT NOT NULL DEFAULT 3,
    not_before        TIMESTAMPTZ,        -- retry backoff gate
    worker_id         TEXT,               -- who holds the lease
    lease_expires_at  TIMESTAMPTZ,

    seeded            BOOLEAN NOT NULL DEFAULT false,  -- demo data, clearly flagged

    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Monotonic insertion order, independent of the wall clock. created_at
    -- remains the user-facing timestamp in the API contract; seq exists
    -- purely so get_task's historical-row tie-break has something to sort
    -- by that a clock jump (NTP correction, VM suspend/resume, container
    -- migration) can't put out of causal order. See DECISIONS.md, "handle
    -- resolution ordering uses a monotonic sequence, not a timestamp".
    seq               BIGSERIAL
);

-- THE load-bearing invariant: a handle can never collide with an active one.
-- Collected and cancelled rows fall outside the predicate, so history accumulates freely
-- while exactly one live scrape-1 can exist per user.
CREATE UNIQUE INDEX uniq_active_handle
    ON tasks (user_id, lane, number)
    WHERE status IN ('queued','running')
       OR (status IN ('ready','failed') AND collected = false);

-- Supports the claim query's ORDER BY without a full scan.
CREATE INDEX idx_claimable
    ON tasks (status, not_before, created_at)
    WHERE status = 'queued';

-- Supports the reaper.
CREATE INDEX idx_expired_leases
    ON tasks (lease_expires_at)
    WHERE status = 'running';

-- Append-only audit trail: powers the detail view's "state history with timestamps".
CREATE TABLE task_events (
    id          BIGSERIAL PRIMARY KEY,
    task_id     UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    from_status task_status,
    to_status   task_status NOT NULL,
    reason      TEXT,
    attempt     INT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
