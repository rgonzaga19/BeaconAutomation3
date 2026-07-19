/*
 * CF2 window renderer logic.
 * Depends on common.js (fetchJSON, showModal, showError, API_BASE) being
 * loaded first, and on window.beabots (see preload.js) for window chrome,
 * the Excel file dialog, and the template save-as dialog.
 */

const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

// ---------------------------------------------------------------------------
// Title bar
// ---------------------------------------------------------------------------
document.getElementById("btnMinimize").addEventListener("click", () => window.beabots?.minimize());
document.getElementById("btnMaximize").addEventListener("click", () => window.beabots?.maximize?.());
document.getElementById("btnClose").addEventListener("click", () => window.beabots?.close());

// ---------------------------------------------------------------------------
// License check — same as open_cf2_window()'s check before the Toplevel
// was ever created. If invalid, show the error and close this window.
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
// Claim Year / Claim Month selects (years 2024-2035, defaults to now)
// ---------------------------------------------------------------------------
const claimYearSelect = document.getElementById("claimYear");
const claimMonthSelect = document.getElementById("claimMonth");

for (let y = 2024; y < 2036; y++) {
  const opt = document.createElement("option");
  opt.value = String(y);
  opt.textContent = String(y);
  claimYearSelect.appendChild(opt);
}
claimYearSelect.value = String(new Date().getFullYear());

MONTH_NAMES.forEach((m) => {
  const opt = document.createElement("option");
  opt.value = m;
  opt.textContent = m;
  claimMonthSelect.appendChild(opt);
});
claimMonthSelect.value = MONTH_NAMES[new Date().getMonth()];

// ---------------------------------------------------------------------------
// Log box (plain single-color box — cf2_window.py's txt_log has no
// per-level color tags, unlike the dashboard/Upload SOA logs)
// ---------------------------------------------------------------------------
const logBox = document.getElementById("cf2LogBox");

function log(text) {
  const line = document.createElement("div");
  line.className = "log-line INFO";
  line.textContent = text;
  logBox.appendChild(line);
}

function clearLog() {
  logBox.innerHTML = "";
}

function scrollLogToEnd() {
  logBox.scrollTop = logBox.scrollHeight;
}

// ---------------------------------------------------------------------------
// Upload Excel File (same log format as analyze_workbook())
// ---------------------------------------------------------------------------
const uploadBtn = document.getElementById("uploadBtn");
const fileLabel = document.getElementById("fileLabel");
const sheetsLine = document.getElementById("sheetsLine");
const patientsLine = document.getElementById("patientsLine");

let hasPatientRecords = false;

uploadBtn.addEventListener("click", async () => {
  const path = await window.beabots?.selectExcelFile();
  if (!path) return;

  const filename = path.split(/[\\/]/).pop();
  fileLabel.textContent = `📄 ${filename}`;

  clearLog();
  log("=========================================");
  log(" CF2");
  log("=========================================");
  log("");
  log(`Selected File:`);
  log(path);
  log("");

  const result = await fetchJSON("/api/cf2/upload", {
    method: "POST",
    body: JSON.stringify({
      path,
      claim_year: claimYearSelect.value,
      claim_month: claimMonthSelect.value,
    }),
  });

  if (result.error) {
    log("");
    log("ERROR:");
    log(result.error);
    scrollLogToEnd();
    return;
  }

  log("Workbook loaded successfully.");
  log("");
  log("Worksheets found:");
  result.sheets.forEach((s) => log(`   • ${s}`));
  log("");

  result.records.forEach((record, i) => {
    log(`Patient #${i + 1}`);
    log(`Member PIN  : ${record.member_pin}`);
    log(`Patient     : ${record.patient_name}`);
    log(`Doctor      : ${record.doctor}`);
    log(`Accred. No. : ${record.accreditation_no}`);
    log(`Dates       : ${record.treatment_dates_raw}`);
    log(`Parsed Dates:`);
    record.parsed_dates.forEach((d) => log(`   ${d}`));

    if (record.first_treatment) {
      log(`First Date : ${record.first_treatment}`);
      log(`Last Date  : ${record.last_treatment}`);
      log(`Sessions   : ${record.total_sessions}`);
      log("");
      log("CF2 DATA");
      log(`Transmittal : ${record.cf2.transmittal}`);
      log(`Patient     : ${record.cf2.patient_name}`);
      log(`Doctor      : ${record.cf2.doctor}`);
      log(`Accred. No. : ${record.cf2.accreditation_no}`);
      log(`First Date  : ${record.cf2.first_treatment}`);
      log(`Last Date   : ${record.cf2.last_treatment}`);
      log(`Sessions    : ${record.cf2.total_sessions}`);
    }

    log("");
    log("");
    log("");
  });

  log("=========================================");
  log(`Patients Found : ${result.patient_count}`);
  log("=========================================");
  scrollLogToEnd();

  sheetsLine.textContent = `Sheets : ${result.sheets.length} (${result.sheets.join(", ")})`;
  patientsLine.textContent = `Patients Found : ${result.patient_count}`;
  hasPatientRecords = result.patient_count > 0;

  // Bring the window back to front, same as cf2_window.after(10, lift)/focus_force
  window.beabots?.focusSelf?.();
});

// ---------------------------------------------------------------------------
// Download Excel Template
// ---------------------------------------------------------------------------
document.getElementById("downloadTemplateLink").addEventListener("click", async () => {
  const result = await window.beabots?.saveExcelTemplate();
  if (!result) return;
  if (result.saved) {
    showModal("Success", "Excel template downloaded successfully.");
  } else if (result.error) {
    showModal("Error", `Unable to download template.\n\n${result.error}`);
  }
});

// ---------------------------------------------------------------------------
// User Guide modal — verbatim steps from cf2_window.py's guide_steps list
// ---------------------------------------------------------------------------
const GUIDE_STEPS = [
  [1, "📋", "Prepare the report from AR","The data will be used in the cf2 template \n" + 
    "This Automation includes: Draft, CF2, Signatories and CF2 Preview."],
  [2, "⬇", "Download the Excel Template", "Click 'Download Excel Template'.\nUse the provided template."],
  [3, "🗂", "Edit the Template",
    "Add Member's pin (if dependent add slash at the end).\n" +
    "Only edit the patient information.\n" +
    "Do not change headers, column names,\n" +
    "column order or file format.\n" +
    "Only modify the data rows."],
  [4, "⏷", "Use Excel Filters",
    "To speed up encoding, use Excel Filters.\n\n" +
    "• Doctor Name\n" +
    "• Accreditation Number\n\n" +
    "This helps populate multiple records consistently."],
  [5, "💾", "Save the File", "Save the completed workbook.\nRecommended:\nCF2_Claims_2026.xlsx"],
  [6, "⬆", "Upload the Workbook", "Return to this window.\nClick 'Upload Excel File'.\nSelect the saved workbook."],
  [7, "▶", "Start Automation",
    "Verify that the workbook loaded successfully,\n" +
    "patients detected and claims count are correct.\n" +
    "Then click 'Start Automation'."],
];

document.getElementById("guideCard").addEventListener("click", () => {
  const stepsHtml = GUIDE_STEPS.map(([number, icon, title, description]) => `
    <div class="guide-step">
      <div class="badge-col">
        <div class="badge">${number}</div>
        <div class="step-icon">${icon}</div>
      </div>
      <div>
        <div class="step-title">${title}</div>
        <div class="step-desc">${description}</div>
      </div>
    </div>`).join("");

  const root = document.getElementById("modalRoot");
  root.innerHTML = `
    <div class="modal-overlay guide-modal">
      <div class="modal-box">
        <h2>CF2 USER GUIDE</h2>
        ${stepsHtml}
        <div class="modal-actions" style="justify-content: center; margin-top: 10px;">
          <button class="cyber-btn" id="guideOkBtn">OK</button>
        </div>
      </div>
    </div>`;
  document.getElementById("guideOkBtn").addEventListener("click", () => { root.innerHTML = ""; });
});

// ---------------------------------------------------------------------------
// Start Automation (same worker flow as _run_automation_worker /
// _display_summary, reported over the cf2_done socket event)
// ---------------------------------------------------------------------------
const startBtn = document.getElementById("startBtn");

function setControlsRunning(running) {
  startBtn.disabled = running;
  startBtn.textContent = running ? "Running..." : "Start Automation";
  uploadBtn.disabled = running;
  claimYearSelect.disabled = running;
  claimMonthSelect.disabled = running;
}

startBtn.addEventListener("click", async () => {
  if (!hasPatientRecords) return; // matches: if len(patient_records) == 0: return

  setControlsRunning(true);

  const result = await fetchJSON("/api/cf2/start", { method: "POST" });
  if (result.error) {
    log("");
    log(`ERROR: ${result.error}`);
    scrollLogToEnd();
    setControlsRunning(false);
  }
});

// ---------------------------------------------------------------------------
// Socket.IO — live logs during the run, plus the final summary block
// (verbatim format from _display_summary)
// ---------------------------------------------------------------------------
const socket = io(API_BASE);

socket.on("log", (data) => {
  log(data.message);
  scrollLogToEnd();
});

socket.on("cf2_done", (data) => {
  const results = data.results || [];

  if (results.length > 0) {
    const success = results.filter((r) => r.status === "success");
    const skipped = results.filter((r) => r.status === "skipped");
    const failed = results.filter((r) => r.status === "failed");

    log("");
    log("=========================================");
    log(" AUTOMATION SUMMARY");
    log("=========================================");
    log(`Total     : ${results.length}`);
    log(`Success   : ${success.length}`);
    log(`Skipped   : ${skipped.length}`);
    log(`Failed    : ${failed.length}`);
    log("-----------------------------------------");

    results.forEach((r) => {
      log(`[${r.status.toUpperCase()}] Transmittal: ${r.transmittal}  |  Patient: ${r.patient_name}`);
      if (r.status !== "success" && r.message) {
        log(`        Reason: ${r.message}`);
      }
    });

    log("=========================================");
    scrollLogToEnd();
  }

  setControlsRunning(false);
});