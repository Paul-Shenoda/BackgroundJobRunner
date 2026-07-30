import { useState } from "react";
import { useNav } from "../../context/NavContext";
import { useAuth } from "../../context/AuthContext";
import { useLanes } from "../../hooks/useLanes";
import { submitTask, ApiError } from "../../lib/apiClient";
import { IconClose } from "../icons";

const inputClass =
  "rounded-lg border border-[color:var(--color-hairline)] bg-[color:var(--color-void)] px-3 py-2 text-sm text-[color:var(--color-ink)] outline-none focus:border-[color:var(--color-accent)]";

const FAILURE_MODES = [
  { value: "none", label: "No failure" },
  { value: "retryable", label: "Fail (retryable)" },
  { value: "permanent", label: "Fail permanently" },
];

export default function NewTaskSheet() {
  const { newTaskOpen, setNewTaskOpen } = useNav();
  const { apiKey } = useAuth();
  const lanes = useLanes();

  const [lane, setLane] = useState("");
  const [durationMs, setDurationMs] = useState(5000);
  // A three-way radio, not two independent toggles: the mock worker only
  // has one real failure outcome per run (fail_permanent takes precedence
  // when both are set -- see engine/workers/mock.py), so "both on" was a
  // representable-but-meaningless state. A radio removes it structurally
  // instead of validating against it after the fact.
  const [failureMode, setFailureMode] = useState("none");
  const [rawParams, setRawParams] = useState("");
  const [error, setError] = useState(null);
  const [submitting, setSubmitting] = useState(false);

  if (!newTaskOpen) return null;

  const effectiveLane = lane || lanes[0] || "";

  function close() {
    setNewTaskOpen(false);
    setError(null);
  }

  async function handleSubmit(e) {
    e.preventDefault();
    setError(null);

    let params = { duration_ms: Number(durationMs) || 0 };
    if (failureMode === "retryable") params.fail = true;
    if (failureMode === "permanent") params.fail_permanent = true;

    if (rawParams.trim()) {
      try {
        params = { ...params, ...JSON.parse(rawParams) };
      } catch {
        setError("Extra params must be valid JSON.");
        return;
      }
    }

    if (!effectiveLane) {
      setError("Choose a lane.");
      return;
    }

    setSubmitting(true);
    try {
      await submitTask(apiKey, effectiveLane, params);
      close();
      setFailureMode("none");
      setRawParams("");
    } catch (err) {
      setError(err instanceof ApiError ? err.detail || err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="fixed inset-0 z-40 flex items-end justify-center bg-black/50 sm:items-center" onClick={close}>
      <form
        onClick={(e) => e.stopPropagation()}
        onSubmit={handleSubmit}
        className="flex w-full max-w-md flex-col gap-4 rounded-t-2xl border border-[color:var(--color-hairline)] bg-[color:var(--color-surface)] p-5 sm:rounded-2xl"
      >
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-[color:var(--color-ink)]">New task</h2>
          <button type="button" onClick={close} className="text-[color:var(--color-ink-dim)]">
            <IconClose />
          </button>
        </div>

        <label className="flex flex-col gap-1.5 text-xs text-[color:var(--color-ink-dim)]">
          Lane
          <select value={effectiveLane} onChange={(e) => setLane(e.target.value)} className={inputClass}>
            {lanes.length === 0 && <option value="">Loading lanes…</option>}
            {lanes.map((l) => (
              <option key={l} value={l}>
                {l}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1.5 text-xs text-[color:var(--color-ink-dim)]">
          Duration (ms)
          <input
            type="number"
            min="0"
            value={durationMs}
            onChange={(e) => setDurationMs(e.target.value)}
            className={inputClass}
          />
        </label>

        <fieldset className="flex flex-col gap-2">
          <legend className="mb-0.5 text-xs text-[color:var(--color-ink-dim)]">Failure mode</legend>
          {FAILURE_MODES.map((opt) => (
            <label
              key={opt.value}
              className="flex cursor-pointer items-center gap-2 text-xs text-[color:var(--color-ink)]"
            >
              <input
                type="radio"
                name="failureMode"
                value={opt.value}
                checked={failureMode === opt.value}
                onChange={() => setFailureMode(opt.value)}
                className="h-3.5 w-3.5 accent-[color:var(--color-accent)]"
              />
              {opt.label}
            </label>
          ))}
        </fieldset>

        <label className="flex flex-col gap-1.5 text-xs text-[color:var(--color-ink-dim)]">
          Extra params (JSON, optional)
          <textarea
            value={rawParams}
            onChange={(e) => setRawParams(e.target.value)}
            placeholder="{}"
            rows={2}
            className={inputClass + " font-mono-tabular"}
          />
        </label>

        {error && <p className="text-xs text-[color:var(--color-failed)]">{error}</p>}

        <button
          type="submit"
          disabled={submitting}
          className="rounded-lg bg-[color:var(--color-accent)] px-3 py-2.5 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
        >
          {submitting ? "Submitting…" : "Submit task"}
        </button>
      </form>
    </div>
  );
}
