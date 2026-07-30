import { createContext, useContext, useEffect, useRef, useState, useCallback } from "react";
import { connectEventStream } from "../lib/eventStream";
import { useAuth } from "./AuthContext";

const EventStreamContext = createContext(null);

// One SSE connection per mounted apiKey, fanned out to any number of
// listeners (task list, toasts, an open detail view) via a tiny pub/sub
// -- avoids opening a second stream per consumer.
export function EventStreamProvider({ children }) {
  const { apiKey, setAuthError } = useAuth();
  const [connectionState, setConnectionState] = useState("reconnecting");
  const listenersRef = useRef(new Set());

  const subscribe = useCallback((listener) => {
    listenersRef.current.add(listener);
    return () => listenersRef.current.delete(listener);
  }, []);

  useEffect(() => {
    const handle = connectEventStream({
      apiKey,
      onEvent: (event) => {
        for (const listener of listenersRef.current) listener(event);
      },
      onStateChange: setConnectionState,
      onUnauthorized: () => setAuthError("That key was rejected by the server. Enter a valid API key."),
    });
    return () => handle.stop();
    // apiKey is stable for the lifetime of this provider (the app tree is
    // remounted with a fresh `key` on key change -- see App.jsx), but
    // listed for correctness.
  }, [apiKey, setAuthError]);

  return (
    <EventStreamContext.Provider value={{ connectionState, subscribe }}>
      {children}
    </EventStreamContext.Provider>
  );
}

export function useEventStream() {
  const ctx = useContext(EventStreamContext);
  if (!ctx) throw new Error("useEventStream must be used within EventStreamProvider");
  return ctx;
}
