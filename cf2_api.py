"""
Direct HTTP client for a subset of Beacon's PHIC CF2 backend endpoints,
discovered by capturing (HAR) a real CF2 encoding session in the browser
and confirming the exact request/response shape for each call used here.

This bypasses the Beacon UI and authenticates via the bearer token from
browser_session.get_auth_token() (the same OAuth2 /token endpoint
Beacon's own SIGN IN button uses, confirmed via an earlier HAR).

Every function raises on a non-2xx response (via response.raise_for_status())
so callers see a real, specific error instead of a generic timeout, and
returns the parsed JSON body (or None for an empty response) on success.
cf2_automation.py handles endpoint failures as patient-level automation
errors so one failed record does not abort the remaining batch.

Covered here (confirmed via HAR, request AND response bodies inspected):
  - Claim eligibility validation: IsClaimEligible + EditPHICClaimEligibilityStatus
  - Existing Draft lookup: transmittal search / Manage Claims / claim open
  - Discharge Diagnosis: read / ICD10 search / create / set-primary
  - Surgical Procedure: read / create
  - Case Rate ("1st Case Rate" tag): read / search / tag
    IMPORTANT: on Beacon's backend, a session's date is only actually
    persisted as part of tag_first_case()'s NewPHICAllCaseRate call, not
    a separate save - new_surgical_procedure()'s own response always
    comes back with sessionDate=null. tag_first_case() takes the session
    date directly for this reason - there is no separate
    "just save the session date" endpoint to call.
  - Doctors: read / lookup by accreditation number / accreditation
    check / create
  - Claim Form 2 PDF signature block: read current PDF data via
    GetCf2PdfDetails and save the complete object via NewPdfClaimFormTwo.
    The generated CF2 PDF was verified to contain the submitted HCI
    representative signature, designation, and date. GetCf2PdfDetails is
    intentionally NOT used as post-save verification because Beacon's
    response does not echo those rendered PDF fields after a successful save.

Draft creation and the main CF2 save are implemented by the dedicated API
orchestration modules rather than browser automation.
"""

import re
from datetime import datetime, timedelta

import requests

import browser_session


ECLAIMS_API_BASES = {
    "s2": "https://eclaimsapi-s2.azurewebsites.net/api/EClaims/v3",
    "s4": "https://eclaimsapi-s4.azurewebsites.net/api/EClaims/v3",
}


class Cf2ApiError(Exception):
    """Raised for any cf2_api failure that isn't already a requests
    exception (e.g. missing auth token, unexpected response shape).
    Callers should treat this the same as any other API exception."""
    pass


def _base_url():
    return browser_session._get_beacon_url().rstrip("/")


def _headers():
    token = browser_session.get_auth_token()
    if not token:
        raise Cf2ApiError("No API auth token available.")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _eclaims_api_base():
    server = browser_session.load_login_settings().get("server", "s4")
    return ECLAIMS_API_BASES.get(server, ECLAIMS_API_BASES["s4"])


def _raise_for_status(response, path):
    """Like response.raise_for_status(), but folds the response body into
    the raised error instead of discarding it. A bare requests.HTTPError's
    str() is just "400 Client Error: Bad Request for url: ..." - useless
    for figuring out *which* field Beacon rejected. Callers (and the
    fallback-to-UI warning prints in cf2_automation.py) see this message,
    so keep it short enough to print but long enough to actually debug.

    On a 5xx, Beacon's body is deliberately generic ("An unexpected error
    occurred...") - there's no field-level detail to extract client-side,
    that's a server-side bug/edge-case, not a payload mistake we can fix
    by inspection. The one extra thing worth capturing in that case is
    any request/correlation id header, since that's what Beacon's own
    team would need to look up their server-side exception - so grab
    whichever of the common header names is present.
    """
    if response.ok:
        return
    body = (response.text or "").strip()
    if len(body) > 2000:
        body = body[:2000] + "...(truncated)"
    trace_id = None
    for header_name in (
        "x-correlation-id",
        "x-request-id",
        "x-trace-id",
        "request-id",
        "traceid",
    ):
        if header_name in response.headers:
            trace_id = response.headers[header_name]
            break
    suffix = f" [trace id: {trace_id}]" if trace_id else ""
    raise Cf2ApiError(
        f"{response.status_code} {response.reason} for {path}: {body}{suffix}"
    )


def _get(path, params=None, base=None):
    url = f"{base or _base_url()}{path}"
    response = requests.get(url, headers=_headers(), params=params, timeout=20)
    _raise_for_status(response, path)
    return response.json() if response.content else None


def _post(path, json_body=None, params=None, base=None):
    url = f"{base or _base_url()}{path}"
    response = requests.post(
        url, headers=_headers(), json=json_body, params=params, timeout=20
    )
    _raise_for_status(response, path)
    return response.json() if response.content else None


def _put(path, json_body=None, params=None, base=None):
    url = f"{base or _base_url()}{path}"
    response = requests.put(
        url, headers=_headers(), json=json_body, params=params, timeout=20
    )
    _raise_for_status(response, path)
    return response.json() if response.content else None


# ---------------------------------------------------------------------
# Page URL -> IDs
# ---------------------------------------------------------------------

def extract_ids_from_url(page_url):
    """
    Beacon's claim-detail URLs look like:
        .../phic-claims-details/{transmittalId}/{tab}/{claimId}
    e.g. .../phic-claims-details/10560119/summary/16545363#cf2Navigation

    claimId and phicCF2Id are confirmed to be the same numeric value
    (GetPHICCF2ById?claimId=X and GetPHICDischargeDiagnoses?phicCF2Id=X
    used the identical number in the captured session), so this returns
    one id for both purposes.

    Raises Cf2ApiError if the URL doesn't match this pattern.
    """
    match = re.search(r"/phic-claims-details/(\d+)/[^/]+/(\d+)", page_url)
    if not match:
        raise Cf2ApiError(f"Could not extract IDs from URL: {page_url}")
    return {
        "transmittal_id": int(match.group(1)),
        "claim_id": int(match.group(2)),
    }


def get_cf1_summary(claim_id):
    """
    Get the CF1 summary for a PHIC claim.

    Confirmed from Beacon Network traffic: the response includes
    ``memberMobileNumber``, which is used by the Statement of Account
    Signatories flow for the patient representative contact number.
    """
    return _get(
        "/api/PHICCF1/GetPHICCF1Summary",
        params={"id": claim_id},
    )


def _date_from_value(value):
    """Return a date from a date/datetime/Beacon ISO-like value."""
    if hasattr(value, "date") and not isinstance(value, str):
        return value.date()
    if hasattr(value, "year") and not isinstance(value, str):
        return value
    if not value:
        raise Cf2ApiError("Expected a date value but received an empty value.")
    text = str(value).strip()
    # Beacon CF1 timestamps are ISO-like (e.g. 1963-05-25T00:00:00).
    # Also accept the MM-DD-YYYY form used by the eClaims endpoint.
    for candidate, fmt in (
        (text.split("T")[0], "%Y-%m-%d"),
        (text, "%m-%d-%Y"),
        (text, "%m/%d/%Y"),
    ):
        try:
            return datetime.strptime(candidate, fmt).date()
        except ValueError:
            pass
    raise Cf2ApiError(f"Unsupported date value: {value!r}")


def _mm_dd_yyyy(value):
    return _date_from_value(value).strftime("%m-%d-%Y")


def validate_claim_eligibility(claim_id, transmittal_id, admission_date, discharge_date):
    """Validate claim eligibility without using the Beacon UI.

    Confirmed from the 2026-08-31 HAR capture, the Validate Eligibility
    button performs exactly these backend writes for a successful/eligible
    claim:

      1. GET  /api/PHICCF1/GetPHICCF1Summary?id={claim_id}
      2. POST the selected shard's eClaims API /IsClaimEligible
      3. PUT  /api/PHICClaim/EditPHICClaimEligibilityStatus

    There is no intermediate API call supplying the remaining-days / NHTS /
    3-over-6 / 9-over-12 fields.  The captured browser request synthesizes
    them client-side for the successful PBEF path: remainingDays=45,
    eligibleAsOf=one calendar year before admission, and the three flags
    are "NO".  This function intentionally implements only that confirmed
    successful (isok == YES) path; an ineligible/other response raises so we
    do not invent an unobserved Beacon payload.
    """
    cf1 = get_cf1_summary(claim_id)
    if not isinstance(cf1, dict) or not cf1:
        raise Cf2ApiError(
            f"GetPHICCF1Summary returned no usable data for claimId={claim_id}."
        )

    # The UI does not show Validate Eligibility once a result is already
    # stored. Mirror that behavior and avoid generating a duplicate PBEF.
    if str(cf1.get("eligibilityIsOk") or "").strip():
        return {
            "skipped": True,
            "reason": "already_validated",
            "cf1": cf1,
        }

    identity = get_hospital_identity(transmittal_id)
    admission = _date_from_value(admission_date)
    discharge = _date_from_value(discharge_date)

    eligibility_request = {
        "hospitalCode": identity["hospitalCode"],
        "isForOPDHemodialysisClaim": "0",
        "memberPIN": cf1.get("memberPin"),
        "patientPIN": cf1.get("patientPin"),
        "memberBasicInformation": {
            "lastname": cf1.get("memberLastname") or "",
            "firstname": cf1.get("memberFirstname") or "",
            "middlename": cf1.get("memberMiddlename") or "",
            "maidenname": "",
            "sex": cf1.get("memberGender") or "",
            "dateOfBirth": _mm_dd_yyyy(cf1.get("memberBirthday")),
            "suffix": cf1.get("memberSuffix"),
        },
        "patientIs": cf1.get("patientIsCode"),
        "admissiondate": admission.strftime("%m-%d-%Y"),
        "patientBasicInformation": {
            "lastname": cf1.get("patientLastname") or "",
            "firstname": cf1.get("patientFirstname") or "",
            "middlename": cf1.get("patientMiddlename") or "",
            "dateofBirth": _mm_dd_yyyy(cf1.get("patientBirthday")),
            "maidenname": "",
            "sex": cf1.get("patientGender") or "",
            "suffix": cf1.get("patientSuffix"),
        },
        "membershipType": cf1.get("memberTypeCode"),
        "pen": cf1.get("memberPEN"),
        "isFinal": 0,
        "phicIdentity": identity,
    }

    eligibility_result = _post(
        "/IsClaimEligible",
        json_body=eligibility_request,
        base=_eclaims_api_base(),
    )
    if not isinstance(eligibility_result, dict):
        raise Cf2ApiError("IsClaimEligible returned an unexpected response shape.")

    is_ok = str(
        eligibility_result.get("isok")
        or eligibility_result.get("isOk")
        or ""
    ).strip().upper()
    if is_ok != "YES":
        raise Cf2ApiError(
            "IsClaimEligible did not return the confirmed eligible path: "
            f"isok={is_ok!r}, message={eligibility_result.get('message')!r}. "
            "No eligibility status was written because the ineligible PUT "
            "payload has not yet been captured."
        )

    try:
        eligible_as_of_date = admission.replace(year=admission.year - 1)
    except ValueError:
        # Feb 29 -> Feb 28 in the previous non-leap year.
        eligible_as_of_date = admission.replace(
            year=admission.year - 1, month=2, day=28
        )

    status_payload = {
        "Id": str(claim_id),
        "eligibilityIsOk": is_ok,
        "eligibilityTrackingNumber": eligibility_result.get("trackingNumber") or "",
        "eligibilityRemainingDays": 45,
        "eligibleAsOf": eligible_as_of_date.strftime("%m-%d-%Y"),
        "eligibilityDocuments": [],
        "eligibilityIsNHTS": "NO",
        "eligibilityWith3Over6": "NO",
        "eligibilityWith9Over12": "NO",
        "admissionDate": to_utc_midnight_iso(admission),
        "dischargeDate": to_utc_midnight_iso(discharge),
    }

    saved = _put(
        "/api/PHICClaim/EditPHICClaimEligibilityStatus",
        json_body=status_payload,
    )
    return {
        "skipped": False,
        "eligibility": eligibility_result,
        "saved": saved,
        "request": eligibility_request,
        "status_payload": status_payload,
    }


def get_esoa_signatories(phic_claim_id):
    """Get the existing Statement of Account signatory record.

    Confirmed from Beacon Network traffic: this is the GET request used by
    the Signatories tab and its response supplies the existing ``id`` needed
    by Update-signatories.
    """
    return _get(
        "/api/PHICEsoa/GetEsoaSignatories",
        params={"phicClaimId": phic_claim_id},
    )


def update_esoa_signatories(payload):
    """Save the Statement of Account Signatories through Beacon's API.

    Confirmed from the captured SAVE request:
        POST /api/PHICEsoa/Update-signatories
    """
    return _post(
        "/api/PHICEsoa/Update-signatories",
        json_body=payload,
    )


_client_id_cache = None


def get_client_id():
    """
    Returns Beacon's numeric clientId for the logged-in account (263 in
    the captured session) - confirmed via HAR: GetAllClientsByUserId
    ?userId={userId} returns a list whose single entry's "id" field is
    the clientId used throughout the rest of the session (transmittal
    search, doctor lookup, etc).

    Cached in-process after the first successful call, since this is
    account-level and doesn't change mid-run.
    """
    global _client_id_cache

    if _client_id_cache is not None:
        return _client_id_cache

    user_id = browser_session.get_user_id()
    if not user_id:
        raise Cf2ApiError("No userId available (get_user_id() failed).")

    clients = _get(
        "/api/Account/GetAllClientsByUserId", params={"userId": user_id}
    )
    if not clients:
        raise Cf2ApiError(f"GetAllClientsByUserId returned no clients for userId={user_id}.")

    _client_id_cache = clients[0]["id"]
    return _client_id_cache


# ---------------------------------------------------------------------
# Existing draft lookup / claim resolution
# ---------------------------------------------------------------------

def _transmittal_search_date_window():
    """Return the same rolling date window used by Beacon's Transmittals UI.

    The captured Existing Draft HAR used local Philippine day boundaries:
    dateFrom = local midnight 31 days before today, represented in UTC, and
    dateTo = local 23:59:59.999 today, represented in UTC.
    """
    from datetime import timezone

    ph_tz = timezone(timedelta(hours=8))
    today = datetime.now(ph_tz).date()
    start_local = datetime.combine(
        today - timedelta(days=31), datetime.min.time(), tzinfo=ph_tz
    )
    end_local = datetime.combine(
        today, datetime.max.time(), tzinfo=ph_tz
    ).replace(microsecond=999000)
    utc = timezone.utc
    return (
        start_local.astimezone(utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        end_local.astimezone(utc).strftime("%Y-%m-%dT%H:%M:%S.999Z"),
    )


def search_existing_transmittal(transmittal_number, attempts=3):
    """Find an existing transmittal exactly as the proven UI path did.

    Confirmed by HAR:
      GET /api/PHICTransmittal/GetAllPHICTransmittal

    The original UI automation searched by ``data.transmittal``, accepted the
    first matching row, retried up to three times when the client-rendered table
    appeared empty/stale, and on the final attempt accepted the first row if one
    existed even when text formatting prevented an exact string match. This API
    version preserves that selection behavior while removing DOM interaction.
    """
    target = str(transmittal_number or "").strip()
    if not target:
        raise Cf2ApiError("Existing-draft mode requires a transmittal number.")

    client_id = get_client_id()
    date_from, date_to = _transmittal_search_date_window()

    for attempt in range(1, attempts + 1):
        response = _get(
            "/api/PHICTransmittal/GetAllPHICTransmittal",
            params={
                "clientId": client_id,
                "dateFrom": date_from,
                "dateTo": date_to,
                "itemStart": 0,
                "itemEnd": 30,
                "que": target,
                "transmittalPackageType": 7,
            },
        ) or {}
        rows = response.get("transmittalList") or []

        if rows:
            first = rows[0]
            if target in str(first.get("transmittalNumber") or ""):
                return first
            if attempt == attempts:
                # Preserve the original UI behavior: on the final attempt,
                # trust the first returned row even if formatting prevented
                # the exact text check from succeeding.
                return first

    return None


def get_claims_for_transmittal(transmittal_id):
    """Return the claims shown by Beacon's Manage Claims page."""
    return _get(
        "/api/PHICClaim/GetAllPHICClaimByPHICTransmittalId",
        params={"transmittalId": transmittal_id},
    ) or []


def check_transmittal_for_facility(transmittal_id, client_id=None):
    """Mirror Beacon's facility check performed before opening a claim."""
    if client_id is None:
        client_id = get_client_id()
    return _get(
        "/api/PHICTransmittal/CheckIfTransmittalIsForFacility",
        params={"TransmittalId": transmittal_id, "ClientId": client_id},
    )


def get_phic_claim(claim_id):
    """Fetch the claim opened by the Manage action in Beacon."""
    return _get(
        "/api/PHICClaim/GetPHICClaim",
        params={"id": claim_id},
    )


def resolve_existing_draft(transmittal_number, attempts=3):
    """Resolve the same transmittal + first claim selected by the UI flow.

    This is a direct API replacement for:
      _open_and_search_transmittal -> _open_manage_claims -> _open_patient

    It intentionally preserves the old selection rule: one transmittal maps to
    one patient in this workflow, so the first claim returned by Manage Claims
    is the claim that gets processed.
    """
    transmittal = search_existing_transmittal(transmittal_number, attempts=attempts)
    if transmittal is None:
        return None

    transmittal_id = transmittal.get("id")
    if not transmittal_id:
        raise Cf2ApiError(
            f"Transmittal {transmittal_number!r} did not contain an id."
        )

    claims = get_claims_for_transmittal(transmittal_id)
    if not claims:
        raise Cf2ApiError(
            f"Transmittal {transmittal_number!r} contains no PHIC claims."
        )

    claim = claims[0]
    claim_id = claim.get("id")
    if not claim_id:
        raise Cf2ApiError(
            f"First claim for transmittal {transmittal_number!r} has no id."
        )

    facility_ok = check_transmittal_for_facility(transmittal_id)
    if facility_ok is not True:
        raise Cf2ApiError(
            f"Transmittal {transmittal_number!r} is not available for the logged-in facility."
        )

    # Beacon performs this GET when the user clicks Manage. Keep the call so
    # the API migration follows the same backend access path and validates that
    # the selected claim is still readable before CF2 processing begins.
    opened_claim = get_phic_claim(claim_id)
    if not opened_claim or int(opened_claim.get("id") or 0) != int(claim_id):
        raise Cf2ApiError(
            f"Could not open PHIC claim {claim_id} for transmittal {transmittal_number!r}."
        )

    return {
        "transmittal_id": int(transmittal_id),
        "claim_id": int(claim_id),
        "transmittal": transmittal,
        "claim": claim,
    }


def get_hospital_identity(transmittal_id):
    """
    Returns {"hospitalCode": ..., "accreditationNo": ...} for the
    facility that owns this transmittal - needed as the "phicIdentity"
    block on every eclaimsapi call below. Confirmed via HAR entry 18
    (GetPHICTransmittalById): hospitalCode and accreditationNumber are
    top-level fields on that response.
    """
    data = _get(
        "/api/PHICTransmittal/GetPHICTransmittalById",
        params={"transmittalId": transmittal_id},
    )
    return {
        "hospitalCode": data["hospitalCode"],
        "accreditationNo": data["accreditationNumber"],
    }


def to_utc_midnight_iso(local_date):
    """
    Beacon represents a local (Philippines, UTC+8, no DST) calendar date
    as a UTC timestamp of the PREVIOUS day at 16:00 - confirmed via HAR:
    discharge date 07-01-2026 was sent as "2026-06-30T16:00:00.000Z".
    Accepts a date or datetime; returns that same string format.
    """
    d = local_date.date() if hasattr(local_date, "date") else local_date
    prev_day = d - timedelta(days=1)
    return prev_day.strftime("%Y-%m-%dT16:00:00.000Z")


def to_utc_noon_iso(local_date):
    """
    Beacon represents DISCHARGE TIME specifically as 12:00 PM local
    (Philippines, UTC+8) once it's been changed from the default AM to
    PM - confirmed via a full HAR capture of a real CF2 save: discharge
    date 07-01-2026 was sent as dischargeDateTime/dischargeTime =
    "2026-07-01T04:00:00.000Z" (04:00 UTC = 12:00 PM local, SAME
    calendar day - unlike to_utc_midnight_iso, which represents local
    midnight and therefore needs the PREVIOUS day). Accepts a date or
    datetime; returns that same string format.
    """
    d = local_date.date() if hasattr(local_date, "date") else local_date
    return d.strftime("%Y-%m-%dT04:00:00.000Z")


def get_cf2(claim_id):
    """
    Get the full current CF2 record.

    CONFIRMED WRONG ASSUMPTION (found via live debugging, not the
    original HAR capture): GetPHICCF2ById does NOT embed
    surgicalProcedures - even on a claim that already has one tagged
    via NewPHICAllCaseRate, this comes back null/absent here. It has
    to be fetched separately via get_surgical_procedures() and merged
    into the record with build_surgical_procedures_for_cf2() before
    calling edit_cf2() - see that function's docstring for the exact
    required shape, confirmed against a real successful browser save
    payload. Skipping this merge is what caused EditPHICCF2 to 500
    unconditionally, regardless of any other field's value.

    Every OTHER field (newborn/TB DOTS/animal bite/cataract sections,
    claim type, audit fields like version/dateUpdated, etc.) does
    round-trip as-is and must be carried through unchanged - only
    surgicalProcedures needs this extra stitching step.
    """
    return _get("/api/PHICCF2/GetPHICCF2ById", params={"claimId": claim_id})


def build_surgical_procedures_for_cf2(cf2_id):
    """
    Build the "surgicalProcedures" list EditPHICCF2 requires embedded
    in the CF2 record, from get_surgical_procedures()'s raw response.

    Confirmed against a real, successful browser save payload (DevTools
    capture) for a claim with one tagged Hemodialysis procedure/session.
    Two things differ from the raw GetPHICSurgicalProcedure response:

    1. Each session dict must have its 7 audit/tracking keys stripped
       (version, createdById, createdBy, dateCreated, updatedById,
       updatedBy, dateUpdated) - same keys tag_first_case() already
       strips for its own (different) NewPHICAllCaseRate payload.
    2. Unlike tag_first_case()'s payload, sessionDate here must be a
       plain "MM-DD-YYYY" string (matching admissionDate/dischargeDate's
       format elsewhere in the same record) - NOT the ISO datetime
       string tag_first_case() sends, and NOT the raw
       "YYYY-MM-DDTHH:MM:SS" string get_surgical_procedures() returns.

    Everything else (the procedure-level fields, including its own
    audit keys) is passed through unchanged - only the nested session
    dicts need reshaping.

    Returns [] if the claim has no surgical procedure yet (nothing to
    stitch in - fine to assign directly to cf2_record["surgicalProcedures"]).
    """
    procedures = get_surgical_procedures(cf2_id) or []
    result = []
    for procedure in procedures:
        procedure = dict(procedure)  # shallow copy, don't mutate caller's data
        sessions = []
        for session in procedure.get("sessions") or []:
            session = {
                k: v
                for k, v in session.items()
                if k not in (
                    "version",
                    "createdById",
                    "createdBy",
                    "dateCreated",
                    "updatedById",
                    "updatedBy",
                    "dateUpdated",
                )
            }
            raw_date = session.get("sessionDate")
            if raw_date:
                # Raw shape from GetPHICSurgicalProcedure is
                # "YYYY-MM-DDTHH:MM:SS" (no timezone) - reformat to
                # plain MM-DD-YYYY to match the confirmed working
                # payload.
                d = datetime.strptime(raw_date.split("T")[0], "%Y-%m-%d")
                session["sessionDate"] = d.strftime("%m-%d-%Y")
            sessions.append(session)
        procedure["sessions"] = sessions
        result.append(procedure)
    return result


def edit_cf2(cf2_record):
    """PUT the full CF2 record back.

    IMPORTANT: cf2_record["surgicalProcedures"] must be populated via
    build_surgical_procedures_for_cf2() first if the claim has a
    tagged surgical procedure - see get_cf2()'s docstring. Sending the
    record as get_cf2() returns it, unmodified, will 500 on any claim
    that has one, regardless of every other field's value."""
    return _put("/api/PHICCF2/EditPHICCF2", json_body=cf2_record)


# ---------------------------------------------------------------------
# Discharge Diagnosis
# ---------------------------------------------------------------------

def get_discharge_diagnoses(cf2_id):
    """[] means none exist yet - confirmed via HAR entry 45."""
    return _get(
        "/api/PHICDischargeDiagnosis/GetPHICDischargeDiagnoses",
        params={"phicCF2Id": cf2_id},
    )


def search_icd10(search_term):
    """Confirmed via HAR entry 52. Returns a list of
    {"icD10Code": ..., "icD10Value": ...}."""
    return _get("/api/ICD10/SearchICD10", params={"search": search_term})


def new_discharge_diagnosis(cf2_id, icd10_code, icd10_value):
    """Confirmed via HAR entry 53. Returns the created record, whose
    "id" is needed for edit_primary_discharge_diagnosis below."""
    return _post(
        "/api/PHICDischargeDiagnosis/NewPHICDischargeDiagnosis",
        json_body={
            "phiccF2Id": cf2_id,
            "icD10Code": icd10_code,
            "icD10Value": icd10_value,
        },
    )


def edit_primary_discharge_diagnosis(cf2_id, discharge_diagnosis_id):
    """Marks a discharge diagnosis as Primary - confirmed via HAR entry
    54. Replaces the old kebab-menu "Set as Primary" click entirely."""
    return _put(
        "/api/PHICDischargeDiagnosis/EditPrimaryPHICDischargeDiagnosis",
        params={
            "phicCf2Id": cf2_id,
            "phicDischargeDiagnosisId": discharge_diagnosis_id,
        },
    )


def add_discharge_diagnosis_n18_5(cf2_id):
    """
    Convenience wrapper matching exactly what
    CF2Automation._add_discharge_diagnosis does today: search N18.5,
    create it, set it as primary. Returns the created (now primary)
    record.
    """
    matches = search_icd10("N18.5")
    if not matches:
        raise Cf2ApiError("SearchICD10('N18.5') returned no matches.")

    icd10_code = matches[0]["icD10Code"]
    icd10_value = matches[0]["icD10Value"]

    created = new_discharge_diagnosis(cf2_id, icd10_code, icd10_value)
    edit_primary_discharge_diagnosis(cf2_id, created["id"])
    return created


# ---------------------------------------------------------------------
# Surgical Procedure + Case Rate ("1st Case Rate" tag) + session dates
# ---------------------------------------------------------------------

def get_surgical_procedures(cf2_id):
    """[] means none exist yet - confirmed via HAR entry 46."""
    return _get(
        "/api/PHICSurgicalProcedure/GetPHICSurgicalProcedure",
        params={"cf2Id": cf2_id},
    )


def new_surgical_procedure(cf2_id, icd10_code, icd10_value, total_sessions):
    """
    Confirmed via HAR entry 61. Hardcoded to RVS 90935 / Hemodialysis
    Procedure, matching what _add_surgical_procedure hardcodes today.
    The response's sessionDate comes back null - see module docstring.
    """
    return _post(
        "/api/PHICSurgicalProcedure/NewPHICSurgicalProcedure",
        json_body={
            "rvsCode": "90935",
            "repetitive": True,
            "name": "Hemodialysis Procedure",
            "icd10Value": icd10_value,
            "icd10Code": icd10_code,
            "numberOfSessions": total_sessions,
            "typeCode": "HEMODIALYSIS",
            "cf2Id": cf2_id,
            "lateralityValue": None,
            "typeValue": "Hemodialysis",
        },
    )


def get_case_rates(cf2_id):
    """[] means the Surgical Procedure has NOT been tagged as 1st Case
    Rate yet - confirmed via HAR entry 48. Replaces the old
    geometry-based "1ST CASE RATE" badge detection entirely."""
    return _get(
        "/api/PHICAllCaseRate/GetPHICAllCaseRates",
        params={"phicCf2Id": cf2_id},
    )


def search_case_rates(rvs_code, target_date_str, hospital_identity):
    """
    Confirmed via HAR entry 63 (eclaimsapi, NOT beacon-s4 itself).
    target_date_str must be MM-DD-YYYY. Returns the raw response dict;
    callers use response["caserates"][0].
    """
    return _post(
        "/SearchCaseRates",
        json_body={
            "icD10Code": "",
            "caseRateDescription": "",
            "rvsCode": rvs_code,
            "targetDate": target_date_str,
            "phicIdentity": hospital_identity,
        },
        base=_eclaims_api_base(),
    )


def tag_first_case(cf2_id, surgical_procedure, case_rate, session_dates):
    """Tag a Surgical Procedure as 1st Case Rate and submit every session date.

    Confirmed by a multi-session HAR capture: Beacon sends ONE
    NewPHICAllCaseRate request containing all session objects. Each session
    keeps its identity/clinical fields, drops the 7 audit fields, and carries
    sessionDate as MM-DD-YYYY. Multi-session support changes only the session
    list/date handling. Case-rate amount and fee logic remain exactly the same
    as the previously working single-session implementation.
    """
    amount = case_rate["amount"][0]

    if not isinstance(session_dates, (list, tuple)):
        session_dates = [session_dates]
    if not session_dates:
        raise Cf2ApiError("tag_first_case requires at least one session date.")

    source_sessions = surgical_procedure.get("sessions") or []
    if len(source_sessions) < len(session_dates):
        raise Cf2ApiError(
            f"Surgical Procedure has {len(source_sessions)} session object(s), "
            f"but {len(session_dates)} session date(s) were supplied."
        )

    sessions = []
    for index, session_date in enumerate(session_dates):
        if hasattr(session_date, "strftime"):
            date_str = session_date.strftime("%m-%d-%Y")
        elif isinstance(session_date, str) and "T" not in session_date:
            try:
                d = datetime.strptime(session_date, "%m-%d-%Y")
            except ValueError:
                d = datetime.strptime(session_date, "%m/%d/%Y")
            date_str = d.strftime("%m-%d-%Y")
        else:
            date_str = str(session_date)

        session = {
            k: v
            for k, v in source_sessions[index].items()
            if k not in (
                "version", "createdById", "createdBy", "dateCreated",
                "updatedById", "updatedBy", "dateUpdated",
            )
        }
        session["sessionDate"] = date_str
        sessions.append(session)

    payload = {
        "icD10Code": surgical_procedure["icD10Code"],
        "icD10Value": surgical_procedure["icD10Value"],
        "name": surgical_procedure["name"],
        "customName": surgical_procedure.get("customName"),
        "rvsCode": surgical_procedure["rvsCode"],
        "repetitive": surgical_procedure["repetitive"],
        "numberOfSites": surgical_procedure.get("numberOfSites", 0),
        "sessions": sessions,
        "id": surgical_procedure["id"],
        "version": surgical_procedure.get("version", 0),
        "createdById": surgical_procedure.get("createdById"),
        "createdBy": surgical_procedure.get("createdBy"),
        "dateCreated": surgical_procedure.get("dateCreated"),
        "updatedById": surgical_procedure.get("updatedById", 0),
        "updatedBy": surgical_procedure.get("updatedBy"),
        "dateUpdated": surgical_procedure.get("dateUpdated"),
        "surgicalProcedure": surgical_procedure["name"],
        "caseRateType": 0,
        "phicCf2Id": cf2_id,
        "sourceType": 1,
        "sourceId": surgical_procedure["id"],
        "caseRateGroupDescription": case_rate["pCaseRateDescription"],
        "effectivityDate": case_rate["pEffectivityDate"],
        "lateralityType": "NA",
        "caseRateCode": case_rate["pCaseRateCode"],
        "caseRateAmount": float(amount["pPrimaryCaseRate"]),
        "hospitalFee": amount["pPrimaryHCIFee"],
        "profFee": amount["pPrimaryProfFee"],
    }
    return _post("/api/PHICAllCaseRate/NewPHICAllCaseRate", json_body=payload)

def add_surgical_procedure_and_tag_first_case(
    cf2_id,
    icd10_code,
    icd10_value,
    total_sessions,
    session_date_str,
    hospital_identity,
):
    """
    Convenience wrapper combining new_surgical_procedure() +
    search_case_rates() + tag_first_case() into one call, matching the
    always-together workflow confirmed with the user (session dates are
    always filled immediately before 1st Case Rate tagging, and the two
    are one backend operation regardless). Returns the tag_first_case()
    response.
    """
    procedure = new_surgical_procedure(
        cf2_id, icd10_code, icd10_value, total_sessions
    )

    case_rate_response = search_case_rates(
        procedure["rvsCode"], session_date_str, hospital_identity
    )
    caserates = case_rate_response.get("caserates") or []
    if not caserates:
        raise Cf2ApiError(
            f"SearchCaseRates returned no case rates for RVS "
            f"{procedure['rvsCode']} / {session_date_str}."
        )

    return tag_first_case(cf2_id, procedure, caserates[0], [session_date_str])


# ---------------------------------------------------------------------
# Doctors
# ---------------------------------------------------------------------

def get_doctors(claim_id):
    """[] means none exist yet - confirmed via HAR entry 47."""
    return _get(
        "/api/PHICDoctor/GetAllPHICDoctorByClaimId",
        params={"claimId": claim_id},
    )


def get_doctor_by_accreditation_number(client_id, accreditation_number):
    """Confirmed via HAR entry 67 - this is what "Autofill Doctor
    Information" triggers today."""
    return _get(
        "/api/PHICDoctor/GetDoctorByAccreditationNumber",
        params={
            "clientId": client_id,
            "accreditationNumber": accreditation_number,
        },
    )


def is_doctor_accredited(
    accreditation_number,
    admission_date_str,
    discharge_date_iso,
    hospital_identity,
):
    """Confirmed via HAR entry 69 (eclaimsapi). admission_date_str is
    MM-DD-YYYY; discharge_date_iso is a UTC-midnight ISO string, see
    to_utc_midnight_iso() above."""
    return _post(
        "/IsDoctorAccredited",
        json_body={
            "doctorAccreditationCode": accreditation_number,
            "admissionDate": admission_date_str,
            "dischargeDate": discharge_date_iso,
            "phicIdentity": hospital_identity,
        },
        base=_eclaims_api_base(),
    )


def new_doctor(
    claim_id,
    doctor_info,
    sign_date_str,
    admission_date_str,
    discharge_date_iso,
    is_accredited,
):
    """
    Confirmed via HAR entry 70. `doctor_info` is the dict returned by
    get_doctor_by_accreditation_number() above.

    NOTE on the "id" field: the captured request sent the CLAIM id here
    (not a doctor id) - confirmed by comparing against the response,
    which comes back with a genuinely new doctor id. This looks like an
    artifact of Beacon's own form model rather than something meaningful,
    but it's replicated exactly as observed since we can't verify
    changing it wouldn't break something server-side.

    NOTE on "accredited"/"Accredited": the captured request sends BOTH a
    lowercase boolean `accredited: false` AND a separate capitalized
    string `Accredited: "YES"/"NO"` - also replicated exactly as
    observed, redundant as it looks.
    """
    accredited_str = "YES" if is_accredited else "NO"

    payload = {
        "accreditationNumber": doctor_info["accreditationNumber"],
        "firstname": doctor_info["firstname"],
        "lastname": doctor_info["lastname"],
        "middlename": doctor_info.get("middlename"),
        "suffix": doctor_info.get("suffix"),
        "withCoPay": doctor_info.get("withCoPay", "N"),
        "coPayAmount": doctor_info.get("coPayAmount", 0),
        "accredited": False,
        "fullname": doctor_info["fullname"],
        "doctorSignDate": sign_date_str,
        "phicnoNotApplicable": doctor_info.get("phicnoNotApplicable", False),
        "oecbPFCharges": doctor_info.get("oecbPFCharges"),
        "id": claim_id,
        "version": doctor_info.get("version", 1),
        "createdById": doctor_info.get("createdById"),
        "createdBy": doctor_info.get("createdBy"),
        "dateCreated": doctor_info.get("dateCreated"),
        "updatedById": doctor_info.get("updatedById"),
        "updatedBy": doctor_info.get("updatedBy"),
        "dateUpdated": doctor_info.get("dateUpdated"),
        "admissionDate": admission_date_str,
        "dischargeDate": discharge_date_iso,
        "Accredited": accredited_str,
        "webServiceOff": False,
    }
    return _post("/api/PHICDoctor/NewPHICDoctor", json_body=payload)


def add_doctor(
    claim_id,
    client_id,
    accreditation_number,
    sign_date_str,
    admission_date_str,
    discharge_date_iso,
    hospital_identity,
):
    """
    Convenience wrapper matching exactly what
    CF2Automation._add_doctor does today: autofill by accreditation
    number, check accreditation, save. Returns the created doctor
    record.
    """
    doctor_info = get_doctor_by_accreditation_number(
        client_id, accreditation_number
    )

    accreditation_check = is_doctor_accredited(
        accreditation_number,
        admission_date_str,
        discharge_date_iso,
        hospital_identity,
    )
    is_accredited = (accreditation_check.get("isaccredited") == "YES")

    return new_doctor(
        claim_id,
        doctor_info,
        sign_date_str,
        admission_date_str,
        discharge_date_iso,
        is_accredited,
    )


def to_date_signed_iso(local_date):
    """
    NewPdfClaimFormTwo's dateSigned field uses yet another date
    convention from the rest of this codebase - confirmed via HAR:
    local date 07-01-2026 was sent as dateSigned="2026-07-01T16:00:00Z".
    That's the SAME calendar day (unlike to_utc_midnight_iso, which
    shifts back a day), at 16:00 UTC, with a bare "Z" and no
    milliseconds (unlike to_utc_midnight_iso/to_utc_noon_iso's
    ".000Z"). Accepts a date or datetime; returns that exact string
    format.
    """
    d = local_date.date() if hasattr(local_date, "date") else local_date
    return d.strftime("%Y-%m-%dT16:00:00Z")


def get_cf2_pdf_details(claim_id, client_id):
    """
    GET the current data for the Claim Form 2 PDF page (the page at
    .../eclaims/download-pdf/cf2/... - NOT the same as the CF2 data
    tab covered by get_cf2()/edit_cf2() elsewhere in this file; this is
    a completely separate page/endpoint pair confirmed via a fresh HAR
    capture).

    NOTE: this HAR capture did not include the response body (DevTools
    HAR export can omit response content depending on export
    settings), so the exact shape returned here is INFERRED from what
    a real NewPdfClaimFormTwo save request sent back (see
    new_pdf_claim_form_two()'s docstring for that full field list) -
    not independently confirmed the way get_cf2()'s shape was. Verify
    with a real save before trusting this in the full pipeline
    unattended - see verify_claim_form_two_api.py.
    """
    return _get(
        "/api/PHICDocument/GetCf2PdfDetails",
        params={"PHICClaimId": claim_id, "Type": "cf2", "clientId": client_id},
    )


def new_pdf_claim_form_two(claim_id, data):
    """
    Save the Claim Form 2 PDF page - confirmed via a fresh HAR capture
    of a real, successful save (200 OK). The full "data" dict sent in
    that capture (for reference/to sanity-check shape against
    get_cf2_pdf_details()'s response before trusting it) included:

        nameOfReferringHCI, bldgNoAndStreetName, cityMunicipality,
        province, zipCode, pdNameOfReferralHCI, pdBldgNoAndStreetName,
        pdCityMunicipality, pdProvince, pdZipCode,
        phBenefitIsEnoughToCoverHCIAndPFCharges,
        totalActualChargesHCIFees, totalActualChargesProfessionalFees,
        totalActualChargesGrandTotal, benefitOfTheMemberPatient,
        totalHCIFeesActualCharges, totalHCIFeesAmountAfterApplication,
        totalHCIPhilHealthBenefit, totalHCIFeesAmount,
        totalHCIFeesMemberPatient, totalHCIFeesHMO, totalHCIFeesOthers,
        totalProfessionalFeesActualCharges,
        totalProfessionalFeesAmountAfterApplication,
        totalProfessionalPhilHealthBenefit, totalProfessionalFeesAmount,
        totalProfessionalFeesMemberPatient, totalProfessionalFeesHMO,
        totalProfessionalFeesOthers, totalCostOfPurchasesNone,
        totalCostOfPurchasesTotalAmountChk, totalCostOfPurchasesTotalAmount,
        totalCostOfDiagnosticNone, totalCostOfDiagnosticTotalAmountChk,
        totalCostOfDiagnosticTotalAmount, sigOverPrintedNameOfAuthRep,
        patientCheck, part3RepresentativeCheck, patientFullname,
        signatureOverPrintedNameOfAuthHCIRep, officialCapacityDesignation,
        dateSigned

    Only the last three (signatureOverPrintedNameOfAuthHCIRep,
    officialCapacityDesignation, dateSigned) are what this migration
    actually needs to change - everything else must be carried through
    from get_cf2_pdf_details() unchanged, same read-modify-write
    pattern as edit_cf2().

    claim_id is sent as a STRING (confirmed via HAR - "16563846", not
    16563846) - pass str(claim_id) if unsure.
    """
    return _post(
        "/api/PHICDocument/NewPdfClaimFormTwo",
        json_body={
            "phicClaimId": str(claim_id),
            "type": "cf2",
            "revision": "revision",
            "data": data,
        },
    )
