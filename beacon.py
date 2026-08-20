import os
import sys
import time



if getattr(sys, "frozen", False):
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(
        os.path.dirname(sys.executable),
        "ms-playwright"
    )


from logger import logger
from reports import report

from pathlib import Path
import browser_session

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError


def open_transmittals(page):
    try:
        page.get_by_role("button", name="E-CLAIMS").click()
        _safe_networkidle(page)
    except:
        pass

    page.get_by_text(
        "TRANSMITTALS & CLAIMS",
        exact=False
    ).click()

    _safe_networkidle(page)

    logger.info("Returned to patient list")


def _try_step(step_name, action):
    """Run a single best-effort UI step (used by Auto Encode CF4).

    Logs success/failure and always returns without raising, so one broken
    locator (e.g. a field name that changed) can't derail the rest of the
    Auto Encode steps or the whole patient record.
    """
    try:
        action()
        logger.success(f"SUCCESS: {step_name}")
        return True
    except Exception as e:
        logger.warning(f"SKIPPED (Auto Encode CF4): {step_name} — {e}")
        return False

def _safe_networkidle(page, timeout=15000):
    """wait_for_load_state('networkidle') but never let it blow up the run.

    Beacon keeps some background polling/websocket traffic alive on several
    screens, so the page can legitimately never reach true 'networkidle'
    within the default 30s timeout. When that happens Playwright raises
    TimeoutError, which — this being called dozens of times per patient —
    was silently turning normal pages into a failed/skipped patient. This
    doesn't change what the automation does; it just stops a slow-to-settle
    network from being treated the same as a real failure.
    """
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except PlaywrightTimeoutError:
        logger.warning("networkidle wait timed out — continuing anyway")

def _log_page_state(page, stage, transmittal_no=None, medicine_index=None, medicine_name=None):
    """Log the current browser/page state for diagnosing automation freezes."""
    try:
        url = page.url

        logger.info(
            f"[WATCHDOG] Stage={stage} | "
            f"Transmittal={transmittal_no or 'N/A'} | "
            f"Medicine={medicine_index if medicine_index is not None else 'N/A'} | "
            f"Name={medicine_name or 'N/A'} | "
            f"URL={url}"
        )

        try:
            logger.info(
                f"[WATCHDOG] Page title: {page.title()}"
            )
        except Exception:
            pass

    except Exception as e:
        logger.warning(f"[WATCHDOG] Unable to read page state: {e}")

# Maximum time allowed for one medicine-mapping operation.
# This is detection only for now — no automatic recovery yet.
MEDICINE_OPERATION_TIMEOUT = 30

def _check_medicine_operation_timeout(
    start_time,
    page,
    transmittal_no,
    medicine_index,
    medicine_name,
    stage
):
    """Detect when a medicine operation has taken too long.

    This function does not recover or reload anything yet.
    It only logs the timeout and returns True/False.
    """
    elapsed = time.monotonic() - start_time

    if elapsed >= MEDICINE_OPERATION_TIMEOUT:
        logger.error(
            f"[WATCHDOG] MEDICINE TIMEOUT — "
            f"elapsed={elapsed:.1f}s | "
            f"limit={MEDICINE_OPERATION_TIMEOUT}s"
        )

        _log_page_state(
            page,
            f"TIMEOUT: {stage}",
            transmittal_no=transmittal_no,
            medicine_index=medicine_index,
            medicine_name=medicine_name
        )

        return True

    return False


def _wait_for_save_changes_complete(page, dialog_timeout=15000, settle_buffer=1500):
    """After clicking Beacon's SAVE CHANGES confirmation (the workaround we
    use in place of the hard-to-target CF4 Fab), don't assume the save
    landed just because the click succeeded — the click can fire off the
    save request AND start navigating away in the same breath, and
    `_safe_networkidle` alone isn't a reliable enough signal (it can time
    out on its own due to Beacon's background polling, per its docstring,
    regardless of whether the save actually finished).

    Primary signal: wait for the SAVE CHANGES dialog/text to actually
    disappear — Beacon dismisses it once the save call resolves.

    Fallback: if it doesn't disappear within `dialog_timeout` (dialog
    never rendered the way expected, or got swapped out already), fall
    back to a fixed settle buffer so we still give the in-flight request
    time to land before the caller navigates again.

    Either way, add a small extra buffer afterward — the dialog can
    visually dismiss slightly before the backend write actually commits.
    """
    try:
        page.wait_for_selector(
            "text=SAVE CHANGES",
            state="hidden",
            timeout=dialog_timeout
        )
        logger.info("SAVE CHANGES dialog dismissed — save confirmed")
    except PlaywrightTimeoutError:
        logger.warning(
            "SAVE CHANGES dialog didn't disappear within "
            f"{dialog_timeout}ms — falling back to a fixed settle buffer"
        )

    page.wait_for_timeout(settle_buffer)


def _search_transmittal(page, transmittal_no, attempts=3):
    """Type the transmittal number into the search box and confirm the
    table actually refreshed before deciding it was found or not found.

    Root cause of the intermittent false "not found" (especially on
    repeats): Beacon's results table is client-rendered, so `networkidle`
    can fire and the fixed 2s pause can elapse before the table has
    actually swapped in the new rows — at that instant `tbody tr` is
    either still empty or (worse) still showing the *previous* search's
    row. Reading the count at that exact moment made a transmittal that
    was really there look "not found".

    Instead of a fixed sleep, this polls the DOM until either matching
    rows appear or Beacon explicitly reports no results, and it verifies
    the first row's text actually contains the transmittal number rather
    than trusting a bare row count. If a stale/mismatched row is caught,
    it retries the search rather than giving up immediately.

    Returns True if the transmittal was found, False otherwise. Logging
    and reporting for the "not found" case are left to the caller so the
    overall behavior/flow is unchanged.
    """
    search_box = page.locator('input[type="text"]').first

    for attempt in range(1, attempts + 1):
        search_box.click()
        search_box.press("Control+A")
        search_box.press("Backspace")
        search_box.fill(transmittal_no)
        search_box.press("Enter")

        _safe_networkidle(page)

        # Poll instead of a fixed sleep: wait until rows show up or Beacon
        # explicitly says there's nothing, whichever happens first.
        try:
            page.wait_for_function(
                """() => {
                    const rows = document.querySelectorAll('tbody tr');
                    if (rows.length > 0) return true;
                    const bodyText = (document.body.innerText || '').toLowerCase();
                    return bodyText.includes('no data') ||
                           bodyText.includes('no record') ||
                           bodyText.includes('no results');
                }""",
                timeout=8000
            )
        except PlaywrightTimeoutError:
            pass

        # Small settle buffer for slower renders even after the poll passes.
        page.wait_for_timeout(400)

        row_count = page.locator("tbody tr").count()

        if row_count > 0:
            first_row_text = page.locator("tbody tr").first.inner_text()

            if transmittal_no in first_row_text:
                return True

            if attempt < attempts:
                logger.warning(
                    f"Table showed a row not matching {transmittal_no} "
                    f"(likely stale from a previous search) on attempt "
                    f"{attempt}/{attempts}; retrying..."
                )
                page.wait_for_timeout(1000)
                continue

            # Last attempt: trust the row count even if the exact string
            # match failed (e.g. formatting differences), same as the
            # original behavior.
            return True

        if attempt < attempts:
            logger.warning(
                f"No rows found for transmittal {transmittal_no} on attempt "
                f"{attempt}/{attempts}; retrying search..."
            )
            page.wait_for_timeout(1500)

    return page.locator("tbody tr").count() > 0


# Same values this branch used to have hardcoded inline — now the
# fallback for any key the caller (server.py) doesn't supply in
# `cf4_data`, so a run triggered without a cf4_data dict (e.g. direct
# script testing) still behaves exactly as before. Keep this in lockstep
# with DEFAULT_CF4_SETTINGS in server.py / js/cf4.js — same keys, same
# values.
#
# The settings keys below (snake_case) are ours; the Beacon DOM `name`
# attributes each one is checked/filled against inside the auto_encode_cf4
# block further down (camelCase, e.g. "heSunkenFontanelle") were guessed
# from screenshots of the live CF4 form, not read from Beacon's source —
# expect some of those locators to need correcting once verified live.
# Same values this branch used to have hardcoded inline — now the
# fallback for any key the caller (server.py) doesn't supply in
# `cf4_data`, so a run triggered without a cf4_data dict (e.g. direct
# script testing) still behaves exactly as before. Keep this in lockstep
# with DEFAULT_CF4_SETTINGS in server.py / js/cf4.js — same keys, same
# values.
#
# For "Pertinent Signs and Symptoms" and "Physical Examination", each key
# below IS the confirmed Beacon input `name` attribute (camelCase) — no
# separate settings-key -> DOM-name translation anymore, CF4_CHECKBOX_FIELDS
# further down just uses the key directly as the locator. A few of these
# (e.g. heCervicalLympadenopathy, guPresenceofAbnormalDischarge,
# clLumpsOverBreast) preserve exact casing/spelling quirks from the live
# form — don't "fix" the spelling, it needs to match Beacon's actual
# attribute.
DEFAULT_CF4_DATA = {
    "chief_complaint": "FOR HEMODIALYSIS",
    "history_of_present_illness": "N/A",
    "pertinent_past_medical_history": "N/A",
    "general_survey_awake_alert": True,
    "course_in_ward_order": "UF GOAL MET AT L",

    # Pertinent Signs and Symptoms
    "alteredMentalSensorium": False,
    "abdominalCrampPain": False,
    "anorexia": False,
    "bleedingGums": False,
    "bodyWeakness": True,
    "blurringOfVision": False,
    "chestPainDiscomfort": False,
    "constipation": False,
    "cough": False,
    "diarrhea": False,
    "dizziness": False,
    "dysphagia": False,
    "dyspnea": False,
    "dysuria": False,
    "epistaxis": False,
    "fever": False,
    "frequencyOfUrination": False,
    "headache": False,
    "hematemesis": False,
    "hematuria": False,
    "hemoptysis": False,
    "irritability": False,
    "jaundice": False,
    "lowerExtremityEdema": True,
    "myalgia": False,
    "orthopnea": False,
    "pain": False,
    "painSpecify": "",
    "palpitations": False,
    "seizure": False,
    "skinRashes": False,
    "stoolBloodyBlackTarryMucoid": False,
    "sweating": False,
    "urgency": False,
    "vomiting": False,
    "weightLoss": False,
    "others": False,
    "othersSpecify": "",

    # Physical Examination — HEENT
    "heEssentiallyNormal": True,
    "heSunkenFontanelle": False,
    "heAbnormalPupillaryReaction": False,
    "heOthersChk": False,
    "heOthers": "",
    "heCervicalLympadenopathy": False,
    "heDryMucousMembrane": False,
    "heIctericSclerae": False,
    "hePaleConjunctivae": False,
    "heSunkenEyeballs": False,

    # Physical Examination — Chest / Lungs
    "clEssentiallyNormal": True,
    "clOthersChk": False,
    "clOthers": "",
    "clAsymmetricalChestExpansion": False,
    "clDecreasedBreathSounds": False,
    "clWheezes": False,
    "clLumpsOverBreast": False,
    "clCracklesRales": False,
    "clRetractions": False,

    # Physical Examination — CVS
    "cvEssentiallyNormal": True,
    "cvOthersChk": False,
    "cvOthers": "",
    "cvDisplacedApexBeat": False,
    "cvHeavesThrills": False,
    "cvPericardialBulge": False,
    "cvIrregularRhythm": False,
    "cvMuffledHeartSounds": False,
    "cvMurmur": False,

    # Physical Examination — Abdomen
    "abEssentiallyNormal": True,
    "abOthersChk": False,
    "abOthers": "",
    "abAbdominalRigidity": False,
    "abAbdominalTenderness": False,
    "abHyperactiveBowelSounds": False,
    "abPalpableMasses": False,
    "abTympaniticDullAbdomen": False,
    "abUterineContraction": False,

    # Physical Examination — GU (IE)
    "guEssentiallyNormal": False,
    "guBloodStainedInExamFinger": False,
    "guCervicalDilatation": False,
    "guPresenceofAbnormalDischarge": False,
    "guOthersChk": True,
    "guOthers": "NOT EXAMINE",

    # Physical Examination — Skin/Extremities
    "seEssentiallyNormal": True,
    "sePoorSkinTurgor": False,
    "seClubbing": False,
    "seRashesPetechiae": False,
    "seColdClammy": False,
    "seWeakPulse": False,
    "seCyanosisMottledSkin": False,
    "seOthersChk": False,
    "seOthers": "",
    "seEdemaSwelling": False,
    "seDecreasedMobility": False,
    "sePaleNailbeds": False,

    # Physical Examination — Neuro-exam
    "neEssentiallyNormal": True,
    "nePoorCoordination": False,
    "neAbnormalGait": False,
    "neOthersChk": False,
    "neOthers": "",
    "neAbnormalPositionSense": False,
    "neAbnormalSensation": False,
    "neAbnormalReflexes": False,
    "nePoorAlteredMemory": False,
    "nePoorMuscleToneStrength": False,
}

# ---------------------------------------------------------------------------
# CF4 checkbox field table — settings_key doubles as the Beacon input's
# `name` attribute for every row here (see the note on DEFAULT_CF4_DATA
# above: these keys were confirmed against the live CF4 form, not guessed).
# The dom_name column is kept as its own field rather than collapsed away
# so a future rename only needs the settings key changed once — but for
# every row below the two columns are intentionally identical.
#
# Each tuple: (settings_key, dom_name, label, specify_settings_key, specify_dom_name)
CF4_CHECKBOX_FIELDS = [
    # Pertinent Signs and Symptoms
    ("alteredMentalSensorium", "alteredMentalSensorium", "Altered Mental Sensorium", None, None),
    ("abdominalCrampPain", "abdominalCrampPain", "Abdominal cramp/pain", None, None),
    ("anorexia", "anorexia", "Anorexia", None, None),
    ("bleedingGums", "bleedingGums", "Bleeding gums", None, None),
    ("bodyWeakness", "bodyWeakness", "Body weakness", None, None),
    ("blurringOfVision", "blurringOfVision", "Blurring vision", None, None),
    ("chestPainDiscomfort", "chestPainDiscomfort", "Chest pain/discomfort", None, None),
    ("constipation", "constipation", "Constipation", None, None),
    ("cough", "cough", "Cough", None, None),
    ("diarrhea", "diarrhea", "Diarrhea", None, None),
    ("dizziness", "dizziness", "Dizziness", None, None),
    ("dysphagia", "dysphagia", "Dysphagia", None, None),
    ("dyspnea", "dyspnea", "Dyspnea", None, None),
    ("dysuria", "dysuria", "Dysuria", None, None),
    ("epistaxis", "epistaxis", "Epistaxis", None, None),
    ("fever", "fever", "Fever", None, None),
    ("frequencyOfUrination", "frequencyOfUrination", "Frequency of urination", None, None),
    ("headache", "headache", "Headache", None, None),
    ("hematemesis", "hematemesis", "Hematemesis", None, None),
    ("hematuria", "hematuria", "Hematuria", None, None),
    ("hemoptysis", "hemoptysis", "Hemoptysis", None, None),
    ("irritability", "irritability", "Irritability", None, None),
    ("jaundice", "jaundice", "Jaundice", None, None),
    ("lowerExtremityEdema", "lowerExtremityEdema", "Lower extremity edema", None, None),
    ("myalgia", "myalgia", "Myalgia", None, None),
    ("orthopnea", "orthopnea", "Orthopnea", None, None),
    ("pain", "pain", "Pain", "painSpecify", "painSpecify"),
    ("palpitations", "palpitations", "Palpitations", None, None),
    ("seizure", "seizure", "Seizures", None, None),
    ("skinRashes", "skinRashes", "Skin rashes", None, None),
    ("stoolBloodyBlackTarryMucoid", "stoolBloodyBlackTarryMucoid", "Stool, bloody/black tarry/mucoid", None, None),
    ("sweating", "sweating", "Sweating", None, None),
    ("urgency", "urgency", "Urgency", None, None),
    ("vomiting", "vomiting", "Vomiting", None, None),
    ("weightLoss", "weightLoss", "Weight loss", None, None),
    ("others", "others", "Others (Signs and Symptoms)", "othersSpecify", "othersSpecify"),

    # Physical Examination — HEENT
    ("heEssentiallyNormal", "heEssentiallyNormal", "HEENT — Essentially normal", None, None),
    ("heSunkenFontanelle", "heSunkenFontanelle", "HEENT — Sunken fontanelle", None, None),
    ("heAbnormalPupillaryReaction", "heAbnormalPupillaryReaction", "HEENT — Abnormal pupillary reaction", None, None),
    ("heOthersChk", "heOthersChk", "HEENT — Others", "heOthers", "heOthers"),
    ("heCervicalLympadenopathy", "heCervicalLympadenopathy", "HEENT — Cervical lymphadenopathy", None, None),
    ("heDryMucousMembrane", "heDryMucousMembrane", "HEENT — Dry mucous membrane", None, None),
    ("heIctericSclerae", "heIctericSclerae", "HEENT — Icteric sclerae", None, None),
    ("hePaleConjunctivae", "hePaleConjunctivae", "HEENT — Pale conjunctivae", None, None),
    ("heSunkenEyeballs", "heSunkenEyeballs", "HEENT — Sunken eyeballs", None, None),

    # Physical Examination — Chest / Lungs
    ("clEssentiallyNormal", "clEssentiallyNormal", "CHEST/LUNGS — Essentially normal", None, None),
    ("clOthersChk", "clOthersChk", "CHEST/LUNGS — Others", "clOthers", "clOthers"),
    ("clAsymmetricalChestExpansion", "clAsymmetricalChestExpansion", "CHEST/LUNGS — Asymmetrical chest expansion", None, None),
    ("clDecreasedBreathSounds", "clDecreasedBreathSounds", "CHEST/LUNGS — Decreased breath sounds", None, None),
    ("clWheezes", "clWheezes", "CHEST/LUNGS — Wheezes", None, None),
    ("clLumpsOverBreast", "clLumpsOverBreast", "CHEST/LUNGS — Lump/s over breast(s)", None, None),
    ("clCracklesRales", "clCracklesRales", "CHEST/LUNGS — Rales/crackles/rhonchi", None, None),
    ("clRetractions", "clRetractions", "CHEST/LUNGS — Intercostal rib retraction", None, None),

    # Physical Examination — CVS
    ("cvEssentiallyNormal", "cvEssentiallyNormal", "CVS — Essentially normal", None, None),
    ("cvOthersChk", "cvOthersChk", "CVS — Others", "cvOthers", "cvOthers"),
    ("cvDisplacedApexBeat", "cvDisplacedApexBeat", "CVS — Displaced apex beat", None, None),
    ("cvHeavesThrills", "cvHeavesThrills", "CVS — Heave and/or thrills", None, None),
    ("cvPericardialBulge", "cvPericardialBulge", "CVS — Pericardial bulge", None, None),
    ("cvIrregularRhythm", "cvIrregularRhythm", "CVS — Irregular rhythm", None, None),
    ("cvMuffledHeartSounds", "cvMuffledHeartSounds", "CVS — Muffled heart sounds", None, None),
    ("cvMurmur", "cvMurmur", "CVS — Murmur", None, None),

    # Physical Examination — Abdomen
    ("abEssentiallyNormal", "abEssentiallyNormal", "ABDOMEN — Essentially normal", None, None),
    ("abOthersChk", "abOthersChk", "ABDOMEN — Others", "abOthers", "abOthers"),
    ("abAbdominalRigidity", "abAbdominalRigidity", "ABDOMEN — Abdominal rigidity", None, None),
    ("abAbdominalTenderness", "abAbdominalTenderness", "ABDOMEN — Abdominal tenderness", None, None),
    ("abHyperactiveBowelSounds", "abHyperactiveBowelSounds", "ABDOMEN — Hyperactive bowel sounds", None, None),
    ("abPalpableMasses", "abPalpableMasses", "ABDOMEN — Palpable mass(es)", None, None),
    ("abTympaniticDullAbdomen", "abTympaniticDullAbdomen", "ABDOMEN — Tympanitic/dull abdomen", None, None),
    ("abUterineContraction", "abUterineContraction", "ABDOMEN — Uterine contraction", None, None),

    # Physical Examination — GU (IE)
    ("guEssentiallyNormal", "guEssentiallyNormal", "GU (IE) — Essentially normal", None, None),
    ("guBloodStainedInExamFinger", "guBloodStainedInExamFinger", "GU (IE) — Blood stained in examining finger", None, None),
    ("guCervicalDilatation", "guCervicalDilatation", "GU (IE) — Cervical dilatation", None, None),
    ("guPresenceofAbnormalDischarge", "guPresenceofAbnormalDischarge", "GU (IE) — Presence of abnormal discharge", None, None),
    ("guOthersChk", "guOthersChk", "GU (IE) — Others", "guOthers", "guOthers"),

    # Physical Examination — Skin/Extremities
    ("seEssentiallyNormal", "seEssentiallyNormal", "SKIN/EXTREMITIES — Essentially normal", None, None),
    ("sePoorSkinTurgor", "sePoorSkinTurgor", "SKIN/EXTREMITIES — Poor skin turgor", None, None),
    ("seClubbing", "seClubbing", "SKIN/EXTREMITIES — Clubbing", None, None),
    ("seRashesPetechiae", "seRashesPetechiae", "SKIN/EXTREMITIES — Rashes/petechiae", None, None),
    ("seColdClammy", "seColdClammy", "SKIN/EXTREMITIES — Cold clammy skin", None, None),
    ("seWeakPulse", "seWeakPulse", "SKIN/EXTREMITIES — Weak pulses", None, None),
    ("seCyanosisMottledSkin", "seCyanosisMottledSkin", "SKIN/EXTREMITIES — Cyanosis/mottled skin", None, None),
    ("seOthersChk", "seOthersChk", "SKIN/EXTREMITIES — Others", "seOthers", "seOthers"),
    ("seEdemaSwelling", "seEdemaSwelling", "SKIN/EXTREMITIES — Edema/swelling", None, None),
    ("seDecreasedMobility", "seDecreasedMobility", "SKIN/EXTREMITIES — Decreased mobility", None, None),
    ("sePaleNailbeds", "sePaleNailbeds", "SKIN/EXTREMITIES — Pale nailbeds", None, None),

    # Physical Examination — Neuro-exam
    ("neEssentiallyNormal", "neEssentiallyNormal", "NEURO-EXAM — Essentially normal", None, None),
    ("nePoorCoordination", "nePoorCoordination", "NEURO-EXAM — Poor coordination", None, None),
    ("neAbnormalGait", "neAbnormalGait", "NEURO-EXAM — Abnormal gait", None, None),
    ("neOthersChk", "neOthersChk", "NEURO-EXAM — Others", "neOthers", "neOthers"),
    ("neAbnormalPositionSense", "neAbnormalPositionSense", "NEURO-EXAM — Abnormal position sense", None, None),
    ("neAbnormalSensation", "neAbnormalSensation", "NEURO-EXAM — Abnormal sensation", None, None),
    ("neAbnormalReflexes", "neAbnormalReflexes", "NEURO-EXAM — Presence of abnormal reflex(es)", None, None),
    ("nePoorAlteredMemory", "nePoorAlteredMemory", "NEURO-EXAM — Poor/altered memory", None, None),
    ("nePoorMuscleToneStrength", "nePoorMuscleToneStrength", "NEURO-EXAM — Poor muscle tone/strength", None, None),
]


def _apply_cf4_checkbox(page, cf4_data, settings_key, dom_name, label, specify_settings_key, specify_dom_name):
    """Check one CF4 checkbox if cf4_data enables it, and — for
    "Others"-style rows — fill its paired specify text field too. Every
    step goes through _try_step so one wrong/renamed locator only skips
    that field instead of aborting the whole Auto Encode CF4 run."""
    if not cf4_data.get(settings_key):
        return

    _try_step(
        f"Checked {label}",
        lambda: page.locator(f'input[name="{dom_name}"]').check(force=True)
    )

    if specify_settings_key and specify_dom_name:
        specify_text = cf4_data.get(specify_settings_key, "")
        if specify_text:
            def _fill_specify():
                el = page.locator(f'input[name="{specify_dom_name}"]').first
                el.click()
                el.press("Control+A")
                el.press("Backspace")
                el.fill(specify_text)

            _try_step(f"{label} remarks set to '{specify_text}'", _fill_specify)


def run(transmittals, auto_encode_cf4=False, cf4_data=None):
    cf4_data = {**DEFAULT_CF4_DATA, **(cf4_data or {})}

    try:

        report.results.clear()
    
        page = browser_session.connect()

        browser = browser_session.browser
        context = browser_session.context

        # ── Navigate to Transmittals ───────────────────────────────────────
        logger.info("Opening Transmittals...")
        open_transmittals(page)

        # ── Patient loop ───────────────────────────────────────────────────
        for idx, transmittal_no in enumerate(transmittals):
            try:
                transmittal_no = str(transmittal_no).strip()

                logger.info("\n" + "=" * 60)
                logger.info(f"TRANSMITTAL {idx + 1}/{len(transmittals)} : {transmittal_no}")
                logger.info("=" * 60)

                # ── Search patient ─────────────────────────────────────────
                # ── Search transmittal ─────────────────────────────────────
                if not _search_transmittal(page, transmittal_no):
                    logger.warning(f"TRANSMITTAL NOT FOUND: {transmittal_no}")
                    report.skipped(
                        transmittal=transmittal_no,
                        remarks="Transmittal not found"
                    )                

                    continue

                # ── Open first row → Manage Claims ─────────────────────────
                logger.info("Opening row menu...")
                first_row = page.locator("tbody tr").first
                logger.info("Row found.")
                first_row.locator("button").last.click()
                _safe_networkidle(page)

                logger.info("Clicking Manage Claims...")
                page.get_by_text("Manage Claims", exact=True).click()
                _safe_networkidle(page)
                logger.success("SUCCESS: Manage Claims opened")

                # ── Open claim → Manage ────────────────────────────────────
                logger.info("Opening claim row menu...")
                claim_row = page.locator("tbody tr").first
                claim_row.locator("button").last.click()
                _safe_networkidle(page)

                logger.info("Clicking Manage...")
                page.get_by_text("Manage", exact=True).click()
                _safe_networkidle(page)
                logger.success("SUCCESS: PHIC Claim Details opened")

                # --------------------------------------------------
                # Validate Eligibility (if button appears)
                # --------------------------------------------------
                validate_btn = page.locator("button", has_text="Validate Eligibility")

                if validate_btn.count() > 0:
                    print("Clicking Validate Eligibility...")
                    validate_btn.first.click()
                    _safe_networkidle(page)
                else:
                    print("No validation required — skipping.")

                # ── Move to CF2 ────────────────────────────────────────────
                logger.info("Opening CF2 tab...")
                page.get_by_text("CF2", exact=True).click()

                _safe_networkidle(page)

                page.wait_for_selector(
                    "input[id*='sessionDate-DateMM-DD-YYYY']",
                    timeout=10000
                )

                # --------------------------------------------------
                # Read all Session Dates
                # --------------------------------------------------
                logger.info("Reading session dates...")

                session_date_inputs = page.locator(
                    "input[id*='sessions'][id*='sessionDate-DateMM-DD-YYYY']"
                )

                count = session_date_inputs.count()
                logger.info(f"Found {count} session date(s).")

                session_dates = []

                for i in range(count):
                    value = session_date_inputs.nth(i).input_value().strip()
                    session_dates.append(value)
                    logger.info(f"Session {i + 1}: {value}")

                logger.info(f"All Session Dates: {session_dates}")
        
                # ── Move to CF4 ────────────────────────────────────────────
                logger.info("Opening CF4 tab...")
                page.get_by_text("CF4", exact=True).click()
                _safe_networkidle(page)

                logger.info("Clicking MOVE TO CF4...")
                page.get_by_role("button", name="MOVE TO CF4").click()

                logger.info("Waiting for confirmation dialog...")
                page.wait_for_selector("text=Proceed", timeout=10000)
                page.locator("text=Proceed").first.click(force=True)
                _safe_networkidle(page)
                logger.success("SUCCESS: Moved to CF4")
                # REASON FOR ADMISSION - History of Present Illness / Pertinent Past Medical History ____________

                # --------------------------------------------------
                # REASON FOR ADMISSION
                # Fill only empty fields with "N/A"
                # --------------------------------------------------

                def _fill_if_empty(locator, field_name):
                    try:
                        value = locator.input_value().strip()

                        if not value:
                            logger.info(f"{field_name} is empty. Filling with 'N/A'.")

                            locator.click()
                            locator.press("Control+A")
                            locator.press("Backspace")
                            locator.fill("N/A")
                        else:
                            logger.info(
                                f"{field_name} already has value. Skipping."
                            )

                    except Exception as e:
                        logger.warning(
                            f"Unable to process {field_name}: {e}"
                        )

                history_locator = page.locator(
                    'textarea[name="historyOfPresentIllness"]'
                ).first

                pertinent_locator = page.locator(
                    'textarea[name="pertinentPastMedicalHistory"]'
                ).first

                _fill_if_empty(
                    history_locator,
                    "History of Present Illness"
                )

                _fill_if_empty(
                    pertinent_locator,
                    "Pertinent Past Medical History"
                )





                # ── Auto Encode CF4 (test) ──────────────────────────────────
                if auto_encode_cf4:
                    logger.info("Auto Encode CF4 option is enabled.")

                    chief_complaint_text = cf4_data["chief_complaint"]

                    def _set_chief_complaint():
                        box = page.locator('textarea[name="chiefComplaint"]').first
                        box.click()
                        box.press("Control+A")
                        box.press("Backspace")
                        box.fill(chief_complaint_text)

                    _try_step(
                        f"Chief Complaint set to '{chief_complaint_text}'",
                        _set_chief_complaint
                    )

                    history_of_present_illness_text = cf4_data["history_of_present_illness"]

                    def _set_history_of_present_illness():
                        box = page.locator('textarea[name="historyOfPresentIllness"]').first
                        box.click()
                        box.press("Control+A")
                        box.press("Backspace")
                        box.fill(history_of_present_illness_text)

                    _try_step(
                        f"History of Present Illness set to '{history_of_present_illness_text}'",
                        _set_history_of_present_illness
                    )

                    pertinent_past_medical_history_text = cf4_data["pertinent_past_medical_history"]

                    def _set_pertinent_past_medical_history():
                        box = page.locator('textarea[name="pertinentPastMedicalHistory"]').first
                        box.click()
                        box.press("Control+A")
                        box.press("Backspace")
                        box.fill(pertinent_past_medical_history_text)

                    _try_step(
                        f"Pertinent Past Medical History set to '{pertinent_past_medical_history_text}'",
                        _set_pertinent_past_medical_history
                    )

                    if cf4_data["general_survey_awake_alert"]:
                        _try_step(
                            "General Survey set to 'Awake and alert'",
                            lambda: page.get_by_text("Awake and alert", exact=True).click(force=True)
                        )

                    # All Pertinent Signs and Symptoms + Physical
                    # Examination checkboxes (and their paired "Others"
                    # specify text) live in one table — see
                    # CF4_CHECKBOX_FIELDS above.
                    for settings_key, dom_name, label, specify_key, specify_dom_name in CF4_CHECKBOX_FIELDS:
                        _apply_cf4_checkbox(
                            page, cf4_data, settings_key, dom_name, label,
                            specify_key, specify_dom_name
                        )

                    _try_step(
                        "CF4 form saved",
                        lambda: page.get_by_role("button", name="SAVE").click(force=True)
                    )

                    # --------------------------------------------------
                    # COURSE IN THE WARD
                    # --------------------------------------------------
                    def _open_course_in_the_ward():
                        course_btn = page.locator("button:has-text('COURSE IN THE WARD')").first

                        course_btn.wait_for(state="visible", timeout=10000)
                        course_btn.scroll_into_view_if_needed()
                        page.wait_for_timeout(500)

                        try:
                            course_btn.click(timeout=3000)
                        except Exception:
                            logger.warning("Normal click failed. Trying force click...")
                            try:
                                course_btn.click(force=True, timeout=3000)
                            except Exception:
                                logger.warning("Force click failed. Trying JavaScript click...")
                                course_btn.evaluate("el => el.click()")

                        _safe_networkidle(page)

                    _try_step(
                        "Opened COURSE IN THE WARD",
                        _open_course_in_the_ward
                    )

                    def _click_add_course_in_ward():
                        add_btn = page.locator("button:has-text('ADD')").first

                        add_btn.wait_for(state="visible", timeout=10000)
                        add_btn.scroll_into_view_if_needed()
                        page.wait_for_timeout(300)

                        try:
                            add_btn.click(timeout=3000)
                        except Exception:
                            logger.warning("Normal click failed. Trying force click...")
                            try:
                                add_btn.click(force=True, timeout=3000)
                            except Exception:
                                logger.warning("Force click failed. Trying JavaScript click...")
                                add_btn.evaluate("el => el.click()")

                        _safe_networkidle(page)
                    
                    _try_step(
                        "Clicked ADD",
                        _click_add_course_in_ward
                    )

                    # --------------------------------------------------
                    # Add Course in the Ward entries
                    # --------------------------------------------------
                    course_order_text = cf4_data["course_in_ward_order"]

                    for session_date in session_dates:

                        logger.info(f"Adding Course in the Ward entry for {session_date}")

                        # Beacon auto-formats the dashes
                        date_to_type = session_date.replace("-", "")

                        # Date
                        date_input = page.locator('input[name="date"]').first
                        date_input.click()
                        date_input.press("Control+A")
                        date_input.press("Backspace")
                        date_input.type(date_to_type, delay=80)

                        # Doctor's Order / Action
                        order_input = page.locator('textarea[name="order"]').first
                        order_input.click()
                        order_input.press("Control+A")
                        order_input.press("Backspace")
                        order_input.fill(course_order_text)

                        # Save
                        save_btn = page.locator("button:has-text('SAVE')").last
                        save_btn.click(force=True)

                        _safe_networkidle(page)

                        logger.success(f"Saved Course in the Ward entry for {session_date}")

                        # Open Add dialog again for the next session
                        if session_date != session_dates[-1]:
                            _click_add_course_in_ward()

                    def _close_course_in_the_ward():
                        close_btn = page.locator("button:has-text('CLOSE')").first

                        close_btn.wait_for(state="visible", timeout=10000)
                        close_btn.scroll_into_view_if_needed()
                        page.wait_for_timeout(300)

                        try:
                            close_btn.click(timeout=3000)
                        except Exception:
                            logger.warning("Normal click failed. Trying force click...")
                            try:
                                close_btn.click(force=True, timeout=3000)
                            except Exception:
                                logger.warning("Force click failed. Trying JavaScript click...")
                                close_btn.evaluate("el => el.click()")

                        _safe_networkidle(page)


                    _try_step(
                        "Closed COURSE IN THE WARD",
                        _close_course_in_the_ward
                    )

                    logger.info("Auto Encode CF4 finished.")

                # ── Map medicines ──────────────────────────────────────────
                logger.info("Clicking DRUGS / MEDICINES...")
                page.get_by_text("DRUGS / MEDICINES", exact=True).click()
                _safe_networkidle(page)

                rows = page.locator("tbody tr")

                if rows.count() == 0:
                    logger.error("No medicines found. Skipping patient.")
                    report.skipped(
                        transmittal=transmittal_no,
                        remarks="No medicines found"
                    )

                    open_transmittals(page)
                    continue

                rows = page.locator("tbody tr")

                for i in range(rows.count()):
                    logger.info(f"\nProcessing medicine {i + 1}/{rows.count()}")

                    _log_page_state(
                        page,
                        "START MEDICINE",
                        transmittal_no=transmittal_no,
                        medicine_index=i + 1
                    )

                    medicine_start_time = time.monotonic()

                    row = rows.nth(i)
                    text = row.inner_text().upper()
                    logger.info(text)

                    lines = [line.strip() for line in text.splitlines() if line.strip()]

                    medicine_name = ""

                    for line in lines:
                        if (
                            "REGULAR HEPARIN" in line or
                            "PNSS" in line or
                            "HEMODIALYSIS" in line or
                            "EPOETIN ALFA" in line or
                            "EPOETIN BETA" in line
                        ):
                            medicine_name = line
                            break

                    logger.info(f"Medicine Name: {medicine_name}")
                    _log_page_state(
                        page,
                        "MEDICINE IDENTIFIED",
                        transmittal_no=transmittal_no,
                        medicine_index=i + 1,
                        medicine_name=medicine_name
                    )

                    # Determine medicine search term
                    if "REGULAR HEPARIN" in medicine_name:
                        search_term = "HEPARIN"
                    elif "PNSS" in medicine_name:
                        search_term = "SODIUM"
                    elif "HEMODIALYSIS ACID" in medicine_name:
                        search_term = "HEMOD"
                    elif "HEMODIALYSIS BICARBONATE" in medicine_name:
                        search_term = "HEMOD"
                    elif "EPOETIN ALFA" in medicine_name:
                        search_term = "EPO"
                    elif "EPOETIN BETA" in medicine_name:
                        search_term = "EPO"

                    else:
                        logger.warning("Unknown medicine, skipping row...")
                        continue

                    # Open 3-dot menu → Map Medicine
                    _log_page_state(
                        page,
                        "OPENING MAP MEDICINE",
                        transmittal_no=transmittal_no,
                        medicine_index=i + 1,
                        medicine_name=medicine_name
                    )

                    row.locator("button").click()
                    page.wait_for_timeout(1000)

                    page.get_by_text("Map Medicine", exact=True).click()

                    _log_page_state(
                        page,
                        "MAP MEDICINE OPENED",
                        transmittal_no=transmittal_no,
                        medicine_index=i + 1,
                        medicine_name=medicine_name
                    )

                    _safe_networkidle(page)

                    _log_page_state(
                        page,
                        "MAP MEDICINE NETWORK WAIT COMPLETE",
                        transmittal_no=transmittal_no,
                        medicine_index=i + 1,
                        medicine_name=medicine_name
                    )

                    # Type search term
                    search_box = page.locator('input[id*="SearchMedicinetoMap"]').first
                    search_box.click()
                    search_box.press("Control+A")
                    search_box.press("Backspace")
                    
                    _log_page_state(
                        page,
                        f"SEARCHING MEDICINE: {search_term}",
                        transmittal_no=transmittal_no,
                        medicine_index=i + 1,
                        medicine_name=medicine_name
                    )

                    search_box.type(search_term, delay=100)

                    _log_page_state(
                        page,
                        "WAITING FOR MEDICINE RESULTS",
                        transmittal_no=transmittal_no,
                        medicine_index=i + 1,
                        medicine_name=medicine_name
                    )

                    page.wait_for_selector('input[type="radio"]', timeout=10000)

                    logger.info(f"Textbox value: {search_box.input_value()}")

                    _log_page_state(
                        page,
                        "MEDICINE RESULTS LOADED",
                        transmittal_no=transmittal_no,
                        medicine_index=i + 1,
                        medicine_name=medicine_name
                    )

                    _check_medicine_operation_timeout(
                        medicine_start_time,
                        page,
                        transmittal_no,
                        i + 1,
                        medicine_name,
                        "MEDICINE RESULTS LOADED"
                    )

                    popup = (
                        page.locator("text=Please Select the Medicine")
                        .locator("..")
                        .locator("..")
                    )

                    logger.info("=" * 50)
                    logger.info(f"Row {i} detected as:")
                    logger.info(repr(text))
                    logger.info("=" * 50)

                    # Select the correct medicine in the popup
                    if "REGULAR HEPARIN" in medicine_name:
                        popup.locator(
                            "label",
                            has_text="HEPARIN ( As SODIUM) 5000 IU/Ml SOLUTION 5 Ml VIAL"
                        ).locator("xpath=../..").click(force=True)
                        logger.info("Selected HEPARIN 5000 IU/ML 5 ML VIAL")

                    elif "PNSS" in medicine_name:
                        popup.get_by_text(
                            "0.9% SODIUM CHLORIDE SOLUTION 1 L BOTTLE",
                            exact=True
                        ).click(force=True)
                        logger.info("Selected PNSS 1L BOTTLE")

                    elif "HEMODIALYSIS ACID" in medicine_name:
                        popup.get_by_text(
                            "HEMODIALYSIS ACID CONCENTRATE (DIALYSATE ACETATE BASED) 5 L",
                            exact=True
                        ).click(force=True)
                        logger.info("Selected HEMODIALYSIS ACID 5L")

                    elif "HEMODIALYSIS BICARBONATE" in medicine_name:
                        option = popup.get_by_text(
                            "HEMODIALYSIS BICARBONATE CONCENTRATE 5 L",
                            exact=True
                        )

                        logger.info(f"About to click: {option.inner_text()}")

                        option.click(force=True)

                        logger.info("Selected HEMODIALYSIS BICARBONATE 5L")

                    elif "EPOETIN ALFA" in medicine_name:
                        popup.get_by_text(
                            "EPOETIN ALFA (RECOMBINANT HUMAN ERYTHROPOIETIN) 4000 IU/Ml SOLUTION 1 Ml PRE-FILLED GLASS SYRINGE",
                            exact=True
                        ).click(force=True)
                        logger.info("Selected EPOETIN ALFA 4000 IU/ML 1 ML")

                    elif "EPOETIN BETA" in medicine_name:
                        popup.get_by_text(
                            "EPOETIN BETA (RECOMBINANT ERYTHROPOIETIN) 5000IU/0.3Ml SOLUTION PRE-FILLED SYRINGE WITH NEEDLE",
                            exact=True
                        ).click(force=True)
                        logger.info("Selected EPOETIN BETA 5000 IU/0.3 ML")

                    _log_page_state(
                        page,
                        "CLICKING CONTINUE",
                        transmittal_no=transmittal_no,
                        medicine_index=i + 1,
                        medicine_name=medicine_name
                    )

                    continue_btn = page.get_by_role("button", name="CONTINUE")
                    continue_btn.click(force=True)

                    _log_page_state(
                        page,
                        "CONTINUE CLICKED",
                        transmittal_no=transmittal_no,
                        medicine_index=i + 1,
                        medicine_name=medicine_name
                    )

                    _safe_networkidle(page)

                    _log_page_state(
                        page,
                        "MEDICINE NETWORK WAIT COMPLETE",
                        transmittal_no=transmittal_no,
                        medicine_index=i + 1,
                        medicine_name=medicine_name
                    )

                    timed_out = _check_medicine_operation_timeout(
                        medicine_start_time,
                        page,
                        transmittal_no,
                        i + 1,
                        medicine_name,
                        "MEDICINE NETWORK WAIT COMPLETE"
                    )

                    if timed_out:
                        logger.warning(
                            "[WATCHDOG] Medicine exceeded the allowed "
                            "operation time, but recovery is not enabled yet."
                        )

                    logger.info("Medicine mapped")

                logger.success("All medicines mapped")

                page.get_by_role("button", name="CLOSE").click()

                logger.info("Drugs and Medicines window closed")

                # Click E-CLAIMS intentionally
                page.get_by_role("button", name="E-CLAIMS").click()

                # Beacon asks whether to save changes
                page.wait_for_selector("text=SAVE CHANGES", timeout=10000)

                page.get_by_role(
                    "button",
                    name="SAVE CHANGES",
                    exact=True
                ).click()

                logger.info("Navigation save confirmation clicked")

                # Don't trust networkidle alone here — wait for the dialog
                # to actually resolve (with a fallback settle buffer) before
                # doing anything else, so the follow-up navigation below
                # can't cut off an in-flight save.
                _wait_for_save_changes_complete(page)
                _safe_networkidle(page)

                logger.success(f"SUCCESS: Patient {transmittal_no} saved")
                report.success(
                    transmittal=transmittal_no,
                    mapped=rows.count()
                )
            

                open_transmittals(page)

            except Exception as e:
                logger.error(f"\nERROR on patient {idx + 1} ({transmittal_no}): {e}")                
                logger.warning("Skipping to next patient...")
                report.failed(
                    transmittal=transmittal_no,
                    remarks=str(e)
                )

                try:
                    open_transmittals(page)

                except:
                    pass

                continue

        summary = report.summary()

        logger.info("\n")
        logger.info("=" * 60)
        logger.info("AUTOMATION SUMMARY")
        logger.info("=" * 60)
        logger.info(f"Total      : {summary['total']}")
        logger.info(f"Success    : {summary['success']}")
        logger.info(f"Skipped    : {summary['skipped']}")
        logger.info(f"Failed     : {summary['failed']}")
        logger.info("=" * 60)
    finally:
        # Save whatever session state we currently have — even if the run
        # was cut short by an uncaught exception above — before tearing
        # down the browser. A failure here must never block disconnect().
        try:
            browser_session.save_session()
        except Exception as e:
            logger.warning(f"Could not save session: {e}")

        browser_session.disconnect()