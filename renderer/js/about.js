const versionEl = document.getElementById("version");
const ownerEl = document.getElementById("owner");
const planEl = document.getElementById("plan");
const expiresEl = document.getElementById("expires");

const latestVersionEl = document.getElementById("latestVersion");
const updateStatusEl = document.getElementById("updateStatus");
const releaseNotesEl = document.getElementById("releaseNotes");


const btnDownload = document.getElementById("btnDownload");
const btnClose = document.getElementById("btnClose");
const btnTitlebarClose = document.getElementById("btnTitlebarClose");

async function loadAbout() {

    // ----------------------------
    // Current app version
    // ----------------------------
    const version = await window.beabots.getVersion();
    versionEl.textContent = "Version " + version;

    // ----------------------------
    // License information
    // ----------------------------
    const settings = await window.beabots.getSettings();

    ownerEl.textContent =
        settings.license_owner || "Unknown";

    planEl.textContent =
        settings.license_plan || "Unknown";

    expiresEl.textContent =
        settings.license_expiry || "Unknown";

    // ----------------------------
    // Check for updates
    // ----------------------------
    const update = await window.beabots.checkForUpdates();

    latestVersionEl.textContent = update.version;

    const currentVersion = version.trim();
    const latestVersion = update.version.trim();

    if (currentVersion === latestVersion) {

        updateStatusEl.textContent = "✔ You're using the latest version";
        updateStatusEl.className = "update-status success";

        btnDownload.disabled = true;
        btnDownload.textContent = "Up To Date";

    } else {

        updateStatusEl.textContent = "▲ Update Available";
        updateStatusEl.className = "update-status update";

        btnDownload.disabled = false;
        btnDownload.textContent = "Download Update";

        btnDownload.onclick = () => {
            window.beabots.openExternal(update.download);
        };

    }

    releaseNotesEl.innerHTML = "";

    for (const note of update.notes) {

        const li = document.createElement("li");
        li.textContent = note;
        releaseNotesEl.appendChild(li);

    }

}

btnClose.addEventListener("click", () => {
    window.close();
});

btnTitlebarClose.addEventListener("click", () => {
    window.close();
});

loadAbout();