/* Embedded dashboard settings: license, theme, and Beacon server only. */

const accessKeyInput = document.getElementById("accessKey");
const saveBtn = document.getElementById("saveBtn");
const saveStatus = document.getElementById("saveStatus");
const themeButtons = [...document.querySelectorAll(".choice-btn[data-theme]")];
const serverButtons = [...document.querySelectorAll(".choice-btn[data-server]")];
let currentTheme = "dark";
let currentServer = "s4";

function selectChoice(buttons, attribute, value) {
  buttons.forEach((button) => {
    const selected = button.dataset[attribute] === value;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
}

themeButtons.forEach((button) => {
  button.addEventListener("click", () => {
    currentTheme = button.dataset.theme;
    selectChoice(themeButtons, "theme", currentTheme);
    window.beabotsTheme?.setTheme(currentTheme);
  });
});

serverButtons.forEach((button) => {
  button.addEventListener("click", () => {
    currentServer = button.dataset.server;
    selectChoice(serverButtons, "server", currentServer);
  });
});

document.getElementById("toggleLicense").addEventListener("click", (event) => {
  const showing = accessKeyInput.type === "text";
  accessKeyInput.type = showing ? "password" : "text";
  event.currentTarget.textContent = showing ? "Show" : "Hide";
});

async function loadSettings() {
  try {
    const [settings, theme] = await Promise.all([
      fetchJSON("/api/settings"),
      window.beabots?.getTheme(),
    ]);
    accessKeyInput.value = settings.access_key || "";
    currentServer = settings.server === "s2" ? "s2" : "s4";
    currentTheme = theme === "light" ? "light" : "dark";
    selectChoice(serverButtons, "server", currentServer);
    selectChoice(themeButtons, "theme", currentTheme);
  } catch (error) {
    saveStatus.textContent = "Unable to load settings.";
    saveStatus.className = "save-status error";
  }
}

saveBtn.addEventListener("click", async () => {
  saveBtn.disabled = true;
  saveStatus.textContent = "Saving...";
  saveStatus.className = "save-status";
  try {
    await fetchJSON("/api/settings", {
      method: "POST",
      body: JSON.stringify({
        access_key: accessKeyInput.value.trim(),
        server: currentServer,
      }),
    });
    saveStatus.textContent = "Settings saved.";
    saveStatus.className = "save-status success";
  } catch (error) {
    saveStatus.textContent = "Unable to save settings.";
    saveStatus.className = "save-status error";
  } finally {
    saveBtn.disabled = false;
  }
});

window.beabots?.onThemeChanged((theme) => {
  currentTheme = theme;
  selectChoice(themeButtons, "theme", currentTheme);
});

loadSettings();
