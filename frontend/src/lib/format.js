export const STATUSES = ["queued", "running", "ready", "failed", "cancelled"];

export const STATUS_META = {
  queued: { label: "Queued", color: "var(--color-queued)", soft: "var(--color-queued-soft)" },
  running: { label: "Running", color: "var(--color-running)", soft: "var(--color-running-soft)" },
  ready: { label: "Ready", color: "var(--color-ready)", soft: "var(--color-ready-soft)" },
  failed: { label: "Failed", color: "var(--color-failed)", soft: "var(--color-failed-soft)" },
  cancelled: {
    label: "Cancelled",
    color: "var(--color-cancelled)",
    soft: "var(--color-cancelled-soft)",
  },
};

export function relativeTime(isoString) {
  const then = new Date(isoString).getTime();
  const now = Date.now();
  const diffSeconds = Math.round((now - then) / 1000);

  if (diffSeconds < 5) return "just now";
  if (diffSeconds < 60) return `${diffSeconds}s ago`;
  const diffMinutes = Math.round(diffSeconds / 60);
  if (diffMinutes < 60) return `${diffMinutes}m ago`;
  const diffHours = Math.round(diffMinutes / 60);
  if (diffHours < 24) return `${diffHours}h ago`;
  const diffDays = Math.round(diffHours / 24);
  if (diffDays < 14) return `${diffDays}d ago`;
  return new Date(isoString).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function absoluteTime(isoString) {
  return new Date(isoString).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "medium",
  });
}
