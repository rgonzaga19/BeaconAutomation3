# Beabots

Beacon / CF2 / SOA automation suite. Electron front end talking to a local
Python (Flask + SocketIO) backend that owns all the actual automation logic
(Playwright, PhilHealth CF2/SOA processing, license validation).

## Architecture

```
main.js          Electron main process — spawns server.py, owns every
                  BrowserWindow, handles native dialogs (file/folder/save-as)
preload.js       contextBridge — exposes window.beabots to every renderer
config.js        shared server port config (used by main.js and preload.js)
server.py        Flask + SocketIO backend — wraps the existing business
                  logic behind HTTP endpoints + a WebSocket log stream

renderer/        everything that runs inside a BrowserWindow
├── dashboard.html / cf2.html / upload-soa.html / settings.html
├── css/theme.css                shared palette + component styles
└── js/
    ├── common.js                 shared API_BASE / fetchJSON / modal helper
    ├── dashboard.js / cf2.js / upload-soa.js / settings.js

beacon.py, license.py, settings.py, reports.py, logger.py, date_parser.py,
patient_record.py, browser_session.py, cf2_automation.py, cf2_data.py,
cf2_fees.py, cf2_mapper.py, draft_automation.py, draft_title.py,
soa_automation.py
                  backend/automation logic — untouched by the Electron
                  migration, called from server.py exactly as before
```

The old tkinter files (`ui.py`, `cf2_window.py`, `upload_soa_window.py`,
and `settings.py`'s dialog) have been fully replaced by the `renderer/`
front end and are no longer part of the app.

---

## Running in development

**1. Python side** — from the project root, in your venv:
```
pip install -r requirements.txt
```

Optional: run the backend standalone first, to catch any Python-side
errors before Electron ever enters the picture:
```
python server.py
```
Should print `Running on http://127.0.0.1:5417` with no import errors.
Ctrl+C to stop it once confirmed — `npm start` spawns its own copy.

**2. Node/Electron side:**
```
npm install
npm start
```
This runs `electron .`, which spawns `server.py` itself, waits for it to
respond, then opens the dashboard.

### Manual test checklist

- **Dashboard** — type a transmittal, count updates; Settings/About/Move to
  CF2/Upload SOA buttons all work; reopening a window focuses the existing
  one instead of duplicating it.
- **Settings** — fields prefill; show/hide password; view/hide access key;
  empty username shows a validation warning; save flashes "✔ SAVED" and
  auto-closes.
- **CF2** — license-checks on open (test by blanking the access key in
  Settings first); Upload Excel File → native picker → per-patient log
  detail renders; Download Excel Template → native save dialog; User Guide
  card shows all 7 steps; Start Automation disabled until a workbook is
  loaded, summary block appears on completion.
- **Upload SOA** — Browse persists the folder choice across reopens;
  empty folder/empty transmittals show the right warnings; logs stream
  live and are colored by level.

---

## Rebuilding / packaging

Four stages, in this order:

**1. Build the Python backend into an exe:**
```
pyinstaller Beabots.spec --distpath python-build
```
Produces `python-build\server\server.exe` plus its dependencies. The
`templates` folder (for `CF2_Template.xlsx`) is bundled automatically via
the spec's `datas` entry — no manual copy needed for that one.

**2. Paste the Playwright browsers next to `server.exe`:**
```
Copy your ms-playwright folder into python-build\server\
```
This has to sit next to `server.exe` specifically (not next to the
Electron exe) — `beacon.py`/`soa_automation.py` resolve
`PLAYWRIGHT_BROWSERS_PATH` relative to `sys.executable`, i.e. whichever
exe is actually running that code, which is `server.exe` now.

**3. Build the Electron app:**
```
npm run dist
```
`electron-builder` produces `release\win-unpacked\`, and automatically
copies `python-build\server\` (now including `ms-playwright`) into
`release\win-unpacked\resources\server\` — this is configured in
`package.json`'s `build.extraResources`.

**4. Compile the installer:**
```
Compile BeaconInstaller.iss in Inno Setup
```
Packages `release\win-unpacked\*` into a single `Beabots_Setup_v3.0.0.exe`
in the `Output\` folder.

### One-shot rebuild, once you're comfortable with the steps
```
pyinstaller Beabots.spec --distpath python-build
:: paste ms-playwright into python-build\server\ if it isn't already there
npm run dist
:: then compile BeaconInstaller.iss
```

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `npm start` opens a window but no buttons respond, even minimize/close | Check DevTools (`Ctrl+Shift+I`) → Console for a `SyntaxError`. Most likely a renderer JS file redeclares something already declared in `common.js` (e.g. `const API_BASE` in two files loaded on the same page) — that aborts the whole script silently. Also check `window.beabots` isn't `undefined` (a sandboxed preload can't `require("os")`/`require("path")` — `sandbox: false` must be set in `main.js`'s `webPreferences`). |
| `Electron failed to install correctly` after `npm install` | Electron's postinstall binary download failed silently (network/proxy/antivirus). Run `node node_modules/electron/install.js` directly to see the real error, or do a clean `npm install` after deleting `node_modules` + `package-lock.json`. |
| `[server] exited with code 1` immediately | Python import error in `server.py` — run `python server.py` directly to see the actual traceback. |
| `UnicodeEncodeError: 'charmap' codec can't encode character ...` in the logs | Windows console codepage issue, not a logic bug — `main.js` sets `PYTHONIOENCODING=utf-8` for the spawned Python process to fix this. If you see it again, confirm that env var is still being passed in `startServer()`. |
| A window's native file/folder dialog never appears | `preload.js` failed to load — check DevTools Console for a preload error. |
| Playwright can't find a browser on a freshly installed machine | Confirm `ms-playwright` was pasted into `python-build\server\` **before** step 3 (`npm run dist`) — if it's missing from `release\win-unpacked\resources\server\`, it wasn't there in time to get bundled. |

---

## Known follow-ups

- Build artifacts (`__pycache__`, `build\`, `dist\`, `Output\`) are all
  regenerable and safe to delete anytime.
