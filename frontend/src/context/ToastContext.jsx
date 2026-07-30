import { createContext, useContext, useEffect, useMemo, useRef, useState, useCallback } from "react";
import { useEventStream } from "./EventStreamContext";

const ToastContext = createContext(null);
const MAX_VISIBLE = 3;
const AUTO_DISMISS_MS = 5000;

const MESSAGES = {
  ready: (e) => `${e.handle} finished`,
  failed: (e) => `${e.handle} failed`,
  cancelled: (e) => `${e.handle} was cancelled`,
};

export function ToastProvider({ children }) {
  const { subscribe } = useEventStream();
  const [toasts, setToasts] = useState([]);
  const idRef = useRef(0);

  const dismiss = useCallback((id) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  useEffect(() => {
    return subscribe((event) => {
      if (!(event.type in MESSAGES)) return; // only terminal states toast
      const id = ++idRef.current;
      const toast = {
        id,
        type: event.type,
        handle: event.handle,
        message: MESSAGES[event.type](event),
      };
      setToasts((prev) => [...prev.slice(-(MAX_VISIBLE - 1)), toast]);
      setTimeout(() => dismiss(id), AUTO_DISMISS_MS);
    });
  }, [subscribe, dismiss]);

  const value = useMemo(() => ({ toasts, dismiss }), [toasts, dismiss]);

  return <ToastContext.Provider value={value}>{children}</ToastContext.Provider>;
}

export function useToasts() {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToasts must be used within ToastProvider");
  return ctx;
}
