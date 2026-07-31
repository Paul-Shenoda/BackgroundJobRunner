import { STATUS_META } from "../../lib/format";
import { relativeTime } from "../../lib/format";
import StatusPill from "../StatusPill";
import { IconCheck } from "../icons";
import { useNav } from "../../context/NavContext";

export default function TaskRow({ task, recycled = false }) {
  const { openTask, selectedTask } = useNav();
  const meta = STATUS_META[task.status] ?? STATUS_META.queued;
  // Composite identity, not handle alone -- two rows can legitimately
  // share a handle (recycling), and only the row actually clicked should
  // highlight (see NavContext).
  const active = selectedTask?.handle === task.handle && selectedTask?.createdAt === task.created_at;
  // Same ranking rule for both: collected (acknowledged) and cancelled are
  // both "done, nothing left to do here" -- cancelled tasks never carry
  // collected=true (there's no collect action for them), so this needs its
  // own check rather than folding into the collected condition.
  const isDimmed = task.collected || task.status === "cancelled";
  // A seeded row only ever sits non-terminal because seed.py pinned it
  // there on purpose (a future not_before) -- a real queued task moves
  // within seconds. The badge alone reads as "the system is stuck" without
  // this, since it says where the row came from, not that it's inert.
  const isInertSeeded = task.seeded && task.status === "queued";

  return (
    <button
      onClick={() => openTask(task)}
      className={
        "flex w-full items-center gap-3 border-b border-[color:var(--color-hairline-soft)] px-4 py-3 text-left transition-colors " +
        (active ? "bg-[color:var(--color-accent-soft)]" : "hover:bg-[color:var(--color-surface)]") +
        // Done-and-acknowledged rows are ranked below active work visually
        // (reduced opacity), not hidden. hover:opacity-100 restores full
        // weight while actually looking at one.
        (isDimmed ? " opacity-50 hover:opacity-100" : "")
      }
      style={{ borderLeft: `3px solid ${meta.color}` }}
    >
      <div className="flex min-w-0 flex-1 items-center gap-2">
        <span className="shrink-0 font-mono-tabular text-sm font-medium text-[color:var(--color-ink)]">
          {task.handle}
        </span>
        <span className="shrink-0 rounded bg-[color:var(--color-surface-raised)] px-1.5 py-0.5 text-[11px] text-[color:var(--color-ink-dim)]">
          {task.lane}
        </span>
        {recycled && (
          // A glyph, not the word: rows carrying SEEDED + attempts + a
          // status pill already fill the 420px desktop column, and one
          // more worded chip overflows it. The full explanation lives in
          // the detail pane's banner; this only needs to whisper
          // "duplicate handle is history, not a collision" in place.
          <span
            className="shrink-0 text-xs leading-none text-[color:var(--color-ink-faint)]"
            title={`Handle recycled -- this task's number was released and later reused, so ${task.handle} now names a different task. This row is history, not a collision.`}
          >
            ↻
          </span>
        )}
        {task.seeded && (
          <span
            className="shrink-0 rounded bg-[color:var(--color-accent-soft)] px-1.5 py-0.5 text-[10px] font-semibold tracking-wide text-[color:var(--color-accent)]"
            title={
              isInertSeeded
                ? "Seeded demo row, held in queued by a future not_before -- real submissions run normally"
                : undefined
            }
          >
            {isInertSeeded ? "SEEDED · INERT" : "SEEDED"}
          </span>
        )}
      </div>

      <div className="flex shrink-0 items-center gap-2">
        {task.attempts > 1 && (
          <span className="font-mono-tabular text-xs text-[color:var(--color-ink-faint)]">
            {task.attempts}/{task.max_attempts}
          </span>
        )}
        <StatusPill status={task.status} size="sm" />
        {/* The recycled glyph already means "released" (a handle only
            recycles after collect/cancel), so the collected check is
            redundant on those rows -- and the seeded+recycled+attempts
            combination is exactly the row that runs out of width at
            phone/list-column sizes. */}
        {task.collected && !recycled && (
          <IconCheck
            className="shrink-0 text-[color:var(--color-ink-faint)]"
            title="Collected -- this handle has been released"
          />
        )}
        <span className="w-16 text-right font-mono-tabular text-xs text-[color:var(--color-ink-faint)]">
          {relativeTime(task.created_at)}
        </span>
      </div>
    </button>
  );
}
