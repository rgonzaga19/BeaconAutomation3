/*
 * Login window renderer logic.
 * Depends on common.js (fetchJSON, showModal, API_BASE) and window.beabots
 * (preload.js) for window chrome.
 * After successful login, navigates to the dashboard.
 */

document.getElementById("btnMinimize").addEventListener("click", () => window.beabots?.minimize());
document.getElementById("btnClose").addEventListener("click", () => window.beabots?.close());

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
  resetLoginButton();
})();

// Reset login button to initial state
function resetLoginButton() {
  loginBtn.disabled = false;
  loginBtn.textContent = "LOGIN";
  loginBtn.style.color = "";
  loginBtn.style.borderColor = "";
}

// Reset button state when window gains focus (after logout or window show)
window.addEventListener("focus", resetLoginButton);

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
// Login — same validation as login.py's save(): username can't be empty
// After successful login, navigate to dashboard if it's not already open,
// otherwise just close the login window
// ---------------------------------------------------------------------------
const loginBtn = document.getElementById("loginBtn");

loginBtn.addEventListener("click", async () => {
  const username = usernameInput.value.trim();
  const password = passwordInput.value.trim();
  const accessKey = accessKeyInput.value.trim();

  if (!username) {
    showModal("Validation", "Username cannot be empty.");
    return;
  }

  loginBtn.disabled = true;
  loginBtn.textContent = "LOGGING IN...";

  try {
    await fetchJSON("/api/settings", {
      method: "POST",
      body: JSON.stringify({
        username,
        password,
        access_key: accessKey,
        server: currentServer,
      }),
    });

    loginBtn.textContent = "✔  LOGGED IN";
    loginBtn.style.color = "var(--success)";
    loginBtn.style.borderColor = "var(--success)";

    // If called from initial startup (login window is the only window open),
    // navigate to dashboard. If called from dashboard to update credentials,
    // just close the login window.
    setTimeout(() => {
      window.beabots?.goToDashboard();
    }, 900);
  } catch (err) {
    loginBtn.disabled = false;
    loginBtn.textContent = "LOGIN";
    showModal("Login Error", "Failed to login. Please check your credentials and try again.");
  }
});
