import json
import os

from pathlib import Path

APP_NAME = "Beabots"

APP_DATA = Path(os.getenv("LOCALAPPDATA")) / APP_NAME
APP_DATA.mkdir(parents=True, exist_ok=True)

CONFIG_FILE = APP_DATA / "config.json"
SESSION_FILE = APP_DATA / "session.json"


# ── Public API ─────────────────────────────────────────────────────────────────
def load_settings():
    # Create a default config if it doesn't exist
    if not CONFIG_FILE.exists():
        default = {
            "username": "",
            "password": "",
            "access_key": ""
        }

        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=4)

        return default

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_settings(settings):
    # Only the Beacon session is tied to username/password. Other callers
    # (e.g. the license-info refresh that runs before every automation run)
    # call save_settings() too, and previously that silently deleted a
    # perfectly valid session.json on every single run -- forcing a full
    # re-login every time regardless of whether credentials changed.
    old_username = None
    old_password = None

    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                old_settings = json.load(f)
            old_username = old_settings.get("username")
            old_password = old_settings.get("password")
        except Exception:
            pass

    credentials_changed = (
        settings.get("username") != old_username
        or settings.get("password") != old_password
    )

    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=4)

    if credentials_changed and SESSION_FILE.exists():
        SESSION_FILE.unlink()