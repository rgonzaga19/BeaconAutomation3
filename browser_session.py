"""Beacon API authentication shared by the direct HTTP clients.

The module name is retained for compatibility with the existing API layers,
but Beabots no longer creates or manages a browser session. Authentication is
performed directly against Beacon's OAuth2 token endpoint.
"""

import sys
import time

import requests

from login import load_login_settings
from logger import logger


BEACON_URLS = {
    "s2": "https://beacon-s2.bizbox.ph/",
    "s4": "https://beacon-s4.bizbox.ph/",
}

_auth_token = None


def _get_beacon_url():
    """Return the Beacon URL selected in the user's settings."""
    settings = load_login_settings()
    server = settings.get("server", "s4")
    return BEACON_URLS.get(server, BEACON_URLS["s4"])


def login_via_api(username, password):
    """Authenticate against Beacon's OAuth2 password-token endpoint."""
    response = requests.post(
        _get_beacon_url().rstrip("/") + "/token",
        data={
            "grant_type": "password",
            "username": username,
            "password": password,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def _store_auth_token(token_data):
    """Cache an API token and the user ID returned with it."""
    global _auth_token
    _auth_token = {
        "access_token": token_data.get("access_token"),
        "refresh_token": token_data.get("refresh_token"),
        "token_type": token_data.get("token_type", "bearer"),
        "expires_in": token_data.get("expires_in"),
        "user_id": token_data.get("Id"),
        "issued_at": time.time(),
    }


def invalidate_auth_token():
    """Discard cached authentication after credentials or server change."""
    global _auth_token
    _auth_token = None
    for module_name, cache_names in {
        "cf2_api": ("_client_id_cache",),
        "beacon_api": ("_client_id_cache",),
        "soa_api": ("_client_ids_cache", "_client_ids_cache_user_id"),
    }.items():
        module = sys.modules.get(module_name)
        if module is None:
            continue
        for cache_name in cache_names:
            if hasattr(module, cache_name):
                setattr(module, cache_name, None)


def _ensure_auth_token(username=None, password=None):
    """Return a valid cached token or obtain a fresh one from Beacon."""
    if _auth_token is not None:
        issued_at = _auth_token.get("issued_at", 0)
        expires_in = _auth_token.get("expires_in") or 0
        if time.time() < issued_at + max(expires_in - 60, 0):
            return _auth_token.get("access_token")

    if username is None or password is None:
        settings = load_login_settings()
        username = settings.get("username", "")
        password = settings.get("password", "")

    token_data = login_via_api(username, password)
    _store_auth_token(token_data)
    return _auth_token.get("access_token")


def get_auth_token():
    """Return a Beacon bearer token, or ``None`` when login fails."""
    try:
        return _ensure_auth_token()
    except Exception as exc:
        logger.warning(f"get_auth_token(): could not obtain a token ({exc}).")
        return None


def get_user_id():
    """Return Beacon's user ID from the current OAuth2 token response."""
    try:
        _ensure_auth_token()
    except Exception as exc:
        logger.warning(f"get_user_id(): could not obtain a token ({exc}).")
        return None
    return _auth_token.get("user_id") if _auth_token else None
