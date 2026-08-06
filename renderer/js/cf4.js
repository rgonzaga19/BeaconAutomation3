/*
 * CF4 Auto Encode defaults — renderer logic.
 *
 * This screen edits the values beacon.py's `auto_encode_cf4` branch used
 * to have hardcoded (chief complaint text, which "Essentially normal"
 * boxes get checked, the GU (IE) Others remark, the Course in the Ward
 * order text). They're persisted through server.py's /api/cf4/settings
 * endpoint (stored alongside the rest of settings.json under a "cf4" key)
 * and read back by server.py right before it kicks off /api/beacon/start,
 * so beacon.run(..., cf4_data=...) always gets whatever was last saved
 * here instead of a fixed string in the script.
 *
 * Assumes the same `window.beabots` preload bridge as dashboard.js
 * (minimize/maximize/close window chrome) — see dashboard.js's header
 * comment for what that bridge needs to expose.
 */

// API_BASE, fetchJSON, showModal, showError all live in common.js (loaded before this file).

// Mirrors DEFAULT_CF4_SETTINGS in server.py — kept here too so Reset to
// Defaults works instantly without a round trip, and so the form has
// sane values even if the initial GET fails.
const DEFAULT_CF4_SETTINGS = {
  chief_complaint: "FOR HEMODIALYSIS",
  body_weakness: true,
  lower_extremity_edema: true,
  general_survey_awake_alert: true,
  heent_normal: true,
  chest_lungs_normal: true,
  cvs_normal: true,
  abdomen_normal: true,
  gu_others: true,
  gu_others_text: "NOT EXAMINE",
  skin_extremities_normal: true,
  neuro_exam_normal: true,
  course_in_ward_order: "UF GOAL MET AT L",
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
// Field <-> settings-key mapping
// ---------------------------------------------------------------------------
const fields = {
  chief_complaint: document.getElementById("chiefComplaint"),
  body_weakness: document.getElementById("bodyWeakness"),
  lower_extremity_edema: document.getElementById("lowerExtremityEdema"),
  general_survey_awake_alert: document.getElementById("generalSurveyAwakeAlert"),
  heent_normal: document.getElementById("heentNormal"),
  chest_lungs_normal: document.getElementById("chestLungsNormal"),
  cvs_normal: document.getElementById("cvsNormal"),
  abdomen_normal: document.getElementById("abdomenNormal"),
  gu_others: document.getElementById("guOthers"),
  gu_others_text: document.getElementById("guOthersText"),
  skin_extremities_normal: document.getElementById("skinExtremitiesNormal"),
  neuro_exam_normal: document.getElementById("neuroExamNormal"),
  course_in_ward_order: document.getElementById("courseInWardOrder"),
};

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