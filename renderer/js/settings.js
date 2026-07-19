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
// Load current settings into the form
// ---------------------------------------------------------------------------
(async function loadCurrentSettings() {
  const settings = await fetchJSON("/api/settings");
  usernameInput.value = settings.username || "";
  passwordInput.value = settings.password || "";
  accessKeyInput.value = settings.access_key || "";
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
    }),
  });

  saveBtn.textContent = "✔  SAVED";
  saveBtn.style.color = "var(--success)";
  saveBtn.style.borderColor = "var(--success)";

  setTimeout(() => window.beabots?.close(), 900);
});