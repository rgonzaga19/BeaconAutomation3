import os
import sys
import json

from pathlib import Path
from playwright.sync_api import sync_playwright

from settings import load_settings
from logger import logger


APP_NAME = "Beabots"

APP_DATA = Path(os.getenv("LOCALAPPDATA")) / APP_NAME
APP_DATA.mkdir(parents=True, exist_ok=True)

SESSION_FILE = APP_DATA / "session.json"


playwright = None
browser = None
context = None
page = None


def has_session():

    return page is not None


def _perform_login(username, password):
    """Do the actual username/password login flow and persist the resulting
    session to disk."""
    global page

    page.goto(
        "https://beacon-s4.bizbox.ph/",
        wait_until="networkidle"
    )

    page.locator(
        'input[name="Username"]'
    ).fill(username)

    page.locator(
        'input[name="Password"]'
    ).fill(password)

    page.get_by_role(
        "button",
        name="SIGN IN"
    ).click()

    page.wait_for_selector(
        'button:has-text("E-CLAIMS")',
        timeout=30000
    )

    page.wait_for_load_state("networkidle")

    save_session()


def save_session():
    """Persist the current context's storage state to disk. Call this after
    a successful login AND after a successful automation run, since Beacon
    may rotate/refresh tokens during normal use."""
    global context

    if context is None:
        return

    storage_state = context.storage_state()

    with open(SESSION_FILE, "w", encoding="utf-8") as f:
        json.dump(storage_state, f, indent=2)


def disconnect():
    """Tear down the browser/context/playwright and reset the module-level
    globals to None. Without this, a closed browser's dead `page` object
    would still get returned by connect() on the next run, causing every
    Playwright call to fail with 'Target page, context or browser has been
    closed'."""
    global playwright
    global browser
    global context
    global page

    try:
        if browser is not None:
            browser.close()
    except Exception:
        pass

    try:
        if playwright is not None:
            playwright.stop()
    except Exception:
        pass

    playwright = None
    browser = None
    context = None
    page = None


def connect():

    global playwright
    global browser
    global context
    global page

    if page is not None:
        try:
            # cheap liveness check — raises if the browser/page was already
            # closed (e.g. after a previous run finished)
            page.evaluate("1")
            return page
        except Exception:
            disconnect()

    settings = load_settings()

    username = settings["username"]
    password = settings["password"]

    playwright = sync_playwright().start()

    browser = playwright.chromium.launch(
        headless=False,
        slow_mo=0
    )

    if SESSION_FILE.exists():

        with open(SESSION_FILE, "r") as f:

            storage_state = json.load(f)

        context = browser.new_context(
            storage_state=storage_state
        )

    else:

        context = browser.new_context()

    page = context.new_page()

    if not SESSION_FILE.exists():

        _perform_login(username, password)

    else:

        page.goto(
            "https://beacon-s4.bizbox.ph/",
            wait_until="networkidle"
        )

        session_valid = True

        if "/login" in page.url:
            session_valid = False
        else:
            try:
                page.wait_for_selector(
                    'button:has-text("E-CLAIMS")',
                    timeout=8000
                )
            except Exception:
                session_valid = False

        if not session_valid:

            logger.warning("Session expired. Logging in again...")

            try:
                SESSION_FILE.unlink()
            except Exception:
                pass

            _perform_login(username, password)

        else:

            # Session was accepted, but Beacon may have silently rotated
            # the token/refreshToken in localStorage on page load. Persist
            # the current state now, or the on-disk copy will go stale and
            # the *next* run will be rejected and forced to log in again.
            save_session()

    return page