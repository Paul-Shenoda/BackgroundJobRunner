import { STATUS_META } from "../../lib/format";
import { relativeTime } from "../../lib/format";
import StatusPill from "../StatusPill";
import { useNav } from "../../context/NavContext";

export default function TaskRow({ task }) {
  const { openTask, selectedHandle } = useNav();
  const meta = STATUS_META[task.status] ?? STATUS_META.queued;
  const active = selectedHandle === task.handle;

  return (
    <button
      onClick={() => openTask(task.handle)}
      className={
        "flex w-full items-center gap-3 border-b border-[color:var(--color-hairline-soft)] px-4 py-3 text-left transition-colors " +
        (active ? "bg-[color:var(--color-accent-soft)]" : "hover:bg-[color:var(--color-surface)]")
      }
      style={{ borderLeft: `3px solid ${meta.color}` }}
    >
      <div className="flex min-w-0 flex-1 items-center gap-3">
        <span className="shrink-0 font-mono-tabular text-sm font-medium text-[color:var(--color-ink)]">
          {task.handle}
        </span>
        <span className="shrink-0 rounded bg-[color:var(--color-surface-raised)] px-1.5 py-0.5 text-[11px] text-[color:var(--color-ink-dim)]">
          {task.lane}
        </span>
        {task.seeded && (
          <span className="shrink-0 rounded bg-[color:var(--color-accent-soft)] px-1.5 py-0.5 text-[10px] font-semibold tracking-wide text-[color:var(--color-accent)]">
            SEEDED
          </span>
        )}
      </div>

      <div className="flex shrink-0 items-center gap-3">
        {task.attempts > 1 && (
          <span className="font-mono-tabular text-xs text-[color:var(--color-ink-faint)]">
            {task.attempts}/{task.max_attempts}
          </span>
        )}
        <StatusPill status={task.status} size="sm" />
        <span className="w-16 text-right font-mono-tabular text-xs text-[color:var(--color-ink-faint)]">
          {relativeTime(task.created_at)}
        </span>
      </div>
    </button>
  );
}
