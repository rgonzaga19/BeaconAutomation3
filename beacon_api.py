"""Direct Beacon API layer for CF4 automation.

This module mirrors the HTTP calls observed in the recorded CF4 workflow.
Business rules (CF4 settings, medicine selection rules, retry/report behavior)
remain in beacon.py.
"""

from datetime import datetime, timedelta
import requests

import browser_session


class BeaconApiError(RuntimeError):
    pass


_client_id_cache = None


def _base_url():
    getter = getattr(browser_session, "_get_beacon_url", None)
    if callable(getter):
        return getter().rstrip("/")
    return "https://beacon-s4.bizbox.ph"


def _headers():
    token = browser_session.get_auth_token()
    if not token:
        raise BeaconApiError("Beacon auth token is unavailable")

    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _check(response):
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        body = (response.text or "")[:1500]
        raise BeaconApiError(
            f"{response.request.method} {response.url} failed "
            f"({response.status_code}): {body}"
        ) from exc
    return response


def _json(response):
    _check(response)

    if not response.text:
        return None

    try:
        return response.json()
    except ValueError:
        return response.text.strip('"')


def _get(path, params=None):
    return _json(
        requests.get(
            _base_url() + path,
            headers=_headers(),
            params=params,
            timeout=30,
        )
    )


def _post(path, json_body=None, params=None):
    return _json(
        requests.post(
            _base_url() + path,
            headers=_headers(),
            params=params,
            json=json_body,
            timeout=60,
        )
    )


def get_client_id():
    """Resolve the current Beacon client/facility ID dynamically."""
    global _client_id_cache

    if _client_id_cache is not None:
        return _client_id_cache

    user_id = browser_session.get_user_id()
    if not user_id:
        raise BeaconApiError("Beacon userId is unavailable")

    clients = _get(
        "/api/Account/GetAllClientsByUserId",
        params={"userId": user_id},
    ) or []

    if not clients:
        raise BeaconApiError(
            f"GetAllClientsByUserId returned no clients for userId={user_id}"
        )

    _client_id_cache = int(clients[0]["id"])
    return _client_id_cache


def get_transmittal(transmittal_no, client_id=None):
    """Search the exact transmittal number, preserving the original workflow."""
    if client_id is None:
        client_id = get_client_id()

    # Same Transmittals table request shape used by Beacon/SOA API migration.
    today = datetime.now().date()
    date_from = today - timedelta(days=31)
    date_to = today + timedelta(days=1)

    data = _get(
        "/api/PHICTransmittal/GetAllPHICTransmittal",
        params={
            "clientId": client_id,
            "dateFrom": date_from.strftime("%Y-%m-%dT16:00:00.000Z"),
            "dateTo": date_to.strftime("%Y-%m-%dT15:59:59.999Z"),
            "itemStart": 0,
            "itemEnd": 30,
            "que": str(transmittal_no),
            "transmittalPackageType": 7,
        },
    ) or {}

    if isinstance(data, list):
        rows = data
    else:
        rows = (
            data.get("transmittalList")
            or data.get("items")
            or data.get("data")
            or []
        )

    exact = [
        row for row in rows
        if str(row.get("transmittalNumber") or "").strip()
        == str(transmittal_no).strip()
    ]

    return exact[0] if exact else None


def get_transmittal_by_id(transmittal_id):
    return _get(
        "/api/PHICTransmittal/GetPHICTransmittalById",
        params={"transmittalId": transmittal_id},
    )


def get_claims(transmittal_id):
    data = _get(
        "/api/PHICClaim/GetAllPHICClaimByPHICTransmittalId",
        params={"transmittalId": transmittal_id},
    ) or []

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in (
            "phicClaims",
            "claims",
            "claimList",
            "items",
            "data",
        ):
            value = data.get(key)
            if isinstance(value, list):
                return value

    return []


def get_claim(claim_id):
    return _get(
        "/api/PHICClaim/GetPHICClaim",
        params={"id": claim_id},
    )


def get_cf4_values(claim_id):
    return _get(
        "/api/PHICCF4/GetCf4Values",
        params={"ClaimId": claim_id},
    )


def get_doctors_by_claim_id(claim_id):
    """Return doctors already encoded on the claim."""
    data = _get(
        "/api/PHICDoctor/GetAllPHICDoctorByClaimId",
        params={"claimId": claim_id},
    ) or []

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in ("items", "data", "doctors", "phicDoctors"):
            value = data.get(key)
            if isinstance(value, list):
                return value

    return []


def new_pdf_cf4(claim_id, doctor_name, date_signed):
    """Save the CF4 New-tab attending doctor/sign date data."""
    return _post(
        "/api/PHICDocument/NewPdfCF4",
        json_body={
            "phicClaimId": str(claim_id),
            "type": "cf4",
            "revision": "revision",
            "data": {
                "sigOverPrintedNameOfAttendingHCProf": doctor_name,
                "dateSigned": date_signed,
            },
        },
    )


def get_surgical_procedures(cf2_id):
    data = _get(
        "/api/PHICSurgicalProcedure/GetPHICSurgicalProcedure",
        params={"cf2Id": cf2_id},
    ) or []

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in ("items", "data", "procedures", "surgicalProcedures"):
            value = data.get(key)
            if isinstance(value, list):
                return value

    return []


def search_medicines(search_term):
    data = _post(
        "/api/Medicine/SearchMedicines",
        json_body={"search": search_term},
    ) or []

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in ("items", "data", "result", "medicines"):
            value = data.get(key)
            if isinstance(value, list):
                return value

    return []


def save_cf4_values(payload):
    return _post(
        "/api/PHICCF4/SavePhicCf4Values",
        json_body=payload,
    )
