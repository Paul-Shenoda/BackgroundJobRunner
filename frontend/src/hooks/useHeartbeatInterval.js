import { useEffect, useState } from "react";

// GET /health is unauthenticated static-config + liveness (see
// engine.health()) -- heartbeat_interval is the real, configured
// lease-renewal cadence (HEARTBEAT_INTERVAL env var), not a guess. Fetched
// once per page load and shared via a module-level cache so every
// StatusPill instance doesn't issue its own request.
let cached = null;
let inflight = null;

export function useHeartbeatInterval() {
  const [value, setValue] = useState(cached);

  useEffect(() => {
    if (cached != null) return;
    inflight ??= fetch("/health")
      .then((r) => r.json())
      .then((data) => {
        cached = data.heartbeat_interval;
        return cached;
      });
    let cancelled = false;
    inflight.then((v) => {
      if (!cancelled) setValue(v);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  return value;
}
