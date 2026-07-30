import { createContext, useContext, useMemo, useState, useCallback } from "react";

const NavContext = createContext(null);

// Two nav destinations (Tasks, Settings) plus a "detail open" flag, all
// as plain state -- no router dependency needed for a two-destination app.
export function NavProvider({ children }) {
  const [view, setView] = useState("tasks");
  const [selectedHandle, setSelectedHandle] = useState(null);
  const [newTaskOpen, setNewTaskOpen] = useState(false);

  const openTask = useCallback((handle) => {
    setView("tasks");
    setSelectedHandle(handle);
  }, []);

  const closeDetail = useCallback(() => setSelectedHandle(null), []);

  const value = useMemo(
    () => ({
      view,
      setView: (v) => {
        setView(v);
        if (v !== "tasks") setSelectedHandle(null);
      },
      selectedHandle,
      openTask,
      closeDetail,
      newTaskOpen,
      setNewTaskOpen,
    }),
    [view, selectedHandle, openTask, closeDetail, newTaskOpen]
  );

  return <NavContext.Provider value={value}>{children}</NavContext.Provider>;
}

export function useNav() {
  const ctx = useContext(NavContext);
  if (!ctx) throw new Error("useNav must be used within NavProvider");
  return ctx;
}
