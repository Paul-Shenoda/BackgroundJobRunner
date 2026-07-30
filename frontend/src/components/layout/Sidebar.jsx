import { useNav } from "../../context/NavContext";
import { IconTasks, IconSettings } from "../icons";
import ConnectionIndicator from "./ConnectionIndicator";
import UserBadge from "./UserBadge";

const DESTINATIONS = [
  { id: "tasks", label: "Tasks", Icon: IconTasks },
  { id: "settings", label: "Settings", Icon: IconSettings },
];

// Desktop (>=1024px): full sidebar with labels. Tablet (768-1023px): the
// same component collapses to a ~64px icon rail -- the sidebar shrinks
// BEFORE the content splits into two panes, per the brief.
export default function Sidebar({ rail = false }) {
  const { view, setView } = useNav();

  return (
    <nav
      className={
        "flex h-full shrink-0 flex-col justify-between border-r border-[color:var(--color-hairline)] bg-[color:var(--color-surface)] py-4 " +
        (rail ? "w-16 items-center" : "w-56")
      }
    >
      <div className={"flex flex-col gap-1 " + (rail ? "items-center" : "px-3")}>
        {!rail && (
          <div className="mb-4 px-1 text-sm font-semibold tracking-tight text-[color:var(--color-ink)]">
            Job Runner
          </div>
        )}
        {DESTINATIONS.map(({ id, label, Icon }) => {
          const active = view === id;
          return (
            <button
              key={id}
              onClick={() => setView(id)}
              title={label}
              className={
                "flex items-center gap-3 rounded-lg text-sm transition-colors " +
                (rail ? "h-11 w-11 justify-center" : "px-3 py-2.5") +
                " " +
                (active
                  ? "bg-[color:var(--color-accent-soft)] text-[color:var(--color-accent)]"
                  : "text-[color:var(--color-ink-dim)] hover:bg-[color:var(--color-surface-raised)] hover:text-[color:var(--color-ink)]")
              }
            >
              <Icon />
              {!rail && <span>{label}</span>}
            </button>
          );
        })}
      </div>

      <div className={"flex flex-col gap-3 " + (rail ? "items-center" : "px-3")}>
        <ConnectionIndicator compact={rail} />
        <UserBadge compact={rail} />
      </div>
    </nav>
  );
}
