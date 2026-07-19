import os
import sys



if getattr(sys, "frozen", False):
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(
        os.path.dirname(sys.executable),
        "ms-playwright"
    )


from logger import logger
from reports import report

from pathlib import Path
import browser_session


def open_transmittals(page):
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

def run(transmittals, auto_encode_cf4=False):
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
                search_box = page.locator('input[type="text"]').first
                search_box.click()
                search_box.press("Control+A")
                search_box.press("Backspace")
                search_box.fill(transmittal_no)
                search_box.press("Enter")
                page.wait_for_load_state("networkidle")

                page.wait_for_timeout(2000)

                if page.locator("tbody tr").count() == 0:
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
                page.wait_for_load_state("networkidle")

                logger.info("Clicking Manage Claims...")
                page.get_by_text("Manage Claims", exact=True).click()
                page.wait_for_load_state("networkidle")
                logger.success("SUCCESS: Manage Claims opened")

                # ── Open claim → Manage ────────────────────────────────────
                logger.info("Opening claim row menu...")
                claim_row = page.locator("tbody tr").first
                claim_row.locator("button").last.click()
                page.wait_for_load_state("networkidle")

                logger.info("Clicking Manage...")
                page.get_by_text("Manage", exact=True).click()
                page.wait_for_load_state("networkidle")
                logger.success("SUCCESS: PHIC Claim Details opened")

                # --------------------------------------------------
                # Validate Eligibility (if button appears)
                # --------------------------------------------------
                validate_btn = page.locator("button", has_text="Validate Eligibility")

                if validate_btn.count() > 0:
                    print("Clicking Validate Eligibility...")
                    validate_btn.first.click()
                    page.wait_for_load_state("networkidle")
                else:
                    print("No validation required — skipping.")
        
                # ── Move to CF4 ────────────────────────────────────────────
                logger.info("Opening CF4 tab...")
                page.get_by_text("CF4", exact=True).click()
                page.wait_for_load_state("networkidle")

                logger.info("Clicking MOVE TO CF4...")
                page.get_by_role("button", name="MOVE TO CF4").click()

                logger.info("Waiting for confirmation dialog...")
                page.wait_for_selector("text=Proceed", timeout=10000)
                page.locator("text=Proceed").first.click(force=True)
                page.wait_for_load_state("networkidle")
                logger.success("SUCCESS: Moved to CF4")

                # ── Auto Encode CF4 (test) ──────────────────────────────────
                if auto_encode_cf4:
                    logger.info("Auto Encode CF4 option is enabled.")

                    def _set_chief_complaint():
                        box = page.locator('textarea[name="chiefComplaint"]').first
                        box.click()
                        box.press("Control+A")
                        box.press("Backspace")
                        box.fill("FOR HEMODIALYSIS")

                    _try_step("Chief Complaint set to 'FOR HEMODIALYSIS'", _set_chief_complaint)

                    _try_step(
                        "Checked Body Weakness",
                        lambda: page.locator('input[name="bodyWeakness"]').check(force=True)
                    )
                    _try_step(
                        "Checked Lower Extremity Edema",
                        lambda: page.locator('input[name="lowerExtremityEdema"]').check(force=True)
                    )

                    _try_step(
                        "General Survey set to 'Awake and alert'",
                        lambda: page.get_by_text("Awake and alert", exact=True).click(force=True)
                    )

                    _try_step(
                        "HEENT marked Essentially normal",
                        lambda: page.locator('input[name="heEssentiallyNormal"]').check(force=True)
                    )
                    _try_step(
                        "CHEST/LUNGS marked Essentially normal",
                        lambda: page.locator('input[name="clEssentiallyNormal"]').check(force=True)
                    )
                    _try_step(
                        "CVS marked Essentially normal",
                        lambda: page.locator('input[name="cvEssentiallyNormal"]').check(force=True)
                    )
                    _try_step(
                        "ABDOMEN marked Essentially normal",
                        lambda: page.locator('input[name="abEssentiallyNormal"]').check(force=True)
                    )

                    _try_step(
                        "Checked GU (IE) Others",
                        lambda: page.locator('input[name="guOthersChk"]').check(force=True)
                    )

                    def _fill_gu_others():
                        gu_others_input = page.locator('input[name="guOthers"]').first
                        gu_others_input.click()
                        gu_others_input.press("Control+A")
                        gu_others_input.press("Backspace")
                        gu_others_input.fill("NOT EXAMINE")

                    _try_step("GU (IE) Others remarks set to 'NOT EXAMINE'", _fill_gu_others)

                    _try_step(
                        "SKIN/EXTREMITIES marked Essentially normal",
                        lambda: page.locator('input[name="seEssentiallyNormal"]').check(force=True)
                    )
                    _try_step(
                        "NEURO-EXAM marked Essentially normal",
                        lambda: page.locator('input[name="neEssentiallyNormal"]').check(force=True)
                    )

                    _try_step(
                        "CF4 form saved",
                        lambda: page.get_by_role("button", name="SAVE").click(force=True)
                    )

                    logger.info("Auto Encode CF4 finished.")

                # ── Map medicines ──────────────────────────────────────────
                logger.info("Clicking DRUGS / MEDICINES...")
                page.get_by_text("DRUGS / MEDICINES", exact=True).click()
                page.wait_for_load_state("networkidle")

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
                    logger.info(f"\nProcessing row {i}")
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

                    logger.info(f"Textbox value: {search_box.input_value()}")

                    # Open 3-dot menu → Map Medicine
                    row.locator("button").click()
                    page.wait_for_timeout(1000)
                    page.get_by_text("Map Medicine", exact=True).click()
                    page.wait_for_load_state("networkidle")

                    # Type search term
                    search_box = page.locator('input[id*="SearchMedicinetoMap"]').first
                    search_box.click()
                    search_box.press("Control+A")
                    search_box.press("Backspace")
                    search_box.type(search_term, delay=100)

                    page.wait_for_selector('input[type="radio"]', timeout=10000)
                    logger.info(f"Textbox value: {search_box.input_value()}")

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

                    continue_btn = page.get_by_role("button", name="CONTINUE")
                    continue_btn.click(force=True)
                    page.wait_for_load_state("networkidle")
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
                    name="SAVE CHANGES"
                ).click()

                logger.info("Navigation save confirmation clicked")

                page.wait_for_load_state("networkidle")

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