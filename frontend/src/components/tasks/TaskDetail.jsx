import { useTaskDetail } from "../../hooks/useTaskDetail";
import { useElapsedSeconds, formatElapsed } from "../../hooks/useElapsedSeconds";
import { useTasks } from "../../context/TasksContext";
import { absoluteTime, relativeTime } from "../../lib/format";
import StatusPill from "../StatusPill";
import { IconBack, IconAlert, IconClose } from "../icons";

function runningSince(stateHistory) {
  for (let i = stateHistory.length - 1; i >= 0; i--) {
    if (stateHistory[i].to_status === "running") return stateHistory[i].created_at;
  }
  return null;
}

export default function TaskDetail({ task: selectedTask, onBack }) {
  const { handle, createdAt, row } = selectedTask;
  const taskKey = `${handle}::${createdAt ?? ""}`;
  const { task: fetched, loading, error, actionError, clearActionError, cancel, retry, collect } =
    useTaskDetail(handle, taskKey);
  const { tasks: listTasks } = useTasks();

  // GET /tasks/{handle} resolves active-first: when the clicked row is a
  // HISTORICAL one under a recycled handle, the fetch legitimately returns
  // a DIFFERENT task that now holds the same handle string (see
  // DECISIONS.md, "a handle identifies an active task, not a task" — the
  // fixed contract has no way to address one specific historical row).
  // created_at is the identity check: assigned once at INSERT, never
  // updated, part of the public contract. When it disagrees, showing the
  // fetched row here would be showing (and offering actions against) a
  // task the operator did not select.
  const recycled = createdAt != null && fetched != null && fetched.created_at !== createdAt;

  // For a recycled handle, render the clicked row's own data. Prefer the
  // freshest copy from the live task list (refetched on every event) over
  // the snapshot captured at click time, so e.g. a `collected` flag that
  // flipped since the click still reads correctly.
  const task = recycled
    ? (listTasks.find((t) => t.handle === handle && t.created_at === createdAt) ?? row)
    : fetched;

  const elapsedSeconds = useElapsedSeconds(
    !recycled && task?.status === "running" ? runningSince(task.state_history) : null
  );

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 border-b border-[color:var(--color-hairline)] px-4 py-3">
        {onBack && (
          <button onClick={onBack} className="text-[color:var(--color-ink-dim)]">
            <IconBack />
          </button>
        )}
        <span className="font-mono-tabular text-sm font-semibold text-[color:var(--color-ink)]">
          {handle}
        </span>
      </div>

      <div className="flex-1 overflow-y-auto">
        {loading && !task && (
          <div className="flex flex-col gap-3 p-4">
            {Array.from({ length: 4 }).map((_, i) => (
              <div key={i} className="h-6 animate-pulse rounded bg-[color:var(--color-surface)]" />
            ))}
          </div>
        )}

        {error && !task && (
          <div className="flex flex-col items-center gap-2 p-8 text-center">
            <IconAlert className="text-[color:var(--color-failed)]" />
            <p className="text-sm text-[color:var(--color-ink)]">{error}</p>
          </div>
        )}

        {task && (
          <div className="flex flex-col gap-5 p-4">
            {recycled && (
              <div className="rounded-lg border border-[color:var(--color-accent)]/30 bg-[color:var(--color-accent-soft)] p-3 text-xs text-[color:var(--color-ink)]">
                <p className="font-semibold">This handle has been recycled.</p>
                <p className="mt-1">
                  You're viewing a historical row — <span className="font-mono-tabular">{handle}</span>{" "}
                  was released ({row?.status === "cancelled" ? "cancelled" : "collected"}) and now
                  names a newer task. Actions are disabled for historical rows.
                </p>
              </div>
            )}

            <div className="flex flex-wrap items-center gap-2">
              <StatusPill status={task.status} />
              <span className="rounded bg-[color:var(--color-surface-raised)] px-2 py-0.5 text-xs text-[color:var(--color-ink-dim)]">
                {task.lane}
              </span>
              {task.seeded && (
                <span className="rounded bg-[color:var(--color-accent-soft)] px-2 py-0.5 text-[10px] font-semibold tracking-wide text-[color:var(--color-accent)]">
                  {task.status === "queued" ? "SEEDED · INERT" : "SEEDED"}
                </span>
              )}
              <span className="font-mono-tabular text-xs text-[color:var(--color-ink-faint)]">
                attempts {task.attempts}/{task.max_attempts}
              </span>
              {elapsedSeconds != null && (
                <span className="font-mono-tabular text-xs text-[color:var(--color-running)]">
                  running for {formatElapsed(elapsedSeconds)}
                </span>
              )}
              {task.collected && (
                <span className="text-xs text-[color:var(--color-ink-faint)]">collected</span>
              )}
            </div>

            {task.seeded && task.status === "queued" && (
              <div className="rounded-lg border border-[color:var(--color-accent)]/30 bg-[color:var(--color-accent-soft)] p-3 text-xs text-[color:var(--color-ink)]">
                Seeded demo row: held in queued by a future not_before so the
                worker pool never claims it. Real submissions run normally.
              </div>
            )}

            {/* Never render actions for a historical row: cancel/collect/
                retry all address the handle, and the API would apply them
                to the ACTIVE task holding it — not the row on screen. */}
            {!recycled && <Actions task={task} cancel={cancel} retry={retry} collect={collect} />}

            {actionError && (
              <div className="flex items-start justify-between gap-2 rounded-lg border border-[color:var(--color-failed)]/40 bg-[color:var(--color-failed-soft)] px-3 py-2 text-xs text-[color:var(--color-failed)]">
                <span>{actionError}</span>
                <button onClick={clearActionError} className="shrink-0 opacity-70 hover:opacity-100">
                  <IconClose />
                </button>
              </div>
            )}

            <Section label="Created">
              <span className="text-xs text-[color:var(--color-ink-dim)]">
                {absoluteTime(task.created_at)} ({relativeTime(task.created_at)})
              </span>
            </Section>

            <Section label="Params">
              <JsonBlock value={task.params} />
            </Section>

            {task.status === "ready" && (
              <Section label="Result">
                <JsonBlock value={task.result} />
              </Section>
            )}

            {task.status === "failed" && task.error && (
              <Section label="Error">
                <div className="rounded-lg border border-[color:var(--color-failed)]/30 bg-[color:var(--color-failed-soft)] p-3 text-xs text-[color:var(--color-ink)]">
                  <p>{task.error.reason}</p>
                  <p className="mt-1 text-[color:var(--color-ink-dim)]">
                    retryable: {String(task.error.retryable)}
                  </p>
                </div>
              </Section>
            )}

            {recycled ? (
              <Section label="State history">
                <p className="text-xs text-[color:var(--color-ink-dim)]">
                  Not available for a historical row — the API resolves{" "}
                  <span className="font-mono-tabular">{handle}</span> to the task currently
                  holding it, so this row's history can no longer be addressed.
                </p>
              </Section>
            ) : (
            <Section label="State history">
              <div className="flex flex-col gap-2">
                {task.state_history.map((entry, i) => (
                  <div
                    key={i}
                    className="flex flex-col gap-0.5 border-l-2 border-[color:var(--color-hairline)] pl-3 text-xs"
                  >
                    <div className="flex items-center gap-1.5 font-mono-tabular text-[color:var(--color-ink)]">
                      <span>{entry.from_status ?? "—"}</span>
                      <span className="text-[color:var(--color-ink-faint)]">→</span>
                      <span>{entry.to_status}</span>
                      {entry.attempt != null && (
                        <span className="text-[color:var(--color-ink-faint)]">
                          (attempt {entry.attempt})
                        </span>
                      )}
                    </div>
                    {entry.reason && (
                      <span className="text-[color:var(--color-ink-dim)]">{entry.reason}</span>
                    )}
                    <span className="text-[color:var(--color-ink-faint)]">
                      {absoluteTime(entry.created_at)}
                    </span>
                  </div>
                ))}
              </div>
            </Section>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function Actions({ task, cancel, retry, collect }) {
  const canCancel = task.status === "queued" || task.status === "running";
  const canRetry = task.status === "failed";
  const canCollect = (task.status === "ready" || task.status === "failed") && !task.collected;

  if (!canCancel && !canRetry && !canCollect) return null;

  return (
    <div className="flex flex-wrap gap-2">
      {canCancel && <ActionButton onClick={cancel} label="Cancel" tone="failed" />}
      {canRetry && <ActionButton onClick={retry} label="Retry" tone="accent" />}
      {canCollect && <ActionButton onClick={collect} label="Collect result" tone="ready" />}
    </div>
  );
}

function ActionButton({ onClick, label, tone }) {
  const colors = {
    failed: { color: "var(--color-failed)", background: "var(--color-failed-soft)" },
    accent: { color: "var(--color-accent)", background: "var(--color-accent-soft)" },
    ready: { color: "var(--color-ready)", background: "var(--color-ready-soft)" },
  }[tone];

  return (
    <button
      onClick={onClick}
      className="rounded-lg px-3 py-1.5 text-xs font-medium transition-opacity hover:opacity-80"
      style={colors}
    >
      {label}
    </button>
  );
}

function Section({ label, children }) {
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-[11px] font-semibold uppercase tracking-wide text-[color:var(--color-ink-faint)]">
        {label}
      </span>
      {children}
    </div>
  );
}

function JsonBlock({ value }) {
  return (
    <pre className="overflow-x-auto rounded-lg bg-[color:var(--color-void)] p-3 font-mono-tabular text-xs text-[color:var(--color-ink)]">
      {JSON.stringify(value, null, 2) ?? "null"}
    </pre>
  );
}
