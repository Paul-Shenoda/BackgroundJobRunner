import { createContext, useContext, useEffect, useMemo, useState } from "react";

const ThemeContext = createContext(null);

// Session-only, in-memory state -- deliberately NOT persisted to
// localStorage (the brief reserves localStorage for the API key only).
// Defaults to "system" (follows prefers-color-scheme, see index.css);
// picking Light/Dark in Settings overrides that for the current tab
// only and resets to "system" on next load.
export function ThemeProvider({ children }) {
  const [theme, setTheme] = useState("system");

  useEffect(() => {
    if (theme === "system") {
      delete document.documentElement.dataset.theme;
    } else {
      document.documentElement.dataset.theme = theme;
    }
  }, [theme]);

  const value = useMemo(() => ({ theme, setTheme }), [theme]);

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme() {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error("useTheme must be used within ThemeProvider");
  return ctx;
}
