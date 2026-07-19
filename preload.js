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
  openSettingsWindow: () => ipcRenderer.invoke("open:settingsWindow"),

  // Native dialogs
  selectExcelFile: () => ipcRenderer.invoke("dialog:selectExcelFile"),
  selectSoaFolder: (initialDir) => ipcRenderer.invoke("dialog:selectSoaFolder", initialDir),
  saveExcelTemplate: () => ipcRenderer.invoke("dialog:saveExcelTemplate"),
});