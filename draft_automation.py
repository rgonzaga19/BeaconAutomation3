"""
Core Beacon "Create Draft" + "Add Claims" Playwright flow.

This module owns none of the browser lifecycle (no connect/close) — it
just drives an already-open `page`. Called from cf2_automation.py as the
first step of each patient's CF2 fill.

Confirmed via a live run: once Add Claims validation completes, Beacon
leaves the browser sitting directly on the PHIC Claims Details page
itself (CF1/CF2 tabs, admission/discharge dates pre-filled) — not a
claims list to click into. cf2_automation.py's fill_cf2() continues
straight into the CF2 fields from there.
"""

import re
from playwright.sync_api import expect

from beacon import open_transmittals


def run_create_draft_flow(page, member_pin, admission_date, discharge_date, draft_title):
    """
    admission_date / discharge_date: strings in MM/DD/YYYY format.
    draft_title: string, already built and length-capped by the caller.

    Raises on failure — caller decides how to log/handle it.
    """

    # A trailing "/" (or "\", in case Excel/autocorrect flips it) on the
    # Member PIN marks this as a Dependent entry (e.g. "030511447573/").
    # Strip it before it's used as the actual PIN, and remember the flag
    # so we can branch the Add Claims flow.
    raw_pin = member_pin.strip()
    is_dependent = raw_pin.endswith("/") or raw_pin.endswith("\\")
    member_pin = raw_pin[:-1].strip() if is_dependent else raw_pin

    print(
        f"Automate Draft requested — Member PIN: {member_pin}, "
        f"Dependent: {is_dependent}, "
        f"Admission Date: {admission_date}, "
        f"Discharge Date: {discharge_date}, "
        f"Draft Title: {draft_title}"
    )

    print("Opening Transmittals...")
    open_transmittals(page)

    print("Clicking Add Transmittal button...")

    page.wait_for_load_state("networkidle")

    add_button = page.locator('button[title="Shortcut Key: [N]"]')
    add_button.wait_for(state="visible", timeout=15000)
    add_button.click(force=True)

    print("Checking Hemodialysis checkbox...")

    checkbox = page.locator('input[name="isHemodialysis"]')
    checkbox.wait_for(state="attached", timeout=10000)
    checkbox.check(force=True)

    print(f"Entering Draft Title into Remarks: {draft_title}")

    remarks = page.locator('textarea[name="remarks"]')
    remarks.wait_for(state="visible", timeout=10000)
    remarks.click()
    remarks.fill(draft_title)

    page.get_by_role("button", name="Save").click()
    page.wait_for_load_state("networkidle")

    page.get_by_role("button").filter(
        has_text=re.compile(r"^$")
    ).nth(3).click()

    page.get_by_text("Manage Claims").click()

    # Open Actions menu
    page.get_by_role("button").filter(
        has_text=re.compile(r"^$")
    ).nth(2).click()

    if is_dependent:
        print("Opening Add Claims for Dependent...")
        page.get_by_text("Add Claims for Dependent").click()
    else:
        print("Opening Add Claims for Member...")
        page.get_by_text("Add Claims for Member").click()

    print("Waiting for Add Claims window...")

    # Admission Date
    admission_box = page.get_by_role(
        "textbox",
        name="Admission Date (MM-DD-YYYY)"
    )
    admission_box.wait_for(state="visible")

    print(f"Entering Admission Date: {admission_date}")

    date_digits = admission_date.replace("/", "")

    admission_box.click()
    page.keyboard.press("Control+A")
    page.keyboard.press("Backspace")

    for ch in date_digits:
        page.keyboard.press(ch)

    page.keyboard.press("Tab")

    # Discharge Date
    discharge_box = page.get_by_role(
        "textbox",
        name="Discharge Date (MM-DD-YYYY)"
    )
    discharge_box.wait_for(state="visible")

    print(f"Entering Discharge Date: {discharge_date}")

    date_digits = discharge_date.replace("/", "").replace("-", "")

    discharge_box.click()
    page.keyboard.press("Control+A")
    page.keyboard.press("Backspace")

    for ch in date_digits:
        page.keyboard.press(ch)

    page.keyboard.press("Tab")

    # Search by Member PIN
    print("Opening Search By Member PIN...")

    search_member_pin_btn = page.get_by_role(
        "button",
        name="Search By Member Pin"
    )
    search_member_pin_btn.wait_for(state="visible")
    search_member_pin_btn.click()

    member_pin_box = page.locator('input[id*="InputMemberPinHere"]')
    member_pin_box.wait_for(state="visible")

    print(f"Entering Member PIN: {member_pin}")

    member_pin_box.click()
    member_pin_box.fill(member_pin)

    print("Searching Member PIN...")

    search_button = page.get_by_role("button", name="Search", exact=True)
    ok_button = page.get_by_role("button", name="Ok")

    def _confirm_search_result():
        """After clicking Search: Member PINs go straight to the OK
        confirmation. Dependent PINs surface a 'Please Select A
        Dependent' list first — pick a dependent to reach that same
        OK confirmation."""
        if is_dependent:
            page.get_by_text("Please Select A Dependent").wait_for(timeout=5000)
            print("Selecting dependent from list...")
            page.locator("span[tabindex='0']").first.click()

        ok_button.wait_for(state="visible", timeout=5000)
        ok_button.click()

    search_button.click()

    try:
        _confirm_search_result()

    except Exception:
        print("WARNING: Member not found. Retrying with leading zero...")

        if not member_pin.startswith("0"):
            member_pin = "0" + member_pin

        member_pin_box.click()
        page.keyboard.press("Control+A")
        member_pin_box.fill(member_pin)

        search_button.click()

        try:
            _confirm_search_result()

        except Exception:
            print(f"ERROR: Incorrect Member PIN: {member_pin}")
            raise Exception(f"Incorrect Member PIN: {member_pin}")

    print("Validating Membership...")

    page.get_by_role("button", name="VALIDATE MEMBERSHIP").click()
    page.get_by_role("button", name="Validate Eligibility").click()

    page.get_by_text("Member is eligible").first.wait_for(timeout=30000)

    expect(page.get_by_role("button", name="FINALIZE")).to_be_enabled(timeout=30000)

    print("Membership validation completed.")


def try_extract_transmittal_number(page):
    """
    Best-effort read of the transmittal number Beacon just generated,
    for logging purposes only (nothing downstream depends on this being
    correct). Not verified against a live page yet — if it keeps
    returning "AUTO-GENERATED", inspect the Manage Claims page after a
    Create Draft and tell me what element actually shows the number so
    this can be tightened to a real selector.
    """
    try:
        text = page.content()
        match = re.search(r"\b\d{10,15}\b", text)
        if match:
            return match.group(0)
    except Exception as e:
        print(f"WARNING: Could not extract transmittal number: {e}")

    return "AUTO-GENERATED"