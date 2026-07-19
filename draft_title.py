"""
Builds the Beacon "Create Draft" remarks/title from a patient's name and
their first/last treatment date, e.g.:

    ABAO JUNE 1-10, 2026                (same month)
    ABAO JUNE 28 - JUL 2, 2026          (spans two months — 2nd month abbreviated)

The Beacon draft title field is capped at 50 characters, so the result is
truncated (with a console warning) if it would exceed that.
"""

MAX_TITLE_LENGTH = 50

MONTH_FULL = {
    1: "JANUARY", 2: "FEBRUARY", 3: "MARCH", 4: "APRIL",
    5: "MAY", 6: "JUNE", 7: "JULY", 8: "AUGUST",
    9: "SEPTEMBER", 10: "OCTOBER", 11: "NOVEMBER", 12: "DECEMBER",
}

MONTH_ABBR = {
    1: "JAN", 2: "FEB", 3: "MAR", 4: "APR",
    5: "MAY", 6: "JUN", 7: "JUL", 8: "AUG",
    9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC",
}


def build_draft_title(patient_name, first_treatment, last_treatment):
    """
    patient_name: full name as it appears in Excel (used as-is, no
                  surname extraction).
    first_treatment / last_treatment: date objects.
    """
    name = str(patient_name).strip().upper()

    if first_treatment.month == last_treatment.month and first_treatment.year == last_treatment.year:
        date_part = (
            f"{MONTH_FULL[first_treatment.month]} "
            f"{first_treatment.day}-{last_treatment.day}, {last_treatment.year}"
        )
    else:
        date_part = (
            f"{MONTH_FULL[first_treatment.month]} {first_treatment.day} - "
            f"{MONTH_ABBR[last_treatment.month]} {last_treatment.day}, {last_treatment.year}"
        )

    title = f"{name} {date_part}"

    if len(title) > MAX_TITLE_LENGTH:
        print(
            f"WARNING: Draft title exceeds {MAX_TITLE_LENGTH} chars "
            f"({len(title)}) — truncating: {title}"
        )
        title = title[:MAX_TITLE_LENGTH]

    return title