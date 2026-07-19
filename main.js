/*
 * Electron main process.
 *
 * Responsibilities (mirrors what tkinter used to own directly):
 *   1. Spawn server.py as a child process on launch, kill it on quit.
 *   2. Own every BrowserWindow (dashboard, CF2, Upload SOA, Settings) —
 *      including the "if already open, focus it instead of duplicating"
 *      guards ui.py used to implement per-window (_open_cf2_window,
 *      _open_upload_soa_window).
 *   3. Handle window chrome (minimize/maximize/close) since the renderer
 *      has no native window controls once frame:false is used.
 *   4. Own native OS dialogs (file-open, folder-browse, save-as) — the
 *      renderer never gets raw file access, only resolved paths.
 */

const { app, BrowserWindow, ipcMain, dialog } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const fs = require("fs");
const http = require("http");

const { SERVER_PORT, API_BASE } = require("./config");

const isDev = !app.isPackaged;
const ICON_PATH = path.join(__dirname, "bot.ico");

let serverProcess = null;
const windows = {
  dashboard: null,
  cf2: null,
  uploadSoa: null,
  settings: null,
};

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
// Generic frameless-window factory
// ---------------------------------------------------------------------------
function createWindow(key, htmlFile, options = {}) {
  const existing = windows[key];
  if (existing && !existing.isDestroyed()) {
    if (existing.isMinimized()) existing.restore();
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
    icon: ICON_PATH,
    backgroundColor: "#0a0e1a",
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
    ...options.windowOverrides,
  });

  win.loadFile(path.join(__dirname, "renderer", htmlFile));
  win.once("ready-to-show", () => win.show());
  win.on("closed", () => {
    windows[key] = null;
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
    width: 900,
    height: 710,
    minWidth: 700,
    minHeight: 710,
  });
}

function createUploadSoaWindow() {
  return createWindow("uploadSoa", "upload-soa.html", {
    width: 1400,
    height: 710,
    minWidth: 1000,
    minHeight: 710,
  });
}

function createSettingsWindow() {
  return createWindow("settings", "settings.html", {
    width: 380,
    height: 390,
    resizable: false,
  });
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
// IPC — navigation between windows
// ---------------------------------------------------------------------------
ipcMain.handle("open:cf2Window", () => {
  createCf2Window();
});

ipcMain.handle("open:uploadSoaWindow", () => {
  createUploadSoaWindow();
});

ipcMain.handle("open:settingsWindow", () => {
  createSettingsWindow();
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
// from server.py, then writes them wherever the user chooses.
ipcMain.handle("dialog:saveExcelTemplate", async () => {
  const result = await dialog.showSaveDialog({
    title: "Save Excel Template",
    defaultPath: "CF2_Template.xlsx",
    filters: [{ name: "Excel Workbook", extensions: ["xlsx"] }],
  });
  if (result.canceled || !result.filePath) return { saved: false };

  return new Promise((resolve) => {
    http.get(`${API_BASE}/api/cf2/download-template`, (res) => {
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
  startServer();
  waitForServer(() => {
    createDashboardWindow();
  });

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createDashboardWindow();
  });
});

app.on("window-all-closed", () => {
  stopServer();
  if (process.platform !== "darwin") app.quit();
});

app.on("will-quit", stopServer);