const { app, BrowserWindow, ipcMain, dialog, shell } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const fs = require("fs");
const http = require("http");
const https = require("https");
const os = require("os");

const { SERVER_PORT, API_BASE } = require("./config");

const isDev = !app.isPackaged;
const ICON_PATH = path.join(__dirname, "bot.ico");

let serverProcess = null;
const windows = {
  dashboard: null,
  cf2: null,
  uploadSoa: null,
  cf4: null,
  settings: null,
  about: null,
};


let downloadedInstaller = null;

// True whenever a mandatory update is being enforced — the forced About
// window's close is blocked while this is set, and it's cleared the moment
// the user confirms Install (so app.quit() during the install flow isn't
// blocked by the very lock it set up) or if the update turns out to no
// longer be needed (see app:releaseForceLock below).
let forcedUpdateActive = false;

// True for the brief window between the user confirming "Install" and
// app.quit() actually tearing everything down — see app:installUpdate.
let quittingForUpdate = false;

// Windows that are only ever allowed to be open (visible) one at a time,
// and which hide the dashboard while they're open. Settings is the one
// window explicitly allowed to stay open alongside the dashboard, so it's
// deliberately left out of this list.
const EXCLUSIVE_KEYS = ["cf2", "uploadSoa", "cf4", "about"];

// ---------------------------------------------------------------------------
// Theme (light/dark) — persisted to a small JSON file in userData so it
// survives app restarts, and broadcast to every open window so they all
// stay in sync even though each window is a separate renderer with its
// own localStorage.
// ---------------------------------------------------------------------------
const THEME_FILE = path.join(app.getPath("userData"), "theme.json");
let currentTheme = "dark";

function loadTheme() {
  try {
    const parsed = JSON.parse(fs.readFileSync(THEME_FILE, "utf-8"));
    if (parsed.theme === "light" || parsed.theme === "dark") {
      currentTheme = parsed.theme;
    }
  } catch (e) {
    // No file yet (first run) or unreadable — keep the "dark" default.
  }
}

function saveTheme(theme) {
  currentTheme = theme;
  try {
    fs.writeFileSync(THEME_FILE, JSON.stringify({ theme }));
  } catch (e) {
    console.error("[theme] failed to persist:", e);
  }
}

function broadcastTheme(theme, excludeWebContents) {
  BrowserWindow.getAllWindows().forEach((win) => {
    if (win.webContents !== excludeWebContents) {
      win.webContents.send("theme:changed", theme);
    }
  });
}


// ---------------------------------------------------------------------------
// Backend server lifecycle
// ---------------------------------------------------------------------------
function startServer() {
  // Force UTF-8 for the child process's stdout/stderr. On Windows, a
  // spawned Python process otherwise defaults to the system's ANSI
  // codepage (cp1252), which can't represent characters like the
  // checkmark (✓) used in some automation log output — that mismatch
  // throws a UnicodeEncodeError ("'charmap' codec can't encode...")
  // even though the underlying automation itself completed successfully.
  const pythonEnv = {
    ...process.env,
    BEABOTS_PORT: String(SERVER_PORT),
    PYTHONIOENCODING: "utf-8",
    PYTHONUTF8: "1",
  };

  if (isDev) {
    // Dev mode: run the Flask/SocketIO server straight from source.
    serverProcess = spawn("python", ["server.py"], {
      cwd: __dirname,
      env: pythonEnv,
      windowsHide: true,
    });
  } else {
    // Packaged mode: run the PyInstaller-built exe bundled into resources.
    // windowsHide suppresses the console window that a console=True
    // PyInstaller build would otherwise flash on screen — see the note
    // in Beabots.spec for why console=True is still required there.
    const exePath = path.join(process.resourcesPath, "server", "server.exe");
    serverProcess = spawn(exePath, [], {
      env: pythonEnv,
      windowsHide: true,
    });
  }

  serverProcess.stdout?.on("data", (data) => console.log(`[server] ${data}`));
  serverProcess.stderr?.on("data", (data) => console.error(`[server] ${data}`));
  serverProcess.on("exit", (code) => console.log(`[server] exited with code ${code}`));
}

function stopServer() {
  if (serverProcess) {
    serverProcess.kill();
    serverProcess = null;
  }
}

/** Polls the server until it responds, then calls `onReady`. */
function waitForServer(onReady, attempt = 0) {
  const req = http.get(`${API_BASE}/api/settings`, () => onReady());
  req.on("error", () => {
    if (attempt > 50) {
      console.error("[server] never became ready after 50 attempts");
      onReady(); // proceed anyway — the renderer will surface fetch errors
      return;
    }
    setTimeout(() => waitForServer(onReady, attempt + 1), 200);
  });
}

// ---------------------------------------------------------------------------
// Single-window-visible-at-a-time navigation helpers
// ---------------------------------------------------------------------------
function hideDashboard() {
  const dash = windows.dashboard;
  if (dash && !dash.isDestroyed()) dash.hide();
}

function showDashboard() {
  const dash = windows.dashboard;
  if (dash && !dash.isDestroyed()) {
    dash.show();
    dash.focus();
  } else {
    createDashboardWindow();
  }
}

// Hides every exclusive window other than `exceptKey` — used so opening one
// (e.g. CF2) tucks away any other exclusive window (e.g. CF4) that was left
// open, instead of stacking them.
function hideOtherExclusiveWindows(exceptKey) {
  EXCLUSIVE_KEYS.forEach((key) => {
    if (key === exceptKey) return;
    const win = windows[key];
    if (win && !win.isDestroyed()) win.hide();
  });
}

function anyOtherExclusiveWindowVisible(exceptKey) {
  return EXCLUSIVE_KEYS.some((key) => {
    if (key === exceptKey) return false;
    const win = windows[key];
    return win && !win.isDestroyed() && win.isVisible();
  });
}

// ---------------------------------------------------------------------------
// Generic frameless-window factory
// ---------------------------------------------------------------------------
function createWindow(key, htmlFile, options = {}) {
  const existing = windows[key];
  if (existing && !existing.isDestroyed()) {
    if (options.exclusive) {
      hideDashboard();
      hideOtherExclusiveWindows(key);
    }
    if (existing.isMinimized()) existing.restore();
    existing.show();
    existing.focus();
    return existing;
  }

  const win = new BrowserWindow({
    width: options.width || 900,
    height: options.height || 710,
    minWidth: options.minWidth,
    minHeight: options.minHeight,
    resizable: options.resizable !== false,
    frame: false,
    thickFrame: false,
    hasShadow: false,
    transparent: true,
    useContentSize: true,
    icon: ICON_PATH,
    // With transparent:true, backgroundColor paints nothing — the DOM
    // (.app-window in theme.css) draws the real, theme-matched
    // background itself. This is what lets its border-radius actually
    // show rounded corners: the area outside that rounded shape is now
    // genuinely transparent, instead of a same-color rectangle hiding
    // the cut.
    backgroundColor: "#00000000",
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
    ...options.windowOverrides,
  });

  win.loadFile(
    path.join(__dirname, "renderer", htmlFile),
    options.search ? { search: options.search } : undefined
  );
  win.once("ready-to-show", () => {
    if (options.exclusive) {
      hideDashboard();
      hideOtherExclusiveWindows(key);
    }
    win.show();
  });
  win.on("closed", () => {
    windows[key] = null;
    // If this was an exclusive window and nothing else exclusive is still
    // visible, bring the dashboard back so the user is never left with no
    // window open at all. Skipped while the app is quitting to install an
    // update — no point flashing a dashboard open a moment before exit.
    if (options.exclusive && !quittingForUpdate && !anyOtherExclusiveWindowVisible(key)) {
      showDashboard();
    }
  });

  windows[key] = win;
  return win;
}

function createDashboardWindow() {
  return createWindow("dashboard", "dashboard.html", {
    width: 1200,
    height: 800,
    minWidth: 900,
    minHeight: 600,
  });
}

function createCf2Window() {
  return createWindow("cf2", "cf2.html", {
    width: 1300,
    height: 710,
    minWidth: 1000,
    minHeight: 710,
    exclusive: true,
  });
}

function createUploadSoaWindow() {
  return createWindow("uploadSoa", "upload-soa.html", {
    width: 1400,
    height: 710,
    minWidth: 1000,
    minHeight: 710,
    exclusive: true,
  });
}

function createCf4Window() {
  return createWindow("cf4", "cf4.html", {
    width: 820,
    height: 780,
    minWidth: 700,
    minHeight: 600,
    exclusive: true,
  });
}

// Settings is deliberately NOT exclusive — it's the one window allowed to
// stay open alongside the dashboard.
function createSettingsWindow() {
  return createWindow("settings", "settings.html", {
    width: 380,
    height: 390,
    resizable: false,
  });
}

function createAboutWindow(forced = false) {
  const win = createWindow("about", "about.html", {
    width: 860,
    height: 680,
    minWidth: 860,
    minHeight: 680,
    resizable: false,
    exclusive: true,
    search: forced ? "forced=1" : undefined,
  });

  if (forced) {
    forcedUpdateActive = true;
    win.setClosable(false);
    win.on("close", (event) => {
      if (forcedUpdateActive) event.preventDefault();
    });
  }

  return win;
}

// Fetches the worker's /update payload and returns it only if it names a
// mandatory version the user isn't on yet — null in every other case
// (including any network failure, so a flaky connection at launch can
// never lock someone out of an app they already have).
async function checkMandatoryUpdate() {
  try {
    const response = await fetch(
      "https://beabot-license.gonzagaromel19.workers.dev/update"
    );
    if (!response.ok) return null;
    const data = await response.json();
    if (data.mandatory && data.version && data.version !== app.getVersion()) {
      return data;
    }
    return null;
  } catch (err) {
    console.error("[update] mandatory update check failed:", err);
    return null;
  }
}

// ---------------------------------------------------------------------------
// IPC — window chrome
// ---------------------------------------------------------------------------
ipcMain.handle("window:minimize", (event) => {
  BrowserWindow.fromWebContents(event.sender)?.minimize();
});

ipcMain.handle("window:maximize", (event) => {
  const win = BrowserWindow.fromWebContents(event.sender);
  if (!win) return;
  if (win.isMaximized()) {
    win.unmaximize();
  } else {
    win.maximize();
  }
});

ipcMain.handle("window:close", (event) => {
  BrowserWindow.fromWebContents(event.sender)?.close();
});

// Re-focuses the calling window — matches cf2_window.py's
// window.after(10, window.lift); window.after(20, window.focus_force)
// used after the native file-open dialog closes.
ipcMain.handle("window:focusSelf", (event) => {
  const win = BrowserWindow.fromWebContents(event.sender);
  if (win) {
    win.moveTop();
    win.focus();
  }
});

// ---------------------------------------------------------------------------
// IPC — theme (light/dark)
// ---------------------------------------------------------------------------
ipcMain.handle("theme:get", () => currentTheme);

ipcMain.handle("theme:set", (event, theme) => {
  if (theme !== "light" && theme !== "dark") return currentTheme;
  saveTheme(theme);
  broadcastTheme(theme, event.sender);
  return currentTheme;
});

// ---------------------------------------------------------------------------
// IPC — navigation between windows
// ---------------------------------------------------------------------------
ipcMain.handle("open:cf2Window", () => {
  createCf2Window();
});

ipcMain.handle("open:uploadSoaWindow", () => {
  createUploadSoaWindow();
});

ipcMain.handle("open:cf4Window", () => {
  createCf4Window();
});

ipcMain.handle("open:settingsWindow", () => {
  createSettingsWindow();
});

ipcMain.handle("open:aboutWindow", () => {
  createAboutWindow();
});

// "Back to Home" button — hides whichever window called it and brings the
// dashboard back. The calling window is hidden, not closed, so its state
// (uploaded file, form fields, logs, etc.) is still there if the user
// navigates back into it later.
ipcMain.handle("nav:goHome", (event) => {
  if (forcedUpdateActive) return; // no bypassing a required update
  const win = BrowserWindow.fromWebContents(event.sender);
  if (win && !win.isDestroyed()) win.hide();
  showDashboard();
});

// Safety valve for the forced-update window: if it decides (independently
// of the main process's own check) that the user is actually already on
// the latest version, it calls this to lift the lock instead of leaving
// them stuck looking at an unclosable window with nothing to download.
ipcMain.handle("app:releaseForceLock", () => {
  forcedUpdateActive = false;
  const win = windows.about;
  if (win && !win.isDestroyed()) {
    win.setClosable(true);
    win.hide();
  }
  showDashboard();
});

// Close button on the forced-update window itself. A user who downloads
// a mandatory update and picks "Install Later" would otherwise have zero
// way to exit (Home is hidden, native close is blocked). This gives them
// a real way out — quitting the whole app — without ever falling through
// to showDashboard(), which would bypass the mandatory update entirely.
// Relaunching re-runs checkMandatoryUpdate() and re-locks them here until
// they actually install.
ipcMain.handle("app:quitApp", () => {
  forcedUpdateActive = false;
  quittingForUpdate = true;

  // setClosable(false) was set when this window opened in forced mode.
  // It blocks close at the OS level — not just the visible ✕ button, but
  // any programmatic close too, including the one app.quit() below tries
  // to trigger on this window. Without re-enabling it here, quit silently
  // does nothing, same bug releaseForceLock already had to handle.
  const win = windows.about;
  if (win && !win.isDestroyed()) {
    win.setClosable(true);
  }

  app.quit();
});

// ---------------------------------------------------------------------------
// IPC — App Information
// ---------------------------------------------------------------------------

ipcMain.handle("app:getVersion", () => {
  return app.getVersion();
});

ipcMain.handle("app:getSettings", async () => {

  const response = await fetch(`${API_BASE}/api/settings`);

  if (!response.ok) {
    throw new Error("Unable to load settings.");
  }

  return await response.json();

});

// ---------------------------------------------------------------------------
// Update installer temp storage
//
// Every downloaded installer lives in one fixed folder so we can always
// find — and clean up — whatever's sitting there. This fixes two things:
//   1. "Install Later" + relaunch used to forget the download ever
//      happened and made the user fetch it all over again.
//   2. Every mandatory update left its installer behind forever, so this
//      folder only ever grew.
// ---------------------------------------------------------------------------
const UPDATE_TEMP_DIR = path.join(os.tmpdir(), "Beabots");

function installerPathFor(downloadUrl) {
  const fileName = path.basename(new URL(downloadUrl).pathname);
  return path.join(UPDATE_TEMP_DIR, fileName);
}

// Deletes everything in the temp folder except the one file we currently
// care about, so installers from old updates don't pile up release after
// release. Safe to call even if nothing (or the folder itself) exists yet.
function cleanupOldInstallers(keepFilePath) {
  if (!fs.existsSync(UPDATE_TEMP_DIR)) return;
  for (const name of fs.readdirSync(UPDATE_TEMP_DIR)) {
    const full = path.join(UPDATE_TEMP_DIR, name);
    if (full !== keepFilePath) {
      fs.rm(full, { force: true }, () => {});
    }
  }
}

// Checks whether a complete installer for this exact update is already
// sitting on disk from a previous session. Verifies completeness against
// the remote Content-Length rather than trusting a possibly partial or
// corrupt leftover — e.g. from a download that never finished before the
// app was closed. Deletes and returns null on anything that doesn't check
// out, so the caller always falls back to a clean download.
async function findExistingInstaller(filePath, downloadUrl) {
  if (!fs.existsSync(filePath)) return null;

  const actualSize = fs.statSync(filePath).size;
  if (actualSize === 0) {
    fs.unlinkSync(filePath);
    return null;
  }

  try {
    const head = await fetch(downloadUrl, { method: "HEAD" });
    const expectedSize = Number(head.headers.get("content-length") || 0);
    if (expectedSize && actualSize !== expectedSize) {
      fs.unlinkSync(filePath);
      return null;
    }
  } catch (err) {
    // Can't verify size right now (offline, server hiccup, etc.) — the
    // file is non-empty, so treat it as good rather than forcing a
    // re-download the user may not be able to complete anyway.
    console.error("[update] HEAD check failed, trusting existing file:", err);
  }

  return filePath;
}

ipcMain.handle("app:checkForUpdates", async () => {

  const response = await fetch(
    "https://beabot-license.gonzagaromel19.workers.dev/update"
  );

  if (!response.ok) {
    throw new Error("Unable to contact update server.");
  }

  const data = await response.json();

  // Clean up anything left over from older updates, then see if today's
  // target is itself already downloaded from a previous session.
  const targetPath = installerPathFor(data.download);
  cleanupOldInstallers(targetPath);

  const existing = await findExistingInstaller(targetPath, data.download);
  if (existing) {
    downloadedInstaller = existing;
  }

  return { ...data, alreadyDownloaded: !!existing };

});

ipcMain.handle("app:openExternal", async (_event, url) => {
  await shell.openExternal(url);
});

ipcMain.handle("app:downloadUpdate", async (_event, downloadUrl) => {

  fs.mkdirSync(UPDATE_TEMP_DIR, { recursive: true });

  const filePath = installerPathFor(downloadUrl);

  if (fs.existsSync(filePath)) {
    fs.unlinkSync(filePath);
  }

  function download(url, redirects = 0) {

    return new Promise((resolve, reject) => {

      if (redirects > 10) {
        reject(new Error("Too many redirects."));
        return;
      }

      https.get(url, (response) => {

        console.log("Status:", response.statusCode);

        // Follow redirect
        if (
          response.statusCode >= 300 &&
          response.statusCode < 400 &&
          response.headers.location
        ) {

          console.log("Redirect ->", response.headers.location);

          response.resume(); // discard response body

          resolve(
            download(response.headers.location, redirects + 1)
          );

          return;
        }

        if (response.statusCode !== 200) {
          reject(new Error(`Download failed (${response.statusCode})`));
          return;
        }

        const totalBytes = Number(response.headers["content-length"] || 0);
        let downloadedBytes = 0;

        const file = fs.createWriteStream(filePath);

        response.on("data", (chunk) => {

            downloadedBytes += chunk.length;

            const percent = totalBytes
                ? Math.floor(downloadedBytes * 100 / totalBytes)
                : 0;

            windows.about?.webContents.send("update-progress", {
                percent,
                downloadedBytes,
                totalBytes
            });

        });

        response.pipe(file);

        file.on("finish", () => {

          file.close(() => {

            windows.about?.webContents.send("update-progress", {
                percent: 100,
                downloadedBytes: totalBytes,
                totalBytes
            });

            downloadedInstaller = filePath;
            cleanupOldInstallers(filePath);

            resolve({
              success: true,
              path: filePath
            });

          });

        });

        file.on("error", (err) => {

          file.close(() => {
            fs.unlink(filePath, () => {});
            reject(err);
          });

        });

      }).on("error", reject);

    });

  }

  return download(downloadUrl);

});

ipcMain.handle("app:installUpdate", async () => {

  if (!downloadedInstaller || !fs.existsSync(downloadedInstaller)) {
    throw new Error("Installer not found.");
  }

  const result = await dialog.showMessageBox({
    type: "question",
    buttons: ["Install", "Later"],
    defaultId: 0,
    cancelId: 1,
    title: "Install Update",
    message: "The update has been downloaded successfully.\n\nDo you want to install it now?"
  });

  if (result.response !== 0) {
    return false;
  }

  // Lift the lock now — app.quit() below closes every window, and the
  // forced window's own close-guard (still keyed on forcedUpdateActive)
  // would otherwise block that shutdown.
  forcedUpdateActive = false;
  quittingForUpdate = true;

  // Stop the backend server before replacing the installation
  stopServer();

  // Launch the installer
  spawn(downloadedInstaller, [], {
    detached: true,
    stdio: "ignore",
    windowsHide: false
  }).unref();

  // Quit Electron
  app.quit();

  return true;

});


// ---------------------------------------------------------------------------
// IPC — native dialogs (replace filedialog.askopenfilename /
// askdirectory / asksaveasfilename)
// ---------------------------------------------------------------------------

// CF2 window's "Upload Excel File" button
ipcMain.handle("dialog:selectExcelFile", async () => {
  const result = await dialog.showOpenDialog({
    title: "Select Excel File",
    filters: [
      { name: "Excel Workbook", extensions: ["xlsx", "xlsm", "xls"] },
      { name: "All Files", extensions: ["*"] },
    ],
    properties: ["openFile"],
  });
  if (result.canceled || result.filePaths.length === 0) return null;
  return result.filePaths[0];
});

// Upload SOA window's "Browse" button
ipcMain.handle("dialog:selectSoaFolder", async (_event, initialDir) => {
  const result = await dialog.showOpenDialog({
    title: "Select SOA Folder",
    defaultPath: initialDir || undefined,
    properties: ["openDirectory"],
  });
  if (result.canceled || result.filePaths.length === 0) return null;
  return result.filePaths[0];
});

// CF2 window's "Download Excel Template" link — fetches the template bytes
// from server.py, then writes them wherever the user chooses. mode is
// "new_draft" (default) or "existing_draft" — see cf2.js's mode toggle —
// and picks which of the two templates server.py serves.
ipcMain.handle("dialog:saveExcelTemplate", async (_event, mode) => {
  const resolvedMode = mode === "existing_draft" ? "existing_draft" : "new_draft";
  const defaultPath = resolvedMode === "existing_draft"
    ? "CF2_Template_ExistingDraft.xlsx"
    : "CF2_Template.xlsx";

  const result = await dialog.showSaveDialog({
    title: "Save Excel Template",
    defaultPath,
    filters: [{ name: "Excel Workbook", extensions: ["xlsx"] }],
  });
  if (result.canceled || !result.filePath) return { saved: false };

  return new Promise((resolve) => {
    http.get(`${API_BASE}/api/cf2/download-template?mode=${resolvedMode}`, (res) => {
      const chunks = [];
      res.on("data", (chunk) => chunks.push(chunk));
      res.on("end", () => {
        try {
          fs.writeFileSync(result.filePath, Buffer.concat(chunks));
          resolve({ saved: true, path: result.filePath });
        } catch (err) {
          resolve({ saved: false, error: String(err) });
        }
      });
    }).on("error", (err) => resolve({ saved: false, error: String(err) }));
  });
});

// ---------------------------------------------------------------------------
// App lifecycle
// ---------------------------------------------------------------------------
app.whenReady().then(() => {
  loadTheme();
  startServer();
  waitForServer(async () => {
    const mandatoryUpdate = await checkMandatoryUpdate();
    if (mandatoryUpdate) {
      // A required update is pending — show only the locked-down
      // About/Update window and skip the dashboard entirely. Nothing else
      // in the app is reachable from here (no Home button, no closing)
      // until the update is installed.
      createAboutWindow(true);
    } else {
      createDashboardWindow();
    }
  });

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      if (forcedUpdateActive) {
        createAboutWindow(true);
      } else {
        createDashboardWindow();
      }
    }
  });
});

app.on("window-all-closed", () => {
  stopServer();
  if (process.platform !== "darwin") app.quit();
});

app.on("will-quit", stopServer);