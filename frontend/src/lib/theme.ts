/**
 * Theme handling for the class-based dark mode (Tailwind `dark:` variant
 * scoped to the `.dark` class on <html>). The default follows the system
 * preference; an explicit choice from the toggle persists in localStorage
 * and wins over the system from then on.
 */

export type Theme = "light" | "dark";

const THEME_STORAGE_KEY = "tradebot_theme";

/** The user's explicit choice, or null when they never touched the toggle. */
export function getStoredTheme(): Theme | null {
  // localStorage can throw in restricted contexts (sandboxed iframes,
  // blocked storage); degrade to "no stored choice" instead of crashing.
  try {
    const stored = localStorage.getItem(THEME_STORAGE_KEY);
    return stored === "light" || stored === "dark" ? stored : null;
  } catch {
    return null;
  }
}

export function storeTheme(theme: Theme): void {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    // storage unavailable: the choice holds until reload
  }
}

/** What the operating system asks for; dark when undetectable (jsdom). */
export function systemTheme(): Theme {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return "dark";
  }
  return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

/** Explicit choice first, system preference otherwise. */
export function initialTheme(): Theme {
  return getStoredTheme() ?? systemTheme();
}

/** Flip the `.dark` class on <html>; every `dark:` style follows it. */
export function applyTheme(theme: Theme): void {
  document.documentElement.classList.toggle("dark", theme === "dark");
}
