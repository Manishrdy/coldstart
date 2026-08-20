"use strict";

/* Three-state theme control, shared by the dashboard and the template preview.
 *
 * "System" is a real, selectable state rather than the absence of a choice —
 * so someone who set dark at 11pm can get back to following their OS without
 * having to guess how. The choice lives in localStorage and is applied before
 * first paint by a tiny inline script in each page's <head>, which is what
 * stops the light-theme flash on load. */

(() => {
  const KEY = "coldstart-theme";
  const root = document.documentElement;

  const apply = choice => {
    if (choice === "light" || choice === "dark") root.dataset.theme = choice;
    else delete root.dataset.theme;
    for (const button of document.querySelectorAll("[data-theme-choice]")) {
      button.setAttribute("aria-pressed", String(button.dataset.themeChoice === choice));
    }
  };

  const current = () => {
    const saved = localStorage.getItem(KEY);
    return saved === "light" || saved === "dark" ? saved : "system";
  };

  for (const button of document.querySelectorAll("[data-theme-choice]")) {
    button.addEventListener("click", () => {
      const choice = button.dataset.themeChoice;
      if (choice === "system") localStorage.removeItem(KEY);
      else localStorage.setItem(KEY, choice);
      apply(choice);
    });
  }

  apply(current());

  // Another tab changed the setting — stay in sync rather than diverging.
  window.addEventListener("storage", e => { if (e.key === KEY) apply(current()); });
})();
