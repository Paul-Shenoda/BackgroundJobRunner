import { useEffect, useLayoutEffect, useRef, useState } from "react";

// Minimal trigger+panel popover -- no library needed for two small uses
// (Filters, Sort). `trigger` and `children` are render props so the
// content can close itself (e.g. after picking a value).
//
// Position is computed from real geometry, not a static left-0/right-0
// CSS anchor: a fixed CSS corner works only when the trigger sits at the
// edge of its container. Here the trigger can sit anywhere in a row
// that's itself narrower than the panel would naturally need (the
// desktop two-pane layout's 420px list column) -- anchoring to either
// edge of the TRIGGER just moves which side overflows, since the trigger
// isn't at either edge of the column. The panel is anchored under the
// trigger by default, then shifted left only as far as needed to stay
// inside the nearest ancestor carrying `data-popover-boundary` (falling
// back to the viewport if none is found), and never shifted past that
// boundary's own left edge either.
export default function Popover({ trigger, children }) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef(null);
  const panelRef = useRef(null);
  const [left, setLeft] = useState(0);

  useLayoutEffect(() => {
    if (!open) return;
    const triggerEl = rootRef.current;
    const panelEl = panelRef.current;
    if (!triggerEl || !panelEl) return;

    const triggerRect = triggerEl.getBoundingClientRect();
    const boundaryEl = triggerEl.closest("[data-popover-boundary]");
    const boundary = boundaryEl
      ? boundaryEl.getBoundingClientRect()
      : { left: 0, right: window.innerWidth };

    // A real safety margin, not an exact-fit clamp: `boundary.right -
    // panelWidth` alone would put the panel's edge EXACTLY on the
    // boundary's edge with zero margin by construction -- verified this
    // was the actual cause of a near-invisible 1-6px gap a user reported
    // as "still broken" (measured precisely from their screenshots, not
    // eyeballed). The row-padding fix earlier hit the identical lesson:
    // an exact fit reads as broken and is fragile to any small variation
    // (different date values, font metrics, browser). 12px is comfortably
    // visible without being wasteful.
    const MARGIN = 12;
    const panelWidth = panelEl.offsetWidth;
    const naturalLeft = triggerRect.left; // anchored under the trigger, same as before
    const minLeft = boundary.left + MARGIN;
    const maxLeft = boundary.right - panelWidth - MARGIN;
    const clampedLeft = Math.max(minLeft, Math.min(naturalLeft, maxLeft));
    setLeft(clampedLeft - triggerRect.left); // as an offset from the trigger's own position
  }, [open]);

  useEffect(() => {
    if (!open) return;
    function handlePointerDown(e) {
      if (rootRef.current && !rootRef.current.contains(e.target)) setOpen(false);
    }
    function handleKeyDown(e) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [open]);

  return (
    <div className="relative" ref={rootRef}>
      {trigger(() => setOpen((o) => !o), open)}
      {open && (
        <div ref={panelRef} className="absolute top-full z-30 mt-2" style={{ left }}>
          {children(() => setOpen(false))}
        </div>
      )}
    </div>
  );
}
