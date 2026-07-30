import { useAuth } from "../../context/AuthContext";

// There's no /whoami endpoint (keys are opaque, seeded at startup -- see
// DECISIONS.md) so the badge shows the active key itself rather than a
// name the server never tells us. That's still exactly "which key is
// active," per the brief.
export default function UserBadge({ compact = false }) {
  const { apiKey } = useAuth();
  if (!apiKey) return null;

  return (
    <div
      className="flex items-center gap-2 rounded-md border border-[color:var(--color-hairline)] bg-[color:var(--color-surface)] px-2.5 py-1.5"
      title={`Active key: ${apiKey}`}
    >
      <span
        className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-[10px] font-semibold uppercase"
        style={{ background: "var(--color-accent-soft)", color: "var(--color-accent)" }}
        aria-hidden
      >
        {apiKey.slice(0, 1)}
      </span>
      {!compact && (
        <span className="truncate font-mono-tabular text-xs text-[color:var(--color-ink-dim)]">
          {apiKey}
        </span>
      )}
    </div>
  );
}
