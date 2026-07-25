const versionEl = document.getElementById("version");
const ownerEl = document.getElementById("owner");
const planEl = document.getElementById("plan");
const expiresEl = document.getElementById("expires");

const latestVersionEl = document.getElementById("latestVersion");
const releaseNotesEl = document.getElementById("releaseNotes");

const btnDownload = document.getElementById("btnDownload");
const btnClose = document.getElementById("btnClose");

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

    releaseNotesEl.innerHTML = "";

    for (const note of update.notes) {

        const li = document.createElement("li");
        li.textContent = note;
        releaseNotesEl.appendChild(li);

    }

    btnDownload.onclick = () => {
        window.beabots.openExternal(update.download);
    };  

}

btnClose.addEventListener("click", () => {
    window.close();
});

loadAbout();