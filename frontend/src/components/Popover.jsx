import { useEffect, useRef, useState } from "react";

// Minimal trigger+panel popover -- no library needed for two small uses
// (Filters, Sort). `trigger` and `children` are render props so the
// content can close itself (e.g. after picking a value).
export default function Popover({ trigger, children, align = "left" }) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef(null);

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
        <div
          className={
            "absolute top-full z-30 mt-2 " + (align === "right" ? "right-0" : "left-0")
          }
        >
          {children(() => setOpen(false))}
        </div>
      )}
    </div>
  );
}
