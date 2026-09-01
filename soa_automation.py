from pathlib import Path
import re
from datetime import datetime

from logger import logger
from reports import report, summarize_error
import soa_api
from settings import load_settings


DEFAULT_SOA_FOLDER = Path.home() / "Downloads" / "SOA"

def get_facility_id():
    """Get the user's facility ID from settings, with fallback to default."""
    try:
        settings = load_settings()
        return int(settings.get("facility_id", 263))
    except (TypeError, ValueError):
        return 263

# Common Filipino compound-surname particles.
SURNAME_PARTICLES = {
    "DE", "DEL", "DELA", "DELAS", "DELOS",
    "SAN", "SANTA", "STA", "STO", "SANTO",
    "MAC", "MC", "VAN", "VON", "DA", "DI", "LA", "LAS", "LOS",
}

# Generational suffixes that may appear as their own token after the surname.
NAME_SUFFIXES = {
    "JR", "SR",
    "II", "III", "lll", "IV", "V", "VI", "VII", "VIII", "IX", "X",
}


def _strip_name_suffixes(tokens):
    """Remove generational suffix tokens before surname/given-name matching."""
    return [token for token in tokens if token.rstrip(".") not in NAME_SUFFIXES]


def _split_surname_and_given(name):
    """
    Split an upper-cased patient name into surname and given-name tokens.

    The existing matching behavior is preserved:
    - surname is taken from the end of the full name;
    - known surname particles are absorbed;
    - a hyphenated final surname is treated as one compound surname;
    - generational suffixes are ignored.
    """
    tokens = _strip_name_suffixes(name.upper().split())

    if not tokens:
        return [], []

    last = tokens[-1]
    if "-" in last:
        hyphen_parts = [part for part in last.split("-") if part]
        tokens = tokens[:-1] + hyphen_parts
        forced_len = len(hyphen_parts) or 1
    else:
        forced_len = 1

    if len(tokens) <= forced_len:
        return tokens, []

    surname_tokens = tokens[-forced_len:]
    i = len(tokens) - forced_len - 1

    while i >= 0 and tokens[i] in SURNAME_PARTICLES:
        surname_tokens.insert(0, tokens[i])
        i -= 1

    return surname_tokens, tokens[:i + 1]


def _normalize(text):
    """Normalize a name while keeping Ñ as a valid letter."""
    return re.sub(r"[^A-Z0-9Ñ]", "", text.upper())


def _filename_tokens(filename):
    """Return normalized filename tokens used by the existing match rules."""
    stem = Path(filename).stem.upper()
    return [token for token in re.split(r"[^A-Z0-9Ñ]+", stem) if token]


def _find_soa_file(patient_name, soa_folder):
    """
    Find exactly one SOA workbook using the proven filename-selection rules.

    Selection order is unchanged:
    1. exact-length compound surname;
    2. loose compound surname;
    3. bare surname fallback for compound surnames;
    4. if ambiguous, narrow with given name / given-name initial;
    5. never guess when zero or multiple files remain.
    """
    surname_tokens, given_tokens = _split_surname_and_given(patient_name)
    surname_key = _normalize("".join(surname_tokens))
    bare_surname_key = _normalize(surname_tokens[-1]) if surname_tokens else ""
    given_initial = _normalize(given_tokens[0])[:1] if given_tokens else ""

    if not soa_folder.exists():
        raise Exception(f"SOA folder does not exist: {soa_folder}")

    all_files = []
    for pattern in ("*.xlsx", "*.xls"):
        all_files.extend(soa_folder.glob(pattern))

    def matching(tokens_to_match, require_exact_length=False):
        if not tokens_to_match:
            return []

        tokens_to_match = [token.upper() for token in tokens_to_match]
        matches = []
        allowed_after = set(given_tokens)
        if given_initial:
            allowed_after.add(given_initial)

        for file_path in all_files:
            tokens = _filename_tokens(file_path.name)

            for i in range(len(tokens) - len(tokens_to_match) + 1):
                if tokens[i:i + len(tokens_to_match)] != tokens_to_match:
                    continue

                if (
                    require_exact_length
                    and i > 0
                    and tokens[i - 1] in SURNAME_PARTICLES
                ):
                    continue

                j = i + len(tokens_to_match)
                following = tokens[j] if j < len(tokens) else None

                if (
                    following is not None
                    and not following.isdigit()
                    and following not in allowed_after
                ):
                    continue

                matches.append(file_path)
                break

        return matches

    def has_given_name_marker(filename):
        tokens = _filename_tokens(filename)

        for i in range(len(tokens) - len(surname_tokens) + 1):
            if tokens[i:i + len(surname_tokens)] != surname_tokens:
                continue

            before = tokens[i - 1] if i > 0 else None
            j = i + len(surname_tokens)
            after = tokens[j] if j < len(tokens) else None

            if given_tokens and (
                before == given_tokens[0] or after == given_tokens[0]
            ):
                return True

            if given_initial and (
                before == given_initial or after == given_initial
            ):
                return True

        return False

    matches = matching(surname_tokens, True) or matching(surname_tokens)

    if not matches and bare_surname_key != surname_key:
        matches = (
            matching([surname_tokens[-1]], True)
            or matching([surname_tokens[-1]])
        )

    if not matches:
        raise Exception(
            f"No SOA file found for patient '{patient_name}'. "
            "Skipping — will not guess."
        )

    if len(matches) > 1:
        narrowed = [
            file_path
            for file_path in matches
            if has_given_name_marker(file_path.name)
        ]

        if len(narrowed) != 1:
            raise Exception(
                f"Multiple SOA files matched patient '{patient_name}'. "
                "Skipping — will not guess."
            )

        matches = narrowed

    return matches[0]


def _fix_others_unit_mapping(claim_id):
    """Set every XLSO row mapped as Others to PHIC unit PIECE.

    The recorded Beacon edit flow uses EditPHICChargesXLSO and sends unitCode
    for PIECE together with unitDescription="PIECE".  There can be multiple
    "Others" rows, so every matching XLSO charge is updated.
    """
    _, xlso_rows = soa_api.get_charges(claim_id)

    matching_rows = [
        row
        for row in xlso_rows
        if str(row.get("itemName") or "").strip().casefold() == "others"
    ]

    if not matching_rows:
        logger.info(
            "No XLSO PhilHealth Mapping='Others' rows found — "
            "no unit mapping change needed."
        )
        return 0

    units = soa_api.get_phic_units()
    piece = next(
        (
            unit for unit in units
            if str(unit.get("description") or "").strip().upper() == "PIECE"
        ),
        None,
    )

    if not piece:
        raise Exception("PHIC unit PIECE was not returned by Beacon")

    unit_code = str(piece.get("code") or piece.get("id") or "").strip()
    if not unit_code:
        raise Exception("PHIC unit PIECE has no usable code")

    updated = 0

    for row in matching_rows:
        payload = dict(row)

        # Preserve the existing row and change only what the edit dialog changes
        # for the requested PhilHealth unit mapping.
        payload["unitCode"] = unit_code
        payload["unitDescription"] = "PIECE"

        # The successful browser edit payload carries these fields when the
        # PhilHealth mapping is Others. Keep them normalized if absent/null.
        payload["itemName"] = "Others"
        payload["searchText"] = "Others"
        payload["philhealthItemId"] = payload.get("philhealthItemId") or ""
        payload["oecbCategory"] = payload.get("oecbCategory") or ""

        saved = soa_api.edit_xlso_charge(payload)
        if not saved:
            raise Exception(
                f"Failed to save XLSO Others row id={row.get('id')}"
            )

        updated += 1
        logger.success(
            "Updated XLSO Others row "
            f"id={row.get('id')} -> PhilHealth unit PIECE"
        )

    logger.success(
        f"XLSO Others unit mapping updated on {updated} row(s)."
    )
    return updated


def _clear_editable_summary_fields(summary):
    """Clear editable SOA fields before retyping calculated values.

    Beacon can retain an old Senior/PWD discount from a previous pass.  Mirror
    the UI's "clear textbox first, then type" behavior through UpdateSummary:

    - Facility fee rows: clear both editable discount boxes.
      actualCharges is Beacon/import-populated and is intentionally preserved.
    - First professional-fee row: clear the editable actualCharges box and
      both discount boxes, because this automation retypes those values.

    UpdateSummary's model expects these editable money fields to be numeric;
    Beacon's browser payload uses 0 for a cleared field, not an empty string.

    Other server/autofilled fields are left exactly as returned by GetSummary.
    """
    fees = summary.get("feesSummary") or []

    for row in fees[:6]:
        # UpdateSummary binds these as numeric fields. The browser's successful
        # payload represents an empty/cleared discount as numeric 0, not "".
        row["seniorCitizenDiscount"] = 0
        row["pwdDiscount"] = 0

    professional = summary.get("professionalFees") or []
    if professional:
        # Doctor actualCharges is editable too, so clear it before retyping
        # the mapped PF amount.  Use numeric zero for the same API-model reason.
        professional[0]["actualCharges"] = 0
        professional[0]["seniorCitizenDiscount"] = 0
        professional[0]["pwdDiscount"] = 0

    return summary


class SOAAutomation:
    """API-only Statement of Account automation."""

    def __init__(self, soa_folder=None, max_retries=2):
        self.results = []
        self.patient_birthdate = None
        self.patient_age = None
        self.patient_name = None
        self.soa_file = None
        self.soa_folder = (
            Path(soa_folder) if soa_folder else DEFAULT_SOA_FOLDER
        )

        # max_retries is the number of retries after the initial attempt.
        # max_retries=2 therefore means at most 3 total attempts.
        self.max_retries = max(0, int(max_retries))

    def process_transmittal(self, transmittal_no, idx, total):
        """Process one transmittal entirely through Beacon APIs."""
        result = {
            "transmittal": transmittal_no,
            "status": "failed",
            "message": "",
        }

        try:
            transmittal_no = str(transmittal_no).strip()

            logger.info("\n" + "=" * 60)
            logger.info(
                f"TRANSMITTAL {idx + 1}/{total} : {transmittal_no}"
            )
            logger.info("=" * 60)

            # Preserve the proven selection rule: exact transmittal, first claim.
            facility_id = get_facility_id()
            transmittal = soa_api.get_transmittal(transmittal_no, client_id=facility_id)
            if not transmittal:
                result["status"] = "skipped"
                result["message"] = "Transmittal not found"
                return result

            transmittal_id = int(transmittal["id"])
            claims = soa_api.get_claims(transmittal_id) or []

            if isinstance(claims, dict):
                claims = (
                    claims.get("phicClaims")
                    or claims.get("claims")
                    or claims.get("claimList")
                    or []
                )

            if not claims:
                raise Exception(
                    f"No claim found in transmittal {transmittal_no}"
                )

            claim_id = int(claims[0]["id"])
            soa_api.get_claim(claim_id)

            cf1 = soa_api.get_cf1(claim_id) or {}
            birthday_raw = cf1.get("patientBirthday")
            if not birthday_raw:
                raise Exception("Patient birthday is missing from CF1")

            birth_date = datetime.fromisoformat(
                str(birthday_raw).replace("Z", "+00:00")
            ).date()
            today = datetime.today().date()

            self.patient_birthdate = birth_date
            self.patient_age = (
                today.year
                - birth_date.year
                - (
                    (today.month, today.day)
                    < (birth_date.month, birth_date.day)
                )
            )

            self.patient_name = str(
                cf1.get("patientFullname") or ""
            ).strip()

            if not self.patient_name:
                raise Exception("Patient name is missing from CF1")

            logger.info(f"Patient Name: {self.patient_name}")
            logger.success(f"Patient Age = {self.patient_age}")

            # Charge state and generated-ESA document state are intentionally
            # independent. A stale ESA document alone does not mean charges
            # are still imported.
            soa_state = soa_api.get_soa_state(claim_id)
            med_count = int(soa_state.get("med_count") or 0)
            xlso_count = int(soa_state.get("xlso_count") or 0)
            soa_already_uploaded = bool(
                soa_state.get("charges_imported")
            )
            esa_document_exists = bool(
                soa_state.get("esa_document_exists")
            )

            logger.info(
                "SOA state: "
                f"charges_imported={soa_already_uploaded} "
                f"(source={soa_state.get('import_source') or 'none'}, "
                f"MED={med_count}, "
                f"XLSO={xlso_count}, "
                f"summary_total="
                f"{float(soa_state.get('summary_actual_total') or 0):.2f}); "
                f"ESA_document={esa_document_exists}"
            )

            if not soa_already_uploaded:
                soa_path = _find_soa_file(
                    self.patient_name,
                    self.soa_folder,
                )
                self.soa_file = str(soa_path)
                logger.success(f"SOA file found: {self.soa_file}")

                if soa_api.verify_excel(self.soa_file) is not True:
                    raise Exception("Beacon rejected the SOA workbook")

                med_rows, xlso_rows = soa_api.parse_soa_workbook(
                    self.soa_file
                )

                if med_rows:
                    soa_api.batch_upload_medicines(
                        claim_id,
                        med_rows,
                    )

                if xlso_rows:
                    soa_api.batch_upload_xlso(
                        claim_id,
                        xlso_rows,
                    )

                soa_api.upload_payment(
                    self.soa_file,
                    claim_id,
                )
                soa_api.upload_payment_item(
                    self.soa_file,
                    claim_id,
                )

                # Immediately after a fresh SOA upload, normalize every XLSO
                # row whose PhilHealth Mapping is "Others" to unit PIECE.
                _fix_others_unit_mapping(claim_id)

                result["message"] = "SOA uploaded successfully"
            else:
                if esa_document_exists:
                    logger.info(
                        "Existing SOA charge import and ESA document "
                        "detected — skipping Excel re-upload and "
                        "re-verifying discounts."
                    )
                else:
                    logger.info(
                        "Existing SOA charge import detected but ESA "
                        "document is missing — skipping Excel re-upload "
                        "and regenerating ESA from retained charges."
                    )

                result["message"] = (
                    "SOA charges already imported. "
                    "Re-verified discounts without re-uploading."
                )

            # Preserve the proven discount rules exactly.
            # Always refresh after a possible fresh import so actualCharges
            # are never taken from the pre-import state check.
            summary = soa_api.get_summary(claim_id) or {}

            # First clear every editable field this automation is responsible
            # for. This removes stale Senior/PWD discounts (and an old PF
            # amount) before the current patient's values are retyped.
            logger.info(
                "Clearing editable SOA fields before retyping discounts..."
            )
            clear_payload = _clear_editable_summary_fields(summary)
            soa_api.update_summary(clear_payload)

            # Re-fetch after the clear so the calculation starts from Beacon's
            # persisted clean state while retaining all server/autofilled data.
            summary = soa_api.get_summary(claim_id) or {}
            fees = summary.get("feesSummary") or []

            is_senior = self.patient_age >= 60
            discount_key = (
                "seniorCitizenDiscount"
                if is_senior
                else "pwdDiscount"
            )

            for row in fees[:6]:
                amount = float(row.get("actualCharges") or 0)
                if amount:
                    row[discount_key] = str(
                        round(amount * 0.20, 2)
                    )

            summary_total = sum(
                float(row.get("actualCharges") or 0)
                for row in fees[:6]
            )

            pf_actual_map = {
                7500: 437.50,
                15000: 875.00,
                22500: 1312.50,
                30000: 1750.00,
                37500: 2187.50,
                45000: 2625.00,
                52500: 3062.50,
            }

            pf_actual = pf_actual_map.get(summary_total)
            professional = summary.get("professionalFees") or []

            if pf_actual is None:
                logger.warning(
                    f"No Professional Fee mapping for {summary_total}"
                )
            elif not professional:
                logger.error(
                    "No professional fee to map, add a doctor."
                )
            else:
                professional[0]["actualCharges"] = str(pf_actual)
                professional[0][discount_key] = str(
                    round(pf_actual * 0.20, 2)
                )

            soa_api.update_summary(summary)

            esoa = soa_api.get_esoa_xml(
                claim_id,
                facility_id,
            )
            validation = soa_api.validate_esoa(esoa)

            if str(validation).strip() != "XML is Valid!":
                raise Exception(
                    f"ESOA validation failed: {validation}"
                )

            generated = soa_api.generate_and_upload_esoa(
                claim_id,
                facility_id,
            )

            if str(generated).lower() != "true":
                raise Exception(
                    f"ESOA generation/upload failed: {generated}"
                )

            result["status"] = "success"
            logger.success(
                "Statement of Account validated and generated successfully."
            )

        except Exception as exc:
            logger.error(
                f"\nERROR on transmittal {idx + 1} "
                f"({transmittal_no}): {exc}"
            )
            result["status"] = "failed"
            result["message"] = summarize_error(str(exc))

        return result

    def run(self, transmittals):
        """
        Run all transmittals with the existing retry semantics.

        max_retries is retries after the initial attempt, so max_retries=2
        means 3 total attempts. Each retry simply re-runs the API workflow
        for the same transmittal; no browser recovery is required.
        """
        try:
            report.results.clear()
            self.results.clear()

            for idx, transmittal_no in enumerate(transmittals):
                transmittal_no = str(transmittal_no).strip()
                max_attempts = self.max_retries + 1

                for attempt_number in range(1, max_attempts + 1):
                    logger.info("")
                    logger.info("=" * 60)
                    logger.info(
                        f"PROCESSING TRANSMITTAL "
                        f"{idx + 1}/{len(transmittals)}"
                    )
                    logger.info(
                        f"Transmittal: {transmittal_no}"
                    )
                    logger.info(
                        f"Attempt {attempt_number}/{max_attempts}"
                    )
                    logger.info("=" * 60)

                    result = self.process_transmittal(
                        transmittal_no,
                        idx,
                        len(transmittals),
                    )

                    if result["status"] == "success":
                        self.results.append(result)
                        logger.success(
                            f"Transmittal '{transmittal_no}' "
                            f"completed successfully on attempt "
                            f"{attempt_number}/{max_attempts}."
                        )
                        break

                    if result["status"] == "skipped":
                        self.results.append(result)
                        logger.warning(
                            f"Transmittal '{transmittal_no}' was skipped."
                        )
                        break

                    if attempt_number >= max_attempts:
                        self.results.append(result)
                        logger.error(
                            f"Transmittal '{transmittal_no}' FAILED "
                            f"after {max_attempts} attempt(s). "
                            "Retry limit reached — moving to next "
                            "transmittal."
                        )
                        break

                    retries_used = attempt_number
                    logger.warning(
                        f"Transmittal '{transmittal_no}' failed."
                    )
                    logger.warning(
                        f"Retrying SAME transmittal "
                        f"({retries_used}/{self.max_retries})..."
                    )

            logger.success("=" * 60)
            logger.success("SOA UPLOAD AUTOMATION COMPLETED")
            logger.success("=" * 60)

            logger.info("")
            logger.info("RESULTS BREAKDOWN:")
            logger.info("-" * 60)

            success_count = sum(
                result["status"] == "success"
                for result in self.results
            )
            failed_count = sum(
                result["status"] == "failed"
                for result in self.results
            )
            skipped_count = sum(
                result["status"] == "skipped"
                for result in self.results
            )

            for result in self.results:
                line = (
                    f"{result['transmittal']}: "
                    f"{result['status'].upper()} - "
                    f"{result['message']}"
                )

                if result["status"] == "success":
                    logger.success(f"[SUCCESS] {line}")
                elif result["status"] == "skipped":
                    logger.warning(f"[SKIPPED] {line}")
                else:
                    logger.error(f"[FAILED] {line}")

            logger.info("-" * 60)
            logger.info(
                f"Total: {len(self.results)} | "
                f"Success: {success_count} | "
                f"Failed: {failed_count} | "
                f"Skipped: {skipped_count}"
            )
            logger.info("No more transmittals to process.")

            return True

        except Exception as exc:
            logger.error(
                f"Fatal error in SOA automation: {exc}"
            )
            return False

    def get_results(self):
        """Return the final per-transmittal results."""
        return self.results

    def close(self):
        """
        Compatibility no-op.

        The SOA workflow is API-only and owns no browser/page resources.
        Existing callers may still invoke close(), so the method remains
        without any UI/browser interaction.
        """
        return None
