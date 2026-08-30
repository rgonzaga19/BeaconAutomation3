"""
Direct HTTP client for a subset of Beacon's PHIC CF2 backend endpoints,
discovered by capturing (HAR) a real CF2 encoding session in the browser
and confirming the exact request/response shape for each call used here.

This bypasses the Beacon UI entirely for the operations it covers - no
Playwright involved - authenticating via the bearer token from
browser_session.get_auth_token() (the same OAuth2 /token endpoint
Beacon's own SIGN IN button uses, confirmed via an earlier HAR).

Every function raises on a non-2xx response (via response.raise_for_status())
so callers see a real, specific error instead of a generic timeout, and
returns the parsed JSON body (or None for an empty response) on success.
cf2_automation.py wraps every call site in try/except and falls back to
the existing UI automation on failure, so a wrong assumption here (field
name, required value, etc.) degrades to "no worse than before" rather
than breaking a patient outright.

Covered here (confirmed via HAR, request AND response bodies inspected):
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

Deliberately NOT covered here (still pure UI automation in
cf2_automation.py) because we don't have a captured/confirmed endpoint,
or the capture was incomplete:
  - Draft creation / Add Claims / Member PIN validation
    (draft_automation.py's own flow - has its own set of endpoints we
    haven't dumped the request/response bodies for yet)
  - Transmittal search / Manage Claims / Manage navigation
  - The main CF2 field save (EditPHICCF2) - the captured request body
    was cut off mid-JSON, so we don't have full certainty about every
    field Beacon expects back unchanged (newborn care, TB DOTS, animal
    bite, cataract sections, etc. all live in this same record).
    Blindly submitting a payload built from scratch risks wiping fields
    this migration doesn't know about.
  - Statement of Account's "Claim Form 2" PDF tab signature fields -
    NewPdfClaimFormTwo (confirmed) just generates the PDF; whatever the
    signature/date-signed SAVE on that tab actually calls wasn't
    captured (that HAR only has the one PDF-generation request).
"""

import re
from datetime import datetime, timedelta

import requests

import browser_session


ECLAIMS_API_BASE = "https://eclaimsapi-s4.azurewebsites.net/api/EClaims/v3"


class Cf2ApiError(Exception):
    """Raised for any cf2_api failure that isn't already a requests
    exception (e.g. missing auth token, unexpected response shape).
    Callers should treat this the same as any other exception here:
    catch it and fall back to UI automation."""
    pass


def _base_url():
    return browser_session._get_beacon_url().rstrip("/")


def _headers():
    token = browser_session.get_auth_token()
    if not token:
        raise Cf2ApiError(
            "No API auth token available - caller should fall back to "
            "UI automation."
        )
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


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

    Raises Cf2ApiError if the URL doesn't match this pattern - callers
    should treat that as "API path unavailable, fall back to UI" like
    any other failure here.
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
        raise Cf2ApiError(
            "No userId available (get_user_id() failed) - caller "
            "should fall back to UI automation."
        )

    clients = _get(
        "/api/Account/GetAllClientsByUserId", params={"userId": user_id}
    )
    if not clients:
        raise Cf2ApiError(f"GetAllClientsByUserId returned no clients for userId={user_id}.")

    _client_id_cache = clients[0]["id"]
    return _client_id_cache


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
        base=ECLAIMS_API_BASE,
    )


def tag_first_case(cf2_id, surgical_procedure, case_rate, session_date):
    """
    Tags a Surgical Procedure as 1st Case Rate AND persists its (single)
    session date in the same call - confirmed via HAR entry 64
    (NewPHICAllCaseRate). This module only ever creates a Surgical
    Procedure with 1 session (numberOfSessions passed to
    new_surgical_procedure), matching current behavior, so there's
    exactly one session dict to update here.

    `surgical_procedure` is the dict returned by new_surgical_procedure()
    above; `case_rate` is caserates[0] from search_case_rates()'s
    response. `session_date` is a date/datetime or date string.
    """
    amount = case_rate["amount"][0]

    # Convert session_date to UTC midnight ISO string matching Beacon's
    # frontend serialization for DateTime fields
    if hasattr(session_date, "strftime"):
        session_date_iso = to_utc_midnight_iso(session_date)
    elif isinstance(session_date, str) and "T" not in session_date:
        try:
            d = datetime.strptime(session_date, "%m-%d-%Y")
        except ValueError:
            d = datetime.strptime(session_date, "%m/%d/%Y")
        session_date_iso = to_utc_midnight_iso(d)
    else:
        session_date_iso = str(session_date)

    # In the confirmed working payload, sessions[0] has exactly 8 keys:
    # the 7 audit/tracking keys from NewPHICSurgicalProcedure's response
    # (version, createdById, createdBy, dateCreated, updatedById, updatedBy,
    # dateUpdated) must be excluded, otherwise Beacon's backend silently
    # ignores the session update and leaves sessionDate null.
    session = {
        k: v
        for k, v in surgical_procedure["sessions"][0].items()
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
    session["sessionDate"] = session_date_iso

    payload = {
        "icD10Code": surgical_procedure["icD10Code"],
        "icD10Value": surgical_procedure["icD10Value"],
        "name": surgical_procedure["name"],
        "customName": surgical_procedure.get("customName"),
        "rvsCode": surgical_procedure["rvsCode"],
        "repetitive": surgical_procedure["repetitive"],
        "numberOfSites": surgical_procedure.get("numberOfSites", 0),
        "sessions": [session],
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

    return tag_first_case(cf2_id, procedure, caserates[0], session_date_str)


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
        base=ECLAIMS_API_BASE,
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