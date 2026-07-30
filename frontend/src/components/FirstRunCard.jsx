import { useState } from "react";
import { useAuth } from "../context/AuthContext";

const HINTS = [
  { label: "Alice", key: "alice-demo-key" },
  { label: "Bob", key: "bob-demo-key" },
];

export default function FirstRunCard() {
  const { authError, verifyAndSetKey } = useAuth();
  const [value, setValue] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    setSubmitting(true);
    await verifyAndSetKey(value);
    setSubmitting(false);
  }

  return (
    <div className="flex min-h-dvh items-center justify-center bg-[color:var(--color-void)] px-4">
      <div className="w-full max-w-sm rounded-2xl border border-[color:var(--color-hairline)] bg-[color:var(--color-surface)] p-8 shadow-2xl shadow-black/40">
        <div className="mb-1 text-lg font-semibold text-[color:var(--color-ink)]">Job Runner</div>
        <p className="mb-6 text-sm text-[color:var(--color-ink-dim)]">
          Enter an API key to see your tasks. Keys are per-user and never rotate.
        </p>

        <form onSubmit={handleSubmit} className="flex flex-col gap-3">
          <input
            autoFocus
            type="text"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder="X-API-Key"
            className="rounded-lg border border-[color:var(--color-hairline)] bg-[color:var(--color-void)] px-3 py-2.5 font-mono-tabular text-sm text-[color:var(--color-ink)] outline-none placeholder:text-[color:var(--color-ink-faint)] focus:border-[color:var(--color-accent)]"
          />
          {authError && <p className="text-xs text-[color:var(--color-failed)]">{authError}</p>}
          <button
            type="submit"
            disabled={submitting}
            className="rounded-lg bg-[color:var(--color-accent)] px-3 py-2.5 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
          >
            {submitting ? "Checking…" : "Continue"}
          </button>
        </form>

        <div className="mt-6 border-t border-[color:var(--color-hairline)] pt-4">
          <p className="mb-2 text-xs uppercase tracking-wide text-[color:var(--color-ink-faint)]">
            Demo keys
          </p>
          <div className="flex flex-col gap-1.5">
            {HINTS.map((hint) => (
              <button
                key={hint.key}
                type="button"
                onClick={() => setValue(hint.key)}
                className="flex items-center justify-between rounded-lg border border-[color:var(--color-hairline)] px-3 py-2 text-left text-xs transition-colors hover:bg-[color:var(--color-surface-raised)]"
              >
                <span className="text-[color:var(--color-ink-dim)]">{hint.label}</span>
                <span className="font-mono-tabular text-[color:var(--color-ink)]">{hint.key}</span>
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
