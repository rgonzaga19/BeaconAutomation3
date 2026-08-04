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
from playwright.sync_api import TimeoutError


class InvalidMemberPinError(Exception):
    """
    Raised when a Member PIN cannot be validated in Beacon after every
    retry strategy (as-typed, then with a leading zero) has been
    exhausted.

    This is intentionally a distinct exception type (not a bare
    Exception) so callers — specifically cf2_automation.py — can tell
    "this patient's PIN is bad, skip it and move on" apart from "an
    unexpected UI/automation error happened, mark it failed". By the
    time this is raised, _recover_after_pin_failure() has already put
    the page back in a clean state (dialogs dismissed, back on the
    Transmittals list) so the NEXT patient's run_create_draft_flow()
    call starts from a known-good page instead of stalling behind
    whatever half-open modal the failed search left behind.
    """
    pass


def _recover_after_pin_failure(page):
    """
    Best-effort cleanup after Member PIN validation ultimately fails, so
    Beacon's UI doesn't stay stuck on a half-open dialog/toast for the
    next patient. Every action here is individually wrapped and
    swallowed on failure — cleanup failing must never itself raise and
    block the batch from moving to the next row, and every wait uses a
    short explicit timeout so this can't hang.
    """
    print("Recovering UI after failed PIN validation...")

    # 1) Dismiss any confirmation/error dialog Beacon may have surfaced
    #    (name varies by build, so try the common ones).
    for name in ("OK", "Ok", "Close", "Cancel", "Dismiss"):
        try:
            btn = page.get_by_role("button", name=name, exact=True)
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click(timeout=2000)
                page.wait_for_timeout(300)
        except Exception:
            pass

    # 2) Generic "close whatever's on top" fallback.
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
    except Exception:
        pass

    # 3) Re-anchor on the Transmittals list. This is the same page
    #    run_create_draft_flow() starts from for the next patient, so
    #    landing here now guarantees a known, unblocked starting point
    #    rather than leaving the browser wherever the failed search
    #    dialog left it.
    try:
        open_transmittals(page)
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception as e:
        print(f"WARNING: Could not re-anchor to Transmittals after PIN failure: {e}")


def click_newest_transmittal_menu(page):
    """
    Clicks the 3-dot action menu of the newest transmittal (first row).

    Uses several fallback strategies because Beacon's DOM is unstable.
    Raises Exception if all methods fail.
    """

    print("Waiting for newest transmittal row...")

    page.wait_for_selector("tbody tr", timeout=30000)

    first_row = page.locator("tbody tr").first

    first_row.wait_for(state="visible", timeout=30000)

    strategies = [

        # ------------------------------------------------------------------
        # Strategy 1 (BEST)
        # First row -> last cell -> button
        # ------------------------------------------------------------------
        lambda: first_row.locator("td").last.locator("button").click(
            timeout=5000,
            force=True
        ),

        # ------------------------------------------------------------------
        # Strategy 2
        # First row -> any button
        # ------------------------------------------------------------------
        lambda: first_row.locator("button").first.click(
            timeout=5000,
            force=True
        ),

        # ------------------------------------------------------------------
        # Strategy 3
        # First row -> SVG
        # ------------------------------------------------------------------
        lambda: first_row.locator("svg").first.click(
            timeout=5000,
            force=True
        ),

        # ------------------------------------------------------------------
        # Strategy 4
        # First row -> SVG Path
        # ------------------------------------------------------------------
        lambda: first_row.locator("path").first.click(
            timeout=5000,
            force=True
        ),

        # ------------------------------------------------------------------
        # Strategy 5
        # Relative CSS
        # ------------------------------------------------------------------
        lambda: page.locator(
            "tbody tr:first-child td:last-child button"
        ).click(
            timeout=5000,
            force=True
        ),

        # ------------------------------------------------------------------
        # Strategy 6
        # Mouse click at center of last cell
        # ------------------------------------------------------------------
        lambda: (
            first_row.locator("td").last.scroll_into_view_if_needed(),
            first_row.locator("td").last.click(
                position={"x": 24, "y": 24},
                timeout=5000,
                force=True
            )
        ),

        # ------------------------------------------------------------------
        # Strategy 7
        # JavaScript click
        # ------------------------------------------------------------------
        lambda: page.evaluate("""
            () => {
                const row = document.querySelector("tbody tr");
                if(!row) throw "No row";

                const btn = row.querySelector("td:last-child button");
                if(!btn) throw "No button";

                btn.click();
            }
        """),

        # ------------------------------------------------------------------
        # Strategy 8
        # Dispatch MouseEvent
        # ------------------------------------------------------------------
        lambda: page.evaluate("""
            () => {
                const row = document.querySelector("tbody tr");
                if(!row) throw "No row";

                const btn = row.querySelector("button");

                btn.dispatchEvent(
                    new MouseEvent("click", {
                        bubbles:true,
                        cancelable:true
                    })
                );
            }
        """),
    ]

    for i, strategy in enumerate(strategies, start=1):
        try:
            print(f"Trying menu strategy #{i}...")
            strategy()

            page.wait_for_timeout(500)

            if page.get_by_text("Manage Claims").is_visible(timeout=1500):
                print(f"SUCCESS using strategy #{i}")
                return

        except Exception as e:
            print(f"Strategy #{i} failed: {e}")

    raise Exception("Unable to open newest transmittal menu.")


def run_create_draft_flow(page, member_pin, admission_date, discharge_date, draft_title):
    """
    admission_date / discharge_date: strings in MM/DD/YYYY format.
    draft_title: string, already built and length-capped by the caller.

    Raises on failure — caller decides how to log/handle it:
      - InvalidMemberPinError: the PIN itself is bad (confirmed after
        retrying). The page has already been recovered to a clean state
        by the time this is raised — safe to treat as "skip this row
        and continue with the next one".
      - Exception (anything else): an unexpected automation/UI failure.
        Recovery is still attempted before re-raising, so the page is
        left as clean as possible for the caller either way.
    """
    try:
        _run_create_draft_flow(page, member_pin, admission_date, discharge_date, draft_title)
    except InvalidMemberPinError:
        # Already recovered inside _run_create_draft_flow, right where
        # the failure happened — nothing more to do here.
        raise
    except Exception:
        # Any other failure (timeout, missing element, stale reference,
        # etc.) — still attempt the same cleanup so the browser isn't
        # left stuck on a half-open dialog for the next patient.
        _recover_after_pin_failure(page)
        raise


def _run_create_draft_flow(page, member_pin, admission_date, discharge_date, draft_title):
    # A trailing "/" (or "\", in case Excel/autocorrect flips it) on the
    # Member PIN marks this as a Dependent entry (e.g. "030511447573/").
    # Strip it before it's used as the actual PIN, and remember the flag
    # so we can branch the Add Claims flow.
    raw_pin = member_pin.strip()
    is_dependent = raw_pin.endswith("/") or raw_pin.endswith("\\")
    raw_pin = raw_pin[:-1].strip() if is_dependent else raw_pin

    # Excel entries can carry characters that aren't part of the actual
    # PIN — a leading apostrophe (Excel's "force text" marker, e.g.
    # '0190894931403), stray spaces, dashes, etc. Beacon's PIN field
    # rejects anything but digits, so strip everything else here. This
    # keeps leading zeros intact (e.g. 0190894931403) since only
    # non-digit characters are removed.
    member_pin = re.sub(r"\D", "", raw_pin)

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

    page.wait_for_load_state("networkidle")

    click_newest_transmittal_menu(page)

    page.get_by_text("Manage Claims").click()


    page.locator(
        'button:has(path[d^="M19 13h-6"])'
    ).click(force=True)

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

    pin_confirmed = False

    search_button.click()
    try:
        _confirm_search_result()
        pin_confirmed = True
    except Exception:
        print("WARNING: Member not found on first attempt.")

    # Only worth retrying with a leading zero if that actually changes
    # the PIN — retrying the exact same value twice just burns time
    # without changing the outcome.
    if not pin_confirmed and not member_pin.startswith("0"):
        print("Retrying with a leading zero...")
        member_pin = "0" + member_pin

        member_pin_box.click()
        page.keyboard.press("Control+A")
        member_pin_box.fill(member_pin)
        search_button.click()

        try:
            _confirm_search_result()
            pin_confirmed = True
        except Exception:
            pass

    if not pin_confirmed:
        print(f"ERROR: Incorrect Member PIN: {member_pin} — skipping this patient.")
        _recover_after_pin_failure(page)
        raise InvalidMemberPinError(f"Incorrect Member PIN: {member_pin}")

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