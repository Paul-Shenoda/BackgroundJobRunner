import { useTasks } from "../../context/TasksContext";
import TaskRow from "./TaskRow";
import { IconAlert } from "../icons";

export default function TaskList() {
  const { tasks, loading, error, hasMore, loadMore } = useTasks();

  if (loading && tasks.length === 0) {
    return (
      <div className="flex flex-1 flex-col gap-2 p-4">
        {Array.from({ length: 6 }).map((_, i) => (
          <div key={i} className="h-11 animate-pulse rounded-lg bg-[color:var(--color-surface)]" />
        ))}
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-2 p-8 text-center">
        <IconAlert className="text-[color:var(--color-failed)]" />
        <p className="text-sm text-[color:var(--color-ink)]">Couldn't load tasks</p>
        <p className="max-w-xs text-xs text-[color:var(--color-ink-dim)]">{error}</p>
      </div>
    );
  }

  if (tasks.length === 0) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-1 p-8 text-center">
        <p className="text-sm text-[color:var(--color-ink)]">No tasks match these filters</p>
        <p className="max-w-xs text-xs text-[color:var(--color-ink-dim)]">
          Try widening the status, lane, or date range -- or submit a new task.
        </p>
      </div>
    );
  }

  // Handles recycle (CLAUDE.md), so the visible list can contain several
  // rows sharing one handle string -- historical duplicates piling up
  // under a number the partial unique index has since let go. Exactly
  // ONE row per handle is ever "the one GET /tasks/{handle} resolves to"
  // right now -- mirroring store.py's get_task ORDER BY exactly: an
  // active row (queued/running, or ready/failed and uncollected) wins if
  // one exists; otherwise the most recently created row wins. Every
  // OTHER row sharing that handle is what actually gets the "recycled"
  // marker.
  //
  // Getting this wrong bit for real: an earlier version treated "not
  // currently active" as the recycled test, which is right while one
  // copy is still active but wrong once ALL copies of a handle are
  // collected -- with no active row, every terminal duplicate looked
  // equally "not active" and got marked recycled, including the one the
  // API would actually still resolve to (and reference in a click's own
  // detail fetch). created_at is the same disambiguator TaskDetail.jsx's
  // recycled check already uses, specifically so the list marker and the
  // detail banner can never disagree about which row is canonical.
  const holdsHandle = (t) =>
    t.status === "queued" ||
    t.status === "running" ||
    ((t.status === "ready" || t.status === "failed") && !t.collected);

  const byHandle = new Map();
  for (const t of tasks) {
    if (!byHandle.has(t.handle)) byHandle.set(t.handle, []);
    byHandle.get(t.handle).push(t);
  }
  const canonicalCreatedAt = new Map();
  for (const [handle, rows] of byHandle) {
    if (rows.length < 2) continue; // unique handle -- nothing to disambiguate
    const active = rows.find(holdsHandle);
    const canonical = active ?? rows.reduce((latest, t) => (t.created_at > latest.created_at ? t : latest));
    canonicalCreatedAt.set(handle, canonical.created_at);
  }

  return (
    <div className="flex flex-1 flex-col overflow-y-auto">
      {tasks.map((task) => (
        // Handles recycle (CLAUDE.md) -- the API never exposes a row id,
        // and GET /tasks can legitimately return two distinct rows
        // sharing the same handle string (an old collected/cancelled row
        // plus a new active one that reused the number). created_at
        // differs between them, so pairing it with handle keeps React's
        // keys actually unique instead of colliding.
        <TaskRow
          key={`${task.handle}-${task.created_at}`}
          task={task}
          recycled={canonicalCreatedAt.has(task.handle) && canonicalCreatedAt.get(task.handle) !== task.created_at}
        />
      ))}
      {hasMore && (
        <button
          onClick={loadMore}
          className="m-4 rounded-lg border border-[color:var(--color-hairline)] py-2 text-xs text-[color:var(--color-ink-dim)] hover:bg-[color:var(--color-surface)]"
        >
          Load more
        </button>
      )}
    </div>
  );
}
