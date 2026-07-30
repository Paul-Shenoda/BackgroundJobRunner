import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { listTasks, ApiError } from "../lib/apiClient";
import { useAuth } from "./AuthContext";
import { useEventStream } from "./EventStreamContext";

const TasksContext = createContext(null);
const PAGE_SIZE = 25;
const REFRESH_DEBOUNCE_MS = 200;

const DEFAULT_FILTERS = {
  status: "",
  lane: "",
  createdAfter: "",
  createdBefore: "",
  sortBy: "created_at",
  sortOrder: "desc",
};

export function TasksProvider({ children }) {
  const { apiKey } = useAuth();
  const { subscribe } = useEventStream();

  const [filters, setFiltersState] = useState(DEFAULT_FILTERS);
  const [tasks, setTasks] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [hasMore, setHasMore] = useState(false);

  const countRef = useRef(PAGE_SIZE);
  const debounceRef = useRef(null);
  const requestIdRef = useRef(0);

  // Re-evaluates membership against the CURRENT filters by re-querying the
  // server -- never patches rows already rendered. That's what makes a
  // task appear/disappear correctly when a live event changes whether it
  // still matches (CLAUDE.md/brief: "do not just patch the status of rows
  // already rendered").
  const runFetch = useCallback(
    async (count, { silent = false } = {}) => {
      const requestId = ++requestIdRef.current;
      if (!silent) setLoading(true);
      try {
        const result = await listTasks(apiKey, { ...filters, limit: count, offset: 0 });
        if (requestId !== requestIdRef.current) return; // superseded by a newer fetch
        setTasks(result);
        setHasMore(result.length >= count);
        setError(null);
      } catch (err) {
        if (requestId !== requestIdRef.current) return;
        if (err instanceof ApiError && err.status !== 401) setError(err.detail || err.message);
      } finally {
        if (requestId === requestIdRef.current) setLoading(false);
      }
    },
    [apiKey, filters]
  );

  // Filters changed -> reset pagination and refetch immediately.
  useEffect(() => {
    countRef.current = PAGE_SIZE;
    runFetch(PAGE_SIZE);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters, apiKey]);

  // Live events drive re-fetches, debounced to coalesce bursts (e.g. many
  // concurrent completions landing within milliseconds of each other).
  // A fresh stream connection (__stream_connected__) refetches immediately
  // and un-debounced -- it's the authoritative resync point on reconnect.
  useEffect(() => {
    return subscribe((event) => {
      if (event.type === "__stream_connected__") {
        clearTimeout(debounceRef.current);
        runFetch(countRef.current, { silent: true });
        return;
      }
      clearTimeout(debounceRef.current);
      debounceRef.current = setTimeout(() => {
        runFetch(countRef.current, { silent: true });
      }, REFRESH_DEBOUNCE_MS);
    });
  }, [subscribe, runFetch]);

  useEffect(() => () => clearTimeout(debounceRef.current), []);

  const setFilters = useCallback((patch) => {
    setFiltersState((prev) => ({ ...prev, ...patch }));
  }, []);

  const loadMore = useCallback(() => {
    countRef.current += PAGE_SIZE;
    runFetch(countRef.current);
  }, [runFetch]);

  const refetch = useCallback(() => runFetch(countRef.current, { silent: true }), [runFetch]);

  const value = useMemo(
    () => ({ filters, setFilters, tasks, loading, error, hasMore, loadMore, refetch }),
    [filters, setFilters, tasks, loading, error, hasMore, loadMore, refetch]
  );

  return <TasksContext.Provider value={value}>{children}</TasksContext.Provider>;
}

export function useTasks() {
  const ctx = useContext(TasksContext);
  if (!ctx) throw new Error("useTasks must be used within TasksProvider");
  return ctx;
}
