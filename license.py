import requests

LICENSE_SERVER = "https://beabot-license.gonzagaromel19.workers.dev"


class LicenseError(Exception):
    """Raised when license validation fails."""
    pass


def validate_license(license_key: str) -> dict:
    """
    Validate a license against the Cloudflare Worker.

    Returns a dictionary like:

    {
        "valid": True,
        "owner": "Romel Gonzaga",
        "plan": "Professional",
        "expires": "2027-12-31"
    }

    or

    {
        "valid": False
    }
    """

    if not license_key:
        return {"valid": False}

    try:
        response = requests.post(
            LICENSE_SERVER,
            json={"license": license_key.strip()},
            timeout=10
        )

        response.raise_for_status()

        return response.json()

    except requests.exceptions.Timeout:
        raise LicenseError("Connection to the license server timed out.")

    except requests.exceptions.ConnectionError:
        raise LicenseError("Unable to connect to the license server.")

    except requests.exceptions.HTTPError as e:
        raise LicenseError(
            f"License server returned HTTP {response.status_code}"
        ) from e

    except Exception as e:
        raise LicenseError(str(e))