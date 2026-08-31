/* Login renderer: Beacon username and password only. */

document.getElementById("btnMinimize").addEventListener("click", () => window.beabots?.minimize());
document.getElementById("btnClose").addEventListener("click", () => window.beabots?.close());

const loginForm = document.getElementById("loginForm");
const loginBtn = document.getElementById("loginBtn");
const loginStatus = document.getElementById("loginStatus");
const usernameInput = document.getElementById("username");
const passwordInput = document.getElementById("password");
const showPasswordBtn = document.getElementById("showPassword");

function resetLoginButton() {
  document.body.classList.remove("login-success-transition");
  loginBtn.disabled = false;
  loginBtn.textContent = "LOG IN";
  loginStatus.textContent = "";
  loginStatus.className = "login-status";
}

window.addEventListener("focus", resetLoginButton);

showPasswordBtn.addEventListener("click", () => {
  const showing = passwordInput.type === "text";
  passwordInput.type = showing ? "password" : "text";
  showPasswordBtn.textContent = showing ? "Show" : "Hide";
});

(async function loadStoredCredentials() {
  try {
    const settings = await fetchJSON("/api/settings");
    usernameInput.value = settings.username || "";
    passwordInput.value = settings.password || "";
  } catch (error) {
    loginStatus.textContent = "Unable to load saved credentials.";
    loginStatus.className = "login-status error";
  }
})();

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const username = usernameInput.value.trim();
  const password = passwordInput.value.trim();

  if (!username) {
    loginStatus.textContent = "Username is required.";
    loginStatus.className = "login-status error";
    usernameInput.focus();
    return;
  }

  loginBtn.disabled = true;
  loginBtn.textContent = "LOGGING IN...";
  loginStatus.textContent = "Connecting to Beacon...";
  loginStatus.className = "login-status";

  try {
    await fetchJSON("/api/settings", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    loginBtn.textContent = "✓ LOGGED IN";
    loginStatus.textContent = "Login settings saved.";
    loginStatus.className = "login-status success";
    document.body.classList.add("login-success-transition");
    const transitionDelay = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 180 : 840;
    setTimeout(() => window.beabots?.goToDashboard(), transitionDelay);
  } catch (error) {
    loginBtn.disabled = false;
    loginBtn.textContent = "LOG IN";
    loginStatus.textContent = "Login failed. Please check your credentials.";
    loginStatus.className = "login-status error";
  }
});
