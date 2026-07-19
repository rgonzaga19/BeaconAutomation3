import os
import re
import sys
import openpyxl
from cf2_mapper import build_cf2_data
import browser_session
from beacon import open_transmittals
from cf2_fees import get_fees
from draft_automation import run_create_draft_flow, try_extract_transmittal_number
from draft_title import build_draft_title
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError




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


class CF2Automation:

    def __init__(self, uploaded_excel_path=None):
        self.page = browser_session.connect()
        # Path to the workbook the user actually uploaded. Sheet2 (Billing
        # Clerk / Accountant name, contact no., official capacity) must be
        # read from THIS file, not from the bundled CF2_TEMPLATE_PATH.
        self.uploaded_excel_path = uploaded_excel_path or CF2_TEMPLATE_PATH
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

        self._create_draft(page, data)

        # Confirmed via live test: once Add Claims validation completes,
        # the browser is already sitting on the PHIC Claims Details page
        # itself (CF1/CF2 tabs, admission/discharge dates pre-filled) — not
        # a claims list with rows to click into. _open_patient() and
        # _open_manage_claims() are both unnecessary here now.

        self._step(
            "Checking for Validate Eligibility button...",
            lambda: self._validate_eligibility(page),
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
    # Legacy — none of these three are called from fill_cf2() anymore
    # (kept for reference / a possible manual fallback where a transmittal
    # already exists and just needs to be located by number instead of
    # created fresh)
    # ------------------------------------------------------------------
    def _open_and_search_transmittal(self, page, data):
        print("Opening Transmittals...")
        open_transmittals(page)
        page.wait_for_load_state("networkidle")

        print(f"Searching: {data.transmittal}")
        search_box = page.locator('input[type="text"]').first
        search_box.click()
        search_box.press("Control+A")
        search_box.press("Backspace")
        search_box.fill(data.transmittal)
        search_box.press("Enter")

        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2000)

        if page.locator("tbody tr").count() == 0:
            print("Transmittal not found.")
            return False
        return True

    def _open_manage_claims(self, page):
        def _open():
            first_row = page.locator("tbody tr").first
            first_row.locator("button").last.click()
            page.wait_for_timeout(500)
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
            print("No validation required — skipping.")

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
        self._step(
            "Clicking NEW (Surgical Procedure)...",
            lambda: page.locator("#aTesting-newSurgicalProcedure").click(),
            critical=True,
        )
        page.wait_for_timeout(500)

        def _fill_rvs():
            rvs_input = page.get_by_label("RVS Code")
            rvs_input.click()
            rvs_input.type("90935", delay=100)
            page.wait_for_timeout(1000)
            page.get_by_text("90935", exact=True).last.click()

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

    def _fill_session_dates(self, page, data):
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
        page.wait_for_timeout(1500)

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
                    f"expected '{expected_display}' — retrying..."
                )
                page.wait_for_timeout(300)

            # Still wrong after all retries — log it and let the caller continue
            # rather than sinking the whole patient over a cosmetic date field.
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

            page.locator('#cf2Save').get_by_role("button").last.click()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1000)

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

        def _fill_prepared_by():
            billing_clerk_name = self._get_billing_clerk_name()
            prepared_by_input = page.locator('input[name="preparedBy"]')
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
            billing_clerk_cp = self._get_billing_clerk_cp()

            contact_input = page.locator('input[name="adminContactNo"]')

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

        def _fill_signatories():
            admin_date = page.locator('input[id^="adminDateSigned-MM-DD-YYYY"]')
            admin_date.click()
            admin_date.type(date_str, delay=100)
            admin_date.press("Tab")
            page.wait_for_timeout(300)

            page.locator('input[name="patientRepresentative"]').fill(data.patient_name)
            page.wait_for_timeout(300)

            conforme_date = page.locator('input[id^="representativeDateSigned-MM-DD-YYYY"]')
            conforme_date.click()
            page.wait_for_timeout(200)
            conforme_date.type(date_str, delay=100)

        self._step("Filling Signatories...", _fill_signatories, critical=True)
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
            nonlocal page

            card = page.locator('a[href*="download-pdf/cf2"]').locator("..")

            card.hover()

            with page.context.expect_page() as new_page_info:
                page.locator(
                    'a[href*="download-pdf/cf2"]'
                ).click()

            page = new_page_info.value
            page.wait_for_load_state("networkidle")

        self._step(
            "Opening Claim Form 2...",
            _open_claim_form_2,
            critical=True,
        )

        page.wait_for_timeout(500)

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

        self._step(
            "Filling Signature Over Printed Name of Authorized HCI Representative...",
            _fill_signature_over_printed_name,
            critical=True,
        )

        page.wait_for_timeout(300)

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

        self._step(
            "Filling Official Capacity/Designation...",
            _fill_official_capacity_designation,
            critical=True,
        )

        page.wait_for_timeout(300)

        self._step(
            "Setting Part IV Date Signed...",
            lambda: self._set_part_iv_date_signed(page, data.last_treatment),
            critical=True,
        )

        page.wait_for_timeout(500)

        def _save_claim_form_2():
            page.get_by_role(
                "button",
                name="SAVE",
                exact=True
            ).click()

        self._step(
            "Saving CF2...",
            _save_claim_form_2,
            critical=True,
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

    def _set_part_iv_date_signed(self, page, target_date):
        """Set Part IV Date Signed."""

        from datetime import datetime

        target_month = target_date.strftime("%B %Y")
        target_day = str(target_date.day)

        # Open calendar
        page.locator("button.pdfIndicator").last.click()
        page.wait_for_timeout(500)

        while True:
            header = page.locator(
                "text=/^[A-Za-z]+\\s+\\d{4}$/"
            ).first.inner_text().strip()

            current = datetime.strptime(header, "%B %Y")
            wanted = datetime.strptime(target_month, "%B %Y")

            if current.year == wanted.year and current.month == wanted.month:
                break

            nav_buttons = page.locator("div[role='dialog'] button")

            if current < wanted:
                nav_buttons.nth(1).click()
            else:
                nav_buttons.nth(0).click()

            page.wait_for_timeout(250)

        page.get_by_role(
            "button",
            name=target_day,
            exact=True
        ).click()

        page.wait_for_timeout(300)

        page.get_by_role("button", name="OK").click()

        page.wait_for_timeout(300)