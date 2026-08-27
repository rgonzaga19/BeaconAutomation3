import os
import re
import sys
from datetime import datetime
import openpyxl
from cf2_mapper import build_cf2_data
import browser_session
from beacon import open_transmittals
from cf2_fees import get_fees
from draft_automation import (
    run_create_draft_flow,
    try_extract_transmittal_number,
    click_row_menu,
    InvalidMemberPinError,
)
from draft_title import build_draft_title
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

# Guard against print() crashing an otherwise-successful patient run.
# When this runs under a packaged .exe, stdout/stderr can be attached to a
# legacy Windows console codepage (cp1252, cp437, etc.) instead of UTF-8.
# Any print() containing a character outside that codepage (e.g. "✓", "—")
# raises UnicodeEncodeError ('charmap' codec can't encode character ...),
# which — since it's unhandled at the point of the print() call itself —
# aborts the whole patient rather than just producing a garbled log line.
# Setting errors="replace" keeps each stream's existing encoding but swaps
# unencodable characters for a placeholder instead of raising.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass




def resource_path(relative_path):
    """
    Returns the correct path both for development
    and for the packaged PyInstaller executable.
    """
    try:
        base_path = sys._MEIPASS
    except AttributeError:
        base_path = os.path.abspath(".")

    return os.path.join(base_path, relative_path)


CF2_TEMPLATE_PATH = resource_path(
    os.path.join(
        "templates",
        "CF2_Template.xlsx"
    )
)


class TransmittalNotFoundError(Exception):
    """
    Raised when mode="existing_draft" and the transmittal number from the
    uploaded workbook doesn't match anything in Beacon's Transmittals
    list.

    Distinct from a bare Exception for the same reason InvalidMemberPinError
    is (see its docstring): process_patient() needs to tell "this row's
    transmittal number is wrong/doesn't exist yet, skip it and move on"
    apart from "an unexpected UI/automation error happened, mark it
    failed". _open_and_search_transmittal() doesn't leave any dialog open
    on a no-match search — the page is already back on a clean
    Transmittals list — so no separate recovery step is needed here the
    way InvalidMemberPinError's does.
    """
    pass


class CF2Automation:

    def __init__(self, uploaded_excel_path=None, mode="new_draft"):
        self.page = browser_session.connect()
        # Path to the workbook the user actually uploaded. Sheet2 (Billing
        # Clerk / Accountant name, contact no., official capacity) must be
        # read from THIS file, not from the bundled CF2_TEMPLATE_PATH.
        self.uploaded_excel_path = uploaded_excel_path or CF2_TEMPLATE_PATH
        # "new_draft" (default): fill_cf2() creates a brand-new Beacon
        # draft via Create Draft + Add Claims, using each row's Member
        # PIN — the original behavior, unchanged.
        # "existing_draft": fill_cf2() skips draft creation entirely and
        # instead searches the Transmittals list for each row's
        # transmittal number (already created in Beacon some other way),
        # then continues into the exact same CF2 field-filling steps.
        self.mode = mode
        # Every processed patient gets one entry here:
        # {"transmittal": ..., "patient_name": ..., "status": "success"/"skipped"/"failed", "message": ...}
        self.results = []

    # ------------------------------------------------------------------
    # Public entry point — never lets a single patient crash the batch
    # ------------------------------------------------------------------
    def process_patient(self, record):
        result = {
            "transmittal": getattr(record, "transmittal", "?"),
            "patient_name": getattr(record, "patient_name", "?"),
            "status": "failed",
            "message": "",
        }

        try:
            data = build_cf2_data(record)
        except Exception as e:
            result["message"] = f"Could not build CF2 data: {e}"
            print(f"ERROR: {result['message']}")
            self.results.append(result)
            return result

        result["transmittal"] = data.transmittal
        result["patient_name"] = data.patient_name

        print("=" * 50)
        print(f"PROCESSING PATIENT: {data.patient_name}  (Transmittal: {data.transmittal})")
        print("=" * 50)

        try:
            status, message = self.fill_cf2(data)
            result["status"] = status
            result["message"] = message
            # fill_cf2 overwrites data.transmittal with the auto-generated
            # number once the draft is created, so re-read it here.
            result["transmittal"] = data.transmittal
        except (InvalidMemberPinError, TransmittalNotFoundError) as e:
            # Either the PIN was bad (new_draft mode) or the transmittal
            # number didn't match anything (existing_draft mode) — not an
            # automation glitch either way. Safe to record this row as
            # skipped and move on to the next one rather than treating it
            # as a hard failure.
            result["status"] = "skipped"
            result["message"] = str(e)
            print(f"SKIPPED: {data.patient_name} — {e} — moving to next row.")
        except Exception as e:
            result["status"] = "failed"
            result["message"] = f"Unhandled error: {e}"
            print(f"ERROR: Unhandled exception for {data.patient_name}: {e}")

        self.results.append(result)
        return result

    def get_summary(self):
        """Returns the list of per-patient results collected so far."""
        return self.results

    # ------------------------------------------------------------------
    # Teardown — call this once after all patients are processed
    # ------------------------------------------------------------------
    def close(self):
        """Persist the session (in case tokens rotated during the run) and
        tear down the browser. Call this once, after the caller's loop over
        process_patient() finishes — ideally from a try/finally so it still
        runs if a patient-level error escapes process_patient()."""
        try:
            browser_session.save_session()
        except Exception as e:
            print(f"WARNING: Could not save session: {e}")

        browser_session.disconnect()

    # ------------------------------------------------------------------
    # Excel lookup — "Prepared by" (Billing Clerk / Accountant) name
    # ------------------------------------------------------------------
    def _get_billing_clerk_name(self):
        """Reads the Billing Clerk / Accountant name from Sheet2!A1 of the
        UPLOADED workbook (falls back to CF2_TEMPLATE_PATH only if no file
        was uploaded)."""
        wb = openpyxl.load_workbook(self.uploaded_excel_path, data_only=True)
        sheet2 = wb["Sheet2"]
        value = sheet2["A1"].value
        wb.close()
        return str(value).strip() if value is not None else ""

    def _get_billing_clerk_cp(self):
        wb = openpyxl.load_workbook(self.uploaded_excel_path, data_only=True)
        sheet2 = wb["Sheet2"]
        value = sheet2["A2"].value
        wb.close()
        return str(value).strip() if value else ""

    def _get_official_capacity_designation(self):
        """Reads the Official Capacity/Designation from Sheet2!B1 of the
        UPLOADED workbook (falls back to CF2_TEMPLATE_PATH only if no file
        was uploaded)."""
        wb = openpyxl.load_workbook(self.uploaded_excel_path, data_only=True)
        sheet2 = wb["Sheet2"]
        value = sheet2["B1"].value
        wb.close()
        return str(value).strip() if value is not None else ""

    # ------------------------------------------------------------------
    # Step runner — every UI action goes through this
    # ------------------------------------------------------------------
    def _step(self, description, func, critical=True):
        """
        Runs a single automation step.
        - critical=True : failure aborts the current patient (raises up to
          process_patient, which marks the patient as failed and moves on
          to the next one).
        - critical=False: failure is logged as a warning and swallowed so
          the rest of the CF2 form can still be attempted.
        """
        try:
            print(description)
            func()
            return True
        except Exception as e:
            print(f"WARNING: Step failed [{description}]: {e}")
            if critical:
                raise
            return False

    # ------------------------------------------------------------------
    # Main flow
    # ------------------------------------------------------------------
    def fill_cf2(self, data):
        page = self.page

        if self.mode == "existing_draft":
            self._locate_existing_draft(page, data)
        else:
            self._create_draft(page, data)

        # Either path leaves the browser sitting directly on the PHIC
        # Claims Details page itself (CF1/CF2 tabs, admission/discharge
        # dates pre-filled) — confirmed via a live test for new_draft,
        # and _open_patient()'s "Manage" click lands on the same page
        # for existing_draft. Everything below is identical regardless
        # of which path got here.

        self._step(
            "Checking for Validate Eligibility button...",
            lambda: self._validate_eligibility(page),
            critical=False,
        )

        self._step(
            "Checking Patient Type dropdown...",
            lambda: self._check_and_select_patient_type(page),
            critical=False,
        )

        self._fill_referral_and_accommodation(page)
        self._fill_disposition_and_diagnosis(page)
        self._add_discharge_diagnosis(page)

        self._step(
            "Setting discharge diagnosis as Primary...",
            lambda: self._set_primary_diagnosis(page),
            critical=False,
        )

        self._add_surgical_procedure(page, data)
        self._fill_session_dates(page, data)

        self._step(
            "Tagging Surgical Procedure as 1st Case Rate...",
            lambda: self._tag_first_case(page),
            critical=False,
        )

        self._add_doctor(page, data)
        self._fill_benefits_and_fees(page, data)
        self._fill_access_patient_records_date(page, data)
        self._save_cf2(page)
        self._fill_statement_of_account(page, data)

        print("SUCCESS: CF2 completed for this patient.")
        return "success", "CF2 completed successfully."

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------
    def _create_draft(self, page, data):
        """
        Creates the Beacon transmittal (Create Draft) and runs Add Claims
        for this patient's Member PIN, admission date (= first treatment
        date) and discharge date (= last treatment date), with an
        auto-generated draft title. Once this finishes, Beacon leaves the
        browser sitting directly on the PHIC Claims Details page (CF1/CF2
        tabs, admission/discharge dates pre-filled) — confirmed via a live
        test run — so fill_cf2() continues straight into the CF2 fields
        below it, without needing any of the three legacy navigation
        methods further down (_open_and_search_transmittal,
        _open_manage_claims, _open_patient).
        """
        admission_date = data.first_treatment.strftime("%m/%d/%Y")
        discharge_date = data.last_treatment.strftime("%m/%d/%Y")
        draft_title = build_draft_title(data.patient_name, data.first_treatment, data.last_treatment)

        def _run():
            run_create_draft_flow(page, data.member_pin, admission_date, discharge_date, draft_title)

        self._step(
            f"Creating draft + Add Claims (PIN: {data.member_pin}, Title: {draft_title})...",
            _run,
            critical=True,
        )

        # Best-effort only — logging/results use this, nothing downstream
        # depends on it being exact. See draft_automation.try_extract_transmittal_number.
        data.transmittal = try_extract_transmittal_number(page)
        print(f"Draft created. Transmittal number: {data.transmittal}")

    # ------------------------------------------------------------------
    # Existing-draft flow — used when self.mode == "existing_draft".
    # Skips Create Draft + Add Claims entirely and instead locates a
    # transmittal that already exists in Beacon by number, then hands
    # off to the exact same CF2 field-filling steps fill_cf2() runs for
    # a freshly-created draft. One transmittal maps to exactly one
    # patient, so grabbing the first (only) row at each step is correct.
    # ------------------------------------------------------------------
    def _locate_existing_draft(self, page, data):
        def _run():
            row = self._open_and_search_transmittal(page, data)
            if row is None:
                raise TransmittalNotFoundError(
                    f"Transmittal not found: {data.transmittal}"
                )
            self._open_manage_claims(page, row)
            self._open_patient(page)

        self._step(
            f"Locating existing draft (Transmittal: {data.transmittal})...",
            _run,
            critical=True,
        )
        print(f"Located existing draft. Transmittal number: {data.transmittal}")

    def _open_and_search_transmittal(self, page, data, attempts=3):
        """Searches the Transmittals list for data.transmittal. Returns
        the matching row's Locator on success, or None if nothing
        matched (the page is left on a clean, empty-results Transmittals
        list either way — no cleanup needed before the next patient).

        Same intermittent-false-"not found" issue beacon.py's
        _search_transmittal() root-caused, and the same fix applied here:
        the Transmittals table is client-rendered, so a fixed
        networkidle+sleep can read the row count at the exact instant the
        table is still empty or still showing the *previous* search's row
        — making a transmittal that's really there look "not found" (and,
        with a stale row, look like a match for the wrong patient).

        Instead of trusting a bare row count right after a fixed pause,
        this polls the DOM until matching rows appear or Beacon explicitly
        reports no results, then verifies the first row's text actually
        contains the transmittal number before accepting it. If a
        stale/mismatched row is caught, it retries the search rather than
        giving up (or handing back the wrong row) immediately.
        """
        print("Opening Transmittals...")
        open_transmittals(page)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeoutError:
            print("WARNING: networkidle wait timed out — continuing anyway")

        search_box = page.locator('input[type="text"]').first

        for attempt in range(1, attempts + 1):
            print(f"Searching: {data.transmittal} (attempt {attempt}/{attempts})")
            search_box.click()
            search_box.press("Control+A")
            search_box.press("Backspace")
            search_box.fill(data.transmittal)
            search_box.press("Enter")

            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeoutError:
                print("WARNING: networkidle wait timed out — continuing anyway")

            # Poll instead of a fixed sleep: wait until rows show up or
            # Beacon explicitly says there's nothing, whichever comes first.
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
                first_row = page.locator("tbody tr").first
                first_row_text = first_row.inner_text()

                if data.transmittal in first_row_text:
                    return first_row

                if attempt < attempts:
                    print(
                        f"WARNING: Table showed a row not matching "
                        f"{data.transmittal} (likely stale from a previous "
                        f"search) on attempt {attempt}/{attempts}; retrying..."
                    )
                    page.wait_for_timeout(1000)
                    continue

                # Last attempt: trust the row count even if the exact
                # string match failed (e.g. formatting differences), same
                # as the original behavior.
                print("Transmittal row found (row count only, no exact text match).")
                return first_row

            if attempt < attempts:
                print(
                    f"WARNING: No rows found for transmittal "
                    f"{data.transmittal} on attempt {attempt}/{attempts}; "
                    f"retrying search..."
                )
                page.wait_for_timeout(1500)

        print("Transmittal not found.")
        return None

    def _open_manage_claims(self, page, row):
        def _open():
            # Same proven multi-strategy menu click run_create_draft_flow()
            # uses to open a transmittal row's action menu — its success
            # check (does "Manage Claims" become visible) matches this
            # context exactly, since it's the same Transmittals-list row
            # menu either way.
            click_row_menu(page, row)
            page.get_by_text("Manage Claims", exact=True).click()
            page.wait_for_load_state("networkidle")

        self._step("Opening Manage Claims...", _open, critical=True)
        page.wait_for_timeout(1500)

    def _open_patient(self, page):
        def _open():
            claim_row = page.locator("tbody tr").first
            claim_row.locator("button").last.click()
            page.wait_for_timeout(500)
            page.get_by_text("Manage", exact=True).click()
            page.wait_for_load_state("networkidle")

        self._step("Opening PHIC Claim Details...", _open, critical=True)
        page.wait_for_timeout(1500)
        print("SUCCESS: PHIC Claim Details opened.")

    def _validate_eligibility(self, page):
        validate_btn = page.locator("button", has_text="Validate Eligibility")
        if validate_btn.count() > 0:
            validate_btn.first.click()
            page.wait_for_load_state("networkidle")
        else:
            print("No validation required - skipping.")

    def _check_and_select_patient_type(self, page):
        """
        Checks if the Patient Type dropdown field is enabled.
        It is usually disabled, but if enabled, selects 'O - Outpatient'.
        If disabled or not present, ignores and proceeds.
        """
        dropdown = page.locator('div[id^="patientTypeCode-"]').first
        if dropdown.count() == 0:
            print("Patient Type field not found - skipping.")
            return

        is_disabled = dropdown.evaluate(
            """(el) => {
                if (el.hasAttribute('disabled')) return true;
                if (el.getAttribute('aria-disabled') === 'true') return true;
                if (el.disabled === true) return true;
                const notAllowed = el.querySelector('div[style*="cursor: not-allowed"]');
                if (notAllowed) return true;
                const btn = el.querySelector('button');
                if (btn && (btn.disabled || btn.hasAttribute('disabled') || btn.getAttribute('aria-disabled') === 'true')) return true;
                return false;
            }"""
        )

        if is_disabled:
            print("Patient Type field is disabled - skipping.")
            return

        print("Patient Type field is enabled. Selecting 'O - Outpatient'...")
        btn = dropdown.locator("button")
        if btn.count() > 0:
            btn.first.click(force=True)
        else:
            dropdown.click(force=True)

        page.wait_for_timeout(500)

        menu_option = page.locator('div[style*="z-index: 2100"]').get_by_text(
            "O - Outpatient", exact=True
        )
        if menu_option.count() > 0:
            menu_option.first.click()
        else:
            page.get_by_text("O - Outpatient", exact=True).first.click()

        page.wait_for_timeout(500)
        print("Selected Patient Type: O - Outpatient.")

    def _fill_referral_and_accommodation(self, page):
        self._step(
            "Selecting 'No' for Is Patient Referred...",
            lambda: page.locator('input[name="isPatientReferred"][value="N"]').check(),
            critical=True,
        )
        page.wait_for_timeout(500)

        def _accommodation():
            page.locator('div[id^="accomodationTypeCode-"] button').click()
            page.wait_for_timeout(500)
            page.locator(
                'div[style*="z-index: 2100"]'
            ).get_by_text("P - Private", exact=True).first.click()

        self._step("Selecting Accommodation Type: P - Private...", _accommodation, critical=True)
        page.wait_for_timeout(500)
    
        def _discharge_time():
            discharge_time = page.get_by_role(
                "textbox",
                name="Discharge Time (hh:mm am/pm)"
            )

            discharge_time.click()

            # Move cursor to the end
            discharge_time.press("End")

            # Delete "AM"
            discharge_time.press("Backspace")
            discharge_time.press("Backspace")

            # Type "PM"
            discharge_time.type("PM")

        self._step(
            "Changing discharge time from AM to PM...",
            _discharge_time,
            critical=True,
        )

        page.wait_for_timeout(500)


    def _fill_disposition_and_diagnosis(self, page):
        def _disposition():
            page.locator('div[id^="patientDispositionCode-"] button').click(force=True)
            page.wait_for_timeout(500)
            page.locator(
                'div[style*="z-index: 2100"]'
            ).get_by_text("I - Improved", exact=True).first.click()

        self._step("Selecting Patient Disposition: I - Improved...", _disposition, critical=True)
        page.wait_for_timeout(500)

        def _admission_dx():
            page.locator('textarea[name="admissionDiagnosis"]').fill(
                "CHRONIC KIDNEY DISEASE STAGE V"
            )

        self._step("Entering Admission Diagnosis...", _admission_dx, critical=True)
        page.wait_for_timeout(500)

    def _add_discharge_diagnosis(self, page):
        # Guard: check THIS section's own row count before clicking its
        # NEW button — don't rely on any other section's state as a
        # proxy. Scoped to the Discharge Diagnosis table specifically
        # (anchor on its heading, walk up to the ancestor div containing
        # the table), so it can't be confused by rows belonging to a
        # different section.
        diagnosis_container = page.locator(
            "text=Discharge Diagnosis"
        ).first.locator("xpath=ancestor::div[.//table][1]")

        if diagnosis_container.locator("tbody tr").count() > 0:
            print(
                "Discharge Diagnosis already has a row - skipping to "
                "avoid duplicating it."
            )
            return

        def _click_new():
            page.locator(
                "text=Discharge Diagnosis"
            ).locator("..").get_by_role("button", name="NEW").click()

        self._step("Clicking NEW (Discharge Diagnosis)...", _click_new, critical=True)
        page.wait_for_timeout(500)

        def _search_and_add():
            search = page.locator('input[id="aTesting-searchICDCode"]')
            search.click()
            page.wait_for_timeout(300)
            search.press("Control+A")
            search.press("Backspace")
            search.type("N18.5", delay=100)
            page.wait_for_timeout(1500)
            page.locator('input[type="checkbox"]').first.check(force=True)
            page.wait_for_timeout(500)
            page.locator("#aTesting-searchICDCodeSave").click(force=True)

        self._step("Adding Discharge Diagnosis (N18.5)...", _search_and_add, critical=True)
        

    def _click_diagnosis_kebab(
        self,
        page,
        diagnosis_text="CHRONIC KIDNEY DISEASE",
        confirm_menu_text="Set as Primary",
        max_attempts=3,
    ):
        """
        Opens the diagnosis row's kebab (⋮) menu.

        Confirmed via DOM inspection: the kebab is the ONLY <button> inside
        that row's <td>, so `row.locator("button").last` is unambiguous —
        no need to scan every button on the page as a first resort.

        Success is confirmed by waiting for `confirm_menu_text` (the actual
        menu item we're about to click next, e.g. "Set as Primary") to
        become visible — this is more reliable than guessing at the
        dropdown's CSS class, since it directly proves the thing we're
        about to click is there.
        """
        for attempt in range(1, max_attempts + 1):
            try:
                row = page.locator("tr").filter(has_text=diagnosis_text).first

                try:
                    row.wait_for(state="visible", timeout=5000)
                except PlaywrightTimeoutError:
                    print(f"  kebab attempt {attempt}: row not visible in time")
                    page.wait_for_timeout(800)
                    continue

                row.scroll_into_view_if_needed()

                # ---- primary path: the row's own kebab button -----------------
                row_buttons = row.locator("button")
                if row_buttons.count() >= 1:
                    if self._try_click_kebab(page, row_buttons.last, confirm_menu_text):
                        return True

                # ---- fallback: geometry scan across all page buttons ----------
                if self._click_kebab_by_geometry(page, row, confirm_menu_text):
                    return True

                print(f"  kebab attempt {attempt}: menu didn't open, retrying")
                page.wait_for_timeout(800)

            except Exception as e:
                print(f"  kebab attempt {attempt} error: {e}")
                page.wait_for_timeout(800)

        return False


    def _try_click_kebab(self, page, button_locator, confirm_menu_text, timeout=3000):
        """Click a button locator and confirm the expected menu item appeared.
        Returns True/False instead of raising, so callers can fall through to
        the next strategy rather than assume success just because the click
        itself didn't throw."""
        try:
            button_locator.scroll_into_view_if_needed()
            button_locator.click(timeout=timeout)
        except Exception as e:
            print(f"  kebab click failed: {e}")
            return False

        try:
            page.get_by_text(confirm_menu_text, exact=True).wait_for(
                state="visible", timeout=2000
            )
            return True
        except PlaywrightTimeoutError:
            return False


    def _click_kebab_by_geometry(self, page, row, confirm_menu_text):
        """Original geometric fallback, kept for the rare case the row has
        zero/unexpected buttons — now using locator.click() (auto-waits for
        actionability) instead of a raw mouse coordinate click, and confirming
        the menu actually opened before reporting success."""
        row_box = row.bounding_box()
        if not row_box:
            return False

        row_y_center = row_box["y"] + row_box["height"] / 2
        candidates = page.locator("button")

        for i in range(candidates.count()):
            b = candidates.nth(i)
            if not b.is_visible():
                continue

            box = b.bounding_box()
            if not box:
                continue

            btn_y_center = box["y"] + box["height"] / 2
            if abs(btn_y_center - row_y_center) < 20 and 40 <= box["width"] <= 56:
                if self._try_click_kebab(page, b, confirm_menu_text):
                    return True

        return False


    def _set_primary_diagnosis(self, page):
        if self._click_diagnosis_kebab(page, confirm_menu_text="Set as Primary"):
            page.get_by_text("Set as Primary", exact=True).click(force=True)
            page.wait_for_timeout(500)
        else:
            raise RuntimeError("Could not open diagnosis kebab menu.")

    def _add_surgical_procedure(self, page, data):
        # Guard: check THIS section's own state before clicking its NEW
        # button — don't rely on Discharge Diagnosis (or any other
        # section) as a proxy. Beacon only renders the "RVS CODE" label
        # once a Surgical Procedure card actually exists (before a
        # procedure is added this area is blank; after, it shows
        # ICD10 CODE / NAME / RVS CODE / REPETITIVE — see screenshot).
        # At this point in the flow the "Add New Surgical Procedure"
        # modal isn't open yet, so "RVS CODE" being visible on the page
        # can only mean an existing card.
        existing_procedure = page.get_by_text("RVS CODE", exact=True).first

        if existing_procedure.count() > 0 and existing_procedure.is_visible():
            print(
                "Surgical Procedure already exists - skipping to avoid "
                "duplicating it."
            )
            return

        self._step(
            "Clicking NEW (Surgical Procedure)...",
            lambda: page.locator("#aTesting-newSurgicalProcedure").click(),
            critical=True,
        )
        page.wait_for_timeout(500)

        def _fill_rvs():
            # Beacon's RVS Code autocomplete is unreliable — sometimes
            # typing "90935" produces the suggestion immediately (see
            # screenshot 2), sometimes nothing renders below the input at
            # all even after the same delay (see screenshot 1), and the
            # field is just left holding "90935" as plain text with no
            # dropdown to click. The original single-shot version had no
            # way to tell those two cases apart: it always clicked
            # `.last`, which either clicked the real suggestion or clicked
            # nothing found (Playwright would then time out waiting on an
            # empty locator) — no retry, no distinct error.
            #
            # Fix: explicitly wait for the suggestion to become visible;
            # if it doesn't show up in time, clear the field and retype
            # from scratch (a fresh keystroke sequence is what actually
            # gets Beacon's autocomplete to fire again — clicking away
            # and back doesn't reliably re-trigger it), up to a few
            # attempts, before giving up with a clear error.
            rvs_input = page.get_by_label("RVS Code")
            suggestion = page.get_by_text("90935", exact=True).last

            max_attempts = 3
            for attempt in range(1, max_attempts + 1):
                rvs_input.click()
                rvs_input.fill("")  # clear any stale/partial text first
                rvs_input.type("90935", delay=100)

                try:
                    suggestion.wait_for(state="visible", timeout=3000)
                    suggestion.click()
                    return
                except PlaywrightTimeoutError:
                    print(
                        "WARNING: RVS Code suggestion '90935' did not "
                        f"appear (attempt {attempt}/{max_attempts}) — "
                        "retrying..."
                    )
                    page.wait_for_timeout(500)

            raise RuntimeError(
                "RVS Code suggestion '90935' never appeared after "
                f"{max_attempts} attempts — Beacon's autocomplete may be "
                "unresponsive."
            )

        self._step("Selecting RVS Code 90935...", _fill_rvs, critical=True)
        page.wait_for_timeout(500)

        def _select_icd10():
            page.locator('div[id^="icd10Code-"] button').click()
            page.wait_for_timeout(500)
            page.locator(
                'div[style*="z-index: 2100"] span[tabindex="0"]'
            ).first.click()

        self._step("Selecting ICD10 Code...", _select_icd10, critical=True)
        page.wait_for_timeout(500)

        def _select_sessions():
            page.locator('div[id^="numberOfSessions-"] button').click()
            page.wait_for_timeout(500)
            page.locator(
                'div[style*="z-index: 2100"]'
            ).get_by_text(str(data.total_sessions), exact=True).first.click()

        self._step(
            f"Selecting Number of Sessions: {data.total_sessions}...",
            _select_sessions,
            critical=True,
        )
        page.wait_for_timeout(500)

        def _select_type():
            page.locator('div[id^="typeCode-"] button').click()
            page.wait_for_timeout(500)
            page.locator(
                'div[style*="z-index: 2100"]'
            ).get_by_text("Hemodialysis", exact=True).first.click()

        self._step("Selecting Type: Hemodialysis...", _select_type, critical=True)
        page.wait_for_timeout(500)

        def _save():
            page.locator('div.rmq-4d5f58e7').locator('button[type="submit"]').click()
            page.wait_for_load_state("networkidle")

        self._step("Saving Surgical Procedure...", _save, critical=True)
        page.wait_for_timeout(1000)

    def _first_case_tag_present(self, page):
        """
        Returns True if the Surgical Procedure row is already tagged as
        1st Case Rate.

        Scoped to the RVS Code row's vertical position — same
        geometry-based approach used to find the kebab button — since
        a blind page-wide page.get_by_text("1ST CASE RATE") isn't
        reliable: DOM inspection shows the badge's wrapping <div>
        (label + a small svg "x" icon) reports the exact same innerText
        as the <label> itself, so more than one element on the page can
        carry this identical text, with no guarantee a blind search
        lands on the actual visible badge over some other match.
        """
        sp_anchor = page.get_by_text("RVS CODE", exact=True).first

        if sp_anchor.count() == 0:
            return False

        sp_box = sp_anchor.bounding_box()
        if not sp_box:
            return False

        sp_y_center = sp_box["y"] + sp_box["height"] / 2

        tag_labels = page.locator("label", has_text="1ST CASE RATE")

        for i in range(tag_labels.count()):
            lbl = tag_labels.nth(i)
            if not lbl.is_visible():
                continue
            box = lbl.bounding_box()
            if box and abs((box["y"] + box["height"] / 2) - sp_y_center) < 40:
                return True

        return False

    def _fill_session_dates(self, page, data):
        # Guard: if the Surgical Procedure is already tagged as 1st
        # Case Rate, this patient has already been fully processed
        # through this part of CF2 before (e.g. a retry) — session
        # dates would already be filled in too. Re-running this would
        # retype over (or duplicate) dates that are already correct.
        if self._first_case_tag_present(page):
            print(
                "Surgical Procedure is already tagged as 1st Case Rate "
                "- skipping session date entry (already completed)."
            )
            return

        # Guard: every session date must fall within the claim's own
        # Admission Date / Discharge Date range. Read both straight
        # from the page (rather than trusting the uploaded record
        # alone), since that's the actual constraint Beacon enforces —
        # catching a mismatch here, before typing anything, is much
        # clearer than letting it surface later at save time.
        admission_input = page.locator('input[id^="admissionDate-"]').first
        discharge_input = page.locator('input[id^="dischargeDate-"]').first

        admission_raw = admission_input.input_value().strip()
        discharge_raw = discharge_input.input_value().strip()

        try:
            admission_date = datetime.strptime(admission_raw, "%m-%d-%Y").date()
            discharge_date = datetime.strptime(discharge_raw, "%m-%d-%Y").date()
        except ValueError as e:
            raise RuntimeError(
                f"Could not read Admission/Discharge date from the page "
                f"(admission='{admission_raw}', discharge='{discharge_raw}'): {e}"
            )

        def _as_date(value):
            return value.date() if hasattr(value, "date") else value

        out_of_range = [
            session_date for session_date in data.session_dates
            if not (admission_date <= _as_date(session_date) <= discharge_date)
        ]

        if out_of_range:
            raise RuntimeError(
                "The session dates entered do not fall within the "
                "admission and discharge date range."
            )

        print(f"Filling {len(data.session_dates)} session dates...")
        for i, session_date in enumerate(data.session_dates):
            date_str = session_date.strftime("%m%d%Y")

            def _fill_one(i=i, date_str=date_str):
                date_input = page.locator(
                    f'input[id^="surgicalProcedures0sessions{i}sessionDate-Date"]'
                ).first
                date_input.scroll_into_view_if_needed()
                date_input.click()
                date_input.type(date_str, delay=100)
                date_input.press("Tab")

            # Non-critical: a single bad session date shouldn't sink the whole patient.
            self._step(f"  Session {i + 1}: {date_str}", _fill_one, critical=False)
            page.wait_for_timeout(300)

    def _tag_first_case(self, page):
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(500)

        if self._first_case_tag_present(page):
            print("Surgical Procedure is already tagged as 1st Case Rate - skipping.")
            return

        sp_anchor = page.get_by_text("RVS CODE", exact=True).first
        sp_anchor.scroll_into_view_if_needed()
        page.wait_for_timeout(300)

        sp_box = sp_anchor.bounding_box()
        if not sp_box:
            raise RuntimeError("Could not locate Surgical Procedure card header.")
        sp_y_center = sp_box["y"] + sp_box["height"] / 2

        all_buttons = page.locator("button")
        count = all_buttons.count()

        best_match = None
        for i in range(count):
            b = all_buttons.nth(i)
            if not b.is_visible():
                continue
            box = b.bounding_box()
            if box and abs((box["y"] + box["height"] / 2) - sp_y_center) < 40 and 40 <= box["width"] <= 56:
                best_match = box
                break

        if not best_match:
            raise RuntimeError("Could not find surgical procedure kebab button.")

        page.mouse.click(
            best_match["x"] + best_match["width"] / 2,
            best_match["y"] + best_match["height"] / 2,
        )
        page.wait_for_timeout(500)

        page.get_by_text("Tag as 1st Case Rate", exact=True).click(force=True)
        page.wait_for_timeout(500)

        page.get_by_role("button", name="Proceed", exact=True).click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1000)

    def _add_doctor(self, page, data):
        # Guard: check THIS section's own row count before clicking its
        # NEW button — same pattern as Discharge Diagnosis. Scoped to
        # the Doctors table specifically (anchor on its heading, walk
        # up to the ancestor div containing the table), so it can't be
        # confused by rows belonging to a different section.
        doctors_container = page.locator(
            "text=Doctors"
        ).first.locator("xpath=ancestor::div[.//table][1]")

        if doctors_container.locator("tbody tr").count() > 0:
            print(
                "Doctors already has a row - skipping to avoid "
                "duplicating it."
            )
            return

        self._step(
            "Clicking NEW (Doctors)...",
            lambda: page.locator("#aTesting-newDoctorsOrder").click(),
            critical=True,
        )
        page.wait_for_timeout(500)

        def _fill_accred():
            accred_input = page.get_by_label("Accreditation Number")
            accred_input.click()
            accred_input.press("Control+A")
            accred_input.press("Backspace")
            accred_digits = re.sub(r"\D", "", data.accreditation_no)
            accred_input.type(accred_digits, delay=100)
            accred_input.press("Tab")

        self._step("Filling Accreditation Number...", _fill_accred, critical=True)
        page.wait_for_timeout(1000)

        self._step(
            "Autofilling Doctor Information...",
            lambda: page.get_by_text("Autofill Doctor Information", exact=True).click(),
            critical=True,
        )

        # ------------------------------------------------------------------
        # Wait until Beacon finishes autofilling the doctor.
        # The Lastname field is initially empty, so wait until it has a value.
        # ------------------------------------------------------------------
        doctor_lastname = page.locator("#aTesting-doctorLastname")
        doctor_lastname.wait_for(state="visible", timeout=30000)

        # Wait until the lastname field is populated.
        doctor_lastname.wait_for(
            state="attached",
            timeout=30000,
        )

        page.wait_for_function(
            """
            () => {
                const el = document.querySelector("#aTesting-doctorLastname");
                return el && el.value.trim().length > 0;
            }
            """,
            timeout=30000,
        )

        print("Doctor information loaded.")

        # Give Beacon time to finish processing after the UI is filled
        page.wait_for_timeout(1000)

        date_str = data.last_treatment.strftime("%m%d%Y")
        expected_display = data.last_treatment.strftime("%m-%d-%Y")

        def _sign_date():
            sign_date_input = page.locator('input[id^="doctorSignDate-DoctorSignDate"]')

            max_attempts = 3
            actual_value = None

            for attempt in range(1, max_attempts + 1):
                sign_date_input.click()
                sign_date_input.press("Control+A")
                sign_date_input.press("Backspace")
                sign_date_input.type(date_str, delay=100)
                sign_date_input.press("Tab")
                page.wait_for_timeout(300)

                actual_value = sign_date_input.input_value()

                if actual_value == expected_display:
                    return

                print(
                    f"  Attempt {attempt}: date field shows '{actual_value}', "
                    f"expected '{expected_display}' - retrying..."
                )
                page.wait_for_timeout(300)

            print(
                f"  WARNING: Doctor Sign Date could not be verified after "
                f"{max_attempts} attempts (last value: '{actual_value}', "
                f"expected '{expected_display}'). Skipping and continuing."
            )

        self._step(
            f"Filling Doctor Sign Date: {expected_display}...",
            _sign_date,
            critical=False,
        )
        page.wait_for_timeout(500)

        def _save_and_close():
            page.get_by_role("button", name="Save and Create New").click()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1000)

            page.get_by_role("button", name="Close").click()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1000)

        self._step("Saving doctor and closing modal...", _save_and_close, critical=True)

    def _fill_benefits_and_fees(self, page, data):
        self._step(
            "Selecting 'No' for Does Patient Have Enough Benefits?...",
            lambda: page.locator('input[id="aTesting-hasEnoughBenefitsNo"]').check(),
            critical=True,
        )
        page.wait_for_timeout(500)

        fees = get_fees(data.total_sessions)
        hosp_actual = fees["hospital_actual"]
        hosp_discount = fees["hospital_discount"]
        hosp_philhealth = fees["hospital_discount"]
        prof_actual = fees["prof_actual"]
        prof_discount = fees["prof_discount"]
        prof_philhealth = fees["prof_discount"]

        print(
            f"Fees for {data.total_sessions} sessions: "
            f"hosp={hosp_actual}/{hosp_discount}, prof={prof_actual}/{prof_discount}"
        )

        def _fill_field(name, value):
            field = page.locator(f'input[name="{name}"]')
            field.fill(str(value))
            field.press("Tab")

        fee_fields = [
            ("hospitalFeesActualCharges", hosp_actual),
            ("hospitalFeesAmountAfterDiscount", hosp_discount),
            ("hospitalFeesPhilHealthBenefit", hosp_philhealth),
            ("professionalFeesActualCharges", prof_actual),
            ("professionalFeesAmountAfterDiscount", prof_discount),
            ("professionalFeesPhilHealthBenefit", prof_philhealth),
        ]

        for name, value in fee_fields:
            self._step(
                f"Filling {name}...",
                lambda n=name, v=value: _fill_field(n, v),
                critical=True,
            )
            page.wait_for_timeout(300)

        print("Fees filled.")

        radio_ids = [
            "aTesting-hospitalFeesDidPatientPayNo",
            "aTesting-hospitalFeesPatientHasHMONo",
            "aTesting-hospitalFeesPatientHasOtherDeductionsNo",
            "aTesting-professionalFeesDidPatientPayNo",
            "aTesting-professionalFeesPatientHasHMONo",
            "aTesting-professionalFeesPatientHasOtherDeductionsNo",
            "aTesting-purchasesWithDrugsMedSuppliesNo",
            "aTesting-purchasesWithExaminationsNo",
        ]

        for rid in radio_ids:
            # Non-critical: these are best-effort defaults, don't sink the patient over one.
            self._step(f"Checking '{rid}'...", lambda r=rid: page.locator(f'#{r}').check(), critical=False)
            page.wait_for_timeout(200)

        print("Fee/purchase radios done.")

    def _fill_access_patient_records_date(self, page, data):
        date_str = data.last_treatment.strftime("%m%d%Y")

        def _fill():
            apr_date = page.locator('input[id^="aprDate-Date"]')
            apr_date.scroll_into_view_if_needed()
            apr_date.click()
            page.wait_for_timeout(300)
            apr_date.press("Control+A")
            page.wait_for_timeout(100)
            apr_date.press("Delete")
            page.wait_for_timeout(200)
            apr_date.type(date_str, delay=150)
            apr_date.press("Tab")

        self._step(f"Filling Access Patient Records date: {date_str}...", _fill, critical=True)
        page.wait_for_timeout(300)

    # --------------------------------------------------
    # Save CF2
    # --------------------------------------------------
    def _save_cf2(self, page):
        def _save():
            print("Saving CF2...")

            # First SAVE click — existing, proven selector (last button
            # inside the cf2Save container).
            page.locator('#cf2Save').get_by_role("button").last.click()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1000)

            # Beacon is occasionally unstable enough that a single SAVE
            # click doesn't actually persist the form even though the
            # click itself reports success — some users have run into CF2
            # not saving because of this. As a validation/backup, fire a
            # second click at the same SAVE button (the pink "SAVE" button
            # inside #cf2Save), this time located explicitly by its
            # visible label rather than "last button in the container" so
            # it stays correct even if the container's button order shifts
            # under Beacon's instability.
            #
            # This second click is best-effort: the first click already
            # fired above, so a flaky/missing second click shouldn't turn
            # an otherwise-successful save into a failed patient.
            try:
                second_save = page.locator('#cf2Save').get_by_role(
                    "button", name="SAVE", exact=True
                )
                if second_save.count() > 0:
                    print("Triggering second SAVE click as validation...")
                    second_save.first.click()
                    page.wait_for_load_state("networkidle")
                    page.wait_for_timeout(1000)
                else:
                    print(
                        "WARNING: Second SAVE button (by label) not "
                        "found — skipping backup click."
                    )
            except Exception as e:
                print(f"WARNING: Second SAVE click failed, continuing: {e}")

            # Third click: the floating round SAVE button (FAB) at the
            # bottom-right of the CF2 form. This is a visually and
            # structurally separate save affordance from the pink
            # rectangle button — its "SAVE" label only becomes visible on
            # hover, but it's still present in the DOM as hidden text, so
            # it can be matched reliably without needing the hover state.
            #
            # NOTE: matching on "button with an svg child" alone is NOT
            # enough — every dropdown toggle in the CF2 form (Confinement
            # Information, Accommodation Type, etc.) also renders as a
            # button with an svg chevron icon, and several of those sit
            # earlier in the DOM than the floating SAVE button. `.first`
            # on that broader match was landing on one of those dropdown
            # toggles instead — clicking it open and blocking the rest of
            # the flow. Requiring the hidden "SAVE" text alongside the svg
            # excludes those dropdown buttons, which don't carry that
            # label.
            try:
                fab_save = page.locator('#cf2Save').locator('button').filter(
                    has=page.locator('svg')
                ).filter(has_text="SAVE")

                if fab_save.count() > 0:
                    print(
                        "Triggering third SAVE click (floating button) "
                        "as validation..."
                    )
                    fab_save.first.click()
                    page.wait_for_load_state("networkidle")
                    page.wait_for_timeout(1000)
                else:
                    print(
                        "WARNING: Floating SAVE button (svg icon) not "
                        "found — skipping third click."
                    )
            except Exception as e:
                print(
                    f"WARNING: Third SAVE click (floating button) failed, "
                    f"continuing: {e}"
                )

        self._step("Saving CF2 form...", _save, critical=True)
        print("CF2 saved.")

    def _fill_statement_of_account(self, page, data):
        original_page = page  # keep a handle on the original tab so we can return to it

        def _open():
            page.locator('#cf2Save').get_by_role("button", name="Statement of Account").click()
            page.wait_for_load_state("networkidle")

        self._step("Opening Statement of Account...", _open, critical=True)
        page.wait_for_timeout(1000)

        self._step(
            "Opening Signatories tab...",
            lambda: page.get_by_role("button", name="Signatories").click(),
            critical=True,
        )
        page.wait_for_timeout(500)

        date_str = data.last_treatment.strftime("%m%d%Y")

        # Read these once up front (instead of inline inside each fill
        # closure) so the same expected values can be reused below when
        # verifying the fields actually stuck.
        billing_clerk_name = self._get_billing_clerk_name()
        billing_clerk_cp = self._get_billing_clerk_cp()

        # Locators are resolved fresh on every use (Playwright re-queries
        # the DOM each call), so grabbing them once here and reusing them
        # in both the fill and the refill/verify closures below is safe.
        prepared_by_input = page.locator('input[name="preparedBy"]')
        contact_input = page.locator('input[name="adminContactNo"]')
        admin_date_input = page.locator('input[id^="adminDateSigned-MM-DD-YYYY"]')
        patient_rep_input = page.locator('input[name="patientRepresentative"]')
        conforme_date_input = page.locator('input[id^="representativeDateSigned-MM-DD-YYYY"]')

        def _fill_prepared_by():
            prepared_by_input.click()
            prepared_by_input.press("Control+A")
            prepared_by_input.press("Backspace")
            prepared_by_input.fill(billing_clerk_name)
            prepared_by_input.press("Tab")

        self._step(
            "Filling Prepared by (Billing Clerk / Accountant) from Excel...",
            _fill_prepared_by,
            critical=True,
        )
        page.wait_for_timeout(300)

        def _fill_prepared_by_cp():
            contact_input.click()
            contact_input.press("Control+A")
            contact_input.press("Backspace")
            contact_input.fill(billing_clerk_cp)
            contact_input.press("Tab")

        self._step(
            "Filling Billing Clerk Contact No. from Excel...",
            _fill_prepared_by_cp,
            critical=True,
        )

        page.wait_for_timeout(300)

        def _fill_admin_date():
            admin_date_input.click()
            admin_date_input.press("Control+A")
            admin_date_input.press("Backspace")
            admin_date_input.type(date_str, delay=100)
            admin_date_input.press("Tab")

        def _fill_patient_representative():
            patient_rep_input.click()
            patient_rep_input.press("Control+A")
            patient_rep_input.press("Backspace")
            patient_rep_input.fill(data.patient_name)

        def _fill_conforme_date():
            conforme_date_input.click()
            conforme_date_input.press("Control+A")
            conforme_date_input.press("Backspace")
            page.wait_for_timeout(200)
            conforme_date_input.type(date_str, delay=100)

        def _fill_signatories():
            _fill_admin_date()
            page.wait_for_timeout(300)

            _fill_patient_representative()
            page.wait_for_timeout(300)

            _fill_conforme_date()

        self._step("Filling Signatories...", _fill_signatories, critical=True)
        page.wait_for_timeout(300)

        # --- Validation ---------------------------------------------------
        # Beacon is occasionally unstable: even after a field is typed into
        # (and reports a successful click/fill/Tab), the DOM can clear that
        # field's content on its own shortly after — so a field that looked
        # filled a moment ago can be empty by the time Save is clicked,
        # silently producing a Statement of Account with blank signatories.
        # Re-read every field we just filled and, if any came back
        # empty/mismatched, re-run its fill and check again before saving,
        # instead of finding out only after the record is already saved.
        def _digits_only(s):
            return "".join(ch for ch in (s or "") if ch.isdigit())

        def _verify_text_field(locator, expected, field_name, refill, attempts=3):
            if not expected:
                # Nothing expected here (e.g. contact no. genuinely blank
                # in the uploaded sheet) — nothing to validate.
                return

            expected = expected.strip()
            for attempt in range(1, attempts + 1):
                actual = (locator.input_value() or "").strip()
                if actual == expected:
                    return
                print(
                    f"WARNING: '{field_name}' expected '{expected}' but "
                    f"found '{actual}' (attempt {attempt}/{attempts}) — "
                    f"Beacon likely cleared it, refilling..."
                )
                refill()
                page.wait_for_timeout(400)

            actual = (locator.input_value() or "").strip()
            if actual != expected:
                raise Exception(
                    f"'{field_name}' still not filled correctly after "
                    f"{attempts} attempts (expected '{expected}', got "
                    f"'{actual}')"
                )

        def _verify_date_field(locator, expected_date_str, field_name, refill, attempts=3):
            # Date inputs may auto-format what's typed (e.g. adding
            # slashes), so compare digits only rather than exact strings.
            expected_digits = _digits_only(expected_date_str)
            for attempt in range(1, attempts + 1):
                actual = locator.input_value() or ""
                if actual.strip() and _digits_only(actual) == expected_digits:
                    return
                print(
                    f"WARNING: '{field_name}' expected date digits "
                    f"'{expected_digits}' but found '{actual}' (attempt "
                    f"{attempt}/{attempts}) — Beacon likely cleared it, "
                    f"refilling..."
                )
                refill()
                page.wait_for_timeout(400)

            actual = locator.input_value() or ""
            if not (actual.strip() and _digits_only(actual) == expected_digits):
                raise Exception(
                    f"'{field_name}' still not filled correctly after "
                    f"{attempts} attempts (expected date digits "
                    f"'{expected_digits}', got '{actual}')"
                )

        def _verify_signatories_filled():
            _verify_text_field(
                prepared_by_input, billing_clerk_name,
                "Prepared by (Billing Clerk / Accountant)", _fill_prepared_by,
            )
            _verify_text_field(
                contact_input, billing_clerk_cp,
                "Billing Clerk Contact No.", _fill_prepared_by_cp,
            )
            _verify_date_field(
                admin_date_input, date_str,
                "Date Signed (Admin)", _fill_admin_date,
            )
            _verify_text_field(
                patient_rep_input, data.patient_name,
                "Patient Representative", _fill_patient_representative,
            )
            _verify_date_field(
                conforme_date_input, date_str,
                "Date Signed (Conforme)", _fill_conforme_date,
            )

        self._step(
            "Verifying Statement of Account fields filled correctly...",
            _verify_signatories_filled,
            critical=True,
        )
        page.wait_for_timeout(300)

        def _save_signatories():
            page.evaluate("document.querySelector('button[type=\"submit\"]').scrollIntoView()")
            page.wait_for_timeout(300)
            page.get_by_role("button", name="SAVE").last.click()
            page.wait_for_load_state("networkidle")

        self._step("Saving Signatories...", _save_signatories, critical=True)
        page.wait_for_timeout(1000)

        self._step(
            "Closing Statement of Account...",
            lambda: (page.get_by_role("button", name="Close").click(), page.wait_for_load_state("networkidle")),
            critical=False,
        )
        page.wait_for_timeout(1000)

        print("Statement of Account closed.")

        def _open_claim_forms():
            page.get_by_role(
                "link",
                name="CLAIM FORMS"
            ).click()

        self._step(
            "Opening Claim Forms...",
            _open_claim_forms,
            critical=True,
        )
        page.wait_for_timeout(500)

        def _open_claim_form_2():
            """Opens Claim Form 2 in a new tab, retrying in place if Beacon
            is slow to load rather than letting one timeout fail the whole
            patient.

            Beacon can intermittently take longer than Playwright's default
            timeout to actually spawn/load the new tab — when that happens
            we used to let the TimeoutError propagate straight up and fail
            the patient on the spot. Now: on a timeout, close whatever tab
            did (or didn't) come up, reload the Claim Forms list page to
            get back to a clean state, and retry the click a few times
            before finally giving up.
            """
            nonlocal page

            attempts = 3
            last_error = None

            for attempt in range(1, attempts + 1):
                new_page = None
                try:
                    card = page.locator('a[href*="download-pdf/cf2"]').locator("..")
                    card.hover()

                    with page.context.expect_page(timeout=20000) as new_page_info:
                        page.locator(
                            'a[href*="download-pdf/cf2"]'
                        ).click()

                    new_page = new_page_info.value
                    new_page.wait_for_load_state("networkidle", timeout=30000)

                    # Success — hand the new tab back to the caller.
                    page = new_page
                    return
                except PlaywrightTimeoutError as e:
                    last_error = e
                    print(
                        f"WARNING: Opening Claim Form 2 timed out on "
                        f"attempt {attempt}/{attempts} (Beacon loading too "
                        f"long): {e}"
                    )

                    # Don't leave a half-loaded/stuck tab behind — close it
                    # (if one actually opened) so the retry starts clean
                    # instead of piling up orphaned tabs.
                    if new_page is not None:
                        try:
                            new_page.close()
                        except Exception:
                            pass

                    if attempt < attempts:
                        print("Reloading Claim Forms page and retrying...")
                        try:
                            page.reload()
                            page.wait_for_load_state("networkidle", timeout=15000)
                        except Exception as reload_err:
                            print(
                                f"WARNING: Reload after timeout failed: "
                                f"{reload_err}"
                            )
                        page.wait_for_timeout(1000)

            raise Exception(
                f"Could not open Claim Form 2 after {attempts} attempts — "
                f"Beacon kept timing out: {last_error}"
            )

        cf2_tab_opened = self._step(
            "Opening Claim Form 2...",
            _open_claim_form_2,
            critical=True,
        )

        page.wait_for_timeout(500)

        # ------------------------------------------------------------
        # From here on, NOTHING is allowed to skip the SAVE click.
        # Each field is filled in its own try/except so one bad field
        # (missing element, timeout, stale reference, etc.) can't stop
        # the others from being attempted, and — critically — can't
        # stop us from reaching the SAVE button at the end. Whatever
        # data did make it into the form still needs to be persisted.
        # ------------------------------------------------------------
        def _fill_signature_over_printed_name():
            billing_clerk_name = self._get_billing_clerk_name()

            page.wait_for_selector(
                "#signatureOverPrintedNameOfAuthHCIRep",
                state="visible",
                timeout=60000,
            )

            signature_input = page.locator(
                "#signatureOverPrintedNameOfAuthHCIRep"
            )

            signature_input.click()
            signature_input.press("Control+A")
            signature_input.press("Backspace")
            signature_input.fill(billing_clerk_name)
            signature_input.press("Tab")

        def _fill_official_capacity_designation():
            designation = self._get_official_capacity_designation()

            designation_input = page.locator(
                "#officialCapacityDesignation"
            )

            designation_input.click()
            designation_input.press("Control+A")
            designation_input.press("Backspace")
            designation_input.fill(designation)
            designation_input.press("Tab")

        def _fill_hci_representative_date_signed():
            """
            Opens the "Add Date" calendar picker next to Date Signed under
            PART IV - CERTIFICATION OF CONSUMPTION OF HEALTH CARE
            INSTITUTION (the HCI Representative's signature block) and
            selects data.last_treatment — the same date already reused
            everywhere else on this form (Doctor Sign Date, Access Patient
            Records date, Statement of Account signatory dates).

            Whatever happens here must never block the rest of the form or
            the final SAVE click. If anything fails after the picker was
            opened, the popup is dismissed (Cancel, then Escape as a
            fallback) before moving on — an open calendar overlay left
            sitting on screen could otherwise intercept the SAVE click that
            always runs after this, regardless of what happened here.
            """
            target_date = data.last_treatment
            picker_opened = False

            def _open_picker():
                nonlocal picker_opened
                # Scope to the button that immediately follows the
                # Official Capacity/Designation field in DOM order, since
                # that's the "Add Date" icon sitting on the same signature
                # line (Signature | Designation | Date Signed).
                add_date_btn = page.locator(
                    "#officialCapacityDesignation"
                ).locator(
                    "xpath=following::button[contains(@class,'pdfIndicator')][1]"
                )

                if add_date_btn.count() == 0:
                    add_date_btn = page.get_by_role(
                        "button", name="Add Date"
                    ).first

                add_date_btn.scroll_into_view_if_needed()
                add_date_btn.click(force=True)
                picker_opened = True
                page.wait_for_timeout(500)

            def _navigate_and_select():
                nonlocal picker_opened
                # The picker always opens defaulting to TODAY's date (every
                # screenshot confirms this, e.g. "Thu, Aug 6" when today is
                # Aug 6). That's a far more reliable anchor than reading and
                # parsing the on-screen month/year label — a previous
                # version compared header.inner_text() against a formatted
                # string and, on any mismatch (whitespace, format drift),
                # would silently click one direction for up to 24 iterations
                # and land wherever that happened to end (observed bug: it
                # opened on Aug 2026 but ended up saving June 2026). Compute
                # the required number of clicks directly instead — no text
                # parsing, no equality check, no chance of drifting off.
                today = datetime.now()
                months_diff = (
                    (target_date.year - today.year) * 12
                    + (target_date.month - today.month)
                )

                # Arrow buttons are identified by their fixed SVG icon path
                # (chevron-right = next month, chevron-left = previous
                # month) since neither button carries a class or label.
                next_month_btn = page.locator(
                    'button:has(path[d="M10 6L8.59 7.41 13.17 12l-4.58 4.59L10 18l6-6z"])'
                )
                prev_month_btn = page.locator(
                    'button:has(path[d="M15.41 7.41L14 6l-6 6 6 6 1.41-1.41L10.83 12z"])'
                )

                btn_to_click = next_month_btn if months_diff > 0 else prev_month_btn

                for _ in range(abs(months_diff)):
                    btn_to_click.click()
                    page.wait_for_timeout(250)

                print(
                    f"  Date Signed picker: today={today.strftime('%m/%d/%Y')}, "
                    f"target={target_date.strftime('%m/%d/%Y')}, "
                    f"months_diff={months_diff}"
                )

                # Day cells are 42px-wide buttons rendered left-to-right,
                # top-to-bottom for the CURRENT month only. Confirmed via a
                # live run: leading blank slots before day 1 (e.g. the 2
                # empty cells before Jul 1, 2026, a Wednesday) are NOT
                # clickable buttons — they're empty placeholder cells — so
                # the button list starts right at day 1. (An earlier version
                # added calendar.monthrange()'s weekday offset on top of
                # this, which double-counted those leading cells and landed
                # 2 days late — e.g. clicking "3" instead of "1".) So the
                # button index is simply the day number minus one.
                day_cells = page.locator('button[style*="width: 42px"]')
                cell_index = target_date.day - 1

                print(f"  Date Signed picker: cell_index={cell_index}")

                day_cells.nth(cell_index).click()
                page.wait_for_timeout(300)

                ok_button = page.get_by_role("button", name="OK", exact=True)
                if ok_button.count() == 0:
                    ok_button = page.get_by_text(
                        re.compile(r"^OK$", re.IGNORECASE)
                    )
                ok_button.first.click()
                picker_opened = False  # OK closes the popup on success
                page.wait_for_timeout(300)

            def _close_leftover_picker():
                """
                Best-effort cleanup only — never raises. Tries CANCEL
                first (the picker's own dismiss control), then Escape as a
                generic fallback, so a half-finished date selection can't
                leave an overlay sitting on top of the SAVE button.
                """
                try:
                    cancel_btn = page.get_by_role(
                        "button", name="CANCEL", exact=False
                    )
                    if cancel_btn.count() > 0:
                        cancel_btn.first.click(timeout=2000)
                    else:
                        page.keyboard.press("Escape")
                except Exception:
                    try:
                        page.keyboard.press("Escape")
                    except Exception:
                        pass
                page.wait_for_timeout(300)

            try:
                print("Opening Date Signed picker (HCI Representative)...")
                _open_picker()

                print(
                    f"Selecting Date Signed (HCI Representative): "
                    f"{target_date.strftime('%m/%d/%Y')}..."
                )
                _navigate_and_select()

                print("Date Signed (HCI Representative) set successfully.")
            except Exception as e:
                print(
                    f"WARNING: Date Signed (HCI Representative) step "
                    f"failed: {e}. Skipping this field and continuing — "
                    f"the CF2 form will still be saved."
                )
                if picker_opened:
                    _close_leftover_picker()
            page.wait_for_timeout(300)

        def _save_claim_form_2():
            """
            Always-attempted fallback. Retries the click a few times and,
            if Playwright's normal click still can't land it (e.g. an
            overlay or animation is blocking actionability), falls back to
            a raw JS click on the SAVE button so the click reaches the
            page's SAVE handler no matter what happened earlier in this
            section.
            """
            save_button = page.get_by_role("button", name="SAVE", exact=True)

            last_error = None
            for attempt in range(1, 4):
                try:
                    save_button.wait_for(state="visible", timeout=15000)
                    save_button.click(timeout=15000)
                    page.wait_for_load_state("networkidle", timeout=15000)
                    print(f"CF2 saved successfully (attempt {attempt}).")
                    return
                except Exception as e:
                    last_error = e
                    print(f"WARNING: CF2 save attempt {attempt} failed: {e}")
                    page.wait_for_timeout(1000)

            # Last-ditch attempt: bypass Playwright's actionability checks
            # entirely in case the button exists but is being blocked by
            # something (overlay, animation, focus trap, etc.).
            try:
                page.evaluate(
                    """() => {
                        const btns = [...document.querySelectorAll('button')];
                        const save = btns.find(
                            b => b.textContent.trim().toUpperCase() === 'SAVE'
                        );
                        if (save) save.click();
                    }"""
                )
                page.wait_for_timeout(1500)
                print("CF2 save fallback: clicked SAVE via JS evaluate.")
            except Exception as e:
                print(
                    f"ERROR: All CF2 save attempts failed. "
                    f"Last click error: {last_error}. JS fallback error: {e}"
                )
                raise

        def _fill_cf2_fields_then_save():
            if not cf2_tab_opened:
                # No CF2 tab to fill or save — nothing to do here.
                return

            self._step(
                "Filling Signature Over Printed Name of Authorized HCI Representative...",
                _fill_signature_over_printed_name,
                critical=False,
            )
            page.wait_for_timeout(300)

            self._step(
                "Filling Official Capacity/Designation...",
                _fill_official_capacity_designation,
                critical=False,
            )
            page.wait_for_timeout(300)

            self._step(
                "Filling Date Signed (HCI Representative)...",
                _fill_hci_representative_date_signed,
                critical=False,
            )
            page.wait_for_timeout(300)

        try:
            _fill_cf2_fields_then_save()
        finally:
            # SAVE fires regardless of what happened above — error,
            # timeout, or otherwise — as long as the CF2 tab is open.
            if cf2_tab_opened:
                self._step(
                    "Saving CF2 (fallback - always attempted)...",
                    _save_claim_form_2,
                    critical=False,
                )

        page.wait_for_timeout(1000)

        def _close_claim_form_2_tab():
            nonlocal page
            page.wait_for_timeout(1000)  # let the save actually finish before closing
            page.close()
            page = original_page
            page.bring_to_front()

        self._step(
            "Closing Claim Form 2 tab...",
            _close_claim_form_2_tab,
            critical=False,
        )