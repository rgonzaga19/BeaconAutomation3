"""API implementation of Beacon Create Draft + Add Claims flow.

Endpoints and payloads are based on captured successful Member and Dependent HARs.
"""
import re
from datetime import datetime
import requests
import browser_session
import cf2_api

ECLAIMS_API_BASE = "https://eclaimsapi-s4.azurewebsites.net/api/EClaims/v3"
PRODUCT_ID = 271  # Confirmed Beacon eClaims product id in captured account/session.

class DraftApiError(RuntimeError):
    pass

class InvalidMemberPinError(Exception):
    pass

def _base_url():
    return "https://beacon-s4.bizbox.ph"

def _headers():
    token = browser_session.get_auth_token()
    if not token:
        raise DraftApiError("No Beacon bearer token available.")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

def _request(method, path, *, params=None, json_body=None, base=None):
    url = f"{base or _base_url()}{path}"
    r = requests.request(method, url, headers=_headers(), params=params, json=json_body, timeout=20)
    if not r.ok:
        raise DraftApiError(f"{method} {path} failed: HTTP {r.status_code}: {r.text[:500]}")
    return r.json() if r.content else None

def _get(path, params=None, base=None): return _request("GET", path, params=params, base=base)
def _post(path, json_body=None, params=None, base=None): return _request("POST", path, params=params, json_body=json_body, base=base)
def _put(path, json_body=None, params=None, base=None): return _request("PUT", path, params=params, json_body=json_body, base=base)

def _mmddyyyy(value):
    # Caller dates are normally Excel-derived strings such as 07/01/2026,
    # but keep this tolerant of the other date representations already used
    # by the automation.  Do not truncate before trying date-only formats.
    if isinstance(value, str):
        text = value.strip()
        candidates = (
            (text, "%m/%d/%Y"),
            (text, "%m-%d-%Y"),
            (text, "%Y-%m-%d"),
            (text[:19], "%Y-%m-%dT%H:%M:%S"),
        )
        for candidate, fmt in candidates:
            try:
                return datetime.strptime(candidate, fmt).strftime("%m-%d-%Y")
            except ValueError:
                pass
    if hasattr(value, "strftime"):
        return value.strftime("%m-%d-%Y")
    raise DraftApiError(f"Unsupported date value: {value!r}")

def _utc_midnight(value):
    # Reuse the already verified PH-local-midnight conversion.
    return cf2_api.to_utc_midnight_iso(datetime.strptime(_mmddyyyy(value), "%m-%d-%Y"))

def get_primary_hospital_code():
    rows = _get("/api/Product/GetHospitalCodes", params={"productid": PRODUCT_ID}) or []
    if not rows:
        raise DraftApiError("GetHospitalCodes returned no hospital codes.")
    return next((x for x in rows if x.get("primaryCode")), rows[0])

def create_transmittal(draft_title):
    client_id = cf2_api.get_client_id()
    hospital = get_primary_hospital_code()
    payload = {
        "phicPackage": str(hospital.get("phicPackage", 0)),
        "phichciType": hospital["phichciType"],
        "hospitalCode": hospital["hospitalCode"],
        "accreditationNumber": hospital["accreditationNumber"],
        "isHemodialysis": True,
        "remarks": draft_title,
        "clientId": client_id,
        "verified": bool(hospital.get("verified", True)),
    }
    row = _post("/api/PHICTransmittal/NewPHICTransmittal", json_body=payload)
    if not isinstance(row, dict) or not row.get("id"):
        raise DraftApiError("NewPHICTransmittal did not return a transmittal id.")
    return row

def get_patient_by_member_pin(member_pin, admission_date, discharge_date):
    client_id = cf2_api.get_client_id()
    # The query response observed in the HAR reflects the dates currently in its
    # prior CF1 record, so the new claim payload below always overwrites dates
    # with this run's requested admission/discharge dates.
    return _get("/api/PHICCF1/GetPatientInfoByMemberPinQuery", params={"memberPin": member_pin, "clientId": client_id})

def get_dependents_by_member_pin(member_pin):
    """Return Beacon's dependent list in its original order.

    The proven UI selected the first dependent shown, so the API migration
    intentionally preserves that exact selection rule.
    """
    client_id = cf2_api.get_client_id()
    rows = _get(
        "/api/PHICCF1/GetDependentPatientInfoByMemberPin",
        params={"memberPin": member_pin, "clientId": client_id},
    )
    return rows if isinstance(rows, list) else []

def verify_member_pin(patient, identity):
    payload = {
        "lastname": patient.get("memberLastname") or "",
        "firstname": patient.get("memberFirstname") or "",
        "middlename": patient.get("memberMiddlename") or "",
        "suffix": patient.get("memberSuffix") or "",
        "birthdate": _mmddyyyy(patient.get("memberBirthday")),
        "phicIdentity": identity,
    }
    result = _post("/GetMemberPIN", json_body=payload, base=ECLAIMS_API_BASE) or {}
    return re.sub(r"\D", "", str(result.get("pin") or ""))

def _claim_payload(patient, member_pin, transmittal_id, admission_date, discharge_date, include_facility=False):
    identity = cf2_api.get_hospital_identity(transmittal_id)
    p = {
        # Both captured Member and Dependent Add Claims requests send this
        # literal value. Patient relationship is carried separately below.
        "patientis": "M - Member",
        "memberemployername": patient.get("memberEmployerName") or "",
        "membermiddlename": patient.get("memberMiddlename") or "",
        "memberlastname": patient.get("memberLastname") or "",
        "memberfirstname": patient.get("memberFirstname") or "",
        "memberbirthday": _mmddyyyy(patient.get("memberBirthday")),
        "membergender": patient.get("memberGender") or "",
        "memberpin": member_pin,
        "membertypecode": patient.get("memberTypeCode"),
        "membermailingaddress": patient.get("memberMailingAddress") or "",
        "memberzipcode": patient.get("memberZipCode") or "",
        "memberlandlinenumber": patient.get("memberLandLineNumber") or "",
        "membermobilenumber": patient.get("memberMobileNumber") or "",
        "memberemail": patient.get("memberEmail") or "",
        "admissiondate": _utc_midnight(admission_date),
        "dischargedate": _utc_midnight(discharge_date),
        "phicIdentity": identity,
        "patientiscode": patient.get("patientIsCode") or "M",
        "patientisvalue": patient.get("patientIsValue") or "Member",
        "memberfullname": patient.get("memberFullname") or "",
        "membertypevalue": patient.get("memberTypeValue") or "",
        "patientbirthday": _mmddyyyy(patient.get("patientBirthday")),
        "patientlastname": patient.get("patientLastname") or "",
        "patientfirstname": patient.get("patientFirstname") or "",
        "patientmiddlename": patient.get("patientMiddlename") or "",
        "patientsuffix": patient.get("patientSuffix") or "",
        "membersuffix": patient.get("memberSuffix") or "",
        "patientgender": patient.get("patientGender") or "",
        "patientpin": patient.get("patientPin") or member_pin,
        "transmittalId": str(transmittal_id),
        "memberverified": 1,
    }
    if include_facility:
        p["facilityId"] = cf2_api.get_client_id()
    return p

def create_member_claim(member_pin, admission_date, discharge_date, draft_title):
    """Create Member or Dependent claim using the captured Beacon API paths."""
    raw_pin = str(member_pin or "").strip()
    is_dependent = raw_pin.endswith("/") or raw_pin.endswith("\\")
    raw_pin = raw_pin[:-1].strip() if is_dependent else raw_pin
    pin = re.sub(r"\D", "", raw_pin)
    if not pin:
        raise InvalidMemberPinError("Incorrect Member PIN: empty PIN")

    trans = create_transmittal(draft_title)
    transmittal_id = int(trans["id"])
    identity = cf2_api.get_hospital_identity(transmittal_id)

    patient = None
    confirmed_pin = None
    attempts = [pin] + ([] if pin.startswith("0") else ["0" + pin])
    for candidate in attempts:
        try:
            if is_dependent:
                dependents = get_dependents_by_member_pin(candidate)
                # Preserve the old UI logic exactly: it clicked the first
                # dependent in Beacon's returned/displayed list.
                candidate_patient = dependents[0] if dependents else None
            else:
                candidate_patient = get_patient_by_member_pin(
                    candidate, admission_date, discharge_date
                )

            if not isinstance(candidate_patient, dict) or not candidate_patient:
                continue

            returned_pin = verify_member_pin(candidate_patient, identity)
            if returned_pin and returned_pin == candidate:
                patient, confirmed_pin = candidate_patient, candidate
                break
        except Exception:
            continue
    if patient is None:
        raise InvalidMemberPinError(f"Incorrect Member PIN: {attempts[-1]}")

    duplicate_payload = _claim_payload(patient, confirmed_pin, transmittal_id, admission_date, discharge_date, include_facility=True)
    _put("/api/PHICCF1/GetDuplicatePHICCF1Entry", json_body=duplicate_payload)

    claim_payload = _claim_payload(patient, confirmed_pin, transmittal_id, admission_date, discharge_date, include_facility=False)
    claim = _post("/api/PHICCF1/NewPHICClaimsRecord", json_body=claim_payload)
    if not isinstance(claim, dict) or not claim.get("id"):
        raise DraftApiError("NewPHICClaimsRecord did not return a claim id.")
    claim_id = int(claim["id"])

    # Original draft flow clicks Validate Eligibility before handing off to CF2.
    # Reuse the already proven implementation. CF2's later validation call is
    # idempotent and will skip because eligibilityIsOk is now populated.
    cf2_api.validate_claim_eligibility(claim_id, transmittal_id, admission_date, discharge_date)

    return {
        "transmittal_id": transmittal_id,
        "claim_id": claim_id,
        "transmittal_number": trans.get("transmittalNumber"),
        "member_pin": confirmed_pin,
        "is_dependent": is_dependent,
        "patient_pin": patient.get("patientPin"),
    }
