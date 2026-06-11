import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import "./index.css";
import { applyTheme, initialTheme } from "./lib/theme";

// Apply the theme before the first paint so a dark-preference user never
// sees a white flash (and vice versa). App owns the toggle from here on.
applyTheme(initialTheme());

const rootElement = document.getElementById("root");
if (rootElement === null) {
  throw new Error("missing #root element");
}
createRoot(rootElement).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
