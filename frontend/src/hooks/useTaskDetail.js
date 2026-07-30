import { useCallback, useEffect, useRef, useState } from "react";
import { getTaskDetail, cancelTask, retryTask, collectResult, ApiError } from "../lib/apiClient";
import { useAuth } from "../context/AuthContext";
import { useEventStream } from "../context/EventStreamContext";
import { useTasks } from "../context/TasksContext";

// Keeps a single task's detail live while a panel/screen has it open --
// the brief's single best live-update demo (watching running -> ready
// with state history growing) depends on this NOT requiring a
// close-and-reopen to see the next state.
//
// taskKey disambiguates SELECTION from the actual API call: GET
// /tasks/{handle} only ever takes a handle (the fixed contract has no
// way to address one specific historical row when two share a handle --
// see DECISIONS.md, "a handle identifies an active task, not a task").
// Switching between two rows that happen to share a handle would
// otherwise not reset loading/task state at all, since `handle` itself
// wouldn't have changed -- taskKey (handle+created_at) is what forces
// that reset even when the handle string is identical.
export function useTaskDetail(handle, taskKey = handle) {
  const { apiKey } = useAuth();
  const { subscribe } = useEventStream();
  const { refetch: refetchList } = useTasks();
  const [task, setTask] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [actionError, setActionError] = useState(null);
  const requestIdRef = useRef(0);

  const refetch = useCallback(async () => {
    if (!handle) return;
    const requestId = ++requestIdRef.current;
    try {
      const detail = await getTaskDetail(apiKey, handle);
      if (requestId !== requestIdRef.current) return;
      setTask(detail);
      setError(null);
    } catch (err) {
      if (requestId !== requestIdRef.current) return;
      if (err instanceof ApiError && err.status === 404) {
        setError("This task no longer exists.");
      } else if (!(err instanceof ApiError) || err.status !== 401) {
        setError(err.detail || err.message);
      }
    } finally {
      if (requestId === requestIdRef.current) setLoading(false);
    }
  }, [apiKey, handle]);

  useEffect(() => {
    setLoading(true);
    setTask(null);
    setError(null);
    refetch();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refetch, taskKey]);

  useEffect(() => {
    if (!handle) return;
    return subscribe((event) => {
      if (event.type === "__stream_connected__" || event.handle === handle) {
        refetch();
      }
    });
  }, [handle, subscribe, refetch]);

  const runAction = useCallback(
    async (fn) => {
      setActionError(null);
      try {
        const updated = await fn(apiKey, handle);
        setTask((prev) => (prev ? { ...prev, ...updated } : updated));
        // Collect and retry deliberately emit no SSE event (DECISIONS.md:
        // `collected` is a flag, not a status transition; a retry emits
        // nothing per the fixed 4-event contract) -- the list view's own
        // refresh is entirely event-driven, so without this it would show
        // stale data (e.g. a missing "collected" checkmark) until some
        // UNRELATED event happened to trigger a refetch. Refreshing the
        // list directly here closes that gap regardless of which action
        // ran or whether the backend happens to broadcast it.
        refetchList();
      } catch (err) {
        if (err instanceof ApiError && err.status === 409) {
          setActionError(err.detail || "That action isn't valid for this task right now.");
        } else if (!(err instanceof ApiError) || err.status !== 401) {
          setActionError(err.detail || err.message);
        }
        // A follow-up refetch shows the server's real current state
        // either way, rather than leaving stale local state on screen.
        refetch();
      }
    },
    [apiKey, handle, refetch, refetchList]
  );

  return {
    task,
    loading,
    error,
    actionError,
    clearActionError: () => setActionError(null),
    cancel: () => runAction(cancelTask),
    retry: () => runAction(retryTask),
    collect: () => runAction(collectResult),
  };
}
