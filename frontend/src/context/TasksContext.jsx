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
// Mirrors GET /tasks' server-side bound (api/routes.py: limit le=200).
// Growing past it would turn every subsequent fetch into a 422 and put the
// whole list into its error state; clamping here means a very deep list
// simply stops offering "Load more" at the cap instead.
const API_MAX_LIMIT = 200;
const REFRESH_DEBOUNCE_MS = 200;
// Only four transitions are SSE events at all (accepted/ready/failed/
// cancelled -- the fixed contract, CLAUDE.md). queued -> running (a claim)
// and the automatic retry requeue (running -> queued, see DECISIONS.md "A
// retry emits no lifecycle event") are both real, meaningful transitions
// with no event of their own -- a task cycling through retries can sit
// through several of them with nothing ever telling this list to refetch,
// which reads as the badge being stuck rather than as "no news." This
// poll is the fallback for exactly that gap: while anything visible is
// still non-terminal, nudge the list on a short timer regardless of
// whether an event fired. Idle once everything's queued/running-free, so
// a quiet dashboard isn't polling for no reason.
const IN_FLIGHT_POLL_MS = 2000;

const DEFAULT_FILTERS = {
  status: [], // [] = "All"; a list of Status values otherwise (see FiltersBar)
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
        setHasMore(result.length >= count && count < API_MAX_LIMIT);
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

  // Fallback poll for the two eventless transitions (see IN_FLIGHT_POLL_MS
  // above). Reads `tasks` fresh each tick via a ref rather than depending
  // on it directly -- depending on `tasks` would tear down and rebuild
  // this interval on every single refetch (including the poll's own),
  // which defeats a steady timer.
  const tasksRef = useRef(tasks);
  tasksRef.current = tasks;
  useEffect(() => {
    const id = setInterval(() => {
      const hasInFlight = tasksRef.current.some(
        (t) => t.status === "queued" || t.status === "running"
      );
      if (hasInFlight) runFetch(countRef.current, { silent: true });
    }, IN_FLIGHT_POLL_MS);
    return () => clearInterval(id);
  }, [runFetch]);

  const setFilters = useCallback((patch) => {
    setFiltersState((prev) => ({ ...prev, ...patch }));
  }, []);

  const loadMore = useCallback(() => {
    countRef.current = Math.min(countRef.current + PAGE_SIZE, API_MAX_LIMIT);
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
