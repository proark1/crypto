/**
 * Subscribe to a CSS media query from React. Used to swap dense tables for
 * stacked cards on narrow screens by rendering one layout or the other, rather
 * than shipping both and toggling with CSS — which would put duplicate content
 * in the DOM. Falls back to `false` (the desktop layout) when `matchMedia` is
 * unavailable, as in the jsdom test environment, so components stay testable.
 */
import { useEffect, useState } from "react";

function read(query: string): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function"
    ? window.matchMedia(query).matches
    : false;
}

export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => read(query));
  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return;
    }
    const mql = window.matchMedia(query);
    const onChange = () => {
      setMatches(mql.matches);
    };
    // Sync once on mount in case the query changed between render and effect.
    setMatches(mql.matches);
    mql.addEventListener("change", onChange);
    return () => {
      mql.removeEventListener("change", onChange);
    };
  }, [query]);
  return matches;
}
