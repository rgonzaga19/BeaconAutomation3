/*
 * Shared light/dark theme handling for every Beabots window
 * (dashboard, CF2, Upload SOA, Settings, About).
 *
 * Load this <script> first in <head>, before theme.css's stylesheet
 * link if possible, so data-theme is set on <html> before first paint.
 *
 * Depends on nothing else. If window.beabots exposes a setTheme()
 * method (via preload.js/main.js) it will be called too, so the main
 * process can broadcast the change to any other open windows — but
 * this file works fine without it, using localStorage + the
 * `storage` event as the sync mechanism between windows.
 */

(function () {
  const STORAGE_KEY = "beabots-theme";
  const DEFAULT_THEME = "dark";

  function getStoredTheme() {
    try {
      return localStorage.getItem(STORAGE_KEY);
    } catch (e) {
      return null;
    }
  }

  function updateToggleIcon(theme) {
    const btn = document.getElementById("btnThemeToggle");
    if (!btn) return;
    const isLight = theme === "light";
    const icon = btn.querySelector(".theme-switch-icon");
    const label = btn.querySelector(".theme-switch-label");
    const actionLabel = isLight ? "Switch to dark mode" : "Switch to light mode";

    if (icon) {
      icon.textContent = isLight ? "☀" : "🌙";
      if (label) label.textContent = isLight ? "Light" : "Dark";
      btn.setAttribute("aria-checked", String(isLight));
      btn.setAttribute("aria-label", actionLabel);
    } else {
      btn.textContent = isLight ? "☀" : "🌙";
    }
    btn.title = actionLabel;
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    updateToggleIcon(theme);
  }

  function setTheme(theme) {
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch (e) {
      // localStorage unavailable — theme still applies for this window,
      // it just won't persist or sync to other windows.
    }
    applyTheme(theme);
    window.beabots?.setTheme?.(theme);
  }

  function toggleTheme() {
    const current = document.documentElement.getAttribute("data-theme") || DEFAULT_THEME;
    setTheme(current === "dark" ? "light" : "dark");
  }

  // Apply immediately — don't wait for DOMContentLoaded — to avoid a
  // flash of the wrong theme.
  applyTheme(getStoredTheme() || DEFAULT_THEME);

  // Reconcile with the main process's persisted value. This is the
  // authoritative source (survives app restarts, and covers windows
  // whose localStorage happens to be empty or stale) — the localStorage
  // read above is just for an instant, flash-free first paint.
  window.beabots?.getTheme?.().then((theme) => {
    if (theme && theme !== getStoredTheme()) {
      try {
        localStorage.setItem(STORAGE_KEY, theme);
      } catch (e) {
        // ignore — theme still applies for this window
      }
      applyTheme(theme);
    }
  });

  // If another open window changes the theme, follow it.
  window.addEventListener("storage", (e) => {
    if (e.key === STORAGE_KEY && e.newValue) applyTheme(e.newValue);
  });

  // If main.js broadcasts a theme change via preload (optional; only
  // fires if window.beabots.onThemeChanged exists).
  window.beabots?.onThemeChanged?.((theme) => applyTheme(theme));

  // Optional richer control on the Settings page: a DARK/LIGHT toggle-track,
  // built the same way as the existing S2/S4 server toggle. Only wires up
  // if that markup exists on the current page.
  function updateAppearanceRow(theme) {
    const track = document.getElementById("appearanceToggleTrack");
    if (!track) return;
    track.classList.toggle("on", theme === "light");
    document.getElementById("labelDark")?.classList.toggle("active", theme === "dark");
    document.getElementById("labelLight")?.classList.toggle("active", theme === "light");
  }

  document.addEventListener("DOMContentLoaded", () => {
    const current = document.documentElement.getAttribute("data-theme") || DEFAULT_THEME;
    updateToggleIcon(current);
    updateAppearanceRow(current);

    document.getElementById("btnThemeToggle")?.addEventListener("click", toggleTheme);

    document.getElementById("appearanceToggleTrack")?.addEventListener("click", () => {
      toggleTheme();
      updateAppearanceRow(document.documentElement.getAttribute("data-theme") || DEFAULT_THEME);
    });
    document.getElementById("labelDark")?.addEventListener("click", () => {
      setTheme("dark");
      updateAppearanceRow("dark");
    });
    document.getElementById("labelLight")?.addEventListener("click", () => {
      setTheme("light");
      updateAppearanceRow("light");
    });
  });

  // Exposed in case Settings wants a proper radio/select instead of
  // just the titlebar toggle.
  window.beabotsTheme = {
    setTheme,
    toggleTheme,
    getTheme: () => document.documentElement.getAttribute("data-theme") || DEFAULT_THEME,
  };
})();
