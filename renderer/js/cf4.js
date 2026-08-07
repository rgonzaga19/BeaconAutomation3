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
 * the single source of truth. Those keys — and the guessed DOM `name`
 * attributes beacon.py checks against live Beacon (which live in
 * beacon.py, not here) — were guessed from screenshots of the live CF4
 * form, not read from Beacon's source. If any turn out wrong, this is
 * the only place the settings-side key needs correcting; beacon.py's
 * matching DEFAULT_CF4_DATA key and locator need the same fix there.
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
      { key: "se_edema_swelling", label: "Edema/swelling" },
      { key: "seEdemaSwelling", label: "Decreased mobility" },
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
// Defaults — mirrors DEFAULT_CF4_SETTINGS in server.py exactly (same keys,
// same values). "Essentially normal" boxes, Body weakness, Lower
// extremity edema, and GU (IE) Others default checked, matching what
// beacon.py's auto-encode step always did before this screen existed;
// every other symptom/finding defaults unchecked.
// ---------------------------------------------------------------------------
const DEFAULT_CF4_SETTINGS = {
  chief_complaint: "FOR HEMODIALYSIS",
  history_of_present_illness: "N/A",
  pertinent_past_medical_history: "N/A",
  general_survey_awake_alert: true,
  course_in_ward_order: "UF GOAL MET AT L",

  // Pertinent Signs and Symptoms
  altered_mental_sensorium: false,
  abdominal_cramp_pain: false,
  anorexia: false,
  bleeding_gums: false,
  body_weakness: true,
  blurring_vision: false,
  chest_pain_discomfort: false,
  constipation: false,
  cough: false,
  diarrhea: false,
  dizziness: false,
  dysphagia: false,
  dyspnea: false,
  dysuria: false,
  epistaxis: false,
  fever: false,
  frequency_of_urination: false,
  headache: false,
  hematemesis: false,
  hematuria: false,
  hemoptysis: false,
  irritability: false,
  jaundice: false,
  lower_extremity_edema: true,
  myalgia: false,
  orthopnea: false,
  pain: false,
  pain_specify: "",
  palpitations: false,
  seizures: false,
  skin_rashes: false,
  stool_bloody_black_tarry_mucoid: false,
  sweating: false,
  urgency: false,
  vomiting: false,
  weight_loss: false,
  others: false,
  others_specify: "",

  // Physical Examination — HEENT
  he_essentially_normal: true,
  he_sunken_fontanelle: false,
  he_abnormal_pupillary_reaction: false,
  he_others: false,
  he_others_text: "",
  he_cervical_lymphadenopathy: false,
  he_dry_mucous_membrane: false,
  he_icteric_sclerae: false,
  he_pale_conjunctivae: false,
  he_sunken_eyeballs: false,

  // Physical Examination — Chest / Lungs
  cl_essentially_normal: true,
  cl_others: false,
  cl_others_text: "",
  cl_asymmetrical_chest_expansion: false,
  cl_decreased_breath_sounds: false,
  cl_wheezes: false,
  cl_lumps_over_breasts: false,
  cl_rales_crackles_rhonchi: false,
  cl_intercostal_rib_retraction: false,

  // Physical Examination — CVS
  cv_essentially_normal: true,
  cv_others: false,
  cv_others_text: "",
  cv_displaced_apex_beat: false,
  cv_heave_and_or_thrills: false,
  cv_pericardial_bulge: false,
  cv_irregular_rhythm: false,
  cv_muffled_heart_sounds: false,
  cv_murmur: false,

  // Physical Examination — Abdomen
  ab_essentially_normal: true,
  ab_others: false,
  ab_others_text: "",
  ab_abdominal_rigidity: false,
  ab_abdominal_tenderness: false,
  ab_hyperactive_bowel_sounds: false,
  ab_palpable_masses: false,
  ab_tympanitic_dull_abdomen: false,
  ab_uterine_contraction: false,

  // Physical Examination — GU (IE)
  gu_essentially_normal: false,
  gu_blood_stained_in_examining_finger: false,
  gu_cervical_dilatation: false,
  gu_presence_of_abnormal_discharge: false,
  gu_others: true,
  gu_others_text: "NOT EXAMINE",

  // Physical Examination — Skin/Extremities
  se_essentially_normal: true,
  se_poor_skin_turgor: false,
  se_clubbing: false,
  se_rashes_petechiae: false,
  se_cold_clammy_skin: false,
  se_weak_pulses: false,
  se_cyanosis_mottled_skin: false,
  se_others: false,
  se_others_text: "",
  se_edema_swelling: false,
  se_decreased_mobility: false,
  se_pale_nailbeds: false,

  // Physical Examination — Neuro-exam
  ne_essentially_normal: true,
  ne_poor_coordination: false,
  ne_abnormal_gait: false,
  ne_others: false,
  ne_others_text: "",
  ne_abnormal_position_sense: false,
  ne_abnormal_sensation: false,
  ne_presence_of_abnormal_reflexes: false,
  ne_poor_altered_memory: false,
  ne_poor_muscle_tone_strength: false,
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