import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The REST/SSE contract fixes bare paths (POST /tasks, GET /events, etc --
// no /api prefix; see CLAUDE.md and tests/, which call these paths
// directly). Proxying each of those bare paths to the FastAPI backend on
// :8000 in dev means the frontend can call the exact same same-origin
// paths in both dev and production -- in prod, FastAPI's StaticFiles
// mount serves the SPA at "/" and these API routes stay mounted
// alongside it, so no rewrite is needed either way.
const BACKEND = "http://localhost:8000";

// Node only flushes response headers to the socket on the first body
// write (or an explicit flushHeaders() call) -- invisible for ordinary
// requests, but /events is a stream with no body bytes for up to 15s
// (the ping interval, see api/events.py). Without this, the dev proxy
// silently holds the client's fetch() unresolved for up to 15s on every
// (re)connect -- the connection indicator would sit on "reconnecting"
// and the reconnect-triggered resync would be delayed the same amount.
// FastAPI/uvicorn itself doesn't have this problem (ASGI sends headers
// as their own message, independent of the body) -- confirmed by
// hitting :8000 directly, where headers arrive in single-digit ms.
const flushEventStreamHeaders = {
  "/events": {
    target: BACKEND,
    configure: (proxy) => {
      proxy.on("proxyRes", (proxyRes, req, res) => {
        res.flushHeaders();
      });
    },
  },
};

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/tasks": BACKEND,
      "/lanes": BACKEND,
      "/health": BACKEND,
      ...flushEventStreamHeaders,
    },
  },
  build: {
    outDir: "dist",
  },
});
