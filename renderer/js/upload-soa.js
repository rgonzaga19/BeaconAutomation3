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
const summaryLogPanel = document.getElementById("summaryLogPanel");
const automationLogPanel = document.getElementById("automationLogPanel");
const KNOWN_LEVELS = ["SUCCESS", "WARNING", "ERROR", "INFO"];
let soaSummary = {
  running: false,
  total: 0,
  success: 0,
  warning: 0,
  error: 0,
};
let soaRows = [];
let currentProcessingIndex = 0;
let soaRunActive = false;

function statusLabel(status) {
  return {
    waiting: "Waiting",
    running: "Running",
    success: "Success",
    skipped: "Skipped",
    failed: "Failed",
  }[status] || "Waiting";
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

document.querySelectorAll(".soa-log-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".soa-log-tab").forEach((item) => {
      const active = item === tab;
      item.classList.toggle("active", active);
      item.setAttribute("aria-selected", String(active));
    });
    document.querySelectorAll(".soa-log-panel").forEach((panel) => panel.classList.remove("active"));
    const panel = document.getElementById(tab.dataset.logPanel);
    panel.classList.add("active");
    panel.scrollTop = panel.scrollHeight;
  });
});

function writeLog(message, level) {
  const line = document.createElement("div");
  line.className = KNOWN_LEVELS.includes(level) ? `log-line ${level}` : "log-line";
  line.textContent = message;
  automationLogPanel.appendChild(line);
  automationLogPanel.scrollTop = automationLogPanel.scrollHeight;
  updateSummaryFromLog(message, level);
}

function clearLog() {
  automationLogPanel.innerHTML = "";
  soaSummary = {
    running: false,
    total: 0,
    success: 0,
    warning: 0,
    error: 0,
  };
  currentProcessingIndex = 0;
  renderSummary();
}

function renderSummary() {
  const status = soaSummary.running ? "SOA upload is running." : "SOA upload is ready.";
  const done = soaSummary.success + soaSummary.warning + soaSummary.error;
  const rows = soaRows.map((row, index) => `
    <tr class="soa-status-row ${row.status}" data-index="${index}">
      <td>
        <span class="soa-row-status">
          <span class="soa-row-dot"></span>
          <span>${statusLabel(row.status)}</span>
        </span>
      </td>
      <td title="${escapeHtml(row.transmittal)}">${escapeHtml(row.transmittal)}</td>
    </tr>
  `).join("");
  summaryLogPanel.innerHTML = `
    <div class="soa-summary">
      <div class="soa-summary-strip">
        <div class="soa-summary-stat">
          <div class="label">Total</div>
          <div class="value">${soaSummary.total}</div>
        </div>
        <div class="soa-summary-stat success">
          <div class="label">Success</div>
          <div class="value">${soaSummary.success}</div>
        </div>
        <div class="soa-summary-stat warning">
          <div class="label">Warnings</div>
          <div class="value">${soaSummary.warning}</div>
        </div>
        <div class="soa-summary-stat error">
          <div class="label">Errors</div>
          <div class="value">${soaSummary.error}</div>
        </div>
      </div>
      <div class="soa-summary-status">${status} ${soaSummary.total ? `${done}/${soaSummary.total} result line${done === 1 ? "" : "s"} captured.` : "Enter transmittals and start automation."}</div>
      <div class="soa-status-table-wrap">
        <table class="soa-status-table">
          <thead>
            <tr>
              <th>Status</th>
              <th>Transmittal</th>
            </tr>
          </thead>
          <tbody>
            ${rows || `<tr class="soa-status-row waiting"><td><span class="soa-row-status"><span class="soa-row-dot"></span><span>Ready</span></span></td><td>No transmittals loaded.</td></tr>`}
          </tbody>
        </table>
      </div>
    </div>`;
  summaryLogPanel.scrollTop = summaryLogPanel.scrollHeight;
}

function setSoaRows(transmittals) {
  soaRows = transmittals.map((transmittal) => ({
    transmittal,
    status: "waiting",
  }));
  currentProcessingIndex = 0;
}

function updateSoaRow(index, status) {
  if (index < 0 || index >= soaRows.length) return;
  soaRows.forEach((row, rowIndex) => {
    if (row.status === "running" && rowIndex !== index) {
      row.status = "waiting";
    }
  });
  soaRows[index].status = status;
  renderSummary();
  const row = summaryLogPanel.querySelector(`.soa-status-row[data-index="${index}"]`);
  if (row) row.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function renderFinalSummary(results) {
  const normalized = (results || []).map((item) => ({
    transmittal: item.transmittal || "-",
    status: String(item.status || "failed").toLowerCase(),
    message: item.message || "",
  }));
  const resultByTransmittal = new Map(normalized.map((item) => [String(item.transmittal), item.status]));
  soaRows = soaRows.map((row) => ({
    ...row,
    status: resultByTransmittal.get(String(row.transmittal)) || row.status,
  }));
  soaSummary.running = false;
  soaSummary.total = normalized.length || soaSummary.total;
  soaSummary.success = normalized.filter((item) => item.status === "success").length;
  soaSummary.warning = normalized.filter((item) => item.status === "skipped").length;
  soaSummary.error = normalized.filter((item) => item.status === "failed").length;
  renderSummary();
}

function updateSummaryFromLog(message, level = "INFO") {
  const text = String(message || "");
  const upper = text.toUpperCase();
  let changed = false;
  const processingMatch = text.match(/PROCESSING TRANSMITTAL\s+(\d+)\/(\d+)/i);

  if (processingMatch) {
    currentProcessingIndex = Number(processingMatch[1]) - 1;
    updateSoaRow(currentProcessingIndex, "running");
    return;
  }

  if (upper.includes("[SUCCESS]")) {
    soaSummary.success += 1;
    changed = true;
  } else if (upper.includes("[SKIPPED]")) {
    soaSummary.warning += 1;
    changed = true;
  } else if (upper.includes("[FAILED]") || upper.includes("FATAL ERROR")) {
    soaSummary.error += 1;
    changed = true;
  }

  if (changed) {
    renderSummary();
  }
}

// ---------------------------------------------------------------------------
// Automate Upload button — same validation order as start_soa_automation()
// ---------------------------------------------------------------------------
const automateBtn = document.getElementById("automateBtn");
const automateBtnLabel = document.getElementById("automateBtnLabel");

function setControlsRunning(running) {
  transmittalsInput.disabled = running;
  automateBtn.disabled = running;
  automateBtn.classList.toggle("running", running);
  automateBtnLabel.textContent = running ? "AUTOMATING UPLOAD…" : "AUTOMATE UPLOAD";
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
  setSoaRows(transmittals);
  soaRunActive = true;
  soaSummary.running = true;
  soaSummary.total = transmittals.length;
  renderSummary();
  writeLog(`Starting SOA upload for ${transmittals.length} transmittal(s)...`);

  const result = await fetchJSON("/api/soa/start", {
    method: "POST",
    body: JSON.stringify({ transmittals, soa_folder: soaFolder }),
  });

  if (result.error) {
    showModal("Error", result.error);
    soaRunActive = false;
    soaSummary.running = false;
    renderSummary();
    setControlsRunning(false);
  }
});

// ---------------------------------------------------------------------------
// Socket.IO — live log stream + completion
// ---------------------------------------------------------------------------
const socket = io(API_BASE);

socket.on("log", (data) => {
  if (!soaRunActive) return;
  writeLog(data.message, data.level);
});

socket.on("soa_done", (data) => {
  renderFinalSummary(data?.results || []);
  soaRunActive = false;
  setControlsRunning(false);
});

// Initial state
renderSummary();
updateCount();
