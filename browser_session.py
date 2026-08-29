import os
import sys
import json
import time

import requests

from pathlib import Path
from playwright.sync_api import sync_playwright

from settings import load_settings
from logger import logger


APP_NAME = "Beabots"

APP_DATA = Path(os.getenv("LOCALAPPDATA")) / APP_NAME
APP_DATA.mkdir(parents=True, exist_ok=True)

SESSION_FILE = APP_DATA / "session.json"

BEACON_URLS = {
    "s2": "https://beacon-s2.bizbox.ph/",
    "s4": "https://beacon-s4.bizbox.ph/",
}


def _get_beacon_url():
    """Resolve the Beacon URL from the user's S2/S4 toggle in Settings,
    falling back to S4 (the previous hardcoded default) if unset/unknown."""
    settings = load_settings()
    server = settings.get("server", "s4")
    return BEACON_URLS.get(server, BEACON_URLS["s4"])


playwright = None
browser = None
context = None
page = None

# In-memory cache of the most recent API login result (access_token,
# refresh_token, expiry, etc.) - kept separate from the Playwright
# storage_state so hybrid API modules can get a bearer token without
# needing a live browser/page at all. Populated by _ensure_auth_token().
_auth_token = None


def has_session():

    return page is not None


def login_via_api(username, password):
    """
    Authenticate directly against Beacon's OAuth2 token endpoint
    (POST /token, grant_type=password) instead of driving the login
    form through the browser.

    This is the exact same request Beacon's own frontend makes when you
    click SIGN IN - confirmed via a captured HAR of a real login - so it
    returns the same payload (access_token, refresh_token, expires_in,
    token_type, etc.), and every subsequent Beacon API call authenticates
    via "Authorization: Bearer <access_token>" (also confirmed via HAR).

    Doing this as a direct call, independent of Playwright, means:
      - a bad password or an unreachable server fails fast with a plain
        requests exception, instead of only surfacing ~30s later as a
        Playwright timeout waiting for a selector that will never appear;
      - the resulting access_token is available in Python immediately,
        independent of whether a browser/page exists at all, for any
        future hybrid module that talks to Beacon's API directly instead
        of through the UI.

    This does NOT by itself log the visible Playwright browser in.
    Beacon's SPA keeps its own client-side auth state in the browser's
    localStorage under a scheme we haven't reverse-engineered, so
    guessing at it and injecting a token risks the SPA silently
    disagreeing with itself. The browser is still logged in the normal,
    known-correct way via _perform_login() below, immediately after this
    succeeds - this call and that one are independent and this one is
    never a substitute for it.
    """
    url = _get_beacon_url().rstrip("/") + "/token"

    response = requests.post(
        url,
        data={
            "grant_type": "password",
            "username": username,
            "password": password,
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=15,
    )

    response.raise_for_status()

    return response.json()


def _store_auth_token(token_data):
    """Cache an API login result (from login_via_api) in memory, tagged
    with when it was issued so _ensure_auth_token() can tell when it's
    about to expire. Also keeps the userId (Id) Beacon returns
    alongside the token - confirmed via HAR the /token response
    includes it directly - so callers needing it (e.g. to look up
    clientId via GetAllClientsByUserId) don't need a separate call."""
    global _auth_token

    _auth_token = {
        "access_token": token_data.get("access_token"),
        "refresh_token": token_data.get("refresh_token"),
        "token_type": token_data.get("token_type", "bearer"),
        "expires_in": token_data.get("expires_in"),
        "user_id": token_data.get("Id"),
        "issued_at": time.time(),
    }


def get_user_id():
    """Return the current logged-in userId (Beacon's "Id" field from the
    /token response), refreshing/logging in via _ensure_auth_token()
    first if we don't have one cached. Returns None if unavailable."""
    try:
        _ensure_auth_token()
    except Exception as e:
        logger.warning(f"get_user_id(): could not obtain a token ({e}).")
        return None

    return _auth_token.get("user_id") if _auth_token else None



def _ensure_auth_token(username=None, password=None):
    """
    Return a currently-valid bearer token, fetching a new one via
    login_via_api() only if we don't already have one cached in memory
    (or it's within 60s of expiring). Reads credentials from Settings if
    not passed in directly.

    Raises whatever login_via_api() raises (e.g. requests.HTTPError on
    bad credentials, requests.ConnectionError if Beacon is unreachable)
    - callers decide whether that should be fatal or just logged, since
    a failure here doesn't necessarily mean the browser-based session
    can't still work.
    """
    global _auth_token

    if _auth_token is not None:
        issued_at = _auth_token.get("issued_at", 0)
        expires_in = _auth_token.get("expires_in") or 0
        if time.time() < issued_at + max(expires_in - 60, 0):
            return _auth_token.get("access_token")

    if username is None or password is None:
        settings = load_settings()
        username = settings["username"]
        password = settings["password"]

    token_data = login_via_api(username, password)
    _store_auth_token(token_data)

    return _auth_token.get("access_token")


def get_auth_token():
    """
    Return the current bearer token for use in direct API calls, e.g.:

        headers = {"Authorization": f"Bearer {get_auth_token()}"}

    Intended for future hybrid modules that talk to Beacon's API
    directly. Transparently fetches/refreshes via _ensure_auth_token()
    as needed. Returns None (rather than raising) if a token genuinely
    can't be obtained right now, so callers can fall back to UI
    automation instead of crashing.
    """
    try:
        return _ensure_auth_token()
    except Exception as e:
        logger.warning(f"get_auth_token(): could not obtain a token ({e}).")
        return None


def _perform_login(username, password):
    """Do the actual username/password login flow and persist the resulting
    session to disk."""
    global page

    page.goto(
        _get_beacon_url(),
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
    """Persist the current context's storage state to disk, alongside the
    cached API auth token (if any). Call this after a successful login
    AND after a successful automation run, since Beacon may
    rotate/refresh tokens during normal use."""
    global context

    if context is None:
        return

    storage_state = context.storage_state()

    session_data = {
        "storage_state": storage_state,
        "auth_token": _auth_token,
    }

    with open(SESSION_FILE, "w", encoding="utf-8") as f:
        json.dump(session_data, f, indent=2)


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
    global _auth_token

    if page is not None:
        try:
            # cheap liveness check — raises if the browser/page was already
            # closed (e.g. after a previous run finished)
            page.evaluate("1")

            try:
                _ensure_auth_token()
            except Exception as e:
                logger.warning(
                    f"Could not refresh API auth token ({e}); "
                    "continuing with the existing browser session."
                )

            return page
        except Exception:
            disconnect()

    settings = load_settings()

    username = settings["username"]
    password = settings["password"]

    # Get/refresh the bearer token up front, before opening a browser at
    # all. On correct credentials this is a fast sanity check that also
    # populates the token for any hybrid API module that wants one this
    # run. On bad credentials or an unreachable server, it fails here in
    # under 15s with a clear requests exception instead of only surfacing
    # ~30s later as a Playwright timeout — but it's non-fatal here: if it
    # fails, we still fall through to the exact same browser-based flow
    # this always used, so a temporary API hiccup can't break a session
    # that would otherwise still work fine.
    try:
        _ensure_auth_token(username, password)
    except Exception as e:
        logger.warning(
            f"Could not obtain API auth token ({e}); "
            "continuing with browser-only session."
        )

    playwright = sync_playwright().start()

    browser = playwright.chromium.launch(
        headless=False,
        slow_mo=0
    )

    if SESSION_FILE.exists():

        with open(SESSION_FILE, "r") as f:

            session_data = json.load(f)

        # Backward compatible with the old session.json format, which was
        # a bare Playwright storage_state dict with no wrapping.
        if "storage_state" in session_data:
            storage_state = session_data["storage_state"]
            if _auth_token is None and session_data.get("auth_token"):
                _auth_token = session_data["auth_token"]
        else:
            storage_state = session_data

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
            _get_beacon_url(),
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