const { app, BrowserWindow, WebContentsView, ipcMain, dialog, shell, screen } = require("electron");
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
  login: null,
  dashboard: null,
  cf2: null,
  uploadSoa: null,
  cf4: null,
  settings: null,
  about: null,
};

// In-dashboard automation pages. These are WebContentsViews attached to the
// dashboard BrowserWindow, so they read as pages in one application rather
// than separate operating-system windows.
const workspaceViews = {
  cf2: null,
  uploadSoa: null,
  cf4: null,
  about: null,
  settings: null,
};
const WORKSPACE_TOP = 97; // custom titlebar (34) + dashboard toolbar (63)
let workspaceSidebarWidth = 232;
let activeWorkspaceKey = null;

function layoutWorkspaceView() {
  const dash = windows.dashboard;
  const view = activeWorkspaceKey && workspaceViews[activeWorkspaceKey];
  if (!dash || dash.isDestroyed() || !view) return;
  const [width, height] = dash.getContentSize();
  view.setBounds({
    x: workspaceSidebarWidth,
    y: WORKSPACE_TOP,
    width: Math.max(1, width - workspaceSidebarWidth),
    height: Math.max(1, height - WORKSPACE_TOP),
  });
}

function createWorkspaceView(key, htmlFile) {
  const existing = workspaceViews[key];
  if (existing && !existing.webContents.isDestroyed()) return existing;

  const view = new WebContentsView({
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });
  view.setBackgroundColor("#00000000");
  view.setVisible(false);
  view.webContents.loadFile(path.join(__dirname, "renderer", htmlFile), {
    search: "embedded=1",
  });
  view.webContents.once("did-finish-load", () => {
    // The dashboard supplies the window chrome. Removing the page's own
    // titlebar and rounded outer edge makes the view meet the shell cleanly.
    view.webContents.insertCSS(`
      .titlebar { display: none !important; }
      .app-window { border-radius: 0 !important; }
      .app-window::after { display: none !important; }
    `);
  });
  workspaceViews[key] = view;
  return view;
}

function showWorkspacePage(key, htmlFile) {
  const dash = windows.dashboard || createDashboardWindow();
  if (!dash || dash.isDestroyed()) return null;

  Object.entries(workspaceViews).forEach(([viewKey, view]) => {
    if (view && !view.webContents.isDestroyed()) view.setVisible(viewKey === key);
  });

  const view = createWorkspaceView(key, htmlFile);
  if (!dash.contentView.children.includes(view)) dash.contentView.addChildView(view);
  activeWorkspaceKey = key;
  view.setVisible(true);
  layoutWorkspaceView();
  if (!dash.isVisible()) dash.show();
  if (dash.isMinimized()) dash.restore();
  dash.focus();
  dash.webContents.send("workspace:active", key);
  return view;
}

function hideWorkspacePage() {
  Object.values(workspaceViews).forEach((view) => {
    if (view && !view.webContents.isDestroyed()) view.setVisible(false);
  });
  activeWorkspaceKey = null;
  windows.dashboard?.webContents.send("workspace:active", null);
}

// ---------------------------------------------------------------------------
// Server log forwarding — the Python server/automation's stdout/stderr only
// ever reached console.log/console.error in the MAIN process above. That's
// invisible once packaged (windowsHide:true means there's no console window
// to see it in at all), so "our console" only ever showed Electron's own
// renderer devtools output, never a single line of what the automation was
// actually doing or failing on.
//
// Fix: forward every line to (a) a persistent log file, so it's there after
// the fact even if no window happened to be open when it printed, and (b)
// every open renderer window via IPC, so a log panel in the UI can show it
// live. Chunks from a 'data' event don't line up with actual log lines (a
// single print() can arrive split across chunks, or several prints can
// arrive in one chunk), so this buffers and splits on '\n' rather than
// treating each 'data' event as one line.
// ---------------------------------------------------------------------------
const LOG_DIR = path.join(app.getPath("userData"), "logs");
fs.mkdirSync(LOG_DIR, { recursive: true });
const LOG_FILE = path.join(LOG_DIR, "automation.log");

// Create the file immediately (even empty) rather than waiting for the
// first write. Without this, shell.showItemInFolder() below has nothing
// to point at until the automation has actually printed something — on
// Windows that means "Open Log File" silently does nothing on a fresh
// install/relaunch instead of opening Explorer at all.
if (!fs.existsSync(LOG_FILE)) {
  try {
    fs.writeFileSync(LOG_FILE, "");
  } catch (e) {
    console.error("[logs] could not create log file:", e);
  }
}
let logStream = null;

function getLogStream() {
  if (!logStream) {
    logStream = fs.createWriteStream(LOG_FILE, { flags: "a" });
  }
  return logStream;
}

function broadcastServerLog(level, line) {
  BrowserWindow.getAllWindows().forEach((win) => {
    if (!win.isDestroyed()) {
      win.webContents.send("server:log", { level, line, timestamp: Date.now() });
    }
  });
  Object.values(workspaceViews).forEach((view) => {
    if (view && !view.webContents.isDestroyed()) {
      view.webContents.send("server:log", { level, line, timestamp: Date.now() });
    }
  });
}

function makeLineForwarder(level) {
  let buffer = "";
  return (chunk) => {
    buffer += chunk.toString();
    const lines = buffer.split("\n");
    // Last element is either "" (chunk ended on a newline) or a partial
    // line still waiting for more data — keep it buffered either way.
    buffer = lines.pop();

    for (const rawLine of lines) {
      const line = rawLine.replace(/\r$/, "");
      if (!line) continue;

      // Werkzeug sends all access records to stderr. Successful HTTP
      // requests are routine traffic, not application errors.
      const accessMatch = line.match(/"\s(\d{3})\s(?:-|\d+)/);
      const statusCode = accessMatch ? Number(accessMatch[1]) : null;
      const effectiveLevel = level === "error" && statusCode !== null && statusCode < 400
        ? "info"
        : level;

      // Still print to the main-process console too — free in dev mode
      // (running `electron .` from a terminal) and harmless otherwise.
      if (effectiveLevel === "error") {
        console.error(`[server] ${line}`);
      } else {
        console.log(`[server] ${line}`);
      }

      try {
        const ts = new Date().toISOString();
        getLogStream().write(`[${ts}] [${effectiveLevel}] ${line}\n`);
      } catch (e) {
        // Logging failure shouldn't be fatal to the app itself.
      }

      broadcastServerLog(effectiveLevel, line);
    }
  };
}


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
const EXCLUSIVE_KEYS = ["about"];
const DOCKED_KEYS = ["cf2", "uploadSoa", "cf4"];

// Docked workspace state. Automation screens stay as independent
// BrowserWindows, but are positioned and animated as one workspace with the
// dashboard acting as the navigation rail on the left.
const DOCK_GAP = 8;
const DOCK_ANIMATION_MS = 190;
let activeDockedKey = null;
let normalDashboardBounds = null;
const boundsAnimations = new WeakMap();

function clamp(value, minimum, maximum) {
  return Math.max(minimum, Math.min(maximum, value));
}

function animateWindowBounds(win, target, duration = DOCK_ANIMATION_MS) {
  if (!win || win.isDestroyed()) return Promise.resolve();

  const previousAnimation = boundsAnimations.get(win);
  if (previousAnimation) {
    clearInterval(previousAnimation.timer);
    previousAnimation.resolve();
  }

  const start = win.getBounds();
  const startedAt = Date.now();
  return new Promise((resolve) => {
    const timer = setInterval(() => {
      if (win.isDestroyed()) {
        clearInterval(timer);
        boundsAnimations.delete(win);
        resolve();
        return;
      }

      const progress = Math.min(1, (Date.now() - startedAt) / duration);
      const eased = 1 - Math.pow(1 - progress, 3);
      const value = (from, to) => Math.round(from + (to - from) * eased);
      win.setBounds({
        x: value(start.x, target.x),
        y: value(start.y, target.y),
        width: value(start.width, target.width),
        height: value(start.height, target.height),
      });

      if (progress === 1) {
        clearInterval(timer);
        boundsAnimations.delete(win);
        resolve();
      }
    }, 16);
    boundsAnimations.set(win, { timer, resolve });
  });
}

function getDockLayout() {
  const dash = windows.dashboard;
  const referenceBounds = dash && !dash.isDestroyed()
    ? dash.getBounds()
    : { x: 0, y: 0, width: 1200, height: 800 };
  const { workArea } = screen.getDisplayMatching(referenceBounds);
  const dashboardWidth = Math.min(
    clamp(Math.round(workArea.width * 0.2), 280, 360),
    Math.max(220, workArea.width - DOCK_GAP - 600)
  );
  return {
    dashboard: {
      x: workArea.x,
      y: workArea.y,
      width: dashboardWidth,
      height: workArea.height,
    },
    content: {
      x: workArea.x + dashboardWidth + DOCK_GAP,
      y: workArea.y,
      width: workArea.width - dashboardWidth - DOCK_GAP,
      height: workArea.height,
    },
  };
}

function hideOtherDockedWindows(exceptKey) {
  DOCKED_KEYS.forEach((key) => {
    if (key === exceptKey) return;
    const win = windows[key];
    if (win && !win.isDestroyed()) win.hide();
  });
}

function resetDockedWorkspaceImmediately() {
  hideOtherDockedWindows(null);
  activeDockedKey = null;
  const dash = windows.dashboard;
  if (dash && !dash.isDestroyed() && normalDashboardBounds) {
    dash.setMinimumSize(280, 500);
    dash.setBounds(normalDashboardBounds);
    dash.setMinimumSize(900, 600);
  }
  normalDashboardBounds = null;
}

async function dockAutomationWindow(key, win) {
  const dash = windows.dashboard;
  if (!dash || dash.isDestroyed() || !win || win.isDestroyed()) return;

  if (!activeDockedKey) normalDashboardBounds = dash.getBounds();
  activeDockedKey = key;
  hideOtherDockedWindows(key);

  const layout = getDockLayout();
  dash.setMinimumSize(280, 500);
  win.setMinimumSize(600, 500);

  if (dash.isMinimized()) dash.restore();
  if (dash.isMaximized()) dash.unmaximize();
  if (win.isMinimized()) win.restore();
  if (win.isMaximized()) win.unmaximize();

  if (!dash.isVisible()) dash.show();
  await animateWindowBounds(dash, layout.dashboard);
  if (activeDockedKey !== key || win.isDestroyed()) return;

  // A small offset and opacity fade soften the arrival without making the
  // workspace feel sluggish.
  win.setBounds({ ...layout.content, x: layout.content.x + 36 });
  win.setOpacity(0);
  win.show();
  const fadeStartedAt = Date.now();
  const fadeTimer = setInterval(() => {
    if (win.isDestroyed() || activeDockedKey !== key) {
      clearInterval(fadeTimer);
      return;
    }
    const progress = Math.min(1, (Date.now() - fadeStartedAt) / DOCK_ANIMATION_MS);
    win.setOpacity(progress);
    if (progress === 1) clearInterval(fadeTimer);
  }, 16);
  await animateWindowBounds(win, layout.content);
  if (!win.isDestroyed()) {
    win.setOpacity(1);
    win.focus();
  }
}

async function restoreDashboardLayout() {
  const dash = windows.dashboard;
  activeDockedKey = null;
  hideOtherDockedWindows(null);
  if (!dash || dash.isDestroyed()) return;

  if (!dash.isVisible()) dash.show();
  const target = normalDashboardBounds || dash.getBounds();
  await animateWindowBounds(dash, target);
  if (!dash.isDestroyed()) {
    dash.setMinimumSize(900, 600);
    dash.focus();
  }
  normalDashboardBounds = null;
}

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
  Object.values(workspaceViews).forEach((view) => {
    if (view && !view.webContents.isDestroyed() && view.webContents !== excludeWebContents) {
      view.webContents.send("theme:changed", theme);
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
  //
  // PYTHONUNBUFFERED=1 is equally critical and was missing before: when
  // stdout isn't a real terminal (piped into spawn(), as it always is
  // here), CPython silently switches stdout from line-buffered to fully
  // block-buffered. print() calls then sit in an internal buffer and
  // never actually reach this process's 'data' event until that buffer
  // fills or the child exits — stderr is unbuffered by default, which
  // is why, without this, the log showed every Werkzeug access-log line
  // (stderr) but not a single print() from cf2_automation.py itself
  // (stdout) — it looked like the automation wasn't logging anything at
  // all, when really its output was just stuck in a buffer.
  const pythonEnv = {
    ...process.env,
    BEABOTS_PORT: String(SERVER_PORT),
    PYTHONIOENCODING: "utf-8",
    PYTHONUTF8: "1",
    PYTHONUNBUFFERED: "1",
  };

  if (isDev) {
    // Dev mode: run the Flask/SocketIO server straight from source.
    // "-u" is redundant with PYTHONUNBUFFERED above (both do the same
    // thing in CPython) but kept as a belt-and-suspenders since it's
    // free and makes the intent explicit at the call site too.
    serverProcess = spawn("python", ["-u", "server.py"], {
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

  serverProcess.stdout?.on("data", makeLineForwarder("info"));
  serverProcess.stderr?.on("data", makeLineForwarder("error"));
  serverProcess.on("exit", (code) => {
    const line = `exited with code ${code}`;
    console.log(`[server] ${line}`);
    try {
      getLogStream().write(`[${new Date().toISOString()}] [info] ${line}\n`);
    } catch (e) {
      // ignore
    }
    broadcastServerLog(code === 0 ? "info" : "error", line);
  });
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
function hideLogin() {
  const login = windows.login;
  if (login && !login.isDestroyed()) login.hide();
}

function showLogin() {
  const login = windows.login;
  if (login && !login.isDestroyed()) {
    login.show();
    login.focus();
  } else {
    createLoginWindow();
  }
}

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
    if (options.docked) {
      dockAutomationWindow(key, existing);
      return existing;
    }
    if (options.exclusive) {
      resetDockedWorkspaceImmediately();
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
    if (options.docked) {
      dockAutomationWindow(key, win);
      return;
    }
    if (options.exclusive) {
      resetDockedWorkspaceImmediately();
      hideDashboard();
      hideOtherExclusiveWindows(key);
    }
    win.show();
  });
  win.on("closed", () => {
    windows[key] = null;
    if (options.docked && activeDockedKey === key && !quittingForUpdate) {
      restoreDashboardLayout();
      return;
    }
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

function createLoginWindow() {
  const { width: availableWidth, height: availableHeight } = screen.getPrimaryDisplay().workAreaSize;
  const width = Math.min(1450, Math.floor(availableWidth * 0.92));
  const height = Math.min(800, Math.floor(availableHeight * 0.92));
  return createWindow("login", "login.html", {
    width,
    height,
    minWidth: Math.min(720, width),
    minHeight: Math.min(560, height),
    resizable: true,
    windowOverrides: { center: true },
  });
}

function createDashboardWindow() {
  const { width: availableWidth, height: availableHeight } = screen.getPrimaryDisplay().workAreaSize;
  const width = Math.min(1450, Math.floor(availableWidth * 0.92));
  const height = Math.min(800, Math.floor(availableHeight * 0.92));
  const win = createWindow("dashboard", "dashboard.html", {
    // CF2's 1450x800 canvas remains the preferred size, capped to the
    // monitor work area for smaller screens and Windows display scaling.
    width,
    height,
    minWidth: Math.min(900, width),
    minHeight: Math.min(600, height),
    resizable: true,
    windowOverrides: { center: true },
  });
  if (!win.__workspaceLayoutBound) {
    win.__workspaceLayoutBound = true;
    win.on("resize", layoutWorkspaceView);
    win.on("maximize", layoutWorkspaceView);
    win.on("unmaximize", layoutWorkspaceView);
  }
  return win;
}

function createCf2Window() {
  return showWorkspacePage("cf2", "cf2.html");
}

function createUploadSoaWindow() {
  return showWorkspacePage("uploadSoa", "upload-soa.html");
}

function createCf4Window() {
  return showWorkspacePage("cf4", "cf4.html");
}

// Settings is now the login window — allows users to change credentials
// from the dashboard. It's deliberately NOT exclusive — it's the one window
// allowed to stay open alongside the dashboard.
function createSettingsWindow() {
  return showWorkspacePage("settings", "settings.html");
}

function createAboutWindow(forced = false) {
  if (!forced) return showWorkspacePage("about", "about.html");

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
  const dockedKey = DOCKED_KEYS.find((key) => windows[key] === win);
  if ((win === windows.dashboard && activeDockedKey) || dockedKey) {
    const key = dockedKey || activeDockedKey;
    const dockedWindow = windows[key];
    if (dockedWindow && !dockedWindow.isDestroyed()) dockAutomationWindow(key, dockedWindow);
    return;
  }
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

ipcMain.handle("workspace:setSidebarWidth", (event, width) => {
  if (event.sender !== windows.dashboard?.webContents) return;
  workspaceSidebarWidth = width === 68 ? 68 : 232;
  layoutWorkspaceView();
});

ipcMain.handle("workspace:home", (event) => {
  const fromDashboard = event.sender === windows.dashboard?.webContents;
  const fromWorkspace = Object.values(workspaceViews).some((view) =>
    view && !view.webContents.isDestroyed() && view.webContents === event.sender
  );
  if (!fromDashboard && !fromWorkspace) return;
  hideWorkspacePage();
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
  const workspaceEntry = Object.entries(workspaceViews).find(([, view]) =>
    view && !view.webContents.isDestroyed() && view.webContents === event.sender
  );
  if (workspaceEntry) {
    hideWorkspacePage();
    showDashboard();
    return;
  }
  const win = BrowserWindow.fromWebContents(event.sender);
  if (win && !win.isDestroyed()) win.hide();
  if (DOCKED_KEYS.some((key) => windows[key] === win)) {
    restoreDashboardLayout();
  } else {
    showDashboard();
  }
});

// Navigate from login to dashboard
ipcMain.handle("nav:goToDashboard", (event) => {
  hideLogin();
  showDashboard();
  const dash = windows.dashboard;
  if (!dash || dash.isDestroyed()) return;
  const sendEntrance = () => {
    if (!dash.isDestroyed()) dash.webContents.send("dashboard:enter");
  };
  if (dash.webContents.isLoadingMainFrame()) {
    dash.webContents.once("did-finish-load", sendEntrance);
  } else {
    sendEntrance();
  }
});

// Logout — hide dashboard and show login window
ipcMain.handle("nav:logout", (event) => {
  hideWorkspacePage();
  resetDockedWorkspaceImmediately();
  hideDashboard();
  showLogin();
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

// Start a detached helper that waits for this Electron process to disappear
// before opening the installer. Launching the installer directly here races
// with app.quit(): on a fast machine the installer can begin while Beabots is
// still closing windows or holding files in its installation directory.
function launchInstallerAfterAppExit(installerPath) {
  const waitAndLaunch = [
    "$parentId = [int]$env:BEABOTS_UPDATE_PARENT_PID",
    "$installer = $env:BEABOTS_UPDATE_INSTALLER",
    "Wait-Process -Id $parentId -ErrorAction SilentlyContinue",
    "Start-Process -FilePath $installer",
  ].join("; ");

  const helper = spawn(
    "powershell.exe",
    [
      "-NoProfile",
      "-NonInteractive",
      "-WindowStyle", "Hidden",
      "-Command", waitAndLaunch,
    ],
    {
      detached: true,
      stdio: "ignore",
      windowsHide: true,
      env: {
        ...process.env,
        BEABOTS_UPDATE_PARENT_PID: String(process.pid),
        BEABOTS_UPDATE_INSTALLER: installerPath,
      },
    }
  );

  helper.unref();
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

  // Mandatory-update windows are created with setClosable(false). Restore
  // closability so app.quit() can close the window at the OS level.
  const aboutWindow = windows.about;
  if (aboutWindow && !aboutWindow.isDestroyed()) {
    aboutWindow.setClosable(true);
  }

  // Stop the backend server before replacing the installation
  stopServer();

  // The helper starts now but does not launch the installer until this
  // Electron process has completely exited.
  launchInstallerAfterAppExit(downloadedInstaller);

  // Close every Beabots window and finish shutting down the application.
  app.quit();

  return true;

});


// ---------------------------------------------------------------------------
// IPC — server/automation log access (see broadcastServerLog above for the
// live stream; these are for a renderer that just opened and wants
// history, or a "show me the log file" button).
// ---------------------------------------------------------------------------
ipcMain.handle("logs:getRecent", async (_event, maxLines = 500) => {
  try {
    const content = fs.readFileSync(LOG_FILE, "utf-8");
    const lines = content.split("\n").filter(Boolean);
    return lines.slice(-maxLines);
  } catch (e) {
    return [];
  }
});

ipcMain.handle("logs:openFile", async () => {
  try {
    if (!fs.existsSync(LOG_FILE)) {
      fs.writeFileSync(LOG_FILE, "");
    }
    shell.showItemInFolder(LOG_FILE);
    return { opened: true };
  } catch (e) {
    console.error("[logs] failed to open log file:", e);
    return { opened: false, error: String(e) };
  }
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
      // About/Update window and skip the login/dashboard entirely. Nothing else
      // in the app is reachable from here (no Home button, no closing)
      // until the update is installed.
      createAboutWindow(true);
    } else {
      // Show login window first as entry point
      createLoginWindow();
    }
  });

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      if (forcedUpdateActive) {
        createAboutWindow(true);
      } else {
        createLoginWindow();
      }
    }
  });
});

app.on("window-all-closed", () => {
  stopServer();
  if (process.platform !== "darwin") app.quit();
});

app.on("will-quit", () => {
  stopServer();
  if (logStream) {
    try {
      logStream.end();
    } catch (e) {
      // ignore
    }
  }
});
