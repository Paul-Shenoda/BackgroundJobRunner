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

  return (
    <div className="flex flex-1 flex-col overflow-y-auto">
      {tasks.map((task) => (
        // Handles recycle (CLAUDE.md) -- the API never exposes a row id,
        // and GET /tasks can legitimately return two distinct rows
        // sharing the same handle string (an old collected/cancelled row
        // plus a new active one that reused the number). created_at
        // differs between them, so pairing it with handle keeps React's
        // keys actually unique instead of colliding.
        <TaskRow key={`${task.handle}-${task.created_at}`} task={task} />
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
