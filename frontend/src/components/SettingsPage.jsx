import { useState } from "react";
import { useAuth } from "../context/AuthContext";
import { useTheme } from "../context/ThemeContext";

const THEME_OPTIONS = [
  { value: "system", label: "System" },
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
];

export default function SettingsPage() {
  const { apiKey, authError, verifyAndSetKey, clearKey } = useAuth();
  const { theme, setTheme } = useTheme();
  const [value, setValue] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function handleChange(e) {
    e.preventDefault();
    setSubmitting(true);
    const ok = await verifyAndSetKey(value);
    setSubmitting(false);
    if (ok) setValue("");
  }

  return (
    <div className="mx-auto flex max-w-md flex-col gap-6 p-6">
      <h1 className="text-lg font-semibold text-[color:var(--color-ink)]">Settings</h1>

      <div className="flex flex-col gap-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-[color:var(--color-ink-faint)]">
          Appearance
        </span>
        <div className="inline-flex w-fit rounded-lg border border-[color:var(--color-hairline)] bg-[color:var(--color-surface)] p-1">
          {THEME_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              onClick={() => setTheme(opt.value)}
              className={
                "rounded-md px-3 py-1.5 text-xs font-medium transition-colors " +
                (theme === opt.value
                  ? "bg-[color:var(--color-accent)] text-white"
                  : "text-[color:var(--color-ink-dim)] hover:text-[color:var(--color-ink)]")
              }
            >
              {opt.label}
            </button>
          ))}
        </div>
        <p className="text-xs text-[color:var(--color-ink-faint)]">
          "System" follows your OS setting. Light/Dark apply only to this session and reset on
          next load.
        </p>
      </div>

      <div className="flex flex-col gap-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-[color:var(--color-ink-faint)]">
          Current key
        </span>
        <span className="rounded-lg border border-[color:var(--color-hairline)] bg-[color:var(--color-surface)] px-3 py-2 font-mono-tabular text-sm text-[color:var(--color-ink)]">
          {apiKey}
        </span>
        <button
          onClick={clearKey}
          className="self-start rounded-lg border border-[color:var(--color-hairline)] px-3 py-1.5 text-xs text-[color:var(--color-ink-dim)] hover:bg-[color:var(--color-surface)]"
        >
          Clear key
        </button>
      </div>

      <form onSubmit={handleChange} className="flex flex-col gap-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-[color:var(--color-ink-faint)]">
          Change key
        </span>
        <input
          type="text"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="X-API-Key"
          className="rounded-lg border border-[color:var(--color-hairline)] bg-[color:var(--color-surface)] px-3 py-2 font-mono-tabular text-sm text-[color:var(--color-ink)] outline-none focus:border-[color:var(--color-accent)]"
        />
        {authError && <p className="text-xs text-[color:var(--color-failed)]">{authError}</p>}
        <button
          type="submit"
          disabled={submitting}
          className="self-start rounded-lg bg-[color:var(--color-accent)] px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
        >
          {submitting ? "Checking…" : "Switch key"}
        </button>
      </form>
    </div>
  );
}
