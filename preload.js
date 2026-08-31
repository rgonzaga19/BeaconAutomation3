const { contextBridge, ipcRenderer } = require("electron");
const os = require("os");
const path = require("path");
const { API_BASE } = require("./config");

// Mirrors upload_soa_window.py's DEFAULT_SOA_FOLDER = Path.home()/"Downloads"/"SOA"
const DEFAULT_SOA_FOLDER = path.join(os.homedir(), "Downloads", "SOA");

contextBridge.exposeInMainWorld("beabots", {
  apiBase: API_BASE,
  defaultSoaFolder: DEFAULT_SOA_FOLDER,

  // Window chrome
  minimize: () => ipcRenderer.invoke("window:minimize"),
  maximize: () => ipcRenderer.invoke("window:maximize"),
  close: () => ipcRenderer.invoke("window:close"),
  focusSelf: () => ipcRenderer.invoke("window:focusSelf"),

  // Navigation between windows
  openCf2Window: () => ipcRenderer.invoke("open:cf2Window"),
  openUploadSoaWindow: () => ipcRenderer.invoke("open:uploadSoaWindow"),
  openCf4Window: () => ipcRenderer.invoke("open:cf4Window"),
  openSettingsWindow: () => ipcRenderer.invoke("open:settingsWindow"),
  openAboutWindow: () => ipcRenderer.invoke("open:aboutWindow"),
  goHome: () => ipcRenderer.invoke("nav:goHome"),
  goToDashboard: () => ipcRenderer.invoke("nav:goToDashboard"),
  logout: () => ipcRenderer.invoke("nav:logout"),
  releaseForceLock: () => ipcRenderer.invoke("app:releaseForceLock"),
  quitApp: () => ipcRenderer.invoke("app:quitApp"),

  getVersion: () => ipcRenderer.invoke("app:getVersion"),
  getSettings: () => ipcRenderer.invoke("app:getSettings"),
  checkForUpdates: () => ipcRenderer.invoke("app:checkForUpdates"),
  downloadUpdate: (url) => ipcRenderer.invoke("app:downloadUpdate", url),
  installUpdate: () => ipcRenderer.invoke("app:installUpdate"),
  onUpdateProgress: (callback) => {
      ipcRenderer.removeAllListeners("update-progress");
      ipcRenderer.on("update-progress", (_event, data) => callback(data));
  },
  openExternal: (url) => ipcRenderer.invoke("app:openExternal", url),

  // Theme (light/dark) — persisted in main.js, broadcast to every window
  getTheme: () => ipcRenderer.invoke("theme:get"),
  setTheme: (theme) => ipcRenderer.invoke("theme:set", theme),
  onThemeChanged: (callback) => {
    ipcRenderer.removeAllListeners("theme:changed");
    ipcRenderer.on("theme:changed", (_event, theme) => callback(theme));
  },

  // Native dialogs
  selectExcelFile: () => ipcRenderer.invoke("dialog:selectExcelFile"),
  selectSoaFolder: (initialDir) => ipcRenderer.invoke("dialog:selectSoaFolder", initialDir),
  saveExcelTemplate: (mode) => ipcRenderer.invoke("dialog:saveExcelTemplate", mode),

  // Server/automation log stream (see main.js's makeLineForwarder /
  // broadcastServerLog) — everything the Python server and automation
  // print to stdout/stderr, forwarded here instead of only reaching the
  // invisible main-process console.
  getRecentLogs: (maxLines) => ipcRenderer.invoke("logs:getRecent", maxLines),
  openLogFile: () => ipcRenderer.invoke("logs:openFile"),
  onServerLog: (callback) => {
    ipcRenderer.removeAllListeners("server:log");
    ipcRenderer.on("server:log", (_event, payload) => callback(payload));
  },
});