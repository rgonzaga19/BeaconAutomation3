from pathlib import Path
import os
import re
import sys
from datetime import datetime


DEFAULT_SOA_FOLDER = Path.home() / "Downloads" / "SOA"


if getattr(sys, "frozen", False):
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(
        os.path.dirname(sys.executable),
        "ms-playwright"
    )

from logger import logger
from reports import report
import browser_session


# Common Filipino compound-surname particles. When the token right
# before the last word of a name is one of these, it's treated as part
# of the surname (e.g. "DE OCAMPO", "DE JESUS", "DE LOS SANTOS", "DELA
# CRUZ") instead of being left behind as a stray middle name.
SURNAME_PARTICLES = {
    "DE", "DEL", "DELA", "DELAS", "DELOS",
    "SAN", "SANTA", "STA", "STO", "SANTO",
    "MAC", "MC", "VAN", "VON", "DA", "DI", "LA", "LAS", "LOS",
}

# Generational suffixes that show up as their own token right after the
# surname (e.g. "JUAN DELA CRUZ JR", "JUAN DELA CRUZ III."). Compared with
# any trailing period stripped, so both "JR" and "JR." match.
NAME_SUFFIXES = {
    "JR", "SR",
    "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X",
}


def _strip_name_suffixes(tokens):
    """
    Removes generational suffix tokens like "JR"/"SR"/"II" through "X"
    (with or without a trailing period, e.g. "JR." or "III.") from a
    list of name tokens before surname/given splitting runs.

    _split_surname_and_given() assumes the *last* token is the surname.
    Beacon's patient_name puts a suffix like "JR" right after the
    surname — e.g. "JUAN DELA CRUZ JR" — so without this, the suffix
    itself got treated as the whole surname ("JR") and "DELA CRUZ" was
    left behind as if it were part of the given name, which then made
    the SOA filename search key on the wrong name entirely. Stripping is
    done across all tokens (not just the final one) so a stray suffix
    elsewhere in the string can't confuse the split either.
    """
    return [t for t in tokens if t.rstrip(".") not in NAME_SUFFIXES]


def _split_surname_and_given(name):
    """
    Splits an upper-cased patient name into (surname_tokens, given_tokens),
    assuming Western order (surname last — matches how patient_name comes
    off the Beacon claim details page). Walks backward from the last word
    absorbing known surname particles so multi-word surnames like
    "DE OCAMPO" or "DE LOS SANTOS" are captured whole, instead of losing
    the particle to the given-name side.
    """
    tokens = name.upper().split()
    tokens = _strip_name_suffixes(tokens)

    if len(tokens) <= 1:
        return tokens, []

    surname_tokens = [tokens[-1]]
    i = len(tokens) - 2

    while i >= 0 and tokens[i] in SURNAME_PARTICLES:
        surname_tokens.insert(0, tokens[i])
        i -= 1

    given_tokens = tokens[:i + 1]
    return surname_tokens, given_tokens


def _normalize(text):
    # Keep Ñ as a valid letter.
    text = text.upper()
    return re.sub(r"[^A-Z0-9Ñ]", "", text)

def _filename_tokens(filename):
    """
    Returns normalized filename tokens.

    Examples:
        JUAN DELA CRUZ.xlsx
            -> ["JUAN", "DELA", "CRUZ"]

        VILLA_JUAN_001.xlsx
            -> ["VILLA", "JUAN", "001"]

        SO TESSORO.xls
            -> ["SO", "TESSORO"]
    """
    stem = Path(filename).stem.upper()
    return [t for t in re.split(r"[^A-Z0-9Ñ]+", stem) if t]


def _wait_for_count(page, locator, min_count=1, timeout_ms=15000, poll_ms=300):
    """
    Polls locator.count() until it reaches at least min_count, or the
    timeout elapses. Returns the final count either way.

    Beacon's pages are React-rendered, so a fixed wait_for_timeout() has
    to guess how long something takes to appear — too short and it reads
    an empty/stale DOM, too long and it wastes time on the common case.
    This instead waits for the actual thing to show up, which matters
    most exactly when it's slow (e.g. a large SOA file taking longer to
    process server-side before the Statement of Account modal populates
    its rows).
    """
    elapsed = 0
    count = locator.count()

    while count < min_count and elapsed < timeout_ms:
        page.wait_for_timeout(poll_ms)
        elapsed += poll_ms
        count = locator.count()

    return count


def _type_into_number_field(page, locator, value, max_attempts=3):
    """
    Reliably enters a value into one of Beacon's Summary/Professional Fees
    number-spinner inputs.

    These are React-controlled fields with two known quirks:

    1. Clicking one and typing right after can outrun the field actually
       becoming interactive, which silently drops the first keystrokes
       (e.g. typing "1540" lands as just "40" because the field wasn't
       ready to accept input yet).
    2. The typed value can look correct immediately afterward, but then
       silently snap back to 0 once focus moves to a different field —
       Beacon appears to only commit the value on blur, and that commit
       occasionally discards what was typed. Blurring the field
       ourselves right after typing (via Tab, the same thing a real user
       tabbing to the next field would do) and re-reading the value
       *after* that blur — instead of only right after typing — catches
       this before we move on, rather than only discovering it later
       once we're back at the Save button with every discount reset to
       zero.
    """
    target_value = f"{value:.2f}"

    for attempt in range(1, max_attempts + 1):
        locator.scroll_into_view_if_needed()
        locator.click(force=True)

        # Let the field finish becoming interactive/typable before typing.
        page.wait_for_timeout(200)

        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        locator.type(target_value, delay=50)

        # Force the same commit a real user tabbing away would trigger,
        # then re-read the value AFTER that commit — this is where the
        # field has been observed to snap back to 0.
        page.keyboard.press("Tab")
        page.wait_for_timeout(150)

        actual = locator.input_value().strip()
        if actual == target_value:
            return

        logger.warning(
            f"Attempt {attempt}/{max_attempts}: expected '{target_value}', "
            f"got '{actual}' after blur — retrying"
        )

    # Last resort: set the value directly instead of via keystrokes.
    locator.click(force=True)
    locator.fill(target_value)
    page.keyboard.press("Tab")
    page.wait_for_timeout(150)
    actual = locator.input_value().strip()

    if actual != target_value:
        raise Exception(
            f"Could not set field to '{target_value}' after "
            f"{max_attempts} attempts (final value: '{actual}')"
        )

    logger.warning(f"Fell back to fill() to set '{target_value}'")


def open_transmittals(page):
    """Navigate to transmittals & claims list."""
    try:
        page.get_by_role("button", name="E-CLAIMS").click()
        page.wait_for_load_state("networkidle")
    except:
        pass

    page.get_by_text(
        "TRANSMITTALS & CLAIMS",
        exact=False
    ).click()

    page.wait_for_load_state("networkidle")

    # Make sure the search box is actually there and interactive before
    # handing control back to the next transmittal. Without this, the
    # next iteration's search can fire while the list page is still
    # re-hydrating, silently missing the typed query and producing a
    # false "transmittal not found" even though it exists.
    try:
        page.locator('input[type="text"]').first.wait_for(
            state="visible", timeout=10000
        )
    except Exception:
        pass

    logger.info("Returned to patient list")


class SOAAutomation:
    """Handles automated SOA (Statement of Accounts) upload workflow."""

    def __init__(self, soa_folder=None):
        self.page = None
        self.results = []
        self.patient_birthdate = None
        self.patient_age = None
        self.patient_name = None
        self.soa_file = None
        self.soa_folder = Path(soa_folder) if soa_folder else DEFAULT_SOA_FOLDER

    def process_transmittal(self, transmittal_no, idx, total):
        """Process a single transmittal for SOA upload."""
        result = {
            "transmittal": transmittal_no,
            "status": "failed",
            "message": "",
        }

        try:
            transmittal_no = str(transmittal_no).strip()

            logger.info("\n" + "=" * 60)
            logger.info(f"TRANSMITTAL {idx + 1}/{total} : {transmittal_no}")
            logger.info("=" * 60)

            # ── Search transmittal ─────────────────────────────────────────
            # Retries once: right after a previous transmittal's success/
            # skip, the list page can still be re-hydrating for a moment,
            # so the first search occasionally fires against a not-yet-
            # ready field and comes back with zero rows even though the
            # transmittal genuinely exists. A second attempt after a short
            # wait resolves that without masking a real "not found".
            row_count = 0

            for search_attempt in range(1, 3):
                search_box = self.page.locator('input[type="text"]').first
                search_box.wait_for(state="visible", timeout=10000)
                search_box.click()
                search_box.press("Control+A")
                search_box.press("Backspace")
                search_box.fill(transmittal_no)
                search_box.press("Enter")
                self.page.wait_for_load_state("networkidle")

                row_count = _wait_for_count(
                    self.page,
                    self.page.locator("tbody tr"),
                    min_count=1,
                    timeout_ms=4000,
                    poll_ms=400,
                )

                if row_count > 0:
                    break

                logger.warning(
                    f"Search attempt {search_attempt}/2: no rows found "
                    f"yet for '{transmittal_no}'"
                )

                if search_attempt < 2:
                    self.page.wait_for_timeout(1500)

            if row_count == 0:
                logger.warning(f"TRANSMITTAL NOT FOUND: {transmittal_no}")
                result["status"] = "skipped"
                result["message"] = "Transmittal not found"
                self.results.append(result)
                return result

            # ── Open first row → Manage Claims ─────────────────────────
            logger.info("Opening row menu...")
            logger.info(f"Current URL: {self.page.url}")

            rows = self.page.locator("tbody tr")
            logger.info(f"Rows found: {rows.count()}")

            first_row = rows.first

            buttons = first_row.locator("button")
            logger.info(f"Buttons in first row: {buttons.count()}")

            buttons.last.click()

            self.page.wait_for_timeout(1000)

            logger.info("Clicking Manage Claims...")

            manage_claims = self.page.get_by_text("Manage Claims", exact=True)

            logger.info(f"Manage Claims count: {manage_claims.count()}")

            manage_claims.wait_for(state="visible", timeout=5000)
            manage_claims.click()

            self.page.wait_for_timeout(1000)

            logger.success("SUCCESS: Manage Claims opened")

            # ── Open claim → Manage ────────────────────────────────────
            claim_row = self.page.locator("tbody tr").first

            buttons = claim_row.locator("button")
            logger.info(f"Claim row buttons: {buttons.count()}")

            buttons.last.click()

            self.page.wait_for_timeout(1000)

            logger.info("Clicking Manage...")
            self.page.get_by_text("Manage", exact=True).click()
            self.page.wait_for_timeout(1000)
            logger.success("SUCCESS: PHIC Claim Details opened")

            # ── Validate Eligibility (if button appears) ───────────────────
            validate_btn = self.page.locator("button", has_text="Validate Eligibility")

            if validate_btn.count() > 0:
                logger.info("Clicking Validate Eligibility...")
                validate_btn.first.click()
                self.page.wait_for_load_state("networkidle")
                logger.success("SUCCESS: Eligibility validated")
            else:
                logger.info("No validation required — skipping.")

            # ── Get Patient Birthday and Compute Age ───────────────────────

            logger.info("Reading patient birthday...")

            birthday_text = self.page.locator(
                "//td[normalize-space()='Patient Birthday']/following-sibling::td"
            ).inner_text().strip()

            logger.info(f"Birthday: {birthday_text}")

            birth_date = datetime.strptime(birthday_text, "%B %d, %Y").date()
            today = datetime.today().date()

            age = today.year - birth_date.year - (
                (today.month, today.day) < (birth_date.month, birth_date.day)
            )

            self.patient_birthdate = birth_date
            self.patient_age = age

            logger.success(f"Patient Age = {self.patient_age}")

            # ── Get the Patient Name ─────────────────────────────────────────

            patient_name = self.page.locator(
                "//td[normalize-space()='Patient Name']/following-sibling::td"
            ).inner_text().strip()

            logger.info(f"Patient Name: {patient_name}")

            self.patient_name = patient_name

            # ── Check CHARGES tab for existing items ────────────────────────
            # Guards against uploading a second SOA to a transmittal/patient
            # that has already been processed.

            logger.info("Checking CHARGES tab for existing items...")

            self.page.get_by_role("link", name="CHARGES", exact=True).click()
            self.page.wait_for_load_state("networkidle")
            self.page.wait_for_timeout(1500)

            # ------------------------------------------------------------------
            # Look for uploaded MED items in the Charges grid
            # ------------------------------------------------------------------
            logger.info("Searching for uploaded MED items...")

            med_items = self.page.locator("td[title^='MED']")

            existing_charges_count = med_items.count()

            logger.info(f"MED items found: {existing_charges_count}")

            for i in range(existing_charges_count):
                try:
                    value = med_items.nth(i).get_attribute("title")
                    logger.info(f"Found existing item: {value}")
                except Exception:
                    pass

            if existing_charges_count > 0:
                logger.warning(
                    f"Charges table already has {existing_charges_count} "
                    "item(s) — SOA appears to already be uploaded for this "
                    "transmittal. Skipping to avoid a duplicate upload."
                )

                result["status"] = "skipped"
                result["message"] = (
                    f"Charges table already has {existing_charges_count} "
                    "item(s) — SOA already uploaded, skipping to avoid duplicate."
                )

                self.results.append(result)

                is_last = (idx == total - 1)

                if not is_last:
                    try:
                        open_transmittals(self.page)
                    except Exception:
                        pass

                return result

            logger.success("Charges table is empty — proceeding with upload.")

            # ── Open PAYMENTS tab ─────────────────────────────────

            logger.info("Opening PAYMENTS tab...")

            self.page.get_by_role("link", name="PAYMENTS").click()

            self.page.wait_for_timeout(1000)

            logger.success("PAYMENTS tab opened")

            # ── Upload Statement of Account ───────────────────────────────────────────
            logger.info("Opening Statement of Account upload...")

            logger.info("Clicking Upload Charges and Payment...")

            self.page.get_by_role(
                "button",
                name="UPLOAD CHARGES AND PAYMENT"
            ).click()

            # The upload modal/input can take a moment to mount — wait for
            # the file input to actually attach instead of guessing with a
            # fixed sleep, since that's what caused missed fields further
            # down the line when the modal was slow to appear.
            try:
                self.page.wait_for_selector(
                    "input[type='file']", state="attached", timeout=10000
                )
            except Exception:
                pass

            logger.success("Upload Charges and Payment clicked")

            # -------------------------------------------------------------------------
            # Locate the SOA file automatically
            # -------------------------------------------------------------------------

            surname_tokens, given_tokens = _split_surname_and_given(
                self.patient_name
            )

            surname_key = _normalize("".join(surname_tokens))          # e.g. "DEOCAMPO"
            bare_surname_key = _normalize(surname_tokens[-1]) if surname_tokens else ""  # e.g. "OCAMPO"
            given_key = _normalize("".join(given_tokens))              # e.g. "JUANCRUZ"
            given_initial = _normalize(given_tokens[0])[:1] if given_tokens else ""

            logger.info(f"Searching SOA in: {self.soa_folder}")
            logger.info(
                f"Surname: '{' '.join(surname_tokens)}' | "
                f"Given name: '{' '.join(given_tokens)}'"
            )

            if not self.soa_folder.exists():
                raise Exception(
                    f"SOA folder does not exist: {self.soa_folder}"
                )

            all_files = []
            for pattern in ("*.xlsx", "*.xls"):
                all_files.extend(self.soa_folder.glob(pattern))

            def _matching(tokens_to_match, require_exact_length=False):
                """
                Looks for tokens_to_match as a consecutive, whole-word run
                inside each filename's tokens.

                require_exact_length=True additionally rejects a match if
                the token immediately before the match is itself a surname
                particle (DE/DEL/SAN/etc.). That guards against a shorter
                patient surname (e.g. "RESUS") falsely matching inside a
                filename whose real surname is longer and compound (e.g.
                "DE RESUS.xlsx", which likely belongs to a different
                patient whose actual surname is "DE RESUS", not "RESUS").
                Without this, a patient named plain "RESUS" and a filename
                "DE RESUS.xlsx" belonging to someone else both match on
                the token "RESUS", creating an ambiguity that shouldn't
                exist.
                """
                if not tokens_to_match:
                    return []

                tokens_to_match = [t.upper() for t in tokens_to_match]
                matches = []

                for f in all_files:
                    tokens = _filename_tokens(f.name)

                    # Look for consecutive whole-word matches
                    for i in range(len(tokens) - len(tokens_to_match) + 1):
                        if tokens[i:i + len(tokens_to_match)] != tokens_to_match:
                            continue

                        if require_exact_length:
                            preceding = tokens[i - 1] if i > 0 else None
                            if preceding in SURNAME_PARTICLES:
                                continue

                        matches.append(f)
                        break

                return matches

            def _has_given_name_marker(filename):
                """
                Returns True if the given name (or initial) is immediately
                before or after the matched surname as whole words.
                """

                tokens = _filename_tokens(filename)

                for i in range(len(tokens) - len(surname_tokens) + 1):

                    if tokens[i:i + len(surname_tokens)] != surname_tokens:
                        continue

                    before = tokens[i - 1] if i > 0 else None
                    after = (
                        tokens[i + len(surname_tokens)]
                        if i + len(surname_tokens) < len(tokens)
                        else None
                    )

                    if given_tokens:
                        if before == given_tokens[0] or after == given_tokens[0]:
                            return True

                    if given_initial:
                        if before == given_initial or after == given_initial:
                            return True

                return False

            # 1) Priority: exact-length surname match (compound-aware, e.g.
            #    "DEOCAMPO"). The matched span must NOT be immediately
            #    preceded by another particle — otherwise the filename's
            #    real surname is longer/compound relative to what we
            #    extracted for this patient (e.g. patient surname "RESUS"
            #    should not match "DE RESUS.xlsx", since that file's true
            #    surname is the compound "DE RESUS" and likely belongs to
            #    a different patient).
            matches = _matching(surname_tokens, require_exact_length=True)

            # 2) Fallback: loose surname match, ignoring what precedes it.
            #    Covers cases where our own particle-detection logic didn't
            #    recognize a genuine compound surname (e.g. an uncommon
            #    particle not listed in SURNAME_PARTICLES).
            if not matches:
                loose_matches = _matching(surname_tokens)
                if loose_matches:
                    logger.info(
                        f"No exact-length match for surname '{surname_key}' "
                        f"— falling back to loose match: "
                        f"{[f.name for f in loose_matches]}"
                    )
                matches = loose_matches

            # 3) Fallback: surname without the leading particle, in case the
            #    filename dropped "DE"/"DEL"/etc. (e.g. just "OCAMPO.xlsx").
            #    Exact-length is tried first here too, so a bare-surname
            #    fallback can't accidentally match a different compound-
            #    surname file (e.g. "OCAMPO" incorrectly matching inside
            #    "DE OCAMPO.xlsx" when that file belongs to someone else).
            if not matches and bare_surname_key != surname_key:
                logger.info(
                    f"No match for full surname '{surname_key}' — "
                    f"trying bare surname '{bare_surname_key}'"
                )
                matches = _matching(
                    [surname_tokens[-1]], require_exact_length=True
                )
                if not matches:
                    matches = _matching([surname_tokens[-1]])

            # No exact surname match at all (full or bare) — do NOT fall back
            # to searching by given name alone, since a given-name-only match
            # says nothing about whether it's the right person's surname.
            # It's better to skip the transmittal than upload the wrong file.
            if not matches:
                raise Exception(
                    f"No SOA file found for patient '{self.patient_name}': "
                    f"no filename matched surname '{' '.join(surname_tokens)}' "
                    f"(or bare surname '{bare_surname_key}') "
                    f"inside {self.soa_folder}. Skipping — will not guess."
                )

            # If several files share the same surname (e.g. two patients with
            # the same last name), narrow down using the given name / initial.
            # If narrowing doesn't land on exactly one file, stop and raise —
            # do not fall back to "most recent" or any other closest-match
            # guess, since that risks uploading the wrong patient's SOA.
            if len(matches) > 1:
                narrowed = [
                    f for f in matches
                    if _has_given_name_marker(f.name)
                ]

                if not narrowed:
                    raise Exception(
                        f"Multiple SOA files matched surname "
                        f"'{' '.join(surname_tokens)}' "
                        f"({[f.name for f in matches]}) but none could be "
                        f"confirmed against given name '{' '.join(given_tokens)}'. "
                        "Skipping — will not guess which file belongs to "
                        "this patient."
                    )

                if len(narrowed) > 1:
                    raise Exception(
                        f"Multiple SOA files still match patient "
                        f"'{self.patient_name}' after narrowing by given "
                        f"name ({[f.name for f in narrowed]}). Skipping — "
                        "will not guess which file is correct."
                    )

                logger.info(
                    f"Multiple files matched surname "
                    f"'{' '.join(surname_tokens)}' — narrowed to a single "
                    "exact match using given name"
                )
                matches = narrowed

            logger.info(f"Candidate SOA files: {[f.name for f in matches]}")

            # Exactly one confirmed match at this point.
            soa_file = matches[0]

            self.soa_file = str(soa_file)

            logger.success(f"SOA file found: {self.soa_file}")

            # -------------------------------------------------------------------------
            # Upload without using the Windows File Dialog
            # -------------------------------------------------------------------------

            file_input = self.page.locator("input[type='file']")
            logger.info(f"File inputs found: {file_input.count()}")

            if file_input.count() > 0:

                logger.info("Uploading SOA...")

                file_input.set_input_files(self.soa_file)

                # Larger SOA files take noticeably longer to process
                # server-side — wait for network activity to settle
                # instead of a fixed delay, so we don't race the
                # "Statement of Account" button before the upload has
                # actually finished.
                try:
                    self.page.wait_for_load_state(
                        "networkidle", timeout=20000
                    )
                except Exception:
                    pass
                self.page.wait_for_timeout(500)

                logger.success("SOA uploaded successfully.")
                result["status"] = "success"
                result["message"] = "SOA uploaded successfully"

            else:

                logger.info("Waiting for file chooser...")

                with self.page.expect_file_chooser() as fc:

                    self.page.get_by_role(
                        "button",
                        name="UPLOAD CHARGES AND PAYMENT"
                    ).click()

                fc.value.set_files(self.soa_file)

                try:
                    self.page.wait_for_load_state(
                        "networkidle", timeout=20000
                    )
                except Exception:
                    pass
                self.page.wait_for_timeout(500)

                logger.success("SOA uploaded successfully.")

            
            # ── Open Statement of Account ───────────────────────────────

            logger.info("Opening Statement of Account...")

            soa_button = self.page.locator(
                "button:has(span:text('Statement of Account'))"
            )

            # Wait generously — this button (and the modal it opens) can
            # take a while to become available right after a large
            # upload finishes processing.
            soa_button.wait_for(state="visible", timeout=20000)
            soa_button.click()

            self.page.wait_for_timeout(1000)

            logger.success("Statement of Account opened")

            # ============================================================
            # Populate Senior Citizen / PWD Discount
            # ============================================================

            logger.info("Computing discounts...")

            is_senior = self.patient_age >= 60
            target_prefix = "seniorCitizenDiscount" if is_senior else "pwdDiscount"

            logger.info(f"Using {'Senior' if is_senior else 'PWD'} Discount")

            # Get all disabled Actual Charges (Summary section)
            actual_inputs = self.page.locator("input[id^='actualCharges']:disabled")
            discount_inputs = self.page.locator(f"input[id^='{target_prefix}']")

            # The Summary/Professional Fees table can take a moment to
            # render after the modal opens — especially with larger
            # uploads. Poll for it instead of assuming it's already
            # there: previously, reading actual_inputs.count() too early
            # returned 0, so summary_rows came out as 0 and the entire
            # discount loop below silently did nothing — no error, no
            # warning, just a "successful" upload with every discount
            # left blank.
            actual_count = _wait_for_count(
                self.page, actual_inputs,
                min_count=1, timeout_ms=15000, poll_ms=300
            )

            if actual_count == 0:
                raise Exception(
                    "Statement of Account modal did not populate any "
                    "Actual Charges rows in time — cannot compute "
                    "discounts. The modal may still be loading, or the "
                    "upload didn't parse as expected."
                )

            summary_rows = min(6, actual_count, discount_inputs.count())

            logger.info(f"Processing {summary_rows} Summary rows")

            # Tracks every discount field we set, so we can do a final
            # sweep right before Save and catch any that silently
            # reverted back to 0 after we moved on to a later field.
            discount_targets = []

            for row in range(summary_rows):

                try:
                    actual = actual_inputs.nth(row)
                    target = discount_inputs.nth(row)

                    value = actual.input_value().strip()

                    logger.info(f"Row {row}: actualCharges = '{value}'")

                    if not value:
                        continue

                    amount = float(value.replace(",", ""))

                    if amount == 0:
                        logger.info(f"Row {row}: skipped (0)")
                        continue

                    discount = round(amount * 0.20, 2)

                    _type_into_number_field(self.page, target, discount)
                    discount_targets.append((target, f"{discount:.2f}"))

                    logger.info(
                        f"Row {row}: {amount} -> {discount:.2f}"
                    )

                except Exception as e:
                    logger.warning(f"Row {row}: {e}")

            logger.success("Discount computation completed.")


            # ==========================================================
            # Populate Professional Fees
            # ==========================================================

            logger.info("Computing Professional Fees...")

            summary_total = sum(
                float(actual_inputs.nth(i).input_value().replace(",", ""))
                for i in range(summary_rows)
            )

            logger.info(f"Summary Total = {summary_total}")

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

            if pf_actual is None:
                logger.warning(f"No Professional Fee mapping for {summary_total}")
            else:

                pf_discount = round(pf_actual * 0.20, 2)

                logger.info(f"Professional Actual = {pf_actual}")
                logger.info(f"Professional Discount = {pf_discount}")

                # second actualCharges0 belongs to Professional Fees
                pf_actual_input = self.page.locator("input#actualCharges0").nth(1)

                if is_senior:
                    pf_discount_input = self.page.locator("input#seniorCitizenDiscount0").nth(1)
                else:
                    pf_discount_input = self.page.locator("input#pwdDiscount0").nth(1)

                # Actual Charges
                _type_into_number_field(self.page, pf_actual_input, pf_actual)

                # Discount
                _type_into_number_field(self.page, pf_discount_input, pf_discount)
                discount_targets.append((pf_discount_input, f"{pf_discount:.2f}"))

                logger.success("Professional Fees populated.")

            # ==========================================================
            # Re-verify discount fields before saving
            # ==========================================================
            # Beacon has been observed to silently reset a previously-
            # typed discount back to 0 once focus shifts to a later
            # field elsewhere on the page — even though it read back
            # correctly right after being typed. Do one final pass over
            # every discount field we set, retyping any that reverted,
            # right before committing with Save.

            if discount_targets:
                logger.info("Re-verifying discount fields before saving...")

                for verify_pass in range(1, 4):
                    reverted = []

                    for target_locator, expected in discount_targets:
                        try:
                            current = target_locator.input_value().strip()
                        except Exception:
                            current = None

                        if current != expected:
                            reverted.append((target_locator, expected, current))

                    if not reverted:
                        logger.success("All discount fields verified correct.")
                        break

                    logger.warning(
                        f"Verification pass {verify_pass}/3: "
                        f"{len(reverted)} field(s) reverted — retyping "
                        + ", ".join(
                            f"(expected {exp}, got {cur})"
                            for _, exp, cur in reverted
                        )
                    )

                    for target_locator, expected, _ in reverted:
                        _type_into_number_field(
                            self.page, target_locator, float(expected)
                        )
                else:
                    logger.warning(
                        "Some discount fields may still be incorrect "
                        "after 3 verification passes — proceeding "
                        "anyway, please double-check this transmittal "
                        "manually."
                    )

            # ==========================================================
            # Save Statement of Account
            # ==========================================================

            logger.info("Saving Statement of Account...")

            save_btn = self.page.locator("button[type='submit']").last

            save_btn.scroll_into_view_if_needed()
            save_btn.click(force=True)

            logger.info("Waiting for save to complete...")

            self.page.wait_for_load_state("networkidle")
            self.page.wait_for_timeout(3000)

            logger.success("Statement of Account saved successfully.")
            close_btn = self.page.get_by_role("button", name="CLOSE")
            close_btn.scroll_into_view_if_needed()
            close_btn.click(force=True)

            is_last = (idx == total - 1)

            if is_last:
                logger.info("No more transmittals to process — skipping return to list.")
            else:
                open_transmittals(self.page)

        except Exception as e:
            logger.error(f"\nERROR on transmittal {idx + 1} ({transmittal_no}): {e}")
            logger.warning("Skipping to next transmittal...")
            result["status"] = "failed"
            result["message"] = str(e)

            is_last = (idx == total - 1)

            if not is_last:
                try:
                    open_transmittals(self.page)
                except:
                    pass

        self.results.append(result)
        return result

    def run(self, transmittals):
        """Main entry point for SOA upload automation."""
        try:
            report.results.clear()
            self.page = browser_session.connect()

            # ── Navigate to Transmittals ───────────────────────────────────────
            logger.info("Opening Transmittals...")
            open_transmittals(self.page)

            # ── Transmittal loop ───────────────────────────────────────────────
            for idx, transmittal_no in enumerate(transmittals):
                self.process_transmittal(transmittal_no, idx, len(transmittals))

            logger.success("=" * 60)
            logger.success("SOA UPLOAD AUTOMATION COMPLETED")
            logger.success("=" * 60)

            # ── Per-transmittal breakdown ───────────────────────────────────────
            logger.info("")
            logger.info("RESULTS BREAKDOWN:")
            logger.info("-" * 60)

            success_count = sum(1 for r in self.results if r["status"] == "success")
            failed_count = sum(1 for r in self.results if r["status"] == "failed")
            skipped_count = sum(1 for r in self.results if r["status"] == "skipped")

            for r in self.results:
                line = f"{r['transmittal']}: {r['status'].upper()} - {r['message']}"

                if r["status"] == "success":
                    logger.success(f"[SUCCESS] {line}")
                elif r["status"] == "skipped":
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

            logger.info("No more transmittals to process. Closing browser...")
            self.close()

            return True

        except Exception as e:
            logger.error(f"Fatal error in SOA automation: {e}")
            self.close()
            return False

    def get_results(self):
        """Returns the list of per-transmittal results."""
        return self.results

    def close(self):
        """Close browser session."""
        try:
            if self.page:
                context = self.page.context
                browser = context.browser

                try:
                    context.close()
                except Exception as e:
                    logger.warning(f"Error closing context: {e}")

                if browser:
                    try:
                        browser.close()
                    except Exception as e:
                        logger.warning(f"Error closing browser: {e}")

                logger.info("Browser closed.")

            self.page = None
        except Exception as e:
            logger.warning(f"Error during close(): {e}")