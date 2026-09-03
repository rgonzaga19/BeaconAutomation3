const LICENSES = [
  {
    key: "ABCD-1234-EFGH-5678",
    owner: "Romel Gonzaga",
    plan: "Professional",
    expires: "2026-12-31",
  },
  {
    key: "BEABOTS-0002-AAAA-BBBB",
    owner: "Encoders",
    plan: "Standard",
    expires: "2026-08-31",
  },
  {
    key: "BEABOTS-0003-CCCC-DDDD",
    owner: "Nephro",
    plan: "Enterprise",
    expires: "2027-12-31",
  },
];

// -----------------------------------------------------------------------------
// Release and minimum-version configuration
// -----------------------------------------------------------------------------
// Safe rollout:
//   1. Publish an app build that sends app_version with license checks.
//   2. Set UPDATE_INFO and MIN_SUPPORTED_VERSION to that released version.
//   3. Verify the installer URL.
//   4. Change ENFORCE_MINIMUM_VERSION to true and deploy this Worker again.
const ENFORCE_MINIMUM_VERSION = false;
const MIN_SUPPORTED_VERSION = "4.0.1";

const UPDATE_INFO = {
  version: "4.0.1",
  minimum_version: MIN_SUPPORTED_VERSION,
  mandatory: false,
  notes: [
    "v4.0.1 — Fully migrated CF2-CF4 processing from Playwright UI automation to direct API calls, improving speed and reliability.",
  ],
  download:
    "https://github.com/rgonzaga19/BeaconAutomation3/releases/download/Beabots/Beabots_Setup_v4.0.1.exe",
};

// -----------------------------------------------------------------------------
// HTTP helpers
// -----------------------------------------------------------------------------
const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
  "Cache-Control": "no-store",
};

function jsonResponse(body, status = 200) {
  return Response.json(body, {
    status,
    headers: CORS_HEADERS,
  });
}

function normalizeVersion(value) {
  const match = String(value || "")
    .trim()
    .match(/^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$/i);

  if (!match) return null;

  return [Number(match[1]), Number(match[2]), Number(match[3])];
}

function compareVersions(left, right) {
  const leftParts = normalizeVersion(left);
  const rightParts = normalizeVersion(right);

  if (!leftParts || !rightParts) return null;

  for (let index = 0; index < 3; index += 1) {
    if (leftParts[index] > rightParts[index]) return 1;
    if (leftParts[index] < rightParts[index]) return -1;
  }

  return 0;
}

function requiresUpdate(appVersion) {
  const comparison = compareVersions(appVersion, MIN_SUPPORTED_VERSION);
  return comparison === null || comparison < 0;
}

function updateRequiredResponse() {
  return jsonResponse({
    valid: false,
    code: "UPDATE_REQUIRED",
    reason: "Update required",
    minimum_version: MIN_SUPPORTED_VERSION,
    latest_version: UPDATE_INFO.version,
    download: UPDATE_INFO.download,
  });
}

function todayUtc() {
  return new Date().toISOString().slice(0, 10);
}

// -----------------------------------------------------------------------------
// Cloudflare Worker
// -----------------------------------------------------------------------------
export default {
  async fetch(request) {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: CORS_HEADERS,
      });
    }

    if (request.method === "GET" && url.pathname === "/update") {
      return jsonResponse(UPDATE_INFO);
    }

    // license.py currently sends its validation request to the Worker root.
    if (request.method === "POST" && url.pathname === "/") {
      let body;

      try {
        body = await request.json();
      } catch {
        return jsonResponse(
          {
            valid: false,
            code: "INVALID_REQUEST",
            reason: "The request body must be valid JSON.",
          },
          400,
        );
      }

      const licenseKey = String(body?.license || "").trim();
      const appVersion = String(body?.app_version || "").trim();

      if (!licenseKey) {
        return jsonResponse(
          {
            valid: false,
            code: "LICENSE_REQUIRED",
            reason: "A license key is required.",
          },
          400,
        );
      }

      const license = LICENSES.find((entry) => entry.key === licenseKey);

      if (!license) {
        return jsonResponse({
          valid: false,
          code: "INVALID_LICENSE",
          reason: "Invalid license.",
        });
      }

      // A license remains valid for its entire listed expiration date.
      if (license.expires < todayUtc()) {
        return jsonResponse({
          valid: false,
          code: "LICENSE_EXPIRED",
          reason: "License expired.",
          expires: license.expires,
        });
      }

      if (ENFORCE_MINIMUM_VERSION && requiresUpdate(appVersion)) {
        return updateRequiredResponse();
      }

      return jsonResponse({
        valid: true,
        owner: license.owner,
        plan: license.plan,
        expires: license.expires,
      });
    }

    if (request.method === "GET") {
      return jsonResponse(
        {
          valid: false,
          code: "NOT_FOUND",
          reason: "Not Found",
        },
        404,
      );
    }

    return jsonResponse(
      {
        valid: false,
        code: "METHOD_NOT_ALLOWED",
        reason: "Method Not Allowed",
      },
      405,
    );
  },
};
