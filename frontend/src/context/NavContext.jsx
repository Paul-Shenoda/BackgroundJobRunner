import { createContext, useContext, useMemo, useState, useCallback } from "react";

const NavContext = createContext(null);

// Two nav destinations (Tasks, Settings) plus a "detail open" flag, all
// as plain state -- no router dependency needed for a two-destination app.
export function NavProvider({ children }) {
  const [view, setView] = useState("tasks");
  // {handle, createdAt} | null -- NOT just a handle string. Handles
  // recycle (CLAUDE.md): a handle identifies the currently ACTIVE task,
  // not a permanent identity, so GET /tasks can legitimately return two
  // rows sharing a handle (an old collected/cancelled one plus a new
  // active one, or two collected historical rows). created_at is what
  // makes selection/highlighting point at the one row actually clicked
  // instead of every row that happens to share its handle.
  const [selectedTask, setSelectedTask] = useState(null);
  const [newTaskOpen, setNewTaskOpen] = useState(false);

  // Accepts either a full task-like object ({handle, created_at}, from a
  // list row) or a bare handle string (from a toast, which only carries
  // the handle in its event payload). A bare handle has no row to
  // disambiguate against anyway -- a toast always refers to whatever
  // just transitioned, which is exactly what GET /tasks/{handle}'s
  // active-first/most-recent-fallback resolution already returns.
  const openTask = useCallback((taskOrHandle) => {
    setView("tasks");
    if (typeof taskOrHandle === "string") {
      setSelectedTask({ handle: taskOrHandle, createdAt: null, row: null });
    } else {
      // Keep the full clicked row, not just its identity: GET
      // /tasks/{handle} always resolves to the row currently holding the
      // handle (active-first), so when the clicked row is a HISTORICAL one
      // under a recycled handle, this stored row is the only source of
      // data the detail pane has for what the operator actually selected.
      setSelectedTask({
        handle: taskOrHandle.handle,
        createdAt: taskOrHandle.created_at,
        row: taskOrHandle,
      });
    }
  }, []);

  const closeDetail = useCallback(() => setSelectedTask(null), []);

  const value = useMemo(
    () => ({
      view,
      setView: (v) => {
        setView(v);
        if (v !== "tasks") setSelectedTask(null);
      },
      selectedTask,
      openTask,
      closeDetail,
      newTaskOpen,
      setNewTaskOpen,
    }),
    [view, selectedTask, openTask, closeDetail, newTaskOpen]
  );

  return <NavContext.Provider value={value}>{children}</NavContext.Provider>;
}

export function useNav() {
  const ctx = useContext(NavContext);
  if (!ctx) throw new Error("useNav must be used within NavProvider");
  return ctx;
}
