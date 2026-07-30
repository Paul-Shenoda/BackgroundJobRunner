import { useTasks } from "../../context/TasksContext";
import { useLanes } from "../../hooks/useLanes";
import { STATUSES, STATUS_META } from "../../lib/format";
import { IconPlus } from "../icons";
import { useNav } from "../../context/NavContext";

const selectClass =
  "rounded-lg border border-[color:var(--color-hairline)] bg-[color:var(--color-surface)] px-2.5 py-1.5 text-xs text-[color:var(--color-ink)] outline-none focus:border-[color:var(--color-accent)]";

// Two rows, not one scrolling row: a hidden-overflow "scroll to find the
// button" pattern is technically not clipping anything, but it reads as
// broken (a control sitting half-visible at the viewport edge with no
// scrollbar affordance is exactly what got flagged at 768px). Row 1 is
// short enough to always fit at any of the three breakpoints and never
// scrolls, so the primary action is always reachable. Row 2 (the wider,
// lower-priority date/sort controls) wraps onto additional lines instead
// of scrolling or clipping.
export default function FiltersBar() {
  const { filters, setFilters } = useTasks();
  const { setNewTaskOpen } = useNav();
  const lanes = useLanes();

  return (
    <div className="flex flex-col gap-2 border-b border-[color:var(--color-hairline)] bg-[color:var(--color-void)] px-4 py-3">
      <div className="flex items-center gap-2">
        <select
          className={selectClass}
          value={filters.status}
          onChange={(e) => setFilters({ status: e.target.value })}
        >
          <option value="">All statuses</option>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {STATUS_META[s].label}
            </option>
          ))}
        </select>

        <select
          className={selectClass}
          value={filters.lane}
          onChange={(e) => setFilters({ lane: e.target.value })}
        >
          <option value="">All lanes</option>
          {lanes.map((lane) => (
            <option key={lane} value={lane}>
              {lane}
            </option>
          ))}
        </select>

        <button
          type="button"
          onClick={() => setNewTaskOpen(true)}
          className="ml-auto flex shrink-0 items-center gap-1.5 rounded-lg bg-[color:var(--color-accent)] px-3 py-1.5 text-xs font-medium text-white transition-opacity hover:opacity-90"
        >
          <IconPlus />
          New task
        </button>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <input
          type="date"
          className={selectClass}
          value={filters.createdAfter}
          onChange={(e) => setFilters({ createdAfter: e.target.value })}
          title="Created after"
        />
        <span className="shrink-0 text-xs text-[color:var(--color-ink-faint)]">to</span>
        <input
          type="date"
          className={selectClass}
          value={filters.createdBefore}
          onChange={(e) => setFilters({ createdBefore: e.target.value })}
          title="Created before"
        />

        <select
          className={selectClass}
          value={filters.sortBy}
          onChange={(e) => setFilters({ sortBy: e.target.value })}
        >
          <option value="created_at">Created</option>
          <option value="updated_at">Updated</option>
        </select>
        <button
          type="button"
          onClick={() => setFilters({ sortOrder: filters.sortOrder === "asc" ? "desc" : "asc" })}
          className={selectClass}
          title="Toggle sort direction"
        >
          {filters.sortOrder === "asc" ? "↑ Asc" : "↓ Desc"}
        </button>
      </div>
    </div>
  );
}
