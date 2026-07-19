/*
 * Upload SOA window renderer logic.
 * Depends on common.js (fetchJSON, showModal, API_BASE) and window.beabots
 * (preload.js) for window chrome, folder dialog, and defaultSoaFolder.
 */

// ---------------------------------------------------------------------------
// Title bar
// ---------------------------------------------------------------------------
document.getElementById("btnMinimize").addEventListener("click", () => window.beabots?.minimize());
document.getElementById("btnMaximize").addEventListener("click", () => window.beabots?.maximize());
document.getElementById("btnClose").addEventListener("click", () => window.beabots?.close());

// ---------------------------------------------------------------------------
// License check — same as open_upload_soa_window()'s check before the
// Toplevel was ever created.
// ---------------------------------------------------------------------------
(async function checkLicense() {
  const license = await fetchJSON("/api/license/validate", { method: "POST" });
  if (!license.valid) {
    showModal(
      license.error && license.error.toLowerCase().includes("unable") ? "License Error" : "Access Denied",
      license.error || "Invalid or expired license.",
      { onOk: () => window.beabots?.close() }
    );
  }
})();

// ---------------------------------------------------------------------------
// SOA folder — last used folder remembered in settings, same as
// soa_folder_var = tk.StringVar(value=settings.get("soa_folder", DEFAULT_SOA_FOLDER))
// ---------------------------------------------------------------------------
const soaFolderInput = document.getElementById("soaFolderInput");
const browseBtn = document.getElementById("browseBtn");

(async function initFolder() {
  const settings = await fetchJSON("/api/settings");
  soaFolderInput.value = settings.soa_folder || window.beabots?.defaultSoaFolder || "";
})();

browseBtn.addEventListener("click", async () => {
  const chosen = await window.beabots?.selectSoaFolder(soaFolderInput.value);
  if (!chosen) return;

  soaFolderInput.value = chosen;

  // Remember the choice for next time — same as the old
  // settings["soa_folder"] = chosen; save_settings(settings)
  await fetchJSON("/api/settings", {
    method: "POST",
    body: JSON.stringify({ soa_folder: chosen }),
  });
});

// ---------------------------------------------------------------------------
// Transmittal textarea + count label
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
// Log box — colored by explicit level, same as upload_soa_window.py's
// log_to_ui(message, level): tag = level if level in LOG_LEVEL_COLORS else None
// ---------------------------------------------------------------------------
const logBox = document.getElementById("logBox");
const KNOWN_LEVELS = ["SUCCESS", "WARNING", "ERROR", "INFO"];

function writeLog(message, level) {
  const line = document.createElement("div");
  line.className = KNOWN_LEVELS.includes(level) ? `log-line ${level}` : "log-line";
  line.textContent = message;
  logBox.appendChild(line);
  logBox.scrollTop = logBox.scrollHeight;
}

function clearLog() {
  logBox.innerHTML = "";
}

// ---------------------------------------------------------------------------
// Automate Upload button — same validation order as start_soa_automation()
// ---------------------------------------------------------------------------
const automateBtn = document.getElementById("automateBtn");

function setControlsRunning(running) {
  transmittalsInput.disabled = running;
  automateBtn.disabled = running;
  browseBtn.disabled = running;
}

automateBtn.addEventListener("click", async () => {
  const transmittals = transmittalsInput.value
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);

  if (transmittals.length === 0) {
    showModal("No Transmittals", "Please enter at least one transmittal number.");
    return;
  }

  const soaFolder = soaFolderInput.value.trim();
  if (!soaFolder) {
    showModal("No SOA Folder", "Please select the folder where your SOA files are located.");
    return;
  }

  setControlsRunning(true);
  clearLog();
  writeLog(`Starting SOA upload for ${transmittals.length} transmittal(s)...`);

  const result = await fetchJSON("/api/soa/start", {
    method: "POST",
    body: JSON.stringify({ transmittals, soa_folder: soaFolder }),
  });

  if (result.error) {
    showModal("Error", result.error);
    setControlsRunning(false);
  }
});

// ---------------------------------------------------------------------------
// Socket.IO — live log stream + completion
// ---------------------------------------------------------------------------
const socket = io(API_BASE);

socket.on("log", (data) => writeLog(data.message, data.level));

socket.on("soa_done", () => {
  setControlsRunning(false);
});

// Initial state
updateCount();