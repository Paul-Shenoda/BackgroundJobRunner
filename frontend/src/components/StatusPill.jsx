import { STATUS_META } from "../lib/format";
import { useHeartbeatInterval } from "../hooks/useHeartbeatInterval";

export default function StatusPill({ status, size = "md" }) {
  const meta = STATUS_META[status] ?? STATUS_META.queued;
  const isRunning = status === "running";
  const isCancelled = status === "cancelled";
  const heartbeatInterval = useHeartbeatInterval();

  return (
    <span
      className={
        "inline-flex items-center gap-1.5 rounded-full font-medium " +
        (size === "sm" ? "px-2 py-0.5 text-[11px]" : "px-2.5 py-1 text-xs")
      }
      style={{ background: meta.soft, color: meta.color }}
    >
      <span
        className={"inline-block rounded-full " + (size === "sm" ? "h-1.5 w-1.5" : "h-2 w-2") + (isRunning ? " animate-heartbeat" : "")}
        style={{
          background: meta.color,
          // The real configured lease-renewal cadence (GET /health's
          // heartbeat_interval), not a number chosen to look right.
          // Falls back to a placeholder only until that one-time fetch
          // resolves.
          animationDuration: isRunning ? `${heartbeatInterval ?? 2}s` : undefined,
        }}
        aria-hidden
      />
      <span className={isCancelled ? "line-through decoration-1" : ""}>{meta.label}</span>
    </span>
  );
}
