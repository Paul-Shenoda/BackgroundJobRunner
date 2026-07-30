import { useTasks } from "../../context/TasksContext";
import { useLanes } from "../../hooks/useLanes";
import { STATUSES, STATUS_META } from "../../lib/format";
import { IconPlus, IconFilter, IconSort, IconCheck } from "../icons";
import { useNav } from "../../context/NavContext";
import Popover from "../Popover";

const QUICK_FILTERS = [
  { id: "all", label: "All", statuses: [] },
  { id: "active", label: "Active", statuses: ["queued", "running"] },
  { id: "completed", label: "Completed", statuses: ["ready", "failed", "cancelled"] },
];

function sameSet(a, b) {
  if (a.length !== b.length) return false;
  const setB = new Set(b);
  return a.every((x) => setB.has(x));
}

// Which quick-filter preset (if any) the CURRENT status array matches --
// there's no separate "which preset is selected" state; the quick filters
// and the popover's status checkboxes both read/write filters.status
// directly, so they can never disagree (single source of truth rather
// than two values kept in sync by hand).
function activePreset(status) {
  return QUICK_FILTERS.find((f) => sameSet(f.statuses, status))?.id ?? null;
}

const SORT_FIELDS = [
  { value: "created_at", label: "Created" },
  { value: "updated_at", label: "Updated" },
];

const controlClass =
  "flex items-center gap-1 rounded-lg border border-[color:var(--color-hairline)] bg-[color:var(--color-surface)] px-2 py-1.5 text-xs text-[color:var(--color-ink)] hover:bg-[color:var(--color-surface-raised)]";

const selectClass =
  "rounded-lg border border-[color:var(--color-hairline)] bg-[color:var(--color-void)] px-2.5 py-1.5 text-xs text-[color:var(--color-ink)] outline-none focus:border-[color:var(--color-accent)]";

export default function FiltersBar() {
  const { filters, setFilters } = useTasks();
  const { setNewTaskOpen } = useNav();
  const preset = activePreset(filters.status);

  const filterCount =
    (filters.status.length > 0 ? 1 : 0) +
    (filters.lane ? 1 : 0) +
    (filters.createdAfter || filters.createdBefore ? 1 : 0);

  const sortLabel = SORT_FIELDS.find((f) => f.value === filters.sortBy)?.label ?? "Created";
  const sortArrow = filters.sortOrder === "asc" ? "↑" : "↓";

  return (
    // Flat padding/gap, no viewport-based sm: escalation: this row's real
    // available width does NOT track viewport width. On desktop it sits in
    // a fixed 420px list column (see TasksPage.jsx) regardless of how wide
    // the browser window is; on tablet (icon-rail, single pane) it actually
    // has MORE room than "desktop" despite the narrower viewport. A prior
    // version used sm:px-4 assuming "wider viewport = more room," which is
    // true for the page but false for this specific component -- it made
    // the row wider exactly in its tightest real context (the 420px
    // column), overflowing past the column's own right edge while still
    // comfortably inside the full browser viewport (invisible to a check
    // that only compares against document width).
    <div
      data-popover-boundary
      className="flex items-center gap-1 border-b border-[color:var(--color-hairline)] bg-[color:var(--color-void)] px-2.5 py-3"
    >
      {/* Quick filters: one-click presets over the SAME filters.status the
          popover's multi-select controls -- always visible, at every
          breakpoint. */}
      <div className="flex shrink-0 rounded-lg border border-[color:var(--color-hairline)] bg-[color:var(--color-surface)] p-0.5">
        {QUICK_FILTERS.map((f) => (
          <button
            key={f.id}
            onClick={() => setFilters({ status: f.statuses })}
            className={
              "rounded-md px-1.5 py-1 text-xs font-medium transition-colors " +
              (preset === f.id
                ? "bg-[color:var(--color-accent)] text-white"
                : "text-[color:var(--color-ink-dim)] hover:text-[color:var(--color-ink)]")
            }
          >
            {f.label}
          </button>
        ))}
      </div>

      <Popover
        trigger={(toggle, open) => (
          <button
            onClick={toggle}
            title="Filters"
            className={
              controlClass + " shrink-0 whitespace-nowrap" + (open ? " border-[color:var(--color-accent)]" : "")
            }
          >
            <IconFilter />
            {/* Icon-only, same reasoning as Sort below: "available width"
                for this component doesn't track viewport breakpoint (the
                420px desktop column is narrower than the tablet single-pane
                view despite being "desktop"), so there's no viewport-based
                condition that's actually safe here. The badge count already
                communicates state without needing the word "Filters". */}
            {filterCount > 0 && (
              <span className="flex h-4 min-w-4 items-center justify-center rounded-full bg-[color:var(--color-accent)] px-1 text-[10px] font-semibold text-white">
                {filterCount}
              </span>
            )}
          </button>
        )}
      >
        {(close) => <FiltersPopoverContent onClose={close} />}
      </Popover>

      <Popover
        trigger={(toggle, open) => (
          <button
            onClick={toggle}
            title={`Sort: ${sortLabel}, ${filters.sortOrder === "asc" ? "ascending" : "descending"}`}
            className={
              controlClass + " shrink-0 whitespace-nowrap" + (open ? " border-[color:var(--color-accent)]" : "")
            }
          >
            <IconSort />
            {/* Icon-only, always -- "Created"/"Updated" differ enough in
                width that showing the field name here could wrap onto a
                second line and grow the whole row (see DECISIONS.md). The
                arrow alone is a single fixed-width glyph, so it can't. The
                full field name is one click away in the popover. */}
            <span>{sortArrow}</span>
          </button>
        )}
      >
        {(close) => <SortPopoverContent onClose={close} />}
      </Popover>

      <button
        type="button"
        onClick={() => setNewTaskOpen(true)}
        className="ml-auto flex shrink-0 items-center gap-1 whitespace-nowrap rounded-lg bg-[color:var(--color-accent)] px-2 py-1.5 text-xs font-medium text-white transition-opacity hover:opacity-90"
      >
        <IconPlus />
        New task
      </button>
    </div>
  );
}

function FiltersPopoverContent({ onClose }) {
  const { filters, setFilters } = useTasks();
  const lanes = useLanes();

  function toggleStatus(status) {
    const has = filters.status.includes(status);
    setFilters({
      status: has ? filters.status.filter((s) => s !== status) : [...filters.status, status],
    });
  }

  function clearAll() {
    setFilters({ status: [], lane: "", createdAfter: "", createdBefore: "" });
  }

  return (
    // w-[350px], not w-72 (288px): measured precisely (Playwright) that two
    // native <input type=date> elements have an intrinsic width of 130px
    // each that browsers won't shrink below gracefully -- below that, the
    // calendar icon gets clipped rather than the input shrinking further.
    // 288px was 33px short of what the unshrunk row actually needed. 350px
    // gives ~22px of real slack over the measured need, while still
    // comfortably fitting inside the popover's own boundary clamp (see
    // Popover.jsx) at the narrowest real case (390px phone).
    <div className="flex w-[350px] flex-col gap-4 rounded-xl border border-[color:var(--color-hairline)] bg-[color:var(--color-surface)] p-4 shadow-xl shadow-black/20">
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold uppercase tracking-wide text-[color:var(--color-ink-faint)]">
          Filters
        </span>
        <button onClick={clearAll} className="text-xs text-[color:var(--color-accent)] hover:underline">
          Clear all
        </button>
      </div>

      <div className="flex flex-col gap-1.5">
        <span className="text-xs text-[color:var(--color-ink-dim)]">Status</span>
        <div className="flex flex-wrap gap-1.5">
          {STATUSES.map((s) => {
            const checked = filters.status.includes(s);
            const meta = STATUS_META[s];
            return (
              <button
                key={s}
                onClick={() => toggleStatus(s)}
                className={
                  "flex items-center gap-1 rounded-full border px-2.5 py-1 text-[11px] font-medium transition-colors " +
                  (checked ? "border-transparent" : "border-[color:var(--color-hairline)] text-[color:var(--color-ink-dim)]")
                }
                style={checked ? { background: meta.soft, color: meta.color } : undefined}
              >
                {/* A checkmark, not just color, marks "selected" -- queued
                    and cancelled are themselves muted greys, so a
                    color-only selected state is nearly indistinguishable
                    from unselected for those two. */}
                {checked && <IconCheck />}
                {meta.label}
              </button>
            );
          })}
        </div>
      </div>

      <label className="flex flex-col gap-1.5 text-xs text-[color:var(--color-ink-dim)]">
        Lane
        <select
          value={filters.lane}
          onChange={(e) => setFilters({ lane: e.target.value })}
          className={selectClass}
        >
          <option value="">All lanes</option>
          {lanes.map((lane) => (
            <option key={lane} value={lane}>
              {lane}
            </option>
          ))}
        </select>
      </label>

      <div className="flex flex-col gap-1.5 text-xs text-[color:var(--color-ink-dim)]">
        Created between
        <div className="flex items-center gap-2">
          {/* No flex-1: a native date input has an intrinsic ~130px width
              it won't shrink below without clipping its own calendar
              icon (measured directly, not assumed) -- shrink-0 keeps
              both inputs at that natural size regardless of how much
              room the row has, rather than fighting the browser to
              compress them further. */}
          <input
            type="date"
            value={filters.createdAfter}
            onChange={(e) => setFilters({ createdAfter: e.target.value })}
            className={selectClass + " shrink-0"}
          />
          <span className="text-[color:var(--color-ink-faint)]">to</span>
          <input
            type="date"
            value={filters.createdBefore}
            onChange={(e) => setFilters({ createdBefore: e.target.value })}
            className={selectClass + " shrink-0"}
          />
        </div>
      </div>

      <button
        onClick={onClose}
        className="rounded-lg bg-[color:var(--color-accent)] px-3 py-1.5 text-xs font-medium text-white hover:opacity-90"
      >
        Done
      </button>
    </div>
  );
}

function SortPopoverContent({ onClose }) {
  const { filters, setFilters } = useTasks();

  return (
    <div className="flex w-56 flex-col gap-3 rounded-xl border border-[color:var(--color-hairline)] bg-[color:var(--color-surface)] p-4 shadow-xl shadow-black/20">
      <span className="text-xs font-semibold uppercase tracking-wide text-[color:var(--color-ink-faint)]">
        Sort by
      </span>
      <div className="flex flex-col gap-1">
        {SORT_FIELDS.map((f) => (
          <label
            key={f.value}
            className="flex cursor-pointer items-center gap-2 text-xs text-[color:var(--color-ink)]"
          >
            <input
              type="radio"
              name="sortBy"
              checked={filters.sortBy === f.value}
              onChange={() => setFilters({ sortBy: f.value })}
              className="h-3.5 w-3.5 accent-[color:var(--color-accent)]"
            />
            {f.label}
          </label>
        ))}
      </div>

      <div className="flex rounded-lg border border-[color:var(--color-hairline)] p-0.5">
        {[
          { value: "desc", label: "Descending" },
          { value: "asc", label: "Ascending" },
        ].map((opt) => (
          <button
            key={opt.value}
            onClick={() => setFilters({ sortOrder: opt.value })}
            className={
              "flex-1 rounded-md px-2 py-1 text-xs font-medium transition-colors " +
              (filters.sortOrder === opt.value
                ? "bg-[color:var(--color-accent)] text-white"
                : "text-[color:var(--color-ink-dim)]")
            }
          >
            {opt.label}
          </button>
        ))}
      </div>

      <button
        onClick={onClose}
        className="rounded-lg bg-[color:var(--color-accent)] px-3 py-1.5 text-xs font-medium text-white hover:opacity-90"
      >
        Done
      </button>
    </div>
  );
}
