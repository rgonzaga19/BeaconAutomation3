/*
 * CF4 Auto Encode defaults — renderer logic.
 *
 * This screen edits the values beacon.py's `auto_encode_cf4` branch used
 * to have hardcoded (chief complaint text, which "Essentially normal" /
 * symptom / exam-finding boxes get checked, the "Others" remarks, the
 * Course in the Ward order text). They're persisted through server.py's
 * /api/cf4/settings endpoint (stored alongside the rest of settings.json
 * under a "cf4" key) and read back by server.py right before it kicks off
 * /api/beacon/start, so beacon.run(..., cf4_data=...) always gets
 * whatever was last saved here instead of a fixed string in the script.
 *
 * The "Pertinent Signs and Symptoms" and "Physical Examination" sections
 * are built here from SYMPTOM_COLUMNS / PHYSICAL_EXAM_SECTIONS rather
 * than written out by hand in cf4.html, so the *settings keys* below are
 * the single source of truth. For those two sections, each key now IS
 * the confirmed Beacon `name` attribute (camelCase) — beacon.py reads
 * cf4_data[key] and locates input[name="key"] directly, no separate
 * translation table. The handful of hand-written fields (chief_complaint,
 * history_of_present_illness, pertinent_past_medical_history,
 * general_survey_awake_alert, course_in_ward_order) stay snake_case,
 * since they're settings-only concepts, not 1:1 with a single checkbox
 * name.
 *
 * Assumes the same `window.beabots` preload bridge as dashboard.js
 * (minimize/maximize/close window chrome) — see dashboard.js's header
 * comment for what that bridge needs to expose.
 */

// API_BASE, fetchJSON, showModal, showError all live in common.js (loaded before this file).

// ---------------------------------------------------------------------------
// Field definitions — settings key -> label (+ optional "specify" text
// field key for the Pain/Others rows that pair a checkbox with a remark).
// Order and grouping match the screenshots of Beacon's live CF4 form.
// ---------------------------------------------------------------------------
const SYMPTOM_COLUMNS = [
  [
    { key: "alteredMentalSensorium", label: "Altered Mental Sensorium" },
    { key: "abdominalCrampPain", label: "Abdominal cramp/pain" },
    { key: "anorexia", label: "Anorexia" },
    { key: "bleedingGums", label: "Bleeding gums" },
    { key: "bodyWeakness", label: "Body weakness" },
    { key: "blurringOfVision", label: "Blurring vision" },
    { key: "chestPainDiscomfort", label: "Chest pain/discomfort" },
    { key: "constipation", label: "Constipation" },
    { key: "cough", label: "Cough" },
  ],
  [
    { key: "diarrhea", label: "Diarrhea" },
    { key: "dizziness", label: "Dizziness" },
    { key: "dysphagia", label: "Dysphagia" },
    { key: "dyspnea", label: "Dyspnea" },
    { key: "dysuria", label: "Dysuria" },
    { key: "epistaxis", label: "Epistaxis" },
    { key: "fever", label: "Fever" },
    { key: "frequencyOfUrination", label: "Frequency of urination" },
    { key: "headache", label: "Headache" },
  ],
  [
    { key: "hematemesis", label: "Hematemesis" },
    { key: "hematuria", label: "Hematuria" },
    { key: "hemoptysis", label: "Hemoptysis" },
    { key: "irritability", label: "Irritability" },
    { key: "jaundice", label: "Jaundice" },
    { key: "lowerExtremityEdema", label: "Lower extremity edema" },
    { key: "myalgia", label: "Myalgia" },
    { key: "orthopnea", label: "Orthopnea" },
    { key: "pain", label: "Pain", specifyKey: "painSpecify" },
  ],
  [
    { key: "palpitations", label: "Palpitations" },
    { key: "seizure", label: "Seizures" },
    { key: "skinRashes", label: "Skin rashes" },
    { key: "stoolBloodyBlackTarryMucoid", label: "Stool, bloody/black tarry/mucoid" },
    { key: "sweating", label: "Sweating" },
    { key: "urgency", label: "Urgency" },
    { key: "vomiting", label: "Vomiting" },
    { key: "weightLoss", label: "Weight loss" },
    { key: "others", label: "Others", specifyKey: "othersSpecify" },
  ],
];

const PHYSICAL_EXAM_SECTIONS = [
  {
    system: "HEENT",
    fields: [
      { key: "heEssentiallyNormal", label: "Essentially normal" },
      { key: "heSunkenFontanelle", label: "Sunken fontanelle" },
      { key: "heAbnormalPupillaryReaction", label: "Abnormal pupillary reaction" },
      { key: "heOthersChk", label: "Others", specifyKey: "heOthers" },
      { key: "heCervicalLympadenopathy", label: "Cervical lymphadenopathy" },
      { key: "heDryMucousMembrane", label: "Dry mucous membrane" },
      { key: "heIctericSclerae", label: "Icteric sclerae" },
      { key: "hePaleConjunctivae", label: "Pale conjunctivae" },
      { key: "heSunkenEyeballs", label: "Sunken eyeballs" },
    ],
  },
  {
    system: "CHEST / LUNGS",
    fields: [
      { key: "clEssentiallyNormal", label: "Essentially normal" },
      { key: "clOthersChk", label: "Others", specifyKey: "clOthers" },
      { key: "clAsymmetricalChestExpansion", label: "Asymmetrical chest expansion" },
      { key: "clDecreasedBreathSounds", label: "Decreased breath sounds" },
      { key: "clWheezes", label: "Wheezes" },
      { key: "clLumpsOverBreast", label: "Lump/s over breast(s)" },
      { key: "clCracklesRales", label: "Rales/crackles/rhonchi" },
      { key: "clRetractions", label: "Intercostal rib retraction" },
    ],
  },
  {
    system: "CVS",
    fields: [
      { key: "cvEssentiallyNormal", label: "Essentially normal" },
      { key: "cvOthersChk", label: "Others", specifyKey: "cvOthers" },
      { key: "cvDisplacedApexBeat", label: "Displaced apex beat" },
      { key: "cvHeavesThrills", label: "Heave and/or thrills" },
      { key: "cvPericardialBulge", label: "Pericardial bulge" },
      { key: "cvIrregularRhythm", label: "Irregular rhythm" },
      { key: "cvMuffledHeartSounds", label: "Muffled heart sounds" },
      { key: "cvMurmur", label: "Murmur" },
    ],
  },
  {
    system: "ABDOMEN",
    fields: [
      { key: "abEssentiallyNormal", label: "Essentially normal" },
      { key: "abOthersChk", label: "Others", specifyKey: "abOthers" },
      { key: "abAbdominalRigidity", label: "Abdominal rigidity" },
      { key: "abAbdominalTenderness", label: "Abdominal tenderness" },
      { key: "abHyperactiveBowelSounds", label: "Hyperactive bowel sounds" },
      { key: "abPalpableMasses", label: "Palpable mass(es)" },
      { key: "abTympaniticDullAbdomen", label: "Tympanitic/dull abdomen" },
      { key: "abUterineContraction", label: "Uterine contraction" },
    ],
  },
  {
    system: "GU (IE)",
    fields: [
      { key: "guEssentiallyNormal", label: "Essentially normal" },
      { key: "guBloodStainedInExamFinger", label: "Blood stained in examining finger" },
      { key: "guCervicalDilatation", label: "Cervical dilatation" },
      { key: "guPresenceofAbnormalDischarge", label: "Presence of abnormal discharge" },
      { key: "guOthersChk", label: "Others", specifyKey: "guOthers" },
    ],
  },
  {
    system: "SKIN/EXTREMITIES",
    fields: [
      { key: "seEssentiallyNormal", label: "Essentially normal" },
      { key: "sePoorSkinTurgor", label: "Poor skin turgor" },
      { key: "seClubbing", label: "Clubbing" },
      { key: "seRashesPetechiae", label: "Rashes/petechiae" },
      { key: "seColdClammy", label: "Cold clammy skin" },
      { key: "seWeakPulse", label: "Weak pulses" },
      { key: "seCyanosisMottledSkin", label: "Cyanosis/mottled skin" },
      { key: "seOthersChk", label: "Others", specifyKey: "seOthers" },
      { key: "seEdemaSwelling", label: "Edema/swelling" },
      { key: "seDecreasedMobility", label: "Decreased mobility" },
      { key: "sePaleNailbeds", label: "Pale nailbeds" },
    ],
  },
  {
    system: "NEURO-EXAM",
    fields: [
      { key: "neEssentiallyNormal", label: "Essentially normal" },
      { key: "nePoorCoordination", label: "Poor coordination" },
      { key: "neAbnormalGait", label: "Abnormal gait" },
      { key: "neOthersChk", label: "Others", specifyKey: "neOthers" },
      { key: "neAbnormalPositionSense", label: "Abnormal position sense" },
      { key: "neAbnormalSensation", label: "Abnormal sensation" },
      { key: "neAbnormalReflexes", label: "Presence of abnormal reflex(es)" },
      { key: "nePoorAlteredMemory", label: "Poor/altered memory" },
      { key: "nePoorMuscleToneStrength", label: "Poor muscle tone/strength" },
    ],
  },
];

// ---------------------------------------------------------------------------
// Defaults — mirrors DEFAULT_CF4_SETTINGS in server.py and DEFAULT_CF4_DATA
// in beacon.py exactly (same keys, same values). These settings keys now
// ARE the confirmed Beacon `name` attributes (camelCase) — see
// SYMPTOM_COLUMNS / PHYSICAL_EXAM_SECTIONS above — so beacon.py no longer
// needs a separate settings-key -> DOM-name translation for these
// checkboxes; it reads cf4_data[key] and locates input[name="key"]
// directly. "Essentially normal" boxes, Body weakness, Lower extremity
// edema, and GU (IE) Others default checked, matching what beacon.py's
// auto-encode step always did before this screen existed; every other
// symptom/finding defaults unchecked.
// ---------------------------------------------------------------------------
const DEFAULT_CF4_SETTINGS = {
  chief_complaint: "FOR HEMODIALYSIS",
  history_of_present_illness: "N/A",
  pertinent_past_medical_history: "N/A",
  general_survey_awake_alert: true,
  course_in_ward_order: "UF GOAL MET AT L",

  // Pertinent Signs and Symptoms
  alteredMentalSensorium: false,
  abdominalCrampPain: false,
  anorexia: false,
  bleedingGums: false,
  bodyWeakness: true,
  blurringOfVision: false,
  chestPainDiscomfort: false,
  constipation: false,
  cough: false,
  diarrhea: false,
  dizziness: false,
  dysphagia: false,
  dyspnea: false,
  dysuria: false,
  epistaxis: false,
  fever: false,
  frequencyOfUrination: false,
  headache: false,
  hematemesis: false,
  hematuria: false,
  hemoptysis: false,
  irritability: false,
  jaundice: false,
  lowerExtremityEdema: true,
  myalgia: false,
  orthopnea: false,
  pain: false,
  painSpecify: "",
  palpitations: false,
  seizure: false,
  skinRashes: false,
  stoolBloodyBlackTarryMucoid: false,
  sweating: false,
  urgency: false,
  vomiting: false,
  weightLoss: false,
  others: false,
  othersSpecify: "",

  // Physical Examination — HEENT
  heEssentiallyNormal: true,
  heSunkenFontanelle: false,
  heAbnormalPupillaryReaction: false,
  heOthersChk: false,
  heOthers: "",
  heCervicalLympadenopathy: false,
  heDryMucousMembrane: false,
  heIctericSclerae: false,
  hePaleConjunctivae: false,
  heSunkenEyeballs: false,

  // Physical Examination — Chest / Lungs
  clEssentiallyNormal: true,
  clOthersChk: false,
  clOthers: "",
  clAsymmetricalChestExpansion: false,
  clDecreasedBreathSounds: false,
  clWheezes: false,
  clLumpsOverBreast: false,
  clCracklesRales: false,
  clRetractions: false,

  // Physical Examination — CVS
  cvEssentiallyNormal: true,
  cvOthersChk: false,
  cvOthers: "",
  cvDisplacedApexBeat: false,
  cvHeavesThrills: false,
  cvPericardialBulge: false,
  cvIrregularRhythm: false,
  cvMuffledHeartSounds: false,
  cvMurmur: false,

  // Physical Examination — Abdomen
  abEssentiallyNormal: true,
  abOthersChk: false,
  abOthers: "",
  abAbdominalRigidity: false,
  abAbdominalTenderness: false,
  abHyperactiveBowelSounds: false,
  abPalpableMasses: false,
  abTympaniticDullAbdomen: false,
  abUterineContraction: false,

  // Physical Examination — GU (IE)
  guEssentiallyNormal: false,
  guBloodStainedInExamFinger: false,
  guCervicalDilatation: false,
  guPresenceofAbnormalDischarge: false,
  guOthersChk: true,
  guOthers: "NOT EXAMINE",

  // Physical Examination — Skin/Extremities
  seEssentiallyNormal: true,
  sePoorSkinTurgor: false,
  seClubbing: false,
  seRashesPetechiae: false,
  seColdClammy: false,
  seWeakPulse: false,
  seCyanosisMottledSkin: false,
  seOthersChk: false,
  seOthers: "",
  seEdemaSwelling: false,
  seDecreasedMobility: false,
  sePaleNailbeds: false,

  // Physical Examination — Neuro-exam
  neEssentiallyNormal: true,
  nePoorCoordination: false,
  neAbnormalGait: false,
  neOthersChk: false,
  neOthers: "",
  neAbnormalPositionSense: false,
  neAbnormalSensation: false,
  neAbnormalReflexes: false,
  nePoorAlteredMemory: false,
  nePoorMuscleToneStrength: false,
};

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
// Field <-> settings-key mapping. Starts with the hand-written fields
// already in cf4.html, then SYMPTOM_COLUMNS / PHYSICAL_EXAM_SECTIONS
// below fill in the rest as they're rendered.
// ---------------------------------------------------------------------------
const fields = {
  chief_complaint: document.getElementById("chiefComplaint"),
  history_of_present_illness: document.getElementById("historyOfPresentIllness"),
  pertinent_past_medical_history: document.getElementById("pertinentPastMedicalHistory"),
  general_survey_awake_alert: document.getElementById("generalSurveyAwakeAlert"),
  course_in_ward_order: document.getElementById("courseInWardOrder"),
};

function makeCheckboxRow(field, inline) {
  const row = document.createElement("label");
  row.className = inline ? "checkbox-row inline-specify" : "checkbox-row";

  const box = document.createElement("input");
  box.type = "checkbox";
  box.id = `field_${field.key}`;
  fields[field.key] = box;

  const text = document.createElement("span");
  text.textContent = field.label;

  row.appendChild(box);
  row.appendChild(text);

  if (field.specifyKey) {
    const specify = document.createElement("input");
    specify.type = "text";
    specify.id = `field_${field.specifyKey}`;
    specify.placeholder = "Specify";
    fields[field.specifyKey] = specify;
    row.appendChild(specify);
    row.classList.add("inline-specify");
  }

  return row;
}

// ---------------------------------------------------------------------------
// Render: Pertinent Signs and Symptoms (4 fixed columns)
// ---------------------------------------------------------------------------
function renderSymptoms() {
  const container = document.getElementById("symptomsColumns");
  container.innerHTML = "";

  SYMPTOM_COLUMNS.forEach((column) => {
    const col = document.createElement("div");
    col.className = "symptom-col";
    column.forEach((field) => col.appendChild(makeCheckboxRow(field, Boolean(field.specifyKey))));
    container.appendChild(col);
  });
}

// ---------------------------------------------------------------------------
// Render: Physical Examination (one block per system)
// ---------------------------------------------------------------------------
function renderPhysicalExam() {
  const container = document.getElementById("physicalExamSections");
  container.innerHTML = "";

  PHYSICAL_EXAM_SECTIONS.forEach((section) => {
    const block = document.createElement("div");
    block.className = "system-block";

    const label = document.createElement("div");
    label.className = "system-label";
    label.textContent = section.system;
    block.appendChild(label);

    const grid = document.createElement("div");
    grid.className = "system-grid";
    section.fields.forEach((field) => grid.appendChild(makeCheckboxRow(field, Boolean(field.specifyKey))));
    block.appendChild(grid);

    container.appendChild(block);
  });
}

renderSymptoms();
renderPhysicalExam();

// ---------------------------------------------------------------------------
// Apply / read values (generic — works for every field registered above,
// hand-written or rendered)
// ---------------------------------------------------------------------------
function applyValues(values) {
  for (const [key, el] of Object.entries(fields)) {
    if (!el) continue;
    if (el.type === "checkbox") {
      el.checked = Boolean(values[key]);
    } else {
      el.value = values[key] ?? "";
    }
  }
}

function readValues() {
  const values = {};
  for (const [key, el] of Object.entries(fields)) {
    if (!el) continue;
    values[key] = el.type === "checkbox" ? el.checked : el.value;
  }
  return values;
}

// ---------------------------------------------------------------------------
// Save note helper
// ---------------------------------------------------------------------------
const saveNote = document.getElementById("saveNote");
let saveNoteTimer = null;

function setSaveNote(text, cssClass) {
  saveNote.textContent = text;
  saveNote.className = `save-note ${cssClass || ""}`;
  clearTimeout(saveNoteTimer);
  if (text) {
    saveNoteTimer = setTimeout(() => {
      saveNote.textContent = "";
      saveNote.className = "save-note";
    }, 3000);
  }
}

// ---------------------------------------------------------------------------
// Load current settings on open
// ---------------------------------------------------------------------------
async function loadSettings() {
  try {
    const values = await fetchJSON("/api/cf4/settings");
    applyValues({ ...DEFAULT_CF4_SETTINGS, ...values });
  } catch (err) {
    applyValues(DEFAULT_CF4_SETTINGS);
    setSaveNote("Could not load saved values — showing defaults.", "error");
  }
}

// ---------------------------------------------------------------------------
// Save / Reset / Cancel
// ---------------------------------------------------------------------------
const btnSave = document.getElementById("btnSave");

btnSave.addEventListener("click", async () => {
  btnSave.disabled = true;
  try {
    await fetchJSON("/api/cf4/settings", {
      method: "POST",
      body: JSON.stringify(readValues()),
    });
    setSaveNote("Saved.", "success");
  } catch (err) {
    setSaveNote("Failed to save.", "error");
  } finally {
    btnSave.disabled = false;
  }
});

document.getElementById("btnResetDefaults").addEventListener("click", () => {
  applyValues(DEFAULT_CF4_SETTINGS);
  setSaveNote("Reset to defaults (not yet saved).", "");
});

document.getElementById("btnCancel").addEventListener("click", () => {
  window.beabots?.close();
});

loadSettings();

// ---------------------------------------------------------------------------
// TAB SWITCHING
// ---------------------------------------------------------------------------
const tabBtns = document.querySelectorAll(".tab-btn");
const tabContents = document.querySelectorAll(".tab-content");

tabBtns.forEach((btn) => {
  btn.addEventListener("click", () => {
    const tabName = btn.dataset.tab;
    
    // Deactivate all tabs
    tabBtns.forEach((b) => b.classList.remove("active"));
    tabContents.forEach((content) => content.classList.remove("active"));
    
    // Activate selected tab
    btn.classList.add("active");
    document.getElementById(tabName).classList.add("active");
  });
});

// ---------------------------------------------------------------------------
// AUTOMATION: Transmittal textarea + count label 
// ---------------------------------------------------------------------------
const transmittalsInput = document.getElementById("transmittalsInput");
const countLabel = document.getElementById("countLabel");
let cf4Rows = [];
let cf4Running = false;

function updateCount() {
  const lines = transmittalsInput.value.split("\n").map((l) => l.trim()).filter(Boolean);
  const n = lines.length;
  countLabel.textContent = `${n} transmittal${n !== 1 ? "s" : ""}`;
}
transmittalsInput.addEventListener("keyup", updateCount);

// ---------------------------------------------------------------------------
// AUTOMATION: Log box (same colour-tag detection as ui.py's write_log)
// ---------------------------------------------------------------------------
const logBox = document.getElementById("logBox");
document.querySelectorAll(".cf4-log-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".cf4-log-tab").forEach((item) => {
      const active = item === tab;
      item.classList.toggle("active", active);
      item.setAttribute("aria-selected", String(active));
    });
    document.querySelectorAll(".cf4-log-panel").forEach((panel) => panel.classList.remove("active"));
    const panel = document.getElementById(tab.dataset.logPanel);
    panel.classList.add("active");
    panel.scrollTop = panel.scrollHeight;
  });
});

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

  const processingMatch = message.match(/TRANSMITTAL\s+(\d+)\/(\d+)\s*:/i);
  if (processingMatch) {
    updateCf4Row(Number(processingMatch[1]) - 1, "running");
  }
}

function clearLogs() {
  logBox.innerHTML = "";
}

// ---------------------------------------------------------------------------
// AUTOMATION: Dashboard-style summary report
// ---------------------------------------------------------------------------
const reportBox = document.getElementById("reportBox");

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

function setCf4Rows(transmittals) {
  cf4Rows = transmittals.map((transmittal) => ({
    transmittal,
    status: "waiting",
  }));
}

function updateCf4Row(index, status) {
  if (index < 0 || index >= cf4Rows.length) return;
  cf4Rows.forEach((row, rowIndex) => {
    if (row.status === "running" && rowIndex !== index) {
      row.status = "waiting";
    }
  });
  cf4Rows[index].status = status;
  showReport();
  const row = reportBox.querySelector(`.cf4-status-row[data-index="${index}"]`);
  if (row) row.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function applyCf4Results(results) {
  const resultByTransmittal = new Map((results || []).map((item) => [
    String(item.transmittal || ""),
    String(item.status || "FAILED").toLowerCase(),
  ]));
  cf4Rows = cf4Rows.map((row) => ({
    ...row,
    status: resultByTransmittal.get(String(row.transmittal)) || row.status,
  }));
}

function showReport(results) {
  if (Array.isArray(results) && results.length) {
    applyCf4Results(results);
  }

  const success = cf4Rows.filter((item) => item.status === "success").length;
  const skipped = cf4Rows.filter((item) => item.status === "skipped").length;
  const failed = cf4Rows.filter((item) => item.status === "failed").length;
  const done = success + skipped + failed;
  const total = cf4Rows.length;
  const statusText = total
    ? `CF4 automation ${cf4Running ? "is running" : done === total ? "finished" : "is ready"}. ${done}/${total} result line${done === 1 ? "" : "s"} captured.`
    : "Enter transmittals and start automation.";
  const rows = cf4Rows.map((row, index) => `
    <tr class="cf4-status-row ${row.status}" data-index="${index}">
      <td>
        <span class="cf4-row-status">
          <span class="cf4-row-dot"></span>
          <span>${statusLabel(row.status)}</span>
        </span>
      </td>
      <td title="${escapeHtml(row.transmittal)}">${escapeHtml(row.transmittal)}</td>
    </tr>
  `).join("");

  reportBox.innerHTML = `
    <div class="cf4-summary">
      <div class="cf4-summary-strip">
        <div class="cf4-summary-stat">
          <div class="label">Total</div>
          <div class="value">${total}</div>
        </div>
        <div class="cf4-summary-stat success">
          <div class="label">Success</div>
          <div class="value">${success}</div>
        </div>
        <div class="cf4-summary-stat warning">
          <div class="label">Warnings</div>
          <div class="value">${skipped}</div>
        </div>
        <div class="cf4-summary-stat error">
          <div class="label">Errors</div>
          <div class="value">${failed}</div>
        </div>
      </div>
      <div class="cf4-summary-status">${statusText}</div>
      <div class="cf4-status-table-wrap">
        <table class="cf4-status-table">
          <thead>
            <tr>
              <th>Status</th>
              <th>Transmittal</th>
            </tr>
          </thead>
          <tbody>
            ${rows || `<tr class="cf4-status-row waiting"><td><span class="cf4-row-status"><span class="cf4-row-dot"></span><span>Ready</span></span></td><td>No transmittals loaded.</td></tr>`}
          </tbody>
        </table>
      </div>
    </div>`;
  reportBox.scrollTop = reportBox.scrollHeight;
}

// ---------------------------------------------------------------------------
// AUTOMATION: Status and controls
// ---------------------------------------------------------------------------
const startBtn = document.getElementById("startBtn");

function disableControls() {
  startBtn.disabled = true;
  transmittalsInput.disabled = true;
  startBtn.classList.add("running");
}

function enableControls() {
  startBtn.disabled = false;
  transmittalsInput.disabled = false;
  startBtn.classList.remove("running");
}

// ---------------------------------------------------------------------------
// AUTOMATION: Start automation (same flow as ui.py's start_automation())
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
  setCf4Rows(transmittals);
  cf4Running = true;
  showReport();
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
    cf4Running = false;
    showReport();
    enableControls();
  }
});

// ---------------------------------------------------------------------------
// AUTOMATION: Socket.IO — live log stream + completion events from server.py
// ---------------------------------------------------------------------------
const socket = io(API_BASE);

socket.on("log", (data) => {
  if (!cf4Running) return;
  writeLog(data.message, data.level);
});

socket.on("beacon_done", (data) => {
  cf4Running = false;
  showReport(data.results);
  enableControls();
});

// Initialize counts on load
showReport([]);
updateCount();
