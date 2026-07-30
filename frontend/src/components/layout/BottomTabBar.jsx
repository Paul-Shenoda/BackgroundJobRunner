import { useNav } from "../../context/NavContext";
import { IconTasks, IconSettings } from "../icons";
import ConnectionIndicator from "./ConnectionIndicator";

const DESTINATIONS = [
  { id: "tasks", label: "Tasks", Icon: IconTasks },
  { id: "settings", label: "Settings", Icon: IconSettings },
];

export default function BottomTabBar() {
  const { view, setView } = useNav();

  return (
    <nav className="flex shrink-0 items-center justify-around border-t border-[color:var(--color-hairline)] bg-[color:var(--color-surface)] pb-[env(safe-area-inset-bottom)]">
      {DESTINATIONS.map(({ id, label, Icon }) => {
        const active = view === id;
        return (
          <button
            key={id}
            onClick={() => setView(id)}
            className={
              "flex flex-1 flex-col items-center gap-1 py-2.5 text-[11px] " +
              (active ? "text-[color:var(--color-accent)]" : "text-[color:var(--color-ink-dim)]")
            }
          >
            <Icon />
            {label}
          </button>
        );
      })}
      <div className="flex flex-1 flex-col items-center gap-1 py-2.5">
        <ConnectionIndicator compact />
      </div>
    </nav>
  );
}
