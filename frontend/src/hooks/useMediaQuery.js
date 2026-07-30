import { useEffect, useState } from "react";

export function useMediaQuery(query) {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches);

  useEffect(() => {
    const mql = window.matchMedia(query);
    const handler = (e) => setMatches(e.matches);
    mql.addEventListener("change", handler);
    setMatches(mql.matches);
    return () => mql.removeEventListener("change", handler);
  }, [query]);

  return matches;
}

// Three breakpoints, same components re-flowed: phone <768, tablet
// 768-1023 (icon rail, no two-pane split), desktop >=1024 (full sidebar,
// two-pane list+detail).
export function useBreakpoint() {
  const isDesktop = useMediaQuery("(min-width: 1024px)");
  const isTablet = useMediaQuery("(min-width: 768px) and (max-width: 1023px)");
  const isPhone = !isDesktop && !isTablet;
  return { isPhone, isTablet, isDesktop };
}
