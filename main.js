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
  settings: null,
  about: null,
};


let downloadedInstaller = null;

// ---------------------------------------------------------------------------
// Theme (light/dark) — persisted to a small JSON file in userData so it
// survives app restarts, and broadcast to every open window so they all
// stay in sync even though each window is a separate renderer with its
// own localStorage.
// ---------------------------------------------------------------------------
const THEME_FILE = path.join(app.getPath("userData"), "theme.json");
const THEME_BG = { dark: "#0a0e16", light: "#f5f7fb" };
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
    useContentSize: true,
    icon: ICON_PATH,
    backgroundColor: THEME_BG[currentTheme] || THEME_BG.dark,
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

function createAboutWindow() {
  return createWindow("about", "about.html", {
    width: 700,
    height: 620,
    minWidth: 700,
    minHeight: 620,
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

ipcMain.handle("open:settingsWindow", () => {
  createSettingsWindow();
});

ipcMain.handle("open:aboutWindow", () => {
  createAboutWindow();
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

ipcMain.handle("app:checkForUpdates", async () => {

  const response = await fetch(
    "https://beabot-license.gonzagaromel19.workers.dev/update"
  );

  if (!response.ok) {
    throw new Error("Unable to contact update server.");
  }

  return await response.json();

});

ipcMain.handle("app:openExternal", async (_event, url) => {
  await shell.openExternal(url);
});

ipcMain.handle("app:downloadUpdate", async (_event, downloadUrl) => {

  const tempDir = path.join(os.tmpdir(), "Beabots");
  fs.mkdirSync(tempDir, { recursive: true });

  const fileName = path.basename(new URL(downloadUrl).pathname);
  const filePath = path.join(tempDir, fileName);

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
  loadTheme();
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