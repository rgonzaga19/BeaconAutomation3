import re
from datetime import date


MONTHS = {
    "JAN": 1,
    "JANUARY": 1,
    "FEB": 2,
    "FEBRUARY": 2,
    "MAR": 3,
    "MARCH": 3,
    "APR": 4,
    "APRIL": 4,
    "MAY": 5,
    "JUN": 6,
    "JUNE": 6,
    "JUL": 7,
    "JULY": 7,
    "AUG": 8,
    "AUGUST": 8,
    "SEP": 9,
    "SEPT": 9,
    "SEPTEMBER": 9,
    "OCT": 10,
    "OCTOBER": 10,
    "NOV": 11,
    "NOVEMBER": 11,
    "DEC": 12,
    "DECEMBER": 12,
}


def parse_dates(text, default_year, default_month=None):
    """
    Supported formats:

    June 15,17,19
    June 15 , 17 , 19
    Jun. 15,17,19
    June 21, 23, 26, 29, 2026        (trailing 4-digit year overrides default_year)
    MAY 1,4,8
    06/15,17,19/26
    06/15,17,19/2026
    6/15,17,19/26
    06/15/26
    6/22-26-29                      (no year -> uses default_year)
    6/15,18,20                      (no year -> uses default_year)
    6/22, 24, 26                    (no year -> uses default_year)
    1, 2                            (no month/year -> uses default_month/default_year)
    11,15                           (no month/year -> uses default_month/default_year)

    `default_month` is used whenever the text contains no month name and no
    "M/..." prefix (i.e. it's just a bare list of day numbers). If not
    supplied, it falls back to the current calendar month.
    """

    if text is None:
        return []

    text = str(text).strip().upper()

    text = re.sub(r"\s+", " ", text)

    if not text:
        return []

    dates = _parse_month_name(text, default_year)

    if dates:
        return dates

    dates = _parse_numeric(text, default_year)

    if dates:
        return dates

    return _parse_days_only(text, default_year, default_month)


def _parse_month_name(text, default_year):

    clean = text.replace(".", "")

    month = None

    for name, number in MONTHS.items():

        if clean.startswith(name):

            month = number

            clean = clean[len(name):]

            break

    if month is None:
        return []

    numbers = re.findall(r"\d+", clean)

    if not numbers:
        return []

    year = default_year
    days = []

    for n in numbers:

        # A 4-digit number in the tail is a year, e.g. "JUNE 21, 23, 2026"
        if len(n) == 4:
            year = int(n)
        else:
            days.append(int(n))

    dates = []

    for d in days:

        dates.append(date(year, month, d))

    dates = sorted(set(dates))

    return dates


def _parse_numeric(text, default_year):

    text = text.replace(" ", "")

    # M/day[,-day...][/year]
    # Days can be separated by commas and/or dashes, e.g.:
    #   6/22-26-29   6/15,18,20   6/22,24,26   06/15,17,19/26
    m = re.match(
        r"^(\d{1,2})/([\d,\-]+)(?:/(\d{2,4}))?$",
        text
    )

    if not m:
        return []

    month = int(m.group(1))

    days = re.findall(r"\d+", m.group(2))

    if not days:
        return []

    year_group = m.group(3)

    if year_group:
        year = int(year_group)
        if year < 100:
            year += 2000
    else:
        year = default_year

    dates = []

    for day in days:

        dates.append(
            date(
                year,
                month,
                int(day)
            )
        )

    dates = sorted(set(dates))

    return dates


def _parse_days_only(text, default_year, default_month):
    """Bare list of day numbers with no month name and no "M/..." prefix,
    e.g. "1, 2" or "11,15". Falls back to default_month (or today's month
    if not supplied) and default_year.
    """

    stripped = text.replace(" ", "")

    if not stripped or "/" in stripped:
        return []

    if not re.fullmatch(r"[\d,\-]+", stripped):
        return []

    days = re.findall(r"\d+", stripped)

    if not days:
        return []

    month = default_month if default_month else date.today().month

    dates = []

    for day in days:

        d = int(day)

        if d < 1 or d > 31:
            continue

        dates.append(date(default_year, month, d))

    dates = sorted(set(dates))

    return dates