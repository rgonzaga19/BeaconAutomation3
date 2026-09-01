# Beabots

Beabots is a Windows desktop application for API-driven automation and automated encoding of PhilHealth Beacon claims. It combines an Electron user interface with a local Python service that handles authentication, licensing, Excel processing, CF2/CF4 workflows, SOA uploads, reports, and live automation logs.

Current application version: **4.0.1**

## What the application contains

- **Electron desktop shell** — login, dashboard, settings, About, CF2, CF4, and Upload SOA views.
- **Local Flask/Socket.IO service** — exposes the Python workflows to Electron on `127.0.0.1:5417`.
- **API automation** — communicates with Beacon and EClaims APIs for supported operations.
- **Excel processing** — reads CF2 workbooks and serves the bundled templates in `templates/`.
- **License validation and updates** — communicates with the Beabots license/update service.

## Architecture

```text
main.js                   Electron entry point and Python-service lifecycle
preload.js                Safe bridge exposed to renderer windows
config.js                 Local service port and API base URL

renderer/                 HTML, CSS, and renderer-side JavaScript
├── login.html
├── dashboard.html
├── cf2.html
├── cf4.html
├── upload-soa.html
├── settings.html
├── about.html
├── css/
└── js/

server.py                 Flask and Socket.IO API entry point
browser_session.py        Shared Beacon OAuth2 authentication and token cache
beacon*.py                Beacon automation and API operations
cf2*.py                   CF2 parsing, mapping, fees, and automation
draft*.py                 Draft creation and naming
soa*.py                   SOA discovery, validation, and upload
license.py                Remote license validation
login.py / settings.py    Persistent application settings
logger.py / reports.py    Runtime logs and generated reports

Beabots.spec              PyInstaller definition for server.exe
package.json              Electron dependencies and electron-builder config
templates/                CF2 Excel templates bundled with the Python service
```

In development, Electron starts `python -u server.py`. In a packaged application, it starts `resources/server/server.exe` instead.

## Fresh Windows environment

### Required software

Install the following on the development/build computer:

1. **64-bit Windows 10 or 11**
2. **Git**
3. **Node.js 22 or 24 LTS**, including npm
4. **64-bit Python 3.12 or newer**, including `pip` and the Python launcher
5. **Microsoft Visual C++ Redistributable 2015–2022 (x64)**

Internet access is required for npm packages, Python packages, license validation, update checks, and Beacon/EClaims endpoints.

Confirm the tools are available:

```powershell
git --version
node --version
npm.cmd --version
py --version
```

`npm.cmd` is used in these examples because some PowerShell installations block `npm.ps1` through their execution policy. Plain `npm` is fine when it works normally.

### Clone and install dependencies

```powershell
git clone <repository-url>
Set-Location BeaconAutomation3

py -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

npm.cmd ci
```

If PowerShell blocks virtual-environment activation, either allow locally signed scripts for the current user or call the environment's Python executable directly:

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

Do not commit credentials. Runtime settings are created under:

```text
%LOCALAPPDATA%\Beabots\config.json
%LOCALAPPDATA%\Beabots\logs\
```

The root `config.json` is not the active per-user configuration used by the running application.

## Run in development

Activate the Python environment before starting Electron. The Electron process inherits that environment and uses its `python` executable when it launches `server.py`.

```powershell
Set-Location BeaconAutomation3
.\venv\Scripts\Activate.ps1
npm.cmd start
```

The expected startup sequence is:

1. Electron launches the Python service.
2. The service listens on `http://127.0.0.1:5417`.
3. Electron waits for `/api/settings` to respond.
4. The application checks for a mandatory update.
5. The login window opens when no mandatory update is pending.

To diagnose Python startup separately:

```powershell
.\venv\Scripts\Activate.ps1
python server.py
```

Stop it with `Ctrl+C` before running `npm.cmd start`, because both processes use port `5417`.

### First-run configuration

In the application, provide:

- Beacon username and password
- Beabots access/license key
- Beacon server selection (`S2` or `S4`)
- SOA folder when using Upload SOA

## Rebuild the application

Electron-builder assembles the unpacked Electron application, and Inno Setup
creates the installer distributed to users. Continue using Inno Setup for every
release so its permanent `AppId` upgrades existing installations in place.

### 1. Keep versions synchronized

Before a release, update the same version in:

- `package.json` -> `version`
- `package-lock.json` -> root package version, normally updated by npm
- `BeaconInstaller.iss` -> `MyAppVersion`

To update the npm version without creating a Git tag:

```powershell
npm.cmd version 4.0.2 --no-git-tag-version
```

### 2. Install dependencies

From an activated Python environment with `requirements.txt` installed:

```powershell
python -m pip install -r requirements.txt
npm.cmd ci
```

### 3. Build the application

```powershell
npm.cmd run dist
```

The `predist` script rebuilds the Python service before electron-builder runs,
so the release cannot silently reuse a stale `python-build\server` directory.
Expected backend output:

```text
python-build\server\server.exe
python-build\server\_internal\
python-build\server\_internal\templates\
```

`Beabots.spec` bundles both CF2 templates and certifi's TLS CA bundle
automatically.

electron-builder reads `package.json` and creates output under `release\`,
including the application tree consumed by Inno Setup:

```text
release\win-unpacked\
release\Beabots Setup <version>.exe
release\latest.yml
```

The Electron-builder NSIS executable (`release\Beabots Setup <version>.exe`)
is an intermediate build artifact. Do not publish it: it has a different
installer identity and cannot upgrade installations created by Inno Setup.

Verify that the packaged Python service exists:

```powershell
Test-Path .\release\win-unpacked\resources\server\server.exe
Test-Path .\release\win-unpacked\resources\server\_internal\certifi\cacert.pem
```

### 4. Build the Inno Setup installer

Compile the checked-in installer script after `npm.cmd run dist` finishes:

```powershell
& "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" .\BeaconInstaller.iss
```

Expected distributable output:

```text
Output\Beabots_Setup_v<version>.exe
```

Only distribute this `Output` installer. Keep the following line in
`BeaconInstaller.iss` unchanged across all versions:

```ini
AppId={{D2A91D2F-0B2F-4B8E-9B79-4B2B5A8D7F01}}
```

Running a newer Inno installer over an older Beabots installation will reuse
the prior installation directory and update the existing Windows uninstall
entry. Users do not need to uninstall the old version first.

## Release verification checklist

Test on a clean Windows user profile or virtual machine:

- Installer completes and launches `Beabots.exe`.
- Installing over the previous release upgrades it without creating a second
  Beabots entry under Windows Installed Apps.
- `resources\server\server.exe` starts without a visible console window.
- Login and license validation work.
- S2/S4 selection is retained after restarting.
- Dashboard pages open and return correctly.
- CF2 templates download and both workbook modes load.
- CF2, CF4, and Upload SOA logs stream into the UI.
- CF2, CF4, draft creation, and SOA workflows complete through their API clients.
- About displays version and update information.
- Update download and installer launch work.
- Uninstall removes the application successfully.

## Troubleshooting

### Electron opens but the backend is unavailable

Run `python server.py` directly and inspect the traceback. Confirm port `5417` is free and that the virtual environment was activated before `npm.cmd start`.

### `python` is not found when running `npm start`

Activate `venv` first. Development mode explicitly spawns the command `python`; it does not search for a virtual environment itself.

### PowerShell refuses to run npm

Use `npm.cmd` instead of `npm`, or review the current user's PowerShell execution policy.

### Electron failed to install correctly

The Electron binary download may have been interrupted by a proxy, firewall, or antivirus product. Retry `npm.cmd ci` on a network that permits npm and Electron downloads.

### `server.exe` exits immediately

Rebuild with PyInstaller and first confirm that `python server.py` runs from source. Missing hidden imports belong in `Beabots.spec`.

### File or folder dialogs do not open

Inspect the Electron DevTools console for preload errors. Native dialogs are exposed through `preload.js` and handled by `main.js`.

### Logs

Runtime logs are stored under:

```text
%LOCALAPPDATA%\Beabots\logs\
```

CF2 activity is shown live in the **Step-by-Step Log** tab. Other workflows that use the shared Python logger continue to write dated files in this application-data directory.

## Generated directories

These directories are build/runtime output and can be regenerated:

```text
build\
python-build\
release\
Output\
node_modules\
venv\
__pycache__\
```
