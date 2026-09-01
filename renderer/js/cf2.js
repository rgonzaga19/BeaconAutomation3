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
// New Draft / Existing Draft mode toggle
// ---------------------------------------------------------------------------
let currentMode = "new_draft"; // or "existing_draft"

const modeNewDraftBtn = document.getElementById("modeNewDraft");
const modeExistingDraftBtn = document.getElementById("modeExistingDraft");
const modeHint = document.getElementById("modeHint");

const MODE_HINTS = {
  new_draft:
    'Column A of the template is the patient\'s <strong>Member PIN</strong> — a new Beacon draft is created for each row.',
  existing_draft:
    'Column A of the template is the <strong>Transmittal No.</strong> of a draft that already exists in Beacon — this searches for it instead of creating one.',
};

function setMode(mode) {
  if (mode === currentMode) return;
  currentMode = mode;

  modeNewDraftBtn.classList.toggle("active", mode === "new_draft");
  modeExistingDraftBtn.classList.toggle("active", mode === "existing_draft");
  modeHint.innerHTML = MODE_HINTS[mode];

  // A workbook uploaded for one mode has the wrong meaning in column A
  // for the other (Member PIN vs. Transmittal No.), so switching modes
  // clears whatever was loaded rather than leaving stale/misleading data.
  fileLabel.textContent = "No file selected";
  sheetsLine.textContent = "Sheets : -";
  patientsLine.textContent = "Patients Found : 0";
  hasPatientRecords = false;
  clearLog();
}

modeNewDraftBtn.addEventListener("click", () => setMode("new_draft"));
modeExistingDraftBtn.addEventListener("click", () => setMode("existing_draft"));

// ---------------------------------------------------------------------------
// Log box (plain single-color box — cf2_window.py's txt_log has no
// per-level color tags, unlike the dashboard/Upload SOA logs)
// ---------------------------------------------------------------------------
const summaryLogBox = document.getElementById("summaryLogPanel");
const detailsLogBox = document.getElementById("detailsLogPanel");
let logBox = summaryLogBox;
let cf2RunActive = false;
let cf2LogStopTimer = null;
let lastDetailLine = "";
let lastDetailAt = 0;

function log(text, level = "INFO", target = summaryLogBox) {
  const line = document.createElement("div");
  line.className = `log-line ${level}`;
  line.textContent = text;
  target.appendChild(line);
}

function clearLog() {
  summaryLogBox.innerHTML = "";
  detailsLogBox.innerHTML = "";
}

function scrollLogToEnd() {
  logBox.scrollTop = logBox.scrollHeight;
}

function detailLog(text, level = "INFO") {
  // Keep local HTTP access records and duplicated logger callbacks out of
  // the operator-facing sequence; they are transport noise, not CF2 steps.
  if (/^(?:127\.0\.0\.1|localhost)\s+-\s+-/.test(text)) return;
  const now = Date.now();
  if (text === lastDetailLine && now - lastDetailAt < 1000) return;
  lastDetailLine = text;
  lastDetailAt = now;
  log(text, level, detailsLogBox);
  detailsLogBox.scrollTop = detailsLogBox.scrollHeight;
}

document.querySelectorAll(".log-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".log-tab").forEach((item) => {
      const active = item === tab;
      item.classList.toggle("active", active);
      item.setAttribute("aria-selected", String(active));
    });
    document.querySelectorAll(".log-panel").forEach((panel) => panel.classList.remove("active"));
    logBox = document.getElementById(tab.dataset.logPanel);
    logBox.classList.add("active");
    scrollLogToEnd();
  });
});

// ---------------------------------------------------------------------------
// Raw server/automation stdout+stderr (see preload.js's onServerLog /
// main.js's makeLineForwarder). This is separate from — and a superset
// of — the socket.io "log" events further below: socket.io only carries
// whatever server.py deliberately emits, while this carries every raw
// print() / traceback from the Python process, including the WARNING
// lines cf2_automation.py prints when an API-first step falls back to
// UI automation (previously visible only in Electron's own,
// invisible-once-packaged main-process console).
// ---------------------------------------------------------------------------
window.beabots?.onServerLog?.(({ level, line }) => {
  if (!cf2RunActive) return;
  detailLog(line, level === "error" ? "ERROR" : "INFO");
});

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
      mode: currentMode,
    }),
  });

  if (result.error) {
    log("");
    log("ERROR:");
    log(result.error);
    summaryLogBox.scrollTop = summaryLogBox.scrollHeight;
    return;
  }

  log("Workbook loaded successfully.");
  log("");
  log("Worksheets found:");
  result.sheets.forEach((s) => log(`   • ${s}`));
  log("");

  result.records.forEach((record, i) => {
    log(`Patient #${i + 1}`);
    log(`${record.identifier_label}: ${record.identifier}`);
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
  const result = await window.beabots?.saveExcelTemplate(currentMode);
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
const GUIDE_STEP_3 = {
  new_draft:
    "Add Member's pin (if dependent add slash at the end).\n" +
    "Only edit the patient information.\n" +
    "Do not change headers, column names,\n" +
    "column order or file format.\n" +
    "Only modify the data rows.",
  existing_draft:
    "Add the existing Transmittal No. for each patient.\n" +
    "The draft must already exist in Beacon — this template\n" +
    "does not create a new one, it only locates it.\n" +
    "Do not change headers, column names,\n" +
    "column order or file format.\n" +
    "Only modify the data rows.",
};

function buildGuideSteps() {
  return [
    [1, "📋", "Prepare the report from AR","The data will be used in the cf2 template \n" +
      "This Automation includes: Draft, CF2, Signatories and CF2 Preview."],
    [2, "⬇", "Download the Excel Template", "Click 'Download Excel Template'.\nUse the provided template."],
    [3, "🗂", "Edit the Template", GUIDE_STEP_3[currentMode]],
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
}

function buildCurrentGuideSteps() {
  const isNewDraft = currentMode === "new_draft";
  return [
    {
      title: "Set the claim period",
      description: "Select the correct claim year and month before uploading. These values are used to interpret every treatment date in the workbook.",
      note: "Changing the period requires uploading the workbook again.",
    },
    {
      title: "Choose the workflow mode",
      description: isNewDraft
        ? "Use New Draft when Beacon must create a new claim for each patient."
        : "Use Existing Draft when the claim is already in Beacon and must be located by transmittal number.",
      note: isNewDraft ? "Column A must contain the Member PIN." : "Column A must contain the existing Transmittal No.",
    },
    {
      title: "Download the matching template",
      description: "Click Download Excel Template after selecting the mode. New Draft and Existing Draft use different templates and Column A has a different meaning.",
      note: "Do not reuse a workbook prepared for the other mode.",
    },
    {
      title: "Complete columns A to E",
      description: "Enter Column A identifier, B patient name, C doctor, D accreditation number, and E treatment dates. Add one patient per row and save the workbook.",
      note: "Keep Sheet1, headers, column order, and file format unchanged.",
    },
    {
      title: "Upload and review the workbook",
      description: "Click Upload Excel File. Check the detected patient count, identifiers, doctor details, parsed dates, first and last treatment dates, and session totals in the execution log.",
      note: "Correct the workbook and upload it again if anything is wrong.",
    },
    {
      title: "Run, monitor, and verify",
      description: "Click Start Automation only after the uploaded data is correct. Monitor the execution log and final summary for successful, skipped, or failed records.",
      note: "Always verify the completed draft, CF2, signatories, and preview in Beacon.",
    },
  ];
}

document.getElementById("guideCard").addEventListener("click", () => {
  const stepsHtml = buildCurrentGuideSteps().map((step, index) => `
    <div class="guide-step">
      <div class="guide-step-number">${index + 1}</div>
      <div class="step-title">${step.title}</div>
      <div class="step-desc">${step.description}</div>
      <div class="guide-step-note">${step.note}</div>
    </div>`).join("");

  const root = document.getElementById("modalRoot");
  root.innerHTML = `
    <div class="modal-overlay guide-modal">
      <div class="modal-box">
        <h2>CF2 WORKFLOW GUIDE</h2>
        <div class="guide-intro">
          <span>Follow these steps in order. The guide updates for the selected workflow mode.</span>
          <span class="guide-mode-pill">${currentMode === "new_draft" ? "NEW DRAFT" : "EXISTING DRAFT"}</span>
        </div>
        <div class="guide-steps-grid">${stepsHtml}</div>
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
const startBtnLabel = document.getElementById("startBtnLabel");

function setControlsRunning(running) {
  startBtn.disabled = running;
  startBtnLabel.textContent = running ? "Automation Running…" : "Start Automation";
  startBtn.classList.toggle("running", running);
  uploadBtn.disabled = running;
  claimYearSelect.disabled = running;
  claimMonthSelect.disabled = running;
  modeNewDraftBtn.disabled = running;
  modeExistingDraftBtn.disabled = running;
}

startBtn.addEventListener("click", async () => {
  if (!hasPatientRecords) return; // matches: if len(patient_records) == 0: return

  if (cf2LogStopTimer) clearTimeout(cf2LogStopTimer);
  cf2RunActive = true;
  detailsLogBox.innerHTML = "";
  lastDetailLine = "";
  lastDetailAt = 0;
  log("");
  log("Automation started. Open Step-by-Step Log to follow each action.");
  summaryLogBox.scrollTop = summaryLogBox.scrollHeight;
  setControlsRunning(true);

  const result = await fetchJSON("/api/cf2/start", { method: "POST" });
  if (result.error) {
    log("");
    log(`ERROR: ${result.error}`);
    scrollLogToEnd();
    setControlsRunning(false);
    cf2RunActive = false;
  }
});

// ---------------------------------------------------------------------------
// Socket.IO — live logs during the run, plus the final summary block
// (verbatim format from _display_summary)
// ---------------------------------------------------------------------------
const socket = io(API_BASE);

socket.on("log", (data) => {
  if (!cf2RunActive) return;
  detailLog(data.message, data.level || "INFO");
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
    summaryLogBox.scrollTop = summaryLogBox.scrollHeight;
  }

  setControlsRunning(false);
  // stdout and Socket.IO travel over separate channels. Keep a short grace
  // period so final buffered CF2 lines arrive, then detach this panel from
  // the shared stream before SOA or CF4 starts.
  cf2LogStopTimer = setTimeout(() => {
    cf2RunActive = false;
    cf2LogStopTimer = null;
  }, 1200);
});
