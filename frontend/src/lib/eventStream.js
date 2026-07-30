// Manual SSE client over fetch(), not the browser EventSource API --
// EventSource can't send custom headers, and GET /events is authenticated
// via X-API-Key header only (no query-param fallback). This reads the
// same "data: {...}\n\n" / ": ping\n\n" framing EventSource would.

const BACKOFF_STEPS_MS = [1000, 2000, 4000, 8000, 15000];

export function connectEventStream({ apiKey, onEvent, onStateChange, onUnauthorized }) {
  let stopped = false;
  let attempt = 0;
  let controller = null;
  let retryTimer = null;

  function setState(state) {
    if (!stopped) onStateChange(state);
  }

  async function run() {
    if (stopped) return;
    setState(attempt === 0 ? "reconnecting" : "reconnecting");
    controller = new AbortController();

    let res;
    try {
      res = await fetch("/events", {
        headers: { "X-API-Key": apiKey },
        signal: controller.signal,
      });
    } catch (err) {
      if (stopped) return;
      scheduleRetry();
      return;
    }

    if (res.status === 401) {
      onUnauthorized?.();
      return; // never retry an auth failure -- retrying can't fix it
    }

    if (!res.ok || !res.body) {
      if (stopped) return;
      scheduleRetry();
      return;
    }

    attempt = 0; // connected -- reset backoff
    setState("live");
    onEvent({ type: "__stream_connected__" }); // triggers a full resync (reconnect semantics)

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        let sepIndex;
        while ((sepIndex = buffer.indexOf("\n\n")) !== -1) {
          const frame = buffer.slice(0, sepIndex);
          buffer = buffer.slice(sepIndex + 2);
          for (const line of frame.split("\n")) {
            if (line.startsWith("data:")) {
              try {
                onEvent(JSON.parse(line.slice(5).trim()));
              } catch {
                // malformed frame -- ignore, not fatal to the stream
              }
            }
            // lines starting with ":" are keep-alive pings; nothing to do
          }
        }
      }
    } catch (err) {
      // reader threw (network drop, server closed the connection) --
      // falls through to the disconnect handling below
    }

    if (stopped) return;
    scheduleRetry();
  }

  function scheduleRetry() {
    setState("offline");
    const delay = BACKOFF_STEPS_MS[Math.min(attempt, BACKOFF_STEPS_MS.length - 1)];
    attempt += 1;
    retryTimer = setTimeout(run, delay);
  }

  // Deferred by a tick rather than called synchronously: React StrictMode's
  // dev-only double-invoke (mount -> cleanup -> mount) runs all three steps
  // synchronously in the same flush. Issuing a real fetch() from the first
  // mount means it's already in flight when cleanup aborts it a moment
  // later -- which, through some dev proxies (observed with Vite's), can
  // wedge the underlying connection so the second, surviving mount's
  // request never gets a response at all. Deferring means the abort from
  // that first cleanup lands before this ever calls fetch(), so a
  // double-mount never issues more than one real network request.
  retryTimer = setTimeout(run, 0);

  return {
    stop() {
      stopped = true;
      controller?.abort();
      if (retryTimer) clearTimeout(retryTimer);
    },
  };
}
