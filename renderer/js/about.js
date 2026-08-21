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

const downloadSection = document.getElementById("downloadSection");
const downloadProgressBar = document.getElementById("downloadProgressBar");
const downloadPercent = document.getElementById("downloadPercent");
const downloadText = document.getElementById("downloadText");

window.beabots.onUpdateProgress((data) => {

    downloadSection.style.display = "block";

    downloadProgressBar.style.width = data.percent + "%";

    const downloadedMB =
        (data.downloadedBytes / 1024 / 1024).toFixed(1);

    const totalMB =
        (data.totalBytes / 1024 / 1024).toFixed(1);

    downloadPercent.textContent =
        `${data.percent}% (${downloadedMB} MB / ${totalMB} MB)`;

    if (data.percent >= 100) {

        downloadText.textContent = "Download Complete ✔";

    }

});

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

        btnDownload.onclick = async () => {

            btnDownload.disabled = true;
            btnDownload.textContent = "Downloading...";

            downloadSection.style.display = "block";
            downloadProgressBar.style.width = "0%";
            downloadPercent.textContent = "0%";
            downloadText.textContent = "Downloading update...";

            try {

                const result = await window.beabots.downloadUpdate(update.download);

                updateStatusEl.textContent = "✔ Update downloaded";
                updateStatusEl.className = "update-status success";

                btnDownload.textContent = "Downloaded";

                const installNow = await window.beabots.installUpdate();

                if (!installNow) {
                    btnDownload.textContent = "Install Later";
                    return;
                }

                console.log("User chose Install");

            } catch (err) {

                alert(err.message);

                btnDownload.disabled = false;
                btnDownload.textContent = "Download Update";

            }

        };

    }

    releaseNotesEl.innerHTML = "";

    for (const note of update.notes) {

        const li = document.createElement("li");
        li.textContent = note;
        releaseNotesEl.appendChild(li);

    }

}

document.getElementById("btnHome")?.addEventListener("click", () => {
    window.beabots?.goHome();
});

btnClose.addEventListener("click", () => {
    window.close();
});

btnTitlebarClose.addEventListener("click", () => {
    window.close();
});

loadAbout();