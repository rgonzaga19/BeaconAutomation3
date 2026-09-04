"""Beacon CF4 automation — API-only implementation.

This module preserves the established CF4 business rules while performing the
entire transmittal/claim/CF4/medicine workflow through Beacon HTTP APIs.
There are intentionally no browser, DOM, modal, screenshot, or UI recovery
dependencies in this file.
"""

import copy
from datetime import date as _date, datetime as _datetime

import beacon_api
import cf2_api
from logger import logger
from reports import report


# Keep these values in lockstep with DEFAULT_CF4_SETTINGS in server.py /
# js/cf4.js. They are fallback values when run() is called without a complete
# cf4_data dictionary.
DEFAULT_CF4_DATA = {'chief_complaint': 'FOR HEMODIALYSIS',
 'history_of_present_illness': 'N/A',
 'pertinent_past_medical_history': 'N/A',
 'general_survey_awake_alert': True,
 'course_in_ward_order': 'UF GOAL MET AT L',
 'alteredMentalSensorium': False,
 'abdominalCrampPain': False,
 'anorexia': False,
 'bleedingGums': False,
 'bodyWeakness': True,
 'blurringOfVision': False,
 'chestPainDiscomfort': False,
 'constipation': False,
 'cough': False,
 'diarrhea': False,
 'dizziness': False,
 'dysphagia': False,
 'dyspnea': False,
 'dysuria': False,
 'epistaxis': False,
 'fever': False,
 'frequencyOfUrination': False,
 'headache': False,
 'hematemesis': False,
 'hematuria': False,
 'hemoptysis': False,
 'irritability': False,
 'jaundice': False,
 'lowerExtremityEdema': True,
 'myalgia': False,
 'orthopnea': False,
 'pain': False,
 'painSpecify': '',
 'palpitations': False,
 'seizure': False,
 'skinRashes': False,
 'stoolBloodyBlackTarryMucoid': False,
 'sweating': False,
 'urgency': False,
 'vomiting': False,
 'weightLoss': False,
 'others': False,
 'othersSpecify': '',
 'heEssentiallyNormal': True,
 'heSunkenFontanelle': False,
 'heAbnormalPupillaryReaction': False,
 'heOthersChk': False,
 'heOthers': '',
 'heCervicalLympadenopathy': False,
 'heDryMucousMembrane': False,
 'heIctericSclerae': False,
 'hePaleConjunctivae': False,
 'heSunkenEyeballs': False,
 'clEssentiallyNormal': True,
 'clOthersChk': False,
 'clOthers': '',
 'clAsymmetricalChestExpansion': False,
 'clDecreasedBreathSounds': False,
 'clWheezes': False,
 'clLumpsOverBreast': False,
 'clCracklesRales': False,
 'clRetractions': False,
 'cvEssentiallyNormal': True,
 'cvOthersChk': False,
 'cvOthers': '',
 'cvDisplacedApexBeat': False,
 'cvHeavesThrills': False,
 'cvPericardialBulge': False,
 'cvIrregularRhythm': False,
 'cvMuffledHeartSounds': False,
 'cvMurmur': False,
 'abEssentiallyNormal': True,
 'abOthersChk': False,
 'abOthers': '',
 'abAbdominalRigidity': False,
 'abAbdominalTenderness': False,
 'abHyperactiveBowelSounds': False,
 'abPalpableMasses': False,
 'abTympaniticDullAbdomen': False,
 'abUterineContraction': False,
 'guEssentiallyNormal': False,
 'guBloodStainedInExamFinger': False,
 'guCervicalDilatation': False,
 'guPresenceofAbnormalDischarge': False,
 'guOthersChk': True,
 'guOthers': 'NOT EXAMINE',
 'seEssentiallyNormal': True,
 'sePoorSkinTurgor': False,
 'seClubbing': False,
 'seRashesPetechiae': False,
 'seColdClammy': False,
 'seWeakPulse': False,
 'seCyanosisMottledSkin': False,
 'seOthersChk': False,
 'seOthers': '',
 'seEdemaSwelling': False,
 'seDecreasedMobility': False,
 'sePaleNailbeds': False,
 'neEssentiallyNormal': True,
 'nePoorCoordination': False,
 'neAbnormalGait': False,
 'neOthersChk': False,
 'neOthers': '',
 'neAbnormalPositionSense': False,
 'neAbnormalSensation': False,
 'neAbnormalReflexes': False,
 'nePoorAlteredMemory': False,
 'nePoorMuscleToneStrength': False}


# API field metadata derived from the previously confirmed CF4 form fields.
# Each tuple is: (settings_key, optional_specify_key, is_physical_exam_field).
# DOM names, labels, locators, and UI-only metadata were deliberately removed.
CF4_FIELD_SETTINGS = [('alteredMentalSensorium', None, False),
 ('abdominalCrampPain', None, True),
 ('anorexia', None, False),
 ('bleedingGums', None, False),
 ('bodyWeakness', None, False),
 ('blurringOfVision', None, False),
 ('chestPainDiscomfort', None, False),
 ('constipation', None, False),
 ('cough', None, False),
 ('diarrhea', None, False),
 ('dizziness', None, False),
 ('dysphagia', None, False),
 ('dyspnea', None, False),
 ('dysuria', None, False),
 ('epistaxis', None, False),
 ('fever', None, False),
 ('frequencyOfUrination', None, False),
 ('headache', None, True),
 ('hematemesis', None, True),
 ('hematuria', None, True),
 ('hemoptysis', None, True),
 ('irritability', None, False),
 ('jaundice', None, False),
 ('lowerExtremityEdema', None, False),
 ('myalgia', None, False),
 ('orthopnea', None, False),
 ('pain', 'painSpecify', False),
 ('palpitations', None, False),
 ('seizure', None, True),
 ('skinRashes', None, False),
 ('stoolBloodyBlackTarryMucoid', None, False),
 ('sweating', None, False),
 ('urgency', None, False),
 ('vomiting', None, False),
 ('weightLoss', None, False),
 ('others', 'othersSpecify', False),
 ('heEssentiallyNormal', None, True),
 ('heSunkenFontanelle', None, True),
 ('heAbnormalPupillaryReaction', None, True),
 ('heOthersChk', 'heOthers', True),
 ('heCervicalLympadenopathy', None, True),
 ('heDryMucousMembrane', None, True),
 ('heIctericSclerae', None, True),
 ('hePaleConjunctivae', None, True),
 ('heSunkenEyeballs', None, True),
 ('clEssentiallyNormal', None, True),
 ('clOthersChk', 'clOthers', True),
 ('clAsymmetricalChestExpansion', None, True),
 ('clDecreasedBreathSounds', None, True),
 ('clWheezes', None, True),
 ('clLumpsOverBreast', None, True),
 ('clCracklesRales', None, True),
 ('clRetractions', None, True),
 ('cvEssentiallyNormal', None, True),
 ('cvOthersChk', 'cvOthers', True),
 ('cvDisplacedApexBeat', None, True),
 ('cvHeavesThrills', None, True),
 ('cvPericardialBulge', None, True),
 ('cvIrregularRhythm', None, True),
 ('cvMuffledHeartSounds', None, True),
 ('cvMurmur', None, True),
 ('abEssentiallyNormal', None, True),
 ('abOthersChk', 'abOthers', True),
 ('abAbdominalRigidity', None, True),
 ('abAbdominalTenderness', None, True),
 ('abHyperactiveBowelSounds', None, True),
 ('abPalpableMasses', None, True),
 ('abTympaniticDullAbdomen', None, True),
 ('abUterineContraction', None, True),
 ('guEssentiallyNormal', None, True),
 ('guBloodStainedInExamFinger', None, True),
 ('guCervicalDilatation', None, True),
 ('guPresenceofAbnormalDischarge', None, True),
 ('guOthersChk', 'guOthers', True),
 ('seEssentiallyNormal', None, True),
 ('sePoorSkinTurgor', None, True),
 ('seClubbing', None, True),
 ('seRashesPetechiae', None, True),
 ('seColdClammy', None, True),
 ('seWeakPulse', None, True),
 ('seCyanosisMottledSkin', None, True),
 ('seOthersChk', 'seOthers', True),
 ('seEdemaSwelling', None, True),
 ('seDecreasedMobility', None, True),
 ('sePaleNailbeds', None, True),
 ('neEssentiallyNormal', None, True),
 ('nePoorCoordination', None, True),
 ('neAbnormalGait', None, True),
 ('neOthersChk', 'neOthers', True),
 ('neAbnormalPositionSense', None, True),
 ('neAbnormalSensation', None, True),
 ('neAbnormalReflexes', None, True),
 ('nePoorAlteredMemory', None, True),
 ('nePoorMuscleToneStrength', None, True)]


MEDICINE_TARGETS = {
    "HEPARIN": (
        "HEPARIN",
        "HEPARIN ( As SODIUM) 5000 IU/Ml SOLUTION 5 Ml VIAL",
    ),
    "SODIUM": (
        "SODIUM",
        "0.9% SODIUM CHLORIDE SOLUTION 1 L BOTTLE",
    ),
    "HEMODIALYSIS ACID": (
        "HEMOD",
        "HEMODIALYSIS ACID CONCENTRATE "
        "(DIALYSATE ACETATE BASED) 5 L",
    ),
    "HEMODIALYSIS BICARBONATE": (
        "HEMOD",
        "HEMODIALYSIS BICARBONATE CONCENTRATE 5 L",
    ),
    "EPOETIN ALFA": (
        "EPO",
        "EPOETIN ALFA (RECOMBINANT HUMAN ERYTHROPOIETIN) "
        "4000 IU/Ml SOLUTION 1 Ml PRE-FILLED GLASS SYRINGE",
    ),
    "EPOETIN BETA": (
        "EPO",
        "EPOETIN BETA (RECOMBINANT ERYTHROPOIETIN) "
        "5000IU/0.3Ml SOLUTION PRE-FILLED SYRINGE WITH NEEDLE",
    ),
}


MAPPING_FIELDS = (
    "drugCode",
    "drugDescription",
    "genericCode",
    "saltCode",
    "formCode",
    "strengthCode",
    "unitCode",
    "packageCode",
)


def _normalize_description(value):
    return " ".join(str(value or "").split()).casefold()


def _parse_calendar_date(value):
    if isinstance(value, _datetime):
        return value.date()

    if isinstance(value, _date):
        return value

    text = str(value or "").strip()
    if not text:
        return None

    if "T" in text:
        try:
            return _datetime.fromisoformat(
                text.replace("Z", "+00:00")
            ).date()
        except ValueError:
            pass

    for fmt in ("%m-%d-%Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return _datetime.strptime(text, fmt).date()
        except ValueError:
            continue

    raise ValueError(f"Unsupported CF4/session date: {value!r}")


def _local_date_to_beacon_utc(value):
    parsed = _parse_calendar_date(value)
    if parsed is None:
        raise ValueError("Session date is empty")

    # Reuse the already-proven CF2 date conversion:
    # Philippine local midnight -> previous day 16:00 UTC.
    return cf2_api.to_utc_midnight_iso(parsed)


def _session_dates_from_cf2(claim_id):
    procedures = beacon_api.get_surgical_procedures(claim_id)
    session_dates = []

    for procedure in procedures:
        for session in procedure.get("sessions") or []:
            value = session.get("sessionDate")
            if value:
                session_dates.append(value)

    return session_dates


def _doctor_full_name(doctor):
    """Use Beacon's own fullName when available; otherwise rebuild it."""
    full_name = str(doctor.get("fullName") or "").strip()
    if full_name:
        return full_name

    parts = [
        doctor.get("firstname"),
        doctor.get("middlename"),
        doctor.get("lastname"),
        doctor.get("suffix"),
    ]
    return " ".join(
        str(part).strip()
        for part in parts
        if str(part or "").strip()
    )


def _last_treatment_date(session_dates):
    """Return the latest CF2 treatment/session date."""
    dated = [
        (_parse_calendar_date(value), value)
        for value in session_dates
        if value
    ]
    dated = [(parsed, raw) for parsed, raw in dated if parsed is not None]

    if not dated:
        return None

    _, latest_raw = max(dated, key=lambda item: item[0])
    return latest_raw


def _save_cf4_new_tab_doctor(claim_id, session_dates):
    """Populate CF4 New-tab doctor/sign date only when a doctor exists."""
    doctors = beacon_api.get_doctors_by_claim_id(claim_id)
    if not doctors:
        logger.info(
            "No doctor encoded on claim — skipping CF4 New-tab doctor data."
        )
        return

    # Preserve the automation's established first-row behavior for claim data.
    doctor = doctors[0]
    doctor_name = _doctor_full_name(doctor)

    if not doctor_name:
        logger.warning(
            "Doctor exists but has no usable name — "
            "skipping CF4 New-tab doctor data."
        )
        return

    last_treatment = _last_treatment_date(session_dates)
    if last_treatment is None:
        logger.warning(
            "Doctor exists but no CF2 treatment/session date was found — "
            "skipping CF4 New-tab doctor data."
        )
        return

    date_signed = cf2_api.to_date_signed_iso(
        _parse_calendar_date(last_treatment)
    )

    saved = beacon_api.new_pdf_cf4(
        claim_id,
        doctor_name,
        date_signed,
    )
    if not saved:
        raise Exception(
            "NewPdfCF4 returned an unsuccessful/empty response"
        )

    logger.success(
        "CF4 New tab updated: "
        f"doctor={doctor_name}, last treatment={last_treatment}"
    )


def _medicine_rule(medicine):
    """Preserve the original medicine-identification/search precedence."""
    haystack = " ".join(
        str(medicine.get(key) or "")
        for key in (
            "genericName",
            "brandName",
            "drugDescription",
        )
    ).upper()

    if "REGULAR HEPARIN" in haystack or "HEPARIN" in haystack:
        return MEDICINE_TARGETS["HEPARIN"]

    if "PNSS" in haystack or "SODIUM CHLORIDE" in haystack:
        return MEDICINE_TARGETS["SODIUM"]

    if "HEMODIALYSIS ACID" in haystack:
        return MEDICINE_TARGETS["HEMODIALYSIS ACID"]

    if "HEMODIALYSIS BICARBONATE" in haystack:
        return MEDICINE_TARGETS["HEMODIALYSIS BICARBONATE"]

    if "EPOETIN ALFA" in haystack:
        return MEDICINE_TARGETS["EPOETIN ALFA"]

    if "EPOETIN BETA" in haystack:
        return MEDICINE_TARGETS["EPOETIN BETA"]

    return None


def _map_medicine(medicine, medicine_index):
    rule = _medicine_rule(medicine)
    if rule is None:
        logger.warning(
            f"Unknown medicine, skipping row {medicine_index}: "
            f"{medicine.get('genericName') or medicine.get('brandName') or ''}"
        )
        return False

    search_term, target_description = rule

    logger.info(
        f"Medicine {medicine_index}: "
        f"{medicine.get('genericName') or medicine.get('brandName') or ''}"
    )
    logger.info(f"Searching medicine API: {search_term}")

    results = beacon_api.search_medicines(search_term)
    target_key = _normalize_description(target_description)

    selected = next(
        (
            item
            for item in results
            if _normalize_description(item.get("drugDescription"))
            == target_key
        ),
        None,
    )

    if selected is None:
        raise Exception(
            "Target medicine mapping was not returned by Beacon: "
            f"{target_description!r} (search={search_term!r})"
        )

    for field in MAPPING_FIELDS:
        medicine[field] = selected.get(field)

    logger.success(
        f"Medicine mapped: {selected.get('drugDescription')}"
    )
    return True


def _build_signs_and_symptoms(cf4_data):
    """Build the same symptom payload the proven Auto Encode flow produced."""
    result = {}

    for settings_key, specify_key, is_physical in CF4_FIELD_SETTINGS:
        if is_physical:
            continue

        enabled = bool(cf4_data.get(settings_key))
        if enabled:
            result[settings_key] = True

            if specify_key:
                value = cf4_data.get(specify_key, "")
                result[specify_key] = value if value is not None else ""

    return result


def _build_physical_exam(existing, cf4_data):
    """Overlay configured exam flags while preserving Beacon-provided vitals."""
    exam = copy.deepcopy(existing or {})

    if cf4_data.get("general_survey_awake_alert"):
        exam["gsAwakeAndAlert"] = True

    for settings_key, specify_key, is_physical in CF4_FIELD_SETTINGS:
        if not is_physical:
            continue

        exam[settings_key] = bool(cf4_data.get(settings_key))

        if specify_key:
            value = cf4_data.get(specify_key, "")
            exam[specify_key] = value if value is not None else ""

    return exam


def _derive_outcome(payload):
    for key in (
        "improved",
        "recovered",
        "hama",
        "expired",
        "absconded",
        "transferred",
    ):
        if payload.get(key):
            return key

    return ""


def _normalize_cf4_save_payload(
    cf4,
    claim_id,
    auto_encode_cf4,
    cf4_data,
    session_dates,
):
    payload = copy.deepcopy(cf4)

    # Preserve original behavior: these two fields are filled only when empty.
    if not str(payload.get("historyOfPresentIllness") or "").strip():
        payload["historyOfPresentIllness"] = "N/A"

    if not str(payload.get("pertinentPastMedicalHistory") or "").strip():
        payload["pertinentPastMedicalHistory"] = "N/A"

    if auto_encode_cf4:
        payload["chiefComplaint"] = cf4_data["chief_complaint"]
        payload["historyOfPresentIllness"] = (
            cf4_data["history_of_present_illness"]
        )
        payload["pertinentPastMedicalHistory"] = (
            cf4_data["pertinent_past_medical_history"]
        )

        payload["phiccF4SignAndSymptoms"] = (
            _build_signs_and_symptoms(cf4_data)
        )
        payload["phiccF4PhysicalExam"] = _build_physical_exam(
            payload.get("phiccF4PhysicalExam"),
            cf4_data,
        )

        # Preserve the proven duplicate-prevention rule exactly: if Course in
        # the Ward already has any entry, do not append another session set.
        existing_orders = payload.get("phiccF4DoctorsOrder") or []
        if existing_orders:
            logger.info(
                "Course in the Ward already has entries — "
                "skipping re-add to avoid duplicates."
            )
        else:
            order_text = cf4_data["course_in_ward_order"]
            payload["phiccF4DoctorsOrder"] = [
                {
                    "date": _local_date_to_beacon_utc(session_date),
                    "order": order_text,
                }
                for session_date in session_dates
            ]

    # Match the successful Beacon CF4 save request shape: physical-exam and
    # symptom values are present both in their nested objects and flattened.
    exam = payload.get("phiccF4PhysicalExam") or {}
    for key, value in exam.items():
        payload[key] = value

    if exam.get("gsAwakeAndAlert"):
        payload["gsAwakeAndAlert"] = "AwakeAndAlert"

    signs = payload.get("phiccF4SignAndSymptoms") or {}
    for key, value in signs.items():
        payload[key] = value

    payload["claimId"] = int(claim_id)
    payload["timeAdmitted"] = payload.get("dateTimeAdmitted")
    payload["timeDischarge"] = payload.get("dateTimeDischarge")
    payload["outcomeOfTreatmentCheckBoxes"] = _derive_outcome(payload)

    for key in ("birthDate", "dateTimeAdmitted", "dateTimeDischarge"):
        value = payload.get(key)
        if isinstance(value, str) and value and not value.endswith("Z"):
            base = value.split(".")[0]
            payload[key] = base + ".000Z"

    if (
        payload.get("packageType") == "REGULAR"
        or payload.get("phicPackage") == 0
    ):
        payload["packageType"] = "A"

    if payload.get("reportStatus") in (None, ""):
        payload["reportStatus"] = "V"

    if payload.get("specifyReason") is None:
        payload["specifyReason"] = ""

    birth_date = _parse_calendar_date(payload.get("birthDate"))
    if birth_date:
        today = _datetime.today().date()
        payload["age"] = (
            today.year
            - birth_date.year
            - (
                (today.month, today.day)
                < (birth_date.month, birth_date.day)
            )
        )

    return payload


def _process_transmittal(
    idx,
    transmittal_no,
    transmittals,
    auto_encode_cf4,
    cf4_data,
):
    """Process one transmittal end-to-end using only Beacon APIs."""
    transmittal_no = str(transmittal_no).strip()

    logger.info("\n" + "=" * 60)
    logger.info(
        f"TRANSMITTAL {idx + 1}/{len(transmittals)} : {transmittal_no}"
    )
    logger.info("=" * 60)

    # Preserve original workflow: exact transmittal search, then first claim.
    transmittal = beacon_api.get_transmittal(transmittal_no)
    if not transmittal:
        logger.warning(f"TRANSMITTAL NOT FOUND: {transmittal_no}")
        report.skipped(
            transmittal=transmittal_no,
            remarks="Transmittal not found",
        )
        return

    transmittal_id = int(transmittal["id"])
    claims = beacon_api.get_claims(transmittal_id)

    if not claims:
        raise Exception(
            f"No claim found in transmittal {transmittal_no}"
        )

    claim_id = int(claims[0]["id"])
    claim = beacon_api.get_claim(claim_id) or {}

    # Preserve the old "Validate Eligibility if required" behavior via the
    # already-tested CF2 API implementation.
    cf2_record = claim.get("phiccF2") or {}
    admission = (
        cf2_record.get("admissionDateTime")
        or claim.get("admissionDateTime")
    )
    discharge = (
        cf2_record.get("dischargeDateTime")
        or claim.get("dischargeDateTime")
    )

    if admission and discharge:
        eligibility = cf2_api.validate_claim_eligibility(
            claim_id,
            transmittal_id,
            admission,
            discharge,
        )
        if eligibility.get("skipped"):
            logger.info("No validation required — skipping.")
        else:
            logger.success("Eligibility validated through API.")

    session_dates = _session_dates_from_cf2(claim_id)
    logger.info(f"Found {len(session_dates)} session date(s).")

    for i, session_date in enumerate(session_dates, start=1):
        logger.info(f"Session {i}: {session_date}")

    cf4 = beacon_api.get_cf4_values(claim_id)
    if not isinstance(cf4, dict) or not cf4:
        raise Exception(
            f"GetCf4Values returned no usable CF4 for claim {claim_id}"
        )

    medicines = cf4.get("phiccF4DrugsAndMedicine") or []
    if not medicines:
        logger.error(
            "No medicine found, no uploaded SOA, skipping patient."
        )
        report.skipped(
            transmittal=transmittal_no,
            remarks=(
                "No medicine found, no uploaded SOA, skipping patient"
            ),
        )
        return

    logger.info(f"Found {len(medicines)} medicine row(s).")

    mapped_count = 0
    for medicine_index, medicine in enumerate(medicines, start=1):
        if _map_medicine(medicine, medicine_index):
            mapped_count += 1

    logger.success(
        f"Medicine mapping complete: "
        f"{mapped_count}/{len(medicines)} recognized row(s) mapped."
    )

    payload = _normalize_cf4_save_payload(
        cf4,
        claim_id,
        auto_encode_cf4,
        cf4_data,
        session_dates,
    )

    saved = beacon_api.save_cf4_values(payload)
    if not saved:
        raise Exception(
            "SavePhicCf4Values returned an empty response"
        )

    # Final CF4 step from the recorded New-tab workflow:
    # when a doctor is already encoded, use the first doctor and the latest
    # CF2 treatment/session date for the CF4 attending-doctor signature data.
    _save_cf4_new_tab_doctor(
        claim_id,
        session_dates,
    )

    logger.success(f"SUCCESS: Patient {transmittal_no} saved")
    report.success(
        transmittal=transmittal_no,
        mapped=len(medicines),
    )


def run(transmittals, auto_encode_cf4=False, cf4_data=None):
    """Run CF4 automation for all transmittals using API calls only."""
    cf4_data = {**DEFAULT_CF4_DATA, **(cf4_data or {})}
    report.results.clear()

    # Preserve original recovery semantics: initial attempt + one retry.
    max_recovery_retries = 1

    for idx, transmittal_no in enumerate(transmittals):
        transmittal_no = str(transmittal_no).strip()
        attempt = 0
        last_error = None

        while True:
            try:
                _process_transmittal(
                    idx,
                    transmittal_no,
                    transmittals,
                    auto_encode_cf4,
                    cf4_data,
                )
                last_error = None
                break

            except Exception as exc:
                last_error = exc

                if attempt >= max_recovery_retries:
                    logger.error(
                        f"Maximum API recovery attempts reached for "
                        f"transmittal {transmittal_no}"
                    )
                    break

                attempt += 1
                logger.warning(
                    f"API processing failed for {transmittal_no}: {exc}"
                )
                logger.warning(
                    f"Retrying SAME transmittal "
                    f"({attempt}/{max_recovery_retries})..."
                )

        if last_error is not None:
            logger.error(
                f"\nERROR on patient {idx + 1} "
                f"({transmittal_no}): {last_error}"
            )
            logger.warning("Skipping to next patient...")
            report.failed(
                transmittal=transmittal_no,
                remarks=str(last_error),
            )

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
