"""Beacon Create Draft orchestration backed entirely by API calls."""
import draft_api

InvalidMemberPinError = draft_api.InvalidMemberPinError


def run_create_draft_flow(page, member_pin, admission_date, discharge_date, draft_title):
    """Create transmittal + Member/Dependent claim + validate eligibility via APIs.

    ``page`` is retained in the signature temporarily for compatibility with
    existing callers; it is not used.
    Returns a dict containing transmittal_id, claim_id, and transmittal_number.
    """
    return draft_api.create_member_claim(
        member_pin=member_pin,
        admission_date=admission_date,
        discharge_date=discharge_date,
        draft_title=draft_title,
    )


def try_extract_transmittal_number(page):
    """Deprecated compatibility shim; new callers use the API return value."""
    return "AUTO-GENERATED"
