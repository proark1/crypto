import { afterEach, describe, expect, it } from "vitest";

import {
  applyTheme,
  getStoredTheme,
  initialTheme,
  isDarkClassActive,
  storeTheme,
  systemTheme,
} from "./theme";

afterEach(() => {
  localStorage.clear();
  document.documentElement.classList.remove("dark");
});

describe("theme", () => {
  it("prefers the stored explicit choice over the system", () => {
    storeTheme("light");
    expect(initialTheme()).toBe("light");
    storeTheme("dark");
    expect(initialTheme()).toBe("dark");
  });

  it("ignores junk in storage and falls back to the system preference", () => {
    localStorage.setItem("tradebot_theme", "neon");
    expect(getStoredTheme()).toBeNull();
    // jsdom has no matchMedia: the fallback is dark, never a crash.
    expect(initialTheme()).toBe(systemTheme());
  });

  it("applies the theme by toggling the dark class on <html>", () => {
    applyTheme("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    applyTheme("light");
    expect(document.documentElement.classList.contains("dark")).toBe(false);
  });

  it("reports the active dark class, tracking applyTheme", () => {
    applyTheme("dark");
    expect(isDarkClassActive()).toBe(true);
    applyTheme("light");
    expect(isDarkClassActive()).toBe(false);
  });
});
