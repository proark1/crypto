import { useCallback, useEffect, useState } from "react";

import { applyTheme, initialTheme, storeTheme, type Theme } from "./lib/theme";
import { OverviewScreen } from "./screens/OverviewScreen";

export function App() {
  const [theme, setTheme] = useState<Theme>(initialTheme);

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  const toggleTheme = useCallback(() => {
    setTheme((current) => {
      const next: Theme = current === "dark" ? "light" : "dark";
      // Persist only explicit choices; until then the system preference rules.
      storeTheme(next);
      return next;
    });
  }, []);

  return (
    <div className="min-h-screen overflow-x-hidden bg-zinc-50 text-zinc-900 dark:bg-zinc-950 dark:text-zinc-100">
      <OverviewScreen theme={theme} onToggleTheme={toggleTheme} />
    </div>
  );
}
