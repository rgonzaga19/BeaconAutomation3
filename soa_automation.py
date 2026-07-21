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
NAME_SUFFIXES = {"JR", "SR", "II", "III", "IV"}


def _strip_name_suffixes(tokens):
    """
    Removes generational suffix tokens like "JR"/"SR"/"II"/"III"/"IV"
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


def _type_into_number_field(page, locator, value, max_attempts=3):
    """
    Reliably enters a value into one of Beacon's Summary/Professional Fees
    number-spinner inputs.

    These are React-controlled fields — clicking one and typing right
    after can outrun the field actually becoming interactive, which
    silently drops the first keystrokes (e.g. typing "1540" lands as just
    "40" because the field wasn't ready to accept input yet). This gives
    the field a short moment to settle after the click, then reads back
    the value to confirm it actually landed, retrying if it didn't.
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

        actual = locator.input_value().strip()
        if actual == target_value:
            return

        logger.warning(
            f"Attempt {attempt}/{max_attempts}: expected '{target_value}', "
            f"got '{actual}' — retrying"
        )

    # Last resort: set the value directly instead of via keystrokes.
    locator.click(force=True)
    locator.fill(target_value)
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
            search_box = self.page.locator('input[type="text"]').first
            search_box.click()
            search_box.press("Control+A")
            search_box.press("Backspace")
            search_box.fill(transmittal_no)
            search_box.press("Enter")
            self.page.wait_for_load_state("networkidle")
            self.page.wait_for_timeout(2000)

            if self.page.locator("tbody tr").count() == 0:
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

            self.page.wait_for_timeout(1000)

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

            def _matching(key):
                if not key:
                    return []
                return [f for f in all_files if key in _normalize(f.name)]

            def _has_given_name_marker(filename_norm):
                """
                True if the given name (in full, or just its first letter)
                sits immediately next to the surname in the normalized
                filename — covers "DEOCAMPOJ.xlsx", "DEOCAMPO_JUAN.xlsx",
                "JUAN_DEOCAMPO.xlsx", etc.
                """
                idx = filename_norm.find(surname_key)
                if idx == -1:
                    return False

                after = filename_norm[idx + len(surname_key):]
                before = filename_norm[:idx]

                if given_key and (after.startswith(given_key) or before.endswith(given_key)):
                    return True
                if given_initial and (after.startswith(given_initial) or before.endswith(given_initial)):
                    return True
                return False

            # 1) Priority: full surname match (compound-aware, e.g. "DEOCAMPO").
            matches = _matching(surname_key)

            # 2) Fallback: surname without the leading particle, in case the
            #    filename dropped "DE"/"DEL"/etc. (e.g. just "OCAMPO.xlsx").
            if not matches and bare_surname_key != surname_key:
                logger.info(
                    f"No match for full surname '{surname_key}' — "
                    f"trying bare surname '{bare_surname_key}'"
                )
                matches = _matching(bare_surname_key)

            # 3) Last resort: no surname match at all — search by given name.
            if not matches:
                logger.warning(
                    f"No surname match for '{' '.join(surname_tokens)}' — "
                    "falling back to first-name search"
                )
                matches = _matching(given_key)
                if not matches and given_initial:
                    matches = _matching(given_initial)

            if not matches:
                raise Exception(
                    f"No SOA file found for patient '{self.patient_name}' "
                    f"inside {self.soa_folder}"
                )

            # If several files share the same surname (e.g. two patients with
            # the same last name), narrow down using the given name.
            if len(matches) > 1:
                narrowed = [
                    f for f in matches
                    if _has_given_name_marker(_normalize(f.name))
                ]

                if narrowed:
                    logger.info(
                        f"Multiple files matched surname "
                        f"'{' '.join(surname_tokens)}' — narrowed to "
                        f"{len(narrowed)} using given name"
                    )
                    matches = narrowed
                else:
                    logger.warning(
                        f"Multiple files matched surname "
                        f"'{' '.join(surname_tokens)}' and none could be "
                        "narrowed by given name — using most recent"
                    )

            logger.info(f"Candidate SOA files: {[f.name for f in matches]}")

            # Pick the newest matching file
            soa_file = max(matches, key=lambda f: f.stat().st_mtime)

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

                self.page.wait_for_timeout(1000)

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

                self.page.wait_for_timeout(1000)

                logger.success("SOA uploaded successfully.")

            
            # ── Open Statement of Account ───────────────────────────────

            logger.info("Opening Statement of Account...")

            self.page.locator(
                "button:has(span:text('Statement of Account'))"
            ).click()

            self.page.wait_for_timeout(1000)

            logger.success("Statement of Account opened")

            # ============================================================
            # Populate Senior Citizen / PWD Discount
            # ============================================================

            logger.info("Computing discounts...")

            is_senior = self.patient_age >= 60
            target_prefix = "seniorCitizenDiscount" if is_senior else "pwdDiscount"

            logger.info(f"Using {'Senior' if is_senior else 'PWD'} Discount")

            # Summary of Fees only (first 6 rows)
            actual_inputs = self.page.locator("input[id^='actualCharges']").filter(
                has_not=self.page.locator(":disabled").locator("xpath=..")
            )

            # Get all disabled Actual Charges (Summary section)
            actual_inputs = self.page.locator("input[id^='actualCharges']:disabled")

            discount_inputs = self.page.locator(f"input[id^='{target_prefix}']")

            summary_rows = min(6, actual_inputs.count(), discount_inputs.count())

            logger.info(f"Processing {summary_rows} Summary rows")

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

                logger.success("Professional Fees populated.")

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