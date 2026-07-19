/*
 * Dashboard renderer logic.
 *
 * Assumes a preload script exposes `window.beabots` with:
 *   - apiBase: string, e.g. "http://127.0.0.1:5417"  (the Flask server's URL)
 *   - minimize(), maximize(), close(): window chrome, calling the main
 *     process's BrowserWindow controls (replaces tkinter's minimize_window/
 *     toggle_maximize/close_window)
 *   - openCf2Window(), openUploadSoaWindow(), openSettingsWindow():
 *     ask the main process to open (or focus, if already open) those
 *     windows — replaces ui.py's _open_cf2_window()/_open_upload_soa_window()
 *     duplicate-window guards, which now live in the main process since it
 *     owns window lifecycles.
 *
 * These are stubs until main.js (the Electron main process) is built —
 * see the TODO markers below for exactly what each one needs to do.
 */

// API_BASE, fetchJSON, showModal, and showError all live in common.js (loaded before this file).

// ---------------------------------------------------------------------------
// Title bar controls
// ---------------------------------------------------------------------------
document.getElementById("btnMinimize").addEventListener("click", () => {
  window.beabots?.minimize();
});
document.getElementById("btnMaximize").addEventListener("click", () => {
  window.beabots?.maximize();
});
document.getElementById("btnClose").addEventListener("click", () => {
  window.beabots?.close();
});

// ---------------------------------------------------------------------------
// Toolbar navigation
// ---------------------------------------------------------------------------
document.getElementById("btnCf2").addEventListener("click", () => {
  window.beabots?.openCf2Window();
});
document.getElementById("btnUploadSoa").addEventListener("click", () => {
  window.beabots?.openUploadSoaWindow();
});
document.getElementById("btnSettings").addEventListener("click", () => {
  window.beabots?.openSettingsWindow();
});
document.getElementById("btnAbout").addEventListener("click", showAboutModal);

// ---------------------------------------------------------------------------
// About modal (dashboard-specific — uses the shared showModal from common.js)
// ---------------------------------------------------------------------------
async function showAboutModal() {
  const settings = await fetchJSON("/api/settings");
  const body =
`Beabots
Version 2.0

Automates the mapping of medicines.
Automates the data encoding in CF2/CF4.
Beacon PhilHealth E-Claims.

────────────────────────────
License Information

Licensed To : ${settings.license_owner || "Unknown"}
Plan        : ${settings.license_plan || "Unknown"}
Expires     : ${settings.license_expiry || "Unknown"}

────────────────────────────
Developer: Romel Gonzaga
GitHub: https://github.com/rgonzaga19`;
  showModal("About", body);
}

// ---------------------------------------------------------------------------
// Transmittal textarea + count label (same logic as ui.py's _update_count)
// ---------------------------------------------------------------------------
const transmittalsInput = document.getElementById("transmittalsInput");
const countLabel = document.getElementById("countLabel");

function updateCount() {
  const lines = transmittalsInput.value.split("\n").map((l) => l.trim()).filter(Boolean);
  const n = lines.length;
  countLabel.textContent = `${n} transmittal${n !== 1 ? "s" : ""}`;
}
transmittalsInput.addEventListener("keyup", updateCount);

// ---------------------------------------------------------------------------
// Log box (same colour-tag detection as ui.py's write_log)
// ---------------------------------------------------------------------------
const logBox = document.getElementById("logBox");

function writeLog(message, explicitLevel) {
  let tag = "INFO";
  const upper = message.toUpperCase();

  if (explicitLevel) {
    tag = explicitLevel;
  } else if (upper.includes("[SUCCESS]") || upper.includes("SUCCESS:") || upper.startsWith("SUCCESS")) {
    tag = "SUCCESS";
  } else if (upper.includes("[WARNING]") || upper.includes("WARNING:") || upper.startsWith("WARNING") || upper.includes("[DEV]")) {
    tag = "DIM";
  } else if (upper.includes("[ERROR]") || upper.includes("ERROR:") || upper.startsWith("ERROR")) {
    tag = "ERROR";
  } else if (message.startsWith("=") || message.trim() === "") {
    tag = "DIM";
  }

  const line = document.createElement("div");
  line.className = `log-line ${tag}`;
  line.textContent = message;
  logBox.appendChild(line);
  logBox.scrollTop = logBox.scrollHeight;
}

function clearLogs() {
  logBox.innerHTML = "";
}

// ---------------------------------------------------------------------------
// Report box (same layout as ui.py's show_report)
// ---------------------------------------------------------------------------
const reportBox = document.getElementById("reportBox");

function showReport(results) {
  reportBox.innerHTML = "";

  const hdr = document.createElement("div");
  hdr.className = "log-line INFO";
  hdr.style.color = "var(--accent)";
  hdr.textContent = `${"TRANSMITTAL".padEnd(20)} ${"STATUS".padEnd(12)} REMARKS`;
  reportBox.appendChild(hdr);

  const rule = document.createElement("div");
  rule.className = "log-line INFO";
  rule.style.color = "var(--accent)";
  rule.textContent = "─".repeat(60);
  reportBox.appendChild(rule);

  (results || []).forEach((item) => {
    const tag = item.status === "SUCCESS" ? "SUCCESS" : item.status === "SKIPPED" ? "WARNING" : "ERROR";
    const line = document.createElement("div");
    line.className = `log-line ${tag}`;
    line.textContent = `${String(item.transmittal).padEnd(20)} ${String(item.status).padEnd(12)} ${item.remarks}`;
    reportBox.appendChild(line);
  });
}

// ---------------------------------------------------------------------------
// Status dot / label / progress bar
// ---------------------------------------------------------------------------
const statusDot = document.getElementById("statusDot");
const statusLabel = document.getElementById("statusLabel");
const progressFill = document.getElementById("progressFill");
const startBtn = document.getElementById("startBtn");
const btnSettings = document.getElementById("btnSettings");

function setStatus(text, cssClass) {
  statusLabel.textContent = text;
  statusDot.className = `status-dot ${cssClass || ""}`;
}

function disableControls() {
  startBtn.disabled = true;
  btnSettings.disabled = true;
  transmittalsInput.disabled = true;
  setStatus("RUNNING", "running");
  progressFill.classList.remove("idle");
}

function enableControls() {
  startBtn.disabled = false;
  btnSettings.disabled = false;
  transmittalsInput.disabled = false;
  setStatus("DONE", "done");
  progressFill.classList.add("idle");
}

// ---------------------------------------------------------------------------
// Start automation (same flow as ui.py's start_automation())
// ---------------------------------------------------------------------------
startBtn.addEventListener("click", async () => {
  // License check
  const license = await fetchJSON("/api/license/validate", { method: "POST" });
  if (!license.valid) {
    showError(license.error && license.error.includes("license") ? "License Error" : "Access Denied", license.error || "Invalid or expired license.");
    return;
  }

  const transmittals = transmittalsInput.value
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);

  if (transmittals.length === 0) {
    showError("No Input", "Please paste at least one transmittal number.");
    return;
  }

  clearLogs();
  showReport([]);
  writeLog("Automation started.");
  writeLog(`Found ${transmittals.length} transmittal(s).`);
  disableControls();

  const result = await fetchJSON("/api/beacon/start", {
    method: "POST",
    body: JSON.stringify({
      transmittals,
      auto_encode_cf4: document.getElementById("autoEncodeCf4").checked,
    }),
  });

  if (result.error) {
    writeLog(`ERROR: ${result.error}`, "ERROR");
    enableControls();
  }
});

// ---------------------------------------------------------------------------
// Socket.IO — live log stream + completion events from server.py
// ---------------------------------------------------------------------------
const socket = io(API_BASE);

socket.on("log", (data) => writeLog(data.message, data.level));

socket.on("beacon_done", (data) => {
  showReport(data.results);
  enableControls();
});

// Initial state
updateCount();
setStatus("IDLE", "");