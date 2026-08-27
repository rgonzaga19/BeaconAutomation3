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
from reports import report, summarize_error
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
    "II", "III", "lll", "IV", "V", "VI", "VII", "VIII", "IX", "X",
}


def _strip_name_suffixes(tokens):
    """
    Removes generational suffix tokens like "JR"/"SR"/"II" through "X"
    (with or without a trailing period, e.g. "JR." or "III.") from a
    list of name tokens before surname/given splitting runs.
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

    A hyphen in the last word (e.g. "ONG-HAY") is treated the same way —
    both halves are always kept together as one compound surname.
    """
    tokens = name.upper().split()
    tokens = _strip_name_suffixes(tokens)

    if not tokens:
        return [], []

    last = tokens[-1]
    if "-" in last:
        hyphen_parts = [p for p in last.split("-") if p]
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
    Reliably enters a value into Beacon's Summary/Professional Fees
    number-spinner inputs.
    """
    target_value = f"{value:.2f}"

    for attempt in range(1, max_attempts + 1):
        locator.scroll_into_view_if_needed()
        locator.click(force=True)

        page.wait_for_timeout(200)

        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        locator.type(target_value, delay=50)

        page.keyboard.press("Tab")
        page.wait_for_timeout(150)

        actual = locator.input_value().strip()
        if actual == target_value:
            return

        logger.warning(
            f"Attempt {attempt}/{max_attempts}: expected '{target_value}', "
            f"got '{actual}' after blur — retrying"
        )

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
    except Exception:
        pass

    page.get_by_text(
        "TRANSMITTALS & CLAIMS",
        exact=False
    ).click()

    page.wait_for_load_state("networkidle")

    max_attempts = 3

    for attempt in range(max_attempts):
        try:
            search_box = page.locator(
                'input[type="text"]:not([disabled])'
            ).first

            search_box.wait_for(
                state="visible",
                timeout=8000
            )

            search_box.scroll_into_view_if_needed()
            search_box.click(timeout=5000)
            search_box.fill("")

            logger.info("Search box is ready and enabled.")
            break

        except Exception:
            if attempt < max_attempts - 1:
                page.wait_for_timeout(800)
            else:
                logger.warning(
                    "Search box may not be fully ready, proceeding anyway."
                )

    logger.info("Returned to patient list")


class SOAAutomation:
    """Handles automated SOA (Statement of Accounts) upload workflow."""

    def __init__(self, soa_folder=None, max_retries=2):
        self.page = None
        self.results = []
        self.patient_birthdate = None
        self.patient_age = None
        self.patient_name = None
        self.soa_file = None
        self.soa_folder = Path(soa_folder) if soa_folder else DEFAULT_SOA_FOLDER

        # Step 3:
        # max_retries is the number of RETRIES after the initial attempt.
        # max_retries=2 means at most 3 processing attempts total.
        self.max_retries = max(0, int(max_retries))

    def _cancel_upload_confirmation(self):
        try:
            cancel_btn = self.page.locator("button").filter(has_text="Cancel").first

            if cancel_btn.count() > 0:
                logger.info("Upload confirmation detected. Clicking Cancel...")
                cancel_btn.click(force=True)
                self.page.wait_for_timeout(500)

        except Exception:
            pass

    def _recover_transmittal(self, transmittal_no):
        """
        Recover from any error/timeout while processing a transmittal.

        Recovery sequence:
        1. Reload the current page.
        2. Wait for the page to settle.
        3. Return to TRANSMITTALS & CLAIMS.
        4. Search for the same transmittal again.

        Returns True if the transmittal search page is restored and the
        transmittal is found again, otherwise False.
        """
        try:
            transmittal_no = str(transmittal_no).strip()

            logger.warning(
                f"RECOVERY: Attempting to recover transmittal "
                f"'{transmittal_no}'..."
            )

            logger.info("RECOVERY: Reloading current page...")

            self.page.reload(
                wait_until="domcontentloaded",
                timeout=30000
            )

            try:
                self.page.wait_for_load_state(
                    "networkidle",
                    timeout=15000
                )
            except Exception:
                logger.warning(
                    "RECOVERY: Network did not reach idle state. "
                    "Continuing anyway."
                )

            self.page.wait_for_timeout(1500)

            logger.info(
                "RECOVERY: Returning to TRANSMITTALS & CLAIMS..."
            )

            open_transmittals(self.page)

            logger.info(
                f"RECOVERY: Searching transmittal '{transmittal_no}' again..."
            )

            search_box = self.page.locator(
                'input[type="text"]:not([disabled])'
            ).first

            search_box.wait_for(
                state="visible",
                timeout=10000
            )

            search_box.scroll_into_view_if_needed()
            search_box.click()

            search_box.press("Control+A")
            search_box.press("Backspace")

            search_box.fill(transmittal_no)
            search_box.press("Enter")

            try:
                self.page.wait_for_load_state(
                    "networkidle",
                    timeout=15000
                )
            except Exception:
                logger.warning(
                    "RECOVERY: Search did not reach networkidle. "
                    "Checking rows anyway."
                )

            self.page.wait_for_timeout(1000)

            row_count = _wait_for_count(
                self.page,
                self.page.locator("tbody tr"),
                min_count=1,
                timeout_ms=10000,
                poll_ms=500
            )

            if row_count <= 0:
                logger.error(
                    f"RECOVERY FAILED: Transmittal "
                    f"'{transmittal_no}' was not found after recovery."
                )
                return False

            logger.success(
                f"RECOVERY SUCCESS: Transmittal "
                f"'{transmittal_no}' is available again."
            )

            return True

        except Exception as e:
            logger.error(
                f"RECOVERY FAILED for transmittal "
                f"'{transmittal_no}': {e}"
            )
            return False

    def process_transmittal(self, transmittal_no, idx, total):
        """
        Process ONE attempt for a single transmittal.

        Important for Step 3:
        This method does NOT retry recursively. If an exception occurs,
        it attempts recovery of the SAME transmittal and returns FAILED.
        run() owns the retry counter and decides whether to try again.
        """
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
            logger.info("Searching for transmittal...")

            row_count = 0
            search_attempt_limit = 3

            for search_attempt in range(1, search_attempt_limit + 1):
                try:
                    search_box = self.page.locator(
                        'input[type="text"]:not([disabled])'
                    ).first

                    search_box.wait_for(
                        state="visible",
                        timeout=8000
                    )

                    search_box.scroll_into_view_if_needed()

                    search_box.click()
                    self.page.wait_for_timeout(300)

                    search_box.press("Control+A")
                    self.page.wait_for_timeout(100)
                    search_box.press("Backspace")
                    self.page.wait_for_timeout(200)

                    search_box.fill(transmittal_no)
                    self.page.wait_for_timeout(300)
                    search_box.press("Enter")

                    self.page.wait_for_load_state("networkidle")
                    self.page.wait_for_timeout(500)

                    row_count = _wait_for_count(
                        self.page,
                        self.page.locator("tbody tr"),
                        min_count=1,
                        timeout_ms=5000,
                        poll_ms=400,
                    )

                    if row_count > 0:
                        logger.success(
                            f"Found {row_count} row(s) on attempt {search_attempt}"
                        )
                        break

                    logger.warning(
                        f"Search attempt {search_attempt}/{search_attempt_limit}: "
                        f"no rows found for '{transmittal_no}'"
                    )

                except Exception as e:
                    logger.warning(
                        f"Search attempt {search_attempt}/{search_attempt_limit} "
                        f"failed: {e}"
                    )

                if search_attempt < search_attempt_limit:
                    self.page.wait_for_timeout(1500)

            if row_count == 0:
                logger.error(f"TRANSMITTAL NOT FOUND: {transmittal_no}")

                result["status"] = "skipped"
                result["message"] = "Transmittal not found"

                return result

            logger.success(f"Transmittal '{transmittal_no}' found successfully")

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
            validate_btn = self.page.locator(
                "button",
                has_text="Validate Eligibility"
            )

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

            birth_date = datetime.strptime(
                birthday_text,
                "%B %d, %Y"
            ).date()

            today = datetime.today().date()

            age = today.year - birth_date.year - (
                (today.month, today.day) <
                (birth_date.month, birth_date.day)
            )

            self.patient_birthdate = birth_date
            self.patient_age = age

            logger.success(f"Patient Age = {self.patient_age}")

            # ── Get the Patient Name ─────────────────────────────────────
            patient_name = self.page.locator(
                "//td[normalize-space()='Patient Name']/following-sibling::td"
            ).inner_text().strip()

            logger.info(f"Patient Name: {patient_name}")

            self.patient_name = patient_name

            # ── Check CHARGES tab for existing items ────────────────────────
            logger.info("Checking CHARGES tab for existing items...")

            self.page.get_by_role(
                "link",
                name="CHARGES",
                exact=True
            ).click()

            self.page.wait_for_load_state("networkidle")

            try:
                charges_table = self.page.locator("tbody")
                charges_table.wait_for(
                    state="visible",
                    timeout=10000
                )
            except Exception:
                pass

            self.page.wait_for_timeout(2000)

            logger.info("Searching for existing MED charge items...")

            med_items = self.page.locator("tbody tr td[title^='MED']")

            existing_charges_count = 0
            visible_med_items = []

            try:
                raw_count = med_items.count()

                logger.info(f"Candidate MED elements found: {raw_count}")

                for i in range(raw_count):
                    try:
                        item = med_items.nth(i)

                        if not item.is_visible():
                            continue

                        title = (item.get_attribute("title") or "").strip()
                        text = (item.inner_text() or "").strip()

                        if not title and not text:
                            continue

                        existing_charges_count += 1
                        visible_med_items.append(title or text)

                        logger.info(
                            f"Verified existing charge: "
                            f"{title or text}"
                        )

                    except Exception:
                        pass

            except Exception as e:
                logger.warning(
                    f"Unable to inspect Charges table: {e}"
                )

            logger.info(
                f"Verified MED items: {existing_charges_count}"
            )

            soa_already_uploaded = existing_charges_count > 0

            if soa_already_uploaded:

                logger.warning(
                    f"Charges table already has "
                    f"{existing_charges_count} verified "
                    f"item(s). Skipping upload — proceeding to "
                    f"Statement of Account to re-verify discounts."
                )

                result["status"] = "success"

                result["message"] = (
                    f"Charges table already had "
                    f"{existing_charges_count} "
                    f"item(s) — SOA already uploaded. Re-verified "
                    f"discounts without re-uploading."
                )

            else:

                logger.success(
                    "Charges table is empty — proceeding with upload."
                )

            # ── Open PAYMENTS tab ─────────────────────────────────
            logger.info("Opening PAYMENTS tab...")

            self.page.get_by_role("link", name="PAYMENTS").click()

            self.page.wait_for_timeout(1000)

            logger.success("PAYMENTS tab opened")

            if not soa_already_uploaded:

                # ── Upload Statement of Account ───────────────────────────
                logger.info("Opening Statement of Account upload...")
                logger.info("Clicking Upload Charges and Payment...")

                self.page.get_by_role(
                    "button",
                    name="UPLOAD CHARGES AND PAYMENT"
                ).click()

                try:
                    self.page.wait_for_selector(
                        "input[type='file']",
                        state="attached",
                        timeout=10000
                    )
                except Exception:
                    pass

                logger.success("Upload Charges and Payment clicked")

                # ── Locate SOA file automatically ─────────────────────────
                surname_tokens, given_tokens = _split_surname_and_given(
                    self.patient_name
                )

                surname_key = _normalize("".join(surname_tokens))
                bare_surname_key = (
                    _normalize(surname_tokens[-1])
                    if surname_tokens else ""
                )
                given_key = _normalize("".join(given_tokens))
                given_initial = (
                    _normalize(given_tokens[0])[:1]
                    if given_tokens else ""
                )

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
                    """
                    if not tokens_to_match:
                        return []

                    tokens_to_match = [t.upper() for t in tokens_to_match]
                    matches = []

                    allowed_after = set(given_tokens)

                    if given_initial:
                        allowed_after.add(given_initial)

                    for f in all_files:
                        tokens = _filename_tokens(f.name)

                        for i in range(
                            len(tokens) - len(tokens_to_match) + 1
                        ):
                            if tokens[
                                i:i + len(tokens_to_match)
                            ] != tokens_to_match:
                                continue

                            if require_exact_length:
                                preceding = tokens[i - 1] if i > 0 else None

                                if preceding in SURNAME_PARTICLES:
                                    continue

                            after_idx = i + len(tokens_to_match)

                            following = (
                                tokens[after_idx]
                                if after_idx < len(tokens)
                                else None
                            )

                            if following is not None and not following.isdigit():
                                if following not in allowed_after:
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

                    for i in range(
                        len(tokens) - len(surname_tokens) + 1
                    ):

                        if tokens[
                            i:i + len(surname_tokens)
                        ] != surname_tokens:
                            continue

                        before = tokens[i - 1] if i > 0 else None

                        after = (
                            tokens[i + len(surname_tokens)]
                            if i + len(surname_tokens) < len(tokens)
                            else None
                        )

                        if given_tokens:
                            if (
                                before == given_tokens[0]
                                or after == given_tokens[0]
                            ):
                                return True

                        if given_initial:
                            if (
                                before == given_initial
                                or after == given_initial
                            ):
                                return True

                    return False

                # 1) Priority: exact-length surname match
                matches = _matching(
                    surname_tokens,
                    require_exact_length=True
                )

                # 2) Fallback: loose surname match
                if not matches:
                    loose_matches = _matching(surname_tokens)

                    if loose_matches:
                        logger.info(
                            f"No exact-length match for surname "
                            f"'{surname_key}' — falling back to loose match: "
                            f"{[f.name for f in loose_matches]}"
                        )

                    matches = loose_matches

                # 3) Fallback: surname without leading particle
                if not matches and bare_surname_key != surname_key:

                    logger.info(
                        f"No match for full surname '{surname_key}' — "
                        f"trying bare surname '{bare_surname_key}'"
                    )

                    matches = _matching(
                        [surname_tokens[-1]],
                        require_exact_length=True
                    )

                    if not matches:
                        matches = _matching(
                            [surname_tokens[-1]]
                        )

                if not matches:
                    raise Exception(
                        f"No SOA file found for patient '{self.patient_name}': "
                        f"no filename matched surname "
                        f"'{' '.join(surname_tokens)}' "
                        f"(or bare surname '{bare_surname_key}') "
                        f"inside {self.soa_folder}. Skipping — will not guess."
                    )

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
                            f"confirmed against given name "
                            f"'{ ' '.join(given_tokens) }'. "
                            "Skipping — will not guess which file belongs "
                            "to this patient."
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
                        f"'{ ' '.join(surname_tokens) }' — narrowed to a "
                        f"single exact match using given name"
                    )

                    matches = narrowed

                logger.info(
                    f"Candidate SOA files: {[f.name for f in matches]}"
                )

                soa_file = matches[0]

                self.soa_file = str(soa_file)

                logger.success(f"SOA file found: {self.soa_file}")

                # ── Upload without Windows File Dialog ────────────────────
                file_input = self.page.locator("input[type='file']")

                logger.info(
                    f"File inputs found: {file_input.count()}"
                )

                if file_input.count() > 0:

                    logger.info("Uploading SOA...")

                    file_input.set_input_files(self.soa_file)

                    try:
                        self.page.wait_for_load_state(
                            "networkidle",
                            timeout=20000
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
                            "networkidle",
                            timeout=20000
                        )
                    except Exception:
                        pass

                    self.page.wait_for_timeout(500)

                    logger.success("SOA uploaded successfully.")

                # ── Triple-save after upload (guard against Beacon instability) ──
                # Beacon can accept the file upload request and return networkidle
                # but silently not persist the charges to the claim record until a
                # Save is explicitly triggered. Without this save, the SOA modal
                # that opens next will be empty even though the upload appeared
                # successful. Three progressively-broader save clicks mirror the
                # same strategy used in cf2_automation._save_cf2(). Every click
                # is best-effort — a missing or flaky save button here must not
                # abort an otherwise-successful transmittal.

                logger.info(
                    "Saving after SOA upload (triple-save, best-effort)..."
                )

                # Click 1 — primary submit button (last button[type=submit] on page)
                try:
                    save_btn_1 = self.page.locator(
                        "button[type='submit']"
                    ).last
                    if save_btn_1.count() > 0:
                        save_btn_1.scroll_into_view_if_needed()
                        save_btn_1.click(force=True)
                        try:
                            self.page.wait_for_load_state(
                                "networkidle",
                                timeout=15000
                            )
                        except Exception:
                            pass
                        self.page.wait_for_timeout(1000)
                        logger.info("Save click 1 (submit button) done.")
                    else:
                        logger.warning(
                            "Save click 1: no button[type=submit] found — skipping."
                        )
                except Exception as e:
                    logger.warning(
                        f"Save click 1 (submit button) failed, continuing: {e}"
                    )

                # Click 2 — explicit SAVE label (catches cases where button
                # order shifts or the submit attribute is missing)
                try:
                    save_btn_2 = self.page.get_by_role(
                        "button", name="SAVE", exact=True
                    )
                    if save_btn_2.count() > 0:
                        save_btn_2.last.scroll_into_view_if_needed()
                        save_btn_2.last.click(force=True)
                        try:
                            self.page.wait_for_load_state(
                                "networkidle",
                                timeout=15000
                            )
                        except Exception:
                            pass
                        self.page.wait_for_timeout(1000)
                        logger.info("Save click 2 (SAVE label) done.")
                    else:
                        logger.warning(
                            "Save click 2: no button named 'SAVE' found — skipping."
                        )
                except Exception as e:
                    logger.warning(
                        f"Save click 2 (SAVE label) failed, continuing: {e}"
                    )

                # Click 3 — JS fallback: bypasses Playwright actionability checks
                # entirely in case the button is blocked by an overlay/animation.
                try:
                    self.page.evaluate(
                        """() => {
                            const btns = [...document.querySelectorAll('button')];
                            const save = btns.find(
                                b => b.textContent.trim().toUpperCase() === 'SAVE'
                            );
                            if (save) save.click();
                        }"""
                    )
                    self.page.wait_for_timeout(1500)
                    logger.info("Save click 3 (JS evaluate fallback) done.")
                except Exception as e:
                    logger.warning(
                        f"Save click 3 (JS fallback) failed, continuing: {e}"
                    )

                logger.success(
                    "Triple-save after upload complete — proceeding to "
                    "Statement of Account."
                )

            # ── Open Statement of Account ───────────────────────────────
            logger.info("Opening Statement of Account...")

            soa_button = self.page.locator(
                "button:has(span:text('Statement of Account'))"
            )

            soa_button.wait_for(
                state="visible",
                timeout=20000
            )

            soa_button.click()

            self.page.wait_for_timeout(1000)

            logger.success("Statement of Account opened")

            # ============================================================
            # Populate Senior Citizen / PWD Discount
            # ============================================================
            logger.info("Computing discounts...")

            is_senior = self.patient_age >= 60

            target_prefix = (
                "seniorCitizenDiscount"
                if is_senior
                else "pwdDiscount"
            )

            logger.info(
                f"Using {'Senior' if is_senior else 'PWD'} Discount"
            )

            actual_inputs = self.page.locator(
                "input[id^='actualCharges']:disabled"
            )

            discount_inputs = self.page.locator(
                f"input[id^='{target_prefix}']"
            )

            actual_count = _wait_for_count(
                self.page,
                actual_inputs,
                min_count=1,
                timeout_ms=15000,
                poll_ms=300
            )

            if actual_count == 0:
                raise Exception(
                    "Statement of Account modal did not populate any "
                    "Actual Charges rows in time — cannot compute "
                    "discounts. The modal may still be loading, or the "
                    "upload didn't parse as expected."
                )

            summary_rows = min(
                6,
                actual_count,
                discount_inputs.count()
            )

            logger.info(
                f"Processing {summary_rows} Summary rows"
            )

            discount_targets = []

            for row in range(summary_rows):

                try:
                    actual = actual_inputs.nth(row)
                    target = discount_inputs.nth(row)

                    value = actual.input_value().strip()

                    logger.info(
                        f"Row {row}: actualCharges = '{value}'"
                    )

                    if not value:
                        continue

                    amount = float(
                        value.replace(",", "")
                    )

                    if amount == 0:
                        logger.info(
                            f"Row {row}: skipped (0)"
                        )
                        continue

                    discount = round(
                        amount * 0.20,
                        2
                    )

                    _type_into_number_field(
                        self.page,
                        target,
                        discount
                    )

                    discount_targets.append(
                        (
                            target,
                            f"{discount:.2f}"
                        )
                    )

                    logger.info(
                        f"Row {row}: {amount} -> {discount:.2f}"
                    )

                except Exception as e:
                    logger.warning(
                        f"Row {row}: {e}"
                    )

            logger.success(
                "Discount computation completed."
            )

            # ==========================================================
            # Populate Professional Fees
            # ==========================================================
            logger.info(
                "Computing Professional Fees..."
            )

            summary_total = sum(
                float(
                    actual_inputs.nth(i)
                    .input_value()
                    .replace(",", "")
                )
                for i in range(summary_rows)
            )

            logger.info(
                f"Summary Total = {summary_total}"
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

            pf_actual = pf_actual_map.get(
                summary_total
            )

            if pf_actual is None:

                logger.warning(
                    f"No Professional Fee mapping for "
                    f"{summary_total}"
                )

            else:

                pf_discount = round(
                    pf_actual * 0.20,
                    2
                )

                logger.info(
                    f"Professional Actual = {pf_actual}"
                )

                logger.info(
                    f"Professional Discount = {pf_discount}"
                )

                actual_selector = "input#actualCharges0"
                discount_selector = (
                    "input#seniorCitizenDiscount0"
                    if is_senior
                    else "input#pwdDiscount0"
                )

                pf_actual_input = self.page.locator(
                    actual_selector
                ).nth(1)

                pf_discount_input = self.page.locator(
                    discount_selector
                ).nth(1)

                # Guard: Beacon only renders a Professional Fees ROW
                # (and therefore these input elements) at all when a
                # doctor has been added to the claim. With no doctor,
                # the Professional Fees table has zero data rows —
                # not a disabled row, an ABSENT one (confirmed via
                # screenshot: header row, then straight to "Grand
                # Summaries 0.00", nothing in between).
                #
                # Both `actual_selector` and `discount_selector` are
                # reused by Beacon across the Summary-of-Fees table
                # AND the Professional-Fees table — normally there
                # are 2 matches on the page and `.nth(1)` picks the
                # professional-fees one. When no doctor is added, only
                # the Summary-of-Fees match exists, so `.nth(1)`
                # resolves to zero elements.
                #
                # Calling `.is_disabled()` on a locator with zero
                # matches doesn't return False — it raises, since
                # there's nothing to check the state of. That's why
                # checking is_disabled() alone silently failed to
                # catch this case: the field isn't disabled, it never
                # exists in the first place.
                #
                # Before any check at all, this surfaced as:
                # _type_into_number_field() retrying 3 times against a
                # nonexistent element, falling back to locator.fill()
                # (which requires an actionable element and has no
                # force option), raising, bubbling up to the
                # transmittal-level except block, getting marked
                # "failed", and queuing a retry that could never
                # succeed — retrying doesn't add a doctor.
                #
                # Fix: check count() for both selectors FIRST. Only
                # fall back to is_disabled() when the elements
                # genuinely exist (covers a doctor row being present
                # but Beacon still disabling it for some other
                # reason).
                pf_row_missing = (
                    self.page.locator(actual_selector).count() < 2
                    or self.page.locator(discount_selector).count() < 2
                )

                pf_disabled = (
                    pf_row_missing
                    or pf_actual_input.is_disabled()
                    or pf_discount_input.is_disabled()
                )

                if pf_disabled:
                    logger.error(
                        "No professional fee to map, add a doctor."
                    )
                else:
                    _type_into_number_field(
                        self.page,
                        pf_actual_input,
                        pf_actual
                    )

                    _type_into_number_field(
                        self.page,
                        pf_discount_input,
                        pf_discount
                    )

                    discount_targets.append(
                        (
                            pf_discount_input,
                            f"{pf_discount:.2f}"
                        )
                    )

                    logger.success(
                        "Professional Fees populated."
                    )

            # ==========================================================
            # Re-verify discount fields before saving
            # ==========================================================
            if discount_targets:

                logger.info(
                    "Re-verifying discount fields before saving..."
                )

                for verify_pass in range(1, 4):

                    reverted = []

                    for target_locator, expected in discount_targets:

                        try:
                            current = (
                                target_locator
                                .input_value()
                                .strip()
                            )
                        except Exception:
                            current = None

                        if current != expected:
                            reverted.append(
                                (
                                    target_locator,
                                    expected,
                                    current
                                )
                            )

                    if not reverted:

                        logger.success(
                            "All discount fields verified correct."
                        )

                        break

                    logger.warning(
                        f"Verification pass "
                        f"{verify_pass}/3: "
                        f"{len(reverted)} field(s) reverted — "
                        f"retyping "
                        + ", ".join(
                            f"(expected {exp}, got {cur})"
                            for _, exp, cur in reverted
                        )
                    )

                    for target_locator, expected, _ in reverted:

                        _type_into_number_field(
                            self.page,
                            target_locator,
                            float(expected)
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
            logger.info(
                "Saving Statement of Account..."
            )

            save_btn = self.page.locator(
                "button[type='submit']"
            ).last

            save_btn.scroll_into_view_if_needed()
            save_btn.click(force=True)

            logger.info(
                "Waiting for save to complete..."
            )

            self.page.wait_for_load_state(
                "networkidle"
            )

            self.page.wait_for_timeout(3000)

            logger.success(
                "Statement of Account saved successfully."
            )

            close_btn = self.page.get_by_role(
                "button",
                name="CLOSE"
            )

            close_btn.scroll_into_view_if_needed()
            close_btn.click(force=True)

            is_last = (idx == total - 1)

            if is_last:
                logger.info(
                    "No more transmittals to process — "
                    "skipping return to list."
                )
            else:
                open_transmittals(self.page)

        except Exception as e:

            logger.error(
                f"\nERROR on transmittal "
                f"{idx + 1} ({transmittal_no}): {e}"
            )

            result["status"] = "failed"
            result["message"] = summarize_error(str(e))

            # ---------------------------------------------------------
            # RECOVERY
            # ---------------------------------------------------------
            # Recover the SAME transmittal. The run() method owns the
            # retry counter and will decide whether to retry this
            # transmittal or move to the next one.
            logger.warning(
                f"Attempting recovery for transmittal "
                f"'{transmittal_no}'..."
            )

            recovery_success = False

            try:

                self._cancel_upload_confirmation()

                recovery_success = self._recover_transmittal(
                    transmittal_no
                )

            except Exception as recovery_error:

                logger.error(
                    f"Recovery raised an unexpected error: "
                    f"{recovery_error}"
                )

            if recovery_success:

                logger.success(
                    f"Transmittal '{transmittal_no}' has been "
                    f"recovered and is ready for retry."
                )

            else:

                logger.error(
                    f"Unable to recover transmittal "
                    f"'{transmittal_no}'."
                )

            # IMPORTANT:
            # Do not move to the next transmittal here.
            # run() controls the retry count.

        return result

    def run(self, transmittals):
        """
        Main entry point for SOA upload automation.

        Step 3 retry behavior:
            Attempt 1
                ↓ failure
            Recover SAME transmittal
                ↓
            Retry 1
                ↓ failure
            Recover SAME transmittal
                ↓
            Retry 2
                ↓ failure
            Retry limit reached
                ↓
            Move to next transmittal

        max_retries is the number of retries AFTER the initial attempt.
        Therefore max_retries=2 means 3 attempts total.
        """
        try:
            report.results.clear()
            self.results.clear()

            self.page = browser_session.connect()

            # ── Navigate to Transmittals ───────────────────────────────
            logger.info("Opening Transmittals...")
            open_transmittals(self.page)

            # ── Transmittal loop with controlled retries ───────────────
            for idx, transmittal_no in enumerate(transmittals):

                transmittal_no = str(transmittal_no).strip()

                retry_count = 0
                max_attempts = self.max_retries + 1

                while True:

                    attempt_number = retry_count + 1

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
                        len(transmittals)
                    )

                    # -------------------------------------------------
                    # SUCCESS
                    # -------------------------------------------------
                    if result["status"] == "success":

                        self.results.append(result)

                        logger.success(
                            f"Transmittal '{transmittal_no}' "
                            f"completed successfully on attempt "
                            f"{attempt_number}/{max_attempts}."
                        )

                        break

                    # -------------------------------------------------
                    # SKIPPED
                    # -------------------------------------------------
                    if result["status"] == "skipped":

                        self.results.append(result)

                        logger.warning(
                            f"Transmittal '{transmittal_no}' "
                            f"was skipped."
                        )

                        # A skipped transmittal may already have returned
                        # to the list. Ensure the next transmittal starts
                        # from the list page.
                        if idx < len(transmittals) - 1:

                            try:
                                open_transmittals(
                                    self.page
                                )

                            except Exception as navigation_error:

                                logger.warning(
                                    f"Could not return to transmittal "
                                    f"list: {navigation_error}"
                                )

                        break

                    # -------------------------------------------------
                    # FAILED
                    # -------------------------------------------------
                    retry_count += 1

                    # If retry_count is now greater than max_retries,
                    # the initial attempt plus all allowed retries
                    # have been exhausted.
                    if retry_count > self.max_retries:

                        self.results.append(result)

                        logger.error(
                            f"Transmittal '{transmittal_no}' "
                            f"FAILED after {max_attempts} attempt(s). "
                            f"Retry limit reached — moving to next "
                            f"transmittal."
                        )

                        if idx < len(transmittals) - 1:

                            try:
                                open_transmittals(
                                    self.page
                                )

                            except Exception as navigation_error:

                                logger.warning(
                                    f"Could not return to transmittal "
                                    f"list after final failure: "
                                    f"{navigation_error}"
                                )

                        break

                    # -------------------------------------------------
                    # RETRY SAME TRANSMITTAL
                    # -------------------------------------------------
                    logger.warning(
                        f"Transmittal '{transmittal_no}' failed."
                    )

                    logger.warning(
                        f"Retrying SAME transmittal "
                        f"({retry_count}/{self.max_retries})..."
                    )

                    # process_transmittal() already attempted recovery
                    # of the SAME transmittal in its exception handler.
                    #
                    # If recovery succeeded, the next loop iteration
                    # searches/processes the same transmittal again.
                    #
                    # If recovery failed, we still retry only because
                    # the retry count is controlled. The next attempt
                    # will fail/recover again rather than getting stuck
                    # in an infinite loop.

            logger.success("=" * 60)
            logger.success(
                "SOA UPLOAD AUTOMATION COMPLETED"
            )
            logger.success("=" * 60)

            # ── Per-transmittal breakdown ──────────────────────────────
            logger.info("")
            logger.info(
                "RESULTS BREAKDOWN:"
            )
            logger.info("-" * 60)

            success_count = sum(
                1
                for r in self.results
                if r["status"] == "success"
            )

            failed_count = sum(
                1
                for r in self.results
                if r["status"] == "failed"
            )

            skipped_count = sum(
                1
                for r in self.results
                if r["status"] == "skipped"
            )

            for r in self.results:

                line = (
                    f"{r['transmittal']}: "
                    f"{r['status'].upper()} - "
                    f"{r['message']}"
                )

                if r["status"] == "success":

                    logger.success(
                        f"[SUCCESS] {line}"
                    )

                elif r["status"] == "skipped":

                    logger.warning(
                        f"[SKIPPED] {line}"
                    )

                else:

                    logger.error(
                        f"[FAILED] {line}"
                    )

            logger.info("-" * 60)

            logger.info(
                f"Total: {len(self.results)} | "
                f"Success: {success_count} | "
                f"Failed: {failed_count} | "
                f"Skipped: {skipped_count}"
            )

            logger.info(
                "No more transmittals to process. "
                "Closing browser..."
            )

            self.close()

            return True

        except Exception as e:

            logger.error(
                f"Fatal error in SOA automation: {e}"
            )

            self.close()

            return False

    def get_results(self):
        """Returns the list of final per-transmittal results."""
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

                    logger.warning(
                        f"Error closing context: {e}"
                    )

                if browser:

                    try:
                        browser.close()

                    except Exception as e:

                        logger.warning(
                            f"Error closing browser: {e}"
                        )

                logger.info(
                    "Browser closed."
                )

            self.page = None

        except Exception as e:

            logger.warning(
                f"Error during close(): {e}"
            )