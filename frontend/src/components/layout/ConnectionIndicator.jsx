import { useEventStream } from "../../context/EventStreamContext";

const META = {
  live: { label: "Live", color: "var(--color-ready)" },
  reconnecting: { label: "Reconnecting…", color: "var(--color-queued)" },
  offline: { label: "Offline — retrying", color: "var(--color-failed)" },
};

export default function ConnectionIndicator({ compact = false }) {
  const { connectionState } = useEventStream();
  const meta = META[connectionState] ?? META.offline;
  const pulsing = connectionState !== "live";

  return (
    <div className="flex items-center gap-2 text-xs text-[color:var(--color-ink-dim)]" title={meta.label}>
      <span
        className={"inline-block h-2 w-2 rounded-full" + (pulsing ? " animate-soft-pulse" : "")}
        style={{ background: meta.color }}
        aria-hidden
      />
      {!compact && <span>{meta.label}</span>}
    </div>
  );
}
