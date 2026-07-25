/*
 * Settings window renderer logic.
 * Depends on common.js (fetchJSON, showModal, API_BASE) and window.beabots
 * (preload.js) for window chrome.
 */

document.getElementById("btnMinimize").addEventListener("click", () => window.beabots?.minimize());
document.getElementById("btnClose").addEventListener("click", () => window.beabots?.close());
document.getElementById("cancelBtn").addEventListener("click", () => window.beabots?.close());

const usernameInput = document.getElementById("username");
const passwordInput = document.getElementById("password");
const accessKeyInput = document.getElementById("accessKey");

// ---------------------------------------------------------------------------
// Beacon server toggle (S2 / S4)
// ---------------------------------------------------------------------------
const toggleTrack = document.getElementById("toggleTrack");
const labelS2 = document.getElementById("labelS2");
const labelS4 = document.getElementById("labelS4");

let currentServer = "s4"; // fallback until settings load; s4 matches the previous hardcoded default

function setServerUI(value) {
  currentServer = value === "s2" ? "s2" : "s4";
  const isS4 = currentServer === "s4";
  toggleTrack.classList.toggle("on", isS4);
  labelS4.classList.toggle("active", isS4);
  labelS2.classList.toggle("active", !isS4);
}

function toggleServer() {
  setServerUI(currentServer === "s4" ? "s2" : "s4");
}

toggleTrack.addEventListener("click", toggleServer);
labelS2.addEventListener("click", () => setServerUI("s2"));
labelS4.addEventListener("click", () => setServerUI("s4"));

// ---------------------------------------------------------------------------
// Load current settings into the form
// ---------------------------------------------------------------------------
(async function loadCurrentSettings() {
  const settings = await fetchJSON("/api/settings");
  usernameInput.value = settings.username || "";
  passwordInput.value = settings.password || "";
  accessKeyInput.value = settings.access_key || "";
  setServerUI(settings.server || "s4");
})();

// ---------------------------------------------------------------------------
// Show / hide password toggle
// ---------------------------------------------------------------------------
document.getElementById("showPassword").addEventListener("change", (e) => {
  passwordInput.type = e.target.checked ? "text" : "password";
});

// ---------------------------------------------------------------------------
// Access key section — hidden until "View Access Key" is clicked
// ---------------------------------------------------------------------------
const accessKeyField = document.getElementById("accessKeyField");
const accessKeyToggle = document.getElementById("accessKeyToggle");
let accessKeyVisible = false;

accessKeyToggle.addEventListener("click", () => {
  accessKeyVisible = !accessKeyVisible;
  accessKeyField.style.display = accessKeyVisible ? "block" : "none";
  accessKeyToggle.textContent = accessKeyVisible ? "🙈  Hide Access Key" : "👁  View Access Key";
});

// ---------------------------------------------------------------------------
// Save — same validation as settings.py's save(): username can't be empty
// ---------------------------------------------------------------------------
const saveBtn = document.getElementById("saveBtn");

saveBtn.addEventListener("click", async () => {
  const username = usernameInput.value.trim();
  const password = passwordInput.value.trim();
  const accessKey = accessKeyInput.value.trim();

  if (!username) {
    showModal("Validation", "Username cannot be empty.");
    return;
  }

  await fetchJSON("/api/settings", {
    method: "POST",
    body: JSON.stringify({
      username,
      password,
      access_key: accessKey,
      server: currentServer,
    }),
  });

  saveBtn.textContent = "✔  SAVED";
  saveBtn.style.color = "var(--success)";
  saveBtn.style.borderColor = "var(--success)";

  setTimeout(() => window.beabots?.close(), 900);
});