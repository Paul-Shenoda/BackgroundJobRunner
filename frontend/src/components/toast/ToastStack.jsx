import { useToasts } from "../../context/ToastContext";
import { useNav } from "../../context/NavContext";
import { IconClose } from "../icons";

const TONE = {
  ready: { color: "var(--color-ready)", background: "var(--color-ready-soft)" },
  failed: { color: "var(--color-failed)", background: "var(--color-failed-soft)" },
  cancelled: { color: "var(--color-cancelled)", background: "var(--color-cancelled-soft)" },
};

// Bottom-right on desktop/tablet; on phone it sits above the bottom tab
// bar (safe-area + tab-bar height) so it never covers nav.
export default function ToastStack() {
  const { toasts, dismiss } = useToasts();
  const { openTask } = useNav();

  if (toasts.length === 0) return null;

  return (
    <div className="pointer-events-none fixed inset-x-0 bottom-[calc(env(safe-area-inset-bottom)+64px)] z-50 flex flex-col items-end gap-2 px-4 md:bottom-4">
      {toasts.map((toast) => {
        const tone = TONE[toast.type] ?? TONE.ready;
        return (
          <div
            key={toast.id}
            className="pointer-events-auto flex w-full max-w-xs cursor-pointer items-center gap-2 rounded-lg border border-[color:var(--color-hairline)] bg-[color:var(--color-surface)] px-3 py-2.5 text-xs shadow-lg shadow-black/30"
            onClick={() => {
              openTask(toast.handle);
              dismiss(toast.id);
            }}
          >
            <span
              className="h-2 w-2 shrink-0 rounded-full"
              style={{ background: tone.color }}
              aria-hidden
            />
            <span className="flex-1 text-[color:var(--color-ink)]">{toast.message}</span>
            <button
              onClick={(e) => {
                e.stopPropagation();
                dismiss(toast.id);
              }}
              className="shrink-0 text-[color:var(--color-ink-faint)] hover:text-[color:var(--color-ink)]"
            >
              <IconClose />
            </button>
          </div>
        );
      })}
    </div>
  );
}
