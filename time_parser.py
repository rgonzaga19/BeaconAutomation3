import re
from datetime import time


_TIME_RANGE_PATTERN = re.compile(
    r"^\s*"
    r"(\d{1,2})(?::(\d{1,2}))?\s*([AaPp])\s*\.?\s*[Mm]\.?"
    r"\s*[-\u2013\u2014]\s*"
    r"(\d{1,2})(?::(\d{1,2}))?\s*([AaPp])\s*\.?\s*[Mm]\.?"
    r"\s*$"
)


def parse_time_range(value):
    """Parse an optional admission/discharge time range from the workbook."""
    if value is None or not str(value).strip():
        return None, None

    text = str(value).strip()
    match = _TIME_RANGE_PATTERN.fullmatch(text)
    if not match:
        raise ValueError(
            f"Invalid time range {text!r}. Use a format like "
            "05:25PM-09:25PM."
        )

    return (
        _to_time(match.group(1), match.group(2), match.group(3), text),
        _to_time(match.group(4), match.group(5), match.group(6), text),
    )


def _to_time(hour_text, minute_text, meridiem, original):
    hour = int(hour_text)
    minute = int(minute_text or 0)
    if not 1 <= hour <= 12 or not 0 <= minute <= 59:
        raise ValueError(
            f"Invalid time in range {original!r}. Hours must be 1-12 and "
            "minutes must be 00-59."
        )

    if meridiem.upper() == "A":
        hour = 0 if hour == 12 else hour
    else:
        hour = 12 if hour == 12 else hour + 12
    return time(hour, minute)


def format_beacon_time(value):
    return value.strftime("%I:%M %p")
