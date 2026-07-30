import { useEffect, useState } from "react";

// Live-ticking elapsed time from a start timestamp. Deliberately not a
// progress bar: a progress bar is only computable because the mock
// worker happens to report duration_ms up front. A real worker reports
// nothing until it returns -- the engine never inspects params (CLAUDE.md)
// -- so elapsed time is the only honest "how long has this been running"
// signal for any worker, mock or real.
export function useElapsedSeconds(startIso) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!startIso) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [startIso]);

  if (!startIso) return null;
  return Math.max(0, Math.floor((now - new Date(startIso).getTime()) / 1000));
}

export function formatElapsed(totalSeconds) {
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  const s = totalSeconds % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}
