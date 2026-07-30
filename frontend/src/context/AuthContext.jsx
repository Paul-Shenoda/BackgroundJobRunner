import { createContext, useContext, useEffect, useMemo, useState, useCallback } from "react";
import { listLanes, setUnauthorizedHandler } from "../lib/apiClient";

const STORAGE_KEY = "jobrunner.apiKey";
const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [apiKey, setApiKeyState] = useState(() => localStorage.getItem(STORAGE_KEY));
  const [authError, setAuthError] = useState(null);

  // Any request anywhere hitting a 401 bounces back to the first-run
  // card with an honest message, rather than a dashboard silently stuck
  // on stale/empty data.
  useEffect(() => {
    setUnauthorizedHandler(() => {
      localStorage.removeItem(STORAGE_KEY);
      setApiKeyState(null);
      setAuthError("That key was rejected by the server. Enter a valid API key.");
    });
  }, []);

  const verifyAndSetKey = useCallback(async (candidateKey) => {
    const trimmed = candidateKey.trim();
    if (!trimmed) {
      setAuthError("Enter an API key.");
      return false;
    }
    try {
      await listLanes(trimmed);
    } catch (err) {
      setAuthError(err.status === 401 ? "That key isn't recognized." : "Couldn't reach the server -- try again.");
      return false;
    }
    localStorage.setItem(STORAGE_KEY, trimmed);
    setApiKeyState(trimmed);
    setAuthError(null);
    return true;
  }, []);

  const clearKey = useCallback(() => {
    localStorage.removeItem(STORAGE_KEY);
    setApiKeyState(null);
    setAuthError(null);
  }, []);

  const value = useMemo(
    () => ({ apiKey, authError, setAuthError, verifyAndSetKey, clearKey }),
    [apiKey, authError, verifyAndSetKey, clearKey]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
