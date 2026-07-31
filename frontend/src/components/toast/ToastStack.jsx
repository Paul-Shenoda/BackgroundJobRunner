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
    <div className="pointer-events-none fixed inset-x-0 bottom-[calc(env(safe-area-inset-bottom)+64px)] z-50 flex flex-col items-end gap-2 px-4 md:bottom-4 md:gap-2.5 md:px-5 lg:gap-3 lg:px-6">
      {toasts.map((toast) => {
        const tone = TONE[toast.type] ?? TONE.ready;
        return (
          <div
            key={toast.id}
            // Three tiers, not two: phone/base stays as small as the
            // narrowest screen needs, tablet (md, 768-1023px per
            // useBreakpoint) gets a modest bump -- wider box and a touch
            // more padding, same text size -- and desktop (lg, 1024px+)
            // gets the bigger jump (including text-sm) from the earlier
            // "too small on desktop" pass. Tablet deliberately stops short
            // of desktop's size, not a rounding difference.
            className="pointer-events-auto flex w-full max-w-xs cursor-pointer items-center gap-2 rounded-lg border border-[color:var(--color-hairline)] bg-[color:var(--color-surface)] px-3 py-2.5 text-xs shadow-lg shadow-black/30 md:max-w-sm md:px-3.5 md:py-3 lg:max-w-md lg:gap-3 lg:rounded-xl lg:px-4 lg:py-3.5 lg:text-sm"
            onClick={() => {
              openTask(toast.handle);
              dismiss(toast.id);
            }}
          >
            <span
              className="h-2 w-2 shrink-0 rounded-full lg:h-2.5 lg:w-2.5"
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
              <IconClose className="lg:h-5 lg:w-5" />
            </button>
          </div>
        );
      })}
    </div>
  );
}
