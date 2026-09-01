import json
import os

from pathlib import Path

APP_NAME = "Beabots"

APP_DATA = Path(os.getenv("LOCALAPPDATA")) / APP_NAME
APP_DATA.mkdir(parents=True, exist_ok=True)

CONFIG_FILE = APP_DATA / "config.json"


# ── Public API ─────────────────────────────────────────────────────────────────
def load_settings():
    # Create a default config if it doesn't exist
    if not CONFIG_FILE.exists():
        default = {
            "username": "",
            "password": "",
            "access_key": "",
            "server": "s4",
            "facility_id": 263,
            "transmittal_search_days": 31,
            "transmittal_package_type": 7
        }

        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=4)

        return default

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        settings = json.load(f)

    # Older config.json files predate the S2/S4 toggle and won't have this
    # key. Default them to "s4" to match the previously hardcoded URL.
    settings.setdefault("server", "s4")
    # Older config.json files may not have facility_id; default to 263 for backward compatibility
    settings.setdefault("facility_id", 263)
    # Transmittal search range (in days). Increase if searching for older transmittals.
    settings.setdefault("transmittal_search_days", 31)
    # Transmittal package type filter. Set to null/null to search all package types.
    settings.setdefault("transmittal_package_type", 7)

    return settings


def save_settings(settings):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=4)
