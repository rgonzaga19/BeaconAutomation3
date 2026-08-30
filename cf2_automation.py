import os
import re
import sys
from datetime import datetime
import openpyxl
from cf2_mapper import build_cf2_data
import browser_session
import cf2_api
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
        # Set by the API-first path in _add_discharge_diagnosis when it
        # successfully handles everything (including "set as primary")
        # via a direct API call — lets the corresponding later
        # UI-fallback step in fill_cf2() skip cleanly instead of
        # wastefully retrying/timing out against work that's already
        # done.
        self._discharge_diagnosis_done_via_api = False
        # Set inside _add_surgical_procedure's API path the moment
        # NewPHICSurgicalProcedure itself succeeds. If a later step
        # (session dates / case rate tagging) fails after the procedure
        # was already created, this flag stops the UI fallback below
        # from re-creating a duplicate procedure.
        self._surgical_procedure_created_via_api = False
        # Set inside _tag_first_case_via_api the moment tagging itself
        # succeeds via API - lets fill_cf2()'s "Tagging..." step skip
        # the UI fallback cleanly. IMPORTANT: for single-session claims,
        # EditPHICCF2 must first persist and verify the session date.
        self._case_rate_tagged_via_api = False

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

    def _get_ids(self, page):
        """Wraps cf2_api.extract_ids_from_url(page.url) - shared by every
        API-migrated step below. Raises cf2_api.Cf2ApiError if the URL
        doesn't match the expected pattern, which callers treat the
        same as any other API-path failure: fall back to UI automation."""
        return cf2_api.extract_ids_from_url(page.url)

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
            "Validating eligibility via API...",
            lambda: self._validate_eligibility_via_api(page, data),
            critical=False,
        )

        # Patient Type no longer uses the UI in the normal path.
        # _fill_and_save_cf2() persists O / Outpatient through EditPHICCF2
        # when Beacon's current CF2 record does not already contain a value.

        # Discharge Diagnosis / Surgical Procedure / Doctor all go FIRST,
        # before any of the plain-text CF2 fields below are typed. Two
        # reasons:
        #  1) Whether they go through the API-first path or fall back to
        #     UI automation, Beacon's own in-page state (fetched once,
        #     when the CF2 tab first opened) never learns about the new
        #     rows on its own — only a page.reload() re-syncs it. Doing
        #     that reload here, before anything else is typed, means it
        #     can't wipe unsaved input the way it did when these ran
        #     later in the sequence (the original bug this replaced).
        #  2) Without the reload, Beacon's own client-side SAVE
        #     validation still sees these sections as empty even once
        #     the backend genuinely has the rows — which is what was
        #     producing the "field can't be empty" error on SAVE despite
        #     Discharge Diagnosis / Surgical Procedure showing data when
        #     checked manually afterward.
        self._add_discharge_diagnosis(page)

        self._step(
            "Setting discharge diagnosis as Primary...",
            lambda: self._set_primary_diagnosis(page),
            critical=False,
        )

        self._add_surgical_procedure(page, data)
        self._add_doctor(page, data)

        # CF2 tab reload/click removed from the normal path.
        # Discharge Diagnosis / Surgical Procedure / Doctor are API-backed,
        # and all downstream CF2 data writes use direct API calls, so there
        # is no browser-side CF2 state that needs to be re-synchronized here.

        # Session dates must be persisted to Beacon BEFORE 1st Case Rate tagging.
        # For a single-session claim we now use the same full EditPHICCF2 payload
        # that Beacon accepts when the date is entered manually. NewPHICAllCaseRate
        # creates the case-rate record but does NOT persist surgicalProcedure.sessions[].sessionDate.
        if data.total_sessions == 1:
            if not self._fill_and_save_cf2(page, data, persist_session_dates=True):
                raise RuntimeError(
                    "Could not persist Session 1 date through EditPHICCF2 API; "
                    "refusing to tag 1st Case Rate while backend sessionDate is unknown."
                )
        else:
            # Multi-session API payload shape has not been independently confirmed;
            # retain the existing UI entry path for those cases.
            self._fill_session_dates(page, data)

        def _tag_first_case_api_or_ui():
            ids = self._get_ids(page)
            if not self._tag_first_case_via_api(page, ids, data):
                # Never allow UI tagging unless the backend already contains the
                # expected session date. This prevents a 1st Case Rate tag with a
                # NULL sessionDate when the API path is unavailable.
                if data.total_sessions == 1:
                    procedures = cf2_api.get_surgical_procedures(ids["claim_id"])
                    sessions = (procedures[0].get("sessions") or []) if procedures else []
                    actual = sessions[0].get("sessionDate") if sessions else None
                    expected = data.session_dates[0].strftime("%m-%d-%Y")
                    if self._normalize_backend_session_date(actual) != expected:
                        raise RuntimeError(
                            f"Refusing 1st Case Rate tagging: backend sessionDate is "
                            f"{actual!r}, expected {expected!r}."
                        )
                self._tag_first_case(page)

        self._step(
            "Tagging Surgical Procedure as 1st Case Rate...",
            _tag_first_case_api_or_ui,
            critical=True,
        )

        # The API path already emits a before/after diagnostic. If the UI
        # fallback was used, emit the equivalent post-tag backend check here.
        if not self._case_rate_tagged_via_api:
            self._diagnose_session_date_backend(page, data, "AFTER 1ST CASE TAG (UI)")

        if not self._fill_and_save_cf2(page, data):
            self._fill_referral_and_accommodation(page)
            self._fill_disposition_and_diagnosis(page)
            self._fill_benefits_and_fees(page, data)
            self._fill_access_patient_records_date(page, data)
            self._save_cf2(page)

        # Final diagnostic: check again after the CF2 save path. If the date
        # existed after tagging but is NULL here, the CF2 save path is the
        # point where it was lost.
        self._diagnose_session_date_backend(page, data, "AFTER CF2 SAVE")

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

    def _validate_eligibility_via_api(self, page, data):
        """Run Beacon's Validate Eligibility workflow without Playwright UI actions."""
        ids = self._get_ids(page)
        result = cf2_api.validate_claim_eligibility(
            claim_id=ids["claim_id"],
            transmittal_id=ids["transmittal_id"],
            admission_date=data.first_treatment,
            discharge_date=data.last_treatment,
        )
        if result.get("skipped"):
            print("Eligibility already validated in CF1 - skipping duplicate PBEF generation.")
            return True

        saved = result.get("saved") or {}
        print(
            "Eligibility validated via API: "
            f"status='{saved.get('claimStatusDescription')}', "
            f"eligibleAsOf='{saved.get('eligibleAsOf')}', "
            f"remainingDays='{saved.get('eligibilityRemainingDays')}'."
        )
        return True

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

    def _fill_and_save_cf2(self, page, data, persist_session_dates=False):
        """
        Combines what were 5 separate UI steps
        (_fill_referral_and_accommodation, _fill_disposition_and_diagnosis,
        _fill_benefits_and_fees, _fill_access_patient_records_date,
        _save_cf2) into a single GET -> merge -> PUT API call, confirmed
        via a full (uncut) HAR capture of a real CF2 save.

        Beacon expects the WHOLE CF2 record on every save, not a partial
        patch — fields this migration doesn't know about (newborn care,
        TB DOTS, animal bite, cataract, surgicalProcedures, audit
        fields, etc.) live in this same record. So this always starts
        from cf2_api.get_cf2()'s current state and only mutates the
        specific fields these UI functions used to set, leaving
        everything else exactly as the server returned it — the only
        way to do this safely without risking silently wiping data
        outside this migration's scope.

        Returns True on success (API path handled everything, caller
        should skip the UI fallback), False on any failure (caller
        should run the UI fallback instead).
        """
        try:
            ids = self._get_ids(page)
            cf2_record = cf2_api.get_cf2(ids["claim_id"])
            if not cf2_record:
                raise cf2_api.Cf2ApiError(
                    f"GetPHICCF2ById returned nothing for "
                    f"claimId={ids['claim_id']}."
                )

            # _fill_referral_and_accommodation
            cf2_record["isPatientReferred"] = "N"
            cf2_record["accomodationTypeCode"] = "P"
            cf2_record["accomodationTypeValue"] = "Private"

            # _check_and_select_patient_type: the UI dropdown for this
            # field is disabled for every hemodialysis claim this
            # automation handles, so the UI never actively sets it - it's
            # already "O - Outpatient" client-side and just isn't part of
            # what the UI submits. GetPHICCF2ById doesn't return that
            # default though, so echoing the record back leaves it null
            # and EditPHICCF2 rejects the save with "PatientTypeCode
            # field is required." Only fill it if it's actually missing -
            # never override a value Beacon already has on file.
            if not cf2_record.get("patientTypeCode"):
                cf2_record["patientTypeCode"] = "O"
                cf2_record["patientTypeValue"] = "Outpatient"

            # Admission stays at local midnight; discharge moves to
            # local noon (AM -> PM) — confirmed via HAR, see
            # to_utc_midnight_iso / to_utc_noon_iso docstrings.
            admission_iso = cf2_api.to_utc_midnight_iso(data.first_treatment)
            discharge_iso = cf2_api.to_utc_noon_iso(data.last_treatment)
            cf2_record["admissionDate"] = data.first_treatment.strftime("%m-%d-%Y")
            cf2_record["admissionDateTime"] = admission_iso
            cf2_record["admissionTime"] = admission_iso
            cf2_record["dischargeDate"] = data.last_treatment.strftime("%m-%d-%Y")
            cf2_record["dischargeDateTime"] = discharge_iso
            cf2_record["dischargeTime"] = discharge_iso

            # _fill_disposition_and_diagnosis
            # match the current UI code's behavior exactly.
            cf2_record["patientDispositionCode"] = "I"
            cf2_record["patientDispositionValue"] = "Improved"
            cf2_record["admissionDiagnosis"] = "CHRONIC KIDNEY DISEASE STAGE V"

            # _fill_benefits_and_fees
            fees = get_fees(data.total_sessions)
            cf2_record["doesPatientHasEnoughBenefits"] = "N"
            cf2_record["hospitalFeesActualCharges"] = f"{fees['hospital_actual']:.2f}"
            cf2_record["hospitalFeesAmountAfterDiscount"] = f"{fees['hospital_discount']:.2f}"
            cf2_record["hospitalFeesPhilHealthBenefit"] = f"{fees['hospital_discount']:.2f}"
            cf2_record["professionalFeesActualCharges"] = f"{fees['prof_actual']:.2f}"
            cf2_record["professionalFeesAmountAfterDiscount"] = f"{fees['prof_discount']:.2f}"
            cf2_record["professionalFeesPhilHealthBenefit"] = f"{fees['prof_discount']:.2f}"
            cf2_record["hospitalFeesDidPatientPay"] = "N"
            cf2_record["hospitalFeesPatientHasHMO"] = "N"
            cf2_record["hospitalFeesPatientHasOtherDeductions"] = "N"
            cf2_record["professionalFeesDidPatientPay"] = "N"
            cf2_record["professionalFeesPatientHasHMO"] = "N"
            cf2_record["professionalFeesPatientHasOtherDeductions"] = "N"
            cf2_record["purchasesWithDrugsMedSupplies"] = "N"
            cf2_record["purchasesWithExaminations"] = "N"

            print(
                f"Fees for {data.total_sessions} sessions: "
                f"hosp={fees['hospital_actual']}/{fees['hospital_discount']}, "
                f"prof={fees['prof_actual']}/{fees['prof_discount']}"
            )

            # _fill_access_patient_records_date
            cf2_record["aprDate"] = data.last_treatment.strftime("%m-%d-%Y")

            # GetPHICCF2ById does NOT embed surgicalProcedures (confirmed
            # via live debugging - see cf2_api.get_cf2()'s docstring),
            # but EditPHICCF2 requires it to be present or it 500s
            # unconditionally, regardless of every other field. Fetch it
            # separately and reshape it to match a real captured working
            # save payload before sending.
            cf2_record["surgicalProcedures"] = (
                cf2_api.build_surgical_procedures_for_cf2(ids["claim_id"])
            )

            # When requested, persist session dates through the full EditPHICCF2
            # payload. This mirrors Beacon's successful manual request: the
            # session object keeps its id/session metadata and only sessionDate
            # is changed. Do NOT try to persist this through NewPHICAllCaseRate;
            # that endpoint returns a case-rate record and does not return or
            # reliably store sessions[].sessionDate.
            if persist_session_dates and data.session_dates:
                procedures = cf2_record.get("surgicalProcedures") or []
                if not procedures:
                    raise RuntimeError("EditPHICCF2 payload contains no surgicalProcedures.")

                if len(procedures) < 1:
                    raise RuntimeError("No Surgical Procedure available for session-date persistence.")

                for proc_index, procedure in enumerate(procedures):
                    sessions = procedure.get("sessions") or []
                    if not sessions:
                        raise RuntimeError(
                            f"Surgical Procedure #{proc_index + 1} has no session objects "
                            "in the EditPHICCF2 payload; refusing to invent session IDs."
                        )
                    for session_index, session in enumerate(sessions):
                        if session_index >= len(data.session_dates):
                            break
                        session["sessionDate"] = data.session_dates[session_index].strftime("%m-%d-%Y")
                        print(
                            f"  API Session {session_index + 1}: "
                            f"sessionDate={session['sessionDate']} "
                            f"(session id={session.get('id')})"
                        )

                print("Saving session date(s) through EditPHICCF2 API...")

            cf2_api.edit_cf2(cf2_record)
            print("CF2 form fields filled and saved (via API).")

            if persist_session_dates and data.session_dates:
                # Verify the value returned by Beacon, not merely the request
                # body. A successful HTTP response alone is insufficient.
                verify_procedures = cf2_api.get_surgical_procedures(ids["claim_id"])
                expected_dates = [d.strftime("%m-%d-%Y") for d in data.session_dates]
                actual_dates = []
                for procedure in verify_procedures or []:
                    for session in (procedure.get("sessions") or []):
                        actual_dates.append(session.get("sessionDate"))

                actual_dates = actual_dates[:len(expected_dates)]
                print("===== SESSION DATE API VERIFICATION (EditPHICCF2) =====")
                for idx, expected in enumerate(expected_dates):
                    actual = actual_dates[idx] if idx < len(actual_dates) else None
                    print(
                        f"  Session {idx + 1}: expected={expected!r}, "
                        f"backend={actual!r}"
                    )
                if [self._normalize_backend_session_date(v) for v in actual_dates] != expected_dates:
                    raise RuntimeError(
                        f"EditPHICCF2 returned session dates {actual_dates!r}; "
                        f"expected {expected_dates!r}."
                    )
                print("RESULT: SESSION DATE(S) PERSISTED AND VERIFIED BY BACKEND API.")
                print("========================================================")

            # Safe to reload here (unlike the earlier Discharge
            # Diagnosis / Surgical Procedure / Doctor steps): this is
            # the LAST thing typed into the CF2 tab before moving on to
            # Statement of Account, which reads its own data via
            # separate API calls, not this page's DOM — so there's no
            # unsaved input left to lose. Reloading just lets the
            # visible browser catch up for visual reference.
            page.reload(wait_until="networkidle")
            return True
        except Exception as e:
            print(
                f"WARNING: API path for CF2 field save failed ({e}) - "
                f"falling back to UI automation."
            )
            return False

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
        # --- API-first path -------------------------------------------
        # Confirmed via HAR: GetPHICDischargeDiagnoses (empty = guard
        # check), SearchICD10, NewPHICDischargeDiagnosis,
        # EditPrimaryPHICDischargeDiagnosis. Replaces the DOM row-count
        # guard AND the kebab-menu "Set as Primary" click below in one
        # shot. Any failure here (missing token, unexpected response
        # shape, HTTP error) falls straight through to the exact same
        # UI automation this always used - never left half-done.
        try:
            ids = self._get_ids(page)
            existing = cf2_api.get_discharge_diagnoses(ids["claim_id"])
            if existing:
                print(
                    "Discharge Diagnosis already has a row (via API) - "
                    "skipping to avoid duplicating it."
                )
                return
            cf2_api.add_discharge_diagnosis_n18_5(ids["claim_id"])
            print("Discharge Diagnosis added and set as Primary (via API).")
            # No reload here — fill_cf2() does a single reload after
            # Discharge Diagnosis, Surgical Procedure, and Doctor have
            # all been attempted, not after each one individually. That
            # keeps this call's own success/failure independent of the
            # others while still avoiding the earlier bug where a reload
            # here wiped out already-typed CF2 fields (this now always
            # runs before any of those fields are typed).
            # _discharge_diagnosis_done_via_api is still checked BEFORE
            # any DOM-based guard downstream (see _set_primary_diagnosis)
            # so that skip logic works correctly even in the window
            # before the group reload happens.
            self._discharge_diagnosis_done_via_api = True
            return
        except Exception as e:
            print(
                f"WARNING: API path for Discharge Diagnosis failed "
                f"({e}) - falling back to UI automation."
            )

        # --- UI fallback -------------------------------------------
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

        # UI fallback still needs "Set as Primary" done separately -
        # the API path handled this atomically above, but the UI path
        # never did (fill_cf2() calls _set_primary_diagnosis as its own
        # step right after this one, unchanged).
        

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
        if self._discharge_diagnosis_done_via_api:
            print(
                "Discharge Diagnosis was already set as Primary via "
                "API - skipping."
            )
            return

        if self._click_diagnosis_kebab(page, confirm_menu_text="Set as Primary"):
            page.get_by_text("Set as Primary", exact=True).click(force=True)
            page.wait_for_timeout(500)
        else:
            raise RuntimeError("Could not open diagnosis kebab menu.")

    def _add_surgical_procedure(self, page, data):
        # --- API-first path -------------------------------------------
        # Confirmed via HAR: GetPHICSurgicalProcedure (empty = guard
        # check), NewPHICSurgicalProcedure, GetPHICAllCaseRates (empty
        # = not tagged yet), SearchCaseRates (eclaimsapi),
        # NewPHICSurgicalProcedure creates the procedure. Session dates are
        # persisted separately through the full EditPHICCF2 payload before
        # NewPHICAllCaseRate is called for 1st Case Rate tagging.
        #
        # Restricted to data.total_sessions == 1: the only case we have
        # a captured, confirmed example for. We don't know whether
        # NewPHICAllCaseRate accepts multiple sessions with different
        # dates in one call or needs one call per session, so anything
        # with more than 1 session falls straight through to the
        # existing UI automation rather than guessing.
        try:
            ids = self._get_ids(page)
            existing_procedures = cf2_api.get_surgical_procedures(ids["claim_id"])

            if existing_procedures:
                print(
                    "Surgical Procedure already exists (via API) - skipping creation."
                )
                self._surgical_procedure_created_via_api = True
                return

            icd10_matches = cf2_api.search_icd10("N18.5")
            if not icd10_matches:
                raise cf2_api.Cf2ApiError("SearchICD10('N18.5') returned no matches.")
            procedure = cf2_api.new_surgical_procedure(
                ids["claim_id"],
                icd10_matches[0]["icD10Code"],
                icd10_matches[0]["icD10Value"],
                data.total_sessions,
            )
            self._surgical_procedure_created_via_api = True
            print("Surgical Procedure created (via API).")
            return
        except Exception as e:
            print(
                f"WARNING: API path for Surgical Procedure creation failed ({e}) - "
                f"falling back to UI automation."
            )

        # --- UI fallback -------------------------------------------
        if self._surgical_procedure_created_via_api:
            # NewPHICSurgicalProcedure already succeeded above before
            # something after it failed (session-date search or 1st
            # Case Rate tagging) — the procedure record genuinely
            # exists on the backend. Reload so the DOM guard right
            # below can actually see it (it was never rendered in this
            # tab, since it was created via a raw API call, not a UI
            # click) and correctly skip straight past procedure
            # creation, instead of concluding "RVS CODE" isn't visible
            # and clicking NEW — which would create a duplicate
            # procedure record. The remaining session-date / 1st Case
            # Rate steps then proceed normally via the UI.
            print(
                "Surgical Procedure was already created via API before "
                "the failure above — reloading to avoid creating a "
                "duplicate."
            )
            page.reload(wait_until="networkidle")

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

    @staticmethod
    def _normalize_backend_session_date(value):
        """Normalize Beacon sessionDate values for date-only comparison.

        Beacon may return the same persisted date as either MM-DD-YYYY or
        an ISO datetime such as 2026-07-01T00:00:00. Compare calendar dates,
        not their raw string representations.
        """
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        for fmt in ("%m-%d-%Y", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
            try:
                return datetime.strptime(text, fmt).strftime("%m-%d-%Y")
            except ValueError:
                continue
        # Handle ISO timestamps with timezone/offset by using only the date
        # portion, which is how Beacon represents this field in its API.
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%m-%d-%Y")
        except ValueError:
            return text[:10]

    def _diagnose_session_date_backend(self, page, data, stage):
        """Best-effort diagnostic: print Beacon's persisted sessionDate.

        This deliberately does not modify anything. It distinguishes a
        Playwright-only DOM value from a value actually returned by
        GetPHICSurgicalProcedure, and helps identify whether a later
        operation wipes the session date.
        """
        try:
            ids = self._get_ids(page)
            procedures = cf2_api.get_surgical_procedures(ids["claim_id"])
            print(f"===== SESSION DATE BACKEND DIAGNOSTIC: {stage} =====")
            if not procedures:
                print("  Surgical Procedure records returned: 0")
                print("  Session 1 backend sessionDate: <NO PROCEDURE>")
                print("=======================================================")
                return

            for proc_index, procedure in enumerate(procedures, start=1):
                sessions = procedure.get("sessions") or []
                print(
                    f"  Surgical Procedure #{proc_index}: "
                    f"id={procedure.get('id')}, "
                    f"numberOfSessions={procedure.get('numberOfSessions', len(sessions))}"
                )
                if not sessions:
                    print("    sessions: []")
                    continue
                for session_index, session in enumerate(sessions, start=1):
                    print(
                        f"    Session {session_index} backend sessionDate: "
                        f"{session.get('sessionDate')!r}"
                    )

            expected = (
                data.session_dates[0].strftime("%m-%d-%Y")
                if data.session_dates
                else "<NONE>"
            )
            actual = None
            first_sessions = procedures[0].get("sessions") or []
            if first_sessions:
                actual = first_sessions[0].get("sessionDate")

            print(f"  Expected session 1 date from CF2 data: {expected!r}")
            print(f"  Actual session 1 backend date: {actual!r}")
            if self._normalize_backend_session_date(actual) == expected:
                print("  DIAGNOSTIC RESULT: BACKEND DATE MATCHES EXPECTED DATE.")
            elif actual:
                print(
                    "  DIAGNOSTIC RESULT: BACKEND HAS A DATE, BUT IT DOES NOT "
                    "MATCH EXPECTED."
                )
            else:
                print("  DIAGNOSTIC RESULT: BACKEND SESSION DATE IS EMPTY/NULL.")
            print("=======================================================")
        except Exception as e:
            print(
                f"WARNING: Session date backend diagnostic failed at {stage}: {e}"
            )

    def _tag_first_case_via_api(self, page, ids, data):
        """
        Wires up cf2_api.tag_first_case() - it already existed and was
        fully working, just never called from here. Restricted to
        data.total_sessions == 1, matching the same restriction
        _add_surgical_procedure's API path already documents (the only
        case we have a confirmed example for).

        IMPORTANT ORDERING: must only be called AFTER session dates
        have actually been entered (_fill_session_dates) - confirmed
        in Beacon itself that tagging as 1st Case Rate disables the
        session date field and no date gets persisted if tagging
        happens first. So this always re-fetches the current surgical
        procedure record fresh (rather than being handed one from
        procedure-creation time), to make sure it reflects whatever
        session date state actually exists on the backend right now.

        Self-contained: catches its own exceptions and never raises,
        so a tagging failure is reported as its own warning rather than
        bubbling up as some other step's failure.
        """
        if data.total_sessions != 1:
            print(
                "More than 1 session - 1st Case Rate tagging via API "
                "not yet confirmed for this case, leaving it to the "
                "UI steps below."
            )
            return False

        try:
            if cf2_api.get_case_rates(ids["claim_id"]):
                print(
                    "Surgical Procedure already tagged as 1st Case Rate "
                    "(via API) - skipping."
                )
                self._case_rate_tagged_via_api = True
                return True

            procedures = cf2_api.get_surgical_procedures(ids["claim_id"])
            if not procedures:
                print(
                    "No Surgical Procedure found - can't tag via API, "
                    "leaving it to the UI steps below."
                )
                return False
            surgical_procedure = procedures[0]

            hospital_identity = cf2_api.get_hospital_identity(ids["transmittal_id"])
            session_date = data.session_dates[0]
            target_date_str = session_date.strftime("%m-%d-%Y")

            case_rate_response = cf2_api.search_case_rates(
                "90935", target_date_str, hospital_identity
            )
            case_rate = case_rate_response["caserates"][0]

            # Diagnostic: the Playwright input check only proves the date is
            # present in the browser. Read Beacon's backend immediately
            # before tagging to see whether the date was actually persisted.
            self._diagnose_session_date_backend(page, data, "BEFORE 1ST CASE TAG")

            cf2_api.tag_first_case(
                ids["claim_id"], surgical_procedure, case_rate, session_date
            )
            print("Surgical Procedure tagged as 1st Case Rate (via API).")

            # Diagnostic: NewPHICAllCaseRate should NOT be responsible for the
            # session date. Verify that the date persisted by EditPHICCF2 remains
            # intact after tagging. If it disappears, abort instead of falling
            # back to UI and creating a tagged procedure with a NULL date.
            procedures_after = cf2_api.get_surgical_procedures(ids["claim_id"])
            sessions_after = (procedures_after[0].get("sessions") or []) if procedures_after else []
            actual_after = sessions_after[0].get("sessionDate") if sessions_after else None
            expected_after = data.session_dates[0].strftime("%m-%d-%Y")
            self._diagnose_session_date_backend(page, data, "AFTER 1ST CASE TAG")
            if self._normalize_backend_session_date(actual_after) != expected_after:
                raise RuntimeError(
                    f"1st Case Rate tagging changed/lost sessionDate: "
                    f"expected {expected_after!r}, got {actual_after!r}."
                )
            self._case_rate_tagged_via_api = True
            return True
        except Exception as e:
            print(
                f"WARNING: API path for 1st Case Rate tagging failed ({e})."
            )
            return False

    def _fill_claim_form_two_via_api(self, claim_id, data):
        """
        Save the Claim Form 2 PDF signature block entirely through Beacon's
        API. No Claim Form 2 UI page is opened and no Playwright interaction
        is used for these fields.

        Beacon's GET /api/PHICDocument/GetCf2PdfDetails endpoint is used only
        to obtain the complete current PDF data object. The Part III patient/member
        name fields and Part IV HCI representative fields are then changed, and
        the complete object is submitted
        to POST /api/PHICDocument/NewPdfClaimFormTwo.

        IMPORTANT: GetCf2PdfDetails is NOT used as a post-save verification
        source. A real successful save returned HTTP 200 from
        NewPdfClaimFormTwo, while a subsequent GetCf2PdfDetails continued to
        return some rendered PDF fields as null. The authoritative
        verification for this endpoint is the generated Claim Form 2 PDF,
        which was observed containing the submitted signature/designation/date.

        NewPdfClaimFormTwo returns an empty JSON response body on success, so
        _post() treating the successful response as None is expected.
        """
        client_id = cf2_api.get_client_id()
        base = cf2_api.get_cf2_pdf_details(claim_id, client_id)
        if not isinstance(base, dict) or not base:
            raise cf2_api.Cf2ApiError(
                f"GetCf2PdfDetails returned no usable data for claimId={claim_id}."
            )

        # Part III patient/member signature name must come from Beacon's
        # authoritative CF1 record, not from the uploaded automation data.
        cf1_summary = cf2_api.get_cf1_summary(claim_id)
        patient_fullname = str(
            (cf1_summary or {}).get("patientFullname") or ""
        ).strip()
        if not patient_fullname:
            raise cf2_api.Cf2ApiError(
                f"GetPHICCF1Summary returned no patientFullname "
                f"for claimId={claim_id}."
            )

        billing_clerk_name = self._get_billing_clerk_name()
        designation = self._get_official_capacity_designation()

        # NewPdfClaimFormTwo renders the timestamp in Philippine local time.
        # To display the claim's local calendar date (e.g. 07-01-2026), send
        # local midnight as the previous UTC day at 16:00, without milliseconds.
        date_signed_iso = cf2_api.to_utc_midnight_iso(
            data.last_treatment
        ).replace(".000Z", "Z")

        # Part III - member/patient/authorized representative.
        base["sigOverPrintedNameOfAuthRep"] = patient_fullname
        base["patientFullname"] = patient_fullname

        # Part IV - authorized HCI representative.
        base["signatureOverPrintedNameOfAuthHCIRep"] = billing_clerk_name
        base["officialCapacityDesignation"] = designation
        base["dateSigned"] = date_signed_iso

        print("Saving Claim Form 2 PDF fields via API...")
        print(
            "  Part III: "
            f"patientFullname='{patient_fullname}', "
            f"sigOverPrintedNameOfAuthRep='{patient_fullname}'"
        )
        print(
            "  Part IV: "
            f"signature='{billing_clerk_name}', "
            f"designation='{designation}', "
            f"dateSigned='{date_signed_iso}'"
        )
        cf2_api.new_pdf_claim_form_two(claim_id, base)
        print("Claim Form 2 PDF fields saved via API.")
        return True

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
        # Guard: check if surgical procedures exist on the page
        sp_anchor = page.get_by_text("RVS CODE", exact=True).first
        try:
            sp_anchor.wait_for(state="visible", timeout=10000)
        except Exception:
            if sp_anchor.count() == 0:
                print("No surgical procedures found on page - skipping session date entry.")
                return

        # Guard: every session date must fall within the claim's own
        # Admission Date / Discharge Date range. Read both straight
        # from the page (rather than trusting the uploaded record
        # alone), since that's the actual constraint Beacon enforces —
        # catching a mismatch here, before typing anything, is much
        # clearer than letting it surface later at save time.
        admission_input = page.locator('input[id^="admissionDate-"]').first
        discharge_input = page.locator('input[id^="dischargeDate-"]').first

        admission_raw = admission_input.input_value().strip() if admission_input.count() > 0 else ""
        discharge_raw = discharge_input.input_value().strip() if discharge_input.count() > 0 else ""

        try:
            admission_date = datetime.strptime(admission_raw, "%m-%d-%Y").date() if admission_raw else None
            discharge_date = datetime.strptime(discharge_raw, "%m-%d-%Y").date() if discharge_raw else None
        except ValueError as e:
            print(f"Warning: Could not parse Admission/Discharge date ({e})")
            admission_date = None
            discharge_date = None

        if admission_date and discharge_date:
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

        print(f"Checking/Filling {len(data.session_dates)} session dates...")
        for i, session_date in enumerate(data.session_dates):
            date_str = session_date.strftime("%m%d%Y")

            def _fill_one(i=i, date_str=date_str):
                date_input = page.locator(
                    f'input[id^="surgicalProcedures0sessions{i}sessionDate-Date"]'
                ).first
                if date_input.count() == 0:
                    print(f"  Session {i + 1} date input not found on page.")
                    return
                date_input.scroll_into_view_if_needed()
                current_val = date_input.input_value().strip()
                if current_val:
                    print(f"  Session {i + 1} date already present: '{current_val}' - skipping.")
                    return
                print(f"  Session {i + 1} date is empty - entering {date_str}...")
                # Explicitly clear before typing — matches
                # _fill_access_patient_records_date's proven approach.
                # A freshly-reloaded, API-created session row's date
                # input isn't necessarily in the same clean state as
                # one built entirely through the UI; typing straight
                # after a bare click (no clear) was observed to
                # silently fail to register on this field.
                date_input.click()
                page.wait_for_timeout(300)
                date_input.press("Control+A")
                page.wait_for_timeout(100)
                date_input.press("Delete")
                page.wait_for_timeout(200)
                date_input.type(date_str, delay=150)
                date_input.press("Tab")
                page.wait_for_timeout(300)

                # Verify it actually landed instead of discovering a
                # silent failure later via a generated PDF's "INVALID
                # DATE". Non-fatal — just makes the next failure
                # diagnosable from the log instead of a mystery.
                after_val = date_input.input_value().strip()
                if not after_val:
                    print(
                        f"  WARNING: Session {i + 1} date still empty "
                        f"after typing {date_str} — it did not register."
                    )
                else:
                    print(f"  Session {i + 1} date now shows: '{after_val}'.")

            # Non-critical: a single bad session date shouldn't sink the whole patient.
            self._step(f"  Session {i + 1}: {date_str}", _fill_one, critical=False)
            page.wait_for_timeout(300)

    def _tag_first_case(self, page):
        if self._case_rate_tagged_via_api:
            print("Already tagged as 1st Case Rate (via API) - skipping.")
            return

        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(500)

        if self._first_case_tag_present(page):
            print("Surgical Procedure is already tagged as 1st Case Rate - skipping.")
            return

        sp_anchor = page.get_by_text("RVS CODE", exact=True).first
        sp_anchor.wait_for(state="visible", timeout=10000)
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
        # --- API-first path -------------------------------------------
        # Confirmed via HAR: GetAllPHICDoctorByClaimId (empty = guard
        # check), GetDoctorByAccreditationNumber (what "Autofill Doctor
        # Information" triggers today), IsDoctorAccredited (eclaimsapi),
        # NewPHICDoctor.
        try:
            ids = self._get_ids(page)
            existing_doctors = cf2_api.get_doctors(ids["claim_id"])
            if existing_doctors:
                print(
                    "Doctors already has a row (via API) - skipping to "
                    "avoid duplicating it."
                )
                return

            client_id = cf2_api.get_client_id()
            hospital_identity = cf2_api.get_hospital_identity(ids["transmittal_id"])

            # NOTE: unlike the UI path (which strips this to digits-only
            # before typing into the search field), the API expects the
            # accreditation number in its normal dashed form
            # ("1202-2154632-0") - confirmed via HAR. Passed through
            # as-is from data, on the assumption it's already in that
            # form in the source spreadsheet; worth double-checking on
            # the first real test.
            accreditation_number = data.accreditation_no.strip()

            admission_date_str = data.first_treatment.strftime("%m-%d-%Y")
            discharge_date_iso = cf2_api.to_utc_midnight_iso(data.last_treatment)
            sign_date_str = data.last_treatment.strftime("%m-%d-%Y")

            cf2_api.add_doctor(
                ids["claim_id"],
                client_id,
                accreditation_number,
                sign_date_str,
                admission_date_str,
                discharge_date_iso,
                hospital_identity,
            )

            print("Doctor added (via API).")
            # No reload here (removed) - same reasoning as
            # _add_discharge_diagnosis / _add_surgical_procedure: this
            # runs in the middle of fill_cf2(), after
            # _fill_referral_and_accommodation and
            # _fill_disposition_and_diagnosis have already typed into
            # other CF2 fields, and before _save_cf2() actually
            # persists them. A reload here would wipe that unsaved
            # input the same way. Nothing downstream (benefits/fees,
            # access-records date, the CF2 save itself) reads Doctor
            # DOM state, so there's no guard relying on a refreshed
            # page here either.
            return
        except Exception as e:
            print(
                f"WARNING: API path for Doctor failed ({e}) - falling "
                f"back to UI automation."
            )

        # --- UI fallback -------------------------------------------
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
        """Save the Statement of Account Signatories entirely through API.

        The browser UI is no longer used to populate or save individual
        Signatories fields. We first read the member mobile from CF1, then
        read the existing eSOA signatory record to obtain its ``id``, build
        the same payload Beacon sends from the Signatories SAVE button, POST
        it to Update-signatories, and finally GET the record again to verify
        the values persisted.
        """
        ids = self._get_ids(page)
        claim_id = ids["claim_id"]

        # _open_claim_form_2() below reassigns the `page` name (nonlocal)
        # to the new Claim Form 2 tab once it opens. Keep a handle on the
        # original Claim Forms list tab here so _close_claim_form_2_tab()
        # can switch back to it later - reassigning `page` doesn't create
        # a second variable, so without this the original tab reference
        # is lost the moment the new tab opens.
        original_page = page

        # Member mobile is already API-based in this version of the
        # automation; keep that implementation unchanged.
        member_mobile = None

        def _get_member_mobile_from_api():
            nonlocal member_mobile
            cf1_summary = cf2_api.get_cf1_summary(claim_id)
            member_mobile = (cf1_summary or {}).get("memberMobileNumber")
            if not member_mobile:
                raise cf2_api.Cf2ApiError(
                    f"GetPHICCF1Summary returned no memberMobileNumber "
                    f"for claimId={claim_id}."
                )
            member_mobile = str(member_mobile).strip()
            print(f"Member mobile from CF1 API: {member_mobile}")

        self._step(
            "Getting member mobile from CF1 API...",
            _get_member_mobile_from_api,
            critical=True,
        )

        billing_clerk_name = self._get_billing_clerk_name()
        billing_clerk_cp = self._get_billing_clerk_cp()
        date_str = data.last_treatment.strftime("%m-%d-%Y")

        signatories = cf2_api.get_esoa_signatories(claim_id)
        if not signatories:
            raise cf2_api.Cf2ApiError(
                f"GetEsoaSignatories returned no record for phicClaimId={claim_id}."
            )

        signatory_id = signatories.get("id")
        if signatory_id is None:
            raise cf2_api.Cf2ApiError(
                f"GetEsoaSignatories returned no id for phicClaimId={claim_id}."
            )

        payload = {
            "phicClaimId": claim_id,
            "id": signatory_id,
            "preparedBy": billing_clerk_name,
            "adminContactNo": billing_clerk_cp,
            "adminDateSigned": date_str,
            "patientRepresentative": data.patient_name,
            "relationshipOfRepresentative": "",
            "representativeContactNo": member_mobile,
            "representativeDateSigned": date_str,
        }

        print("Updating Statement of Account Signatories via API...")
        cf2_api.update_esoa_signatories(payload)
        print("Statement of Account Signatories saved via API.")

        # Verify the persisted backend record rather than relying on DOM
        # values. This also catches a successful HTTP response that did not
        # actually persist one of the submitted fields.
        saved = cf2_api.get_esoa_signatories(claim_id)
        if not saved:
            raise cf2_api.Cf2ApiError(
                f"GetEsoaSignatories returned no record after save for "
                f"phicClaimId={claim_id}."
            )

        expected = {
            "preparedBy": billing_clerk_name,
            "adminContactNo": billing_clerk_cp,
            "patientRepresentative": data.patient_name,
            "representativeContactNo": member_mobile,
        }
        for field, expected_value in expected.items():
            actual_value = str(saved.get(field) or "").strip()
            if actual_value != str(expected_value or "").strip():
                raise cf2_api.Cf2ApiError(
                    f"Signatories verification failed for {field}: "
                    f"expected '{expected_value}', got '{actual_value}'."
                )

        print("Statement of Account Signatories verified via API.")

        # Claim Form 2 PDF signature block is now fully API-based.
        # Do NOT open the Claim Forms page / CF2 PDF tab here.
        self._step(
            "Saving Claim Form 2 PDF fields via API...",
            lambda: self._fill_claim_form_two_via_api(claim_id, data),
            critical=True,
        )
        print("Claim Form 2 completed via API — no Claim Form 2 UI was opened.")
