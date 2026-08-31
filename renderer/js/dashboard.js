/*
 * Dashboard renderer logic for the collapsible navigation workspace.
 */

// API_BASE, fetchJSON, showModal, and showError all live in common.js (loaded before this file).

// ---------------------------------------------------------------------------
// Title bar controls
// ---------------------------------------------------------------------------
document.getElementById("btnMinimize").addEventListener("click", () => {
  window.beabots?.minimize();
});
document.getElementById("btnClose").addEventListener("click", () => {
  window.beabots?.close();
});

// ---------------------------------------------------------------------------
// Toolbar navigation
// ---------------------------------------------------------------------------
const sidebar = document.getElementById("dashboardSidebar");
const sidebarToggle = document.getElementById("btnSidebarToggle");

sidebarToggle.addEventListener("click", () => {
  const collapsed = sidebar.classList.toggle("collapsed");
  sidebarToggle.setAttribute("aria-expanded", String(!collapsed));
  sidebarToggle.setAttribute("aria-label", collapsed ? "Expand sidebar" : "Collapse sidebar");
  window.beabots?.setWorkspaceSidebarWidth(collapsed ? 68 : 232);
});

const workspaceButtons = {
  cf2: document.getElementById("btnCf2"),
  uploadSoa: document.getElementById("btnUploadSoa"),
  cf4: document.getElementById("btnCf4"),
  settings: document.getElementById("btnSettings"),
  about: document.getElementById("btnAbout"),
};

window.beabots?.onWorkspaceActive((activeKey) => {
  Object.entries(workspaceButtons).forEach(([key, button]) => {
    button.classList.toggle("active", key === activeKey);
    button.setAttribute("aria-current", key === activeKey ? "page" : "false");
  });
});

document.getElementById("btnDashboardHome").addEventListener("click", () => {
  window.beabots?.showWorkspaceHome();
});

document.getElementById("btnCf2").addEventListener("click", () => {
  window.beabots?.openCf2Window();
});
document.getElementById("btnUploadSoa").addEventListener("click", () => {
  window.beabots?.openUploadSoaWindow();
});
document.getElementById("btnCf4").addEventListener("click", () => {
  window.beabots?.openCf4Window();
});
document.getElementById("btnSettings").addEventListener("click", () => {
  window.beabots?.openSettingsWindow();
});
document.getElementById("btnLogout").addEventListener("click", () => {
  window.beabots?.logout();
});
document.getElementById("btnAbout").addEventListener("click", () => {
  window.beabots?.openAboutWindow();
});

// ---------------------------------------------------------------------------
// Initial state and version check
// ---------------------------------------------------------------------------
const appVersionLabel = document.getElementById("appVersion");
const updateStatusLabel = document.getElementById("updateStatus");

async function checkForUpdates() {
  const currentVersion = await window.beabots.getVersion();
  appVersionLabel.textContent = `v${currentVersion}`;

  updateStatusLabel.textContent = "Checking...";
  updateStatusLabel.className = "version checking";

  try {
    const latest = await window.beabots.checkForUpdates();

    if (latest.version !== currentVersion) {
      updateStatusLabel.textContent = "⬇ Update Available";
      updateStatusLabel.className = "version update-available";
    } else {
      updateStatusLabel.textContent = "✓ Up To Date";
      updateStatusLabel.className = "version up-to-date";
    }
  } catch {
    updateStatusLabel.textContent = "⚠ Offline";
    updateStatusLabel.className = "version offline";
  }
}

checkForUpdates();
