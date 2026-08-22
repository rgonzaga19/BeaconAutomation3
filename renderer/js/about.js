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

// Set by main.js loading this page as about.html?forced=1 when a
// mandatory update is pending — see main.js's createAboutWindow(true).
// In this mode there's no dashboard to go back to, so Home is hidden.
// The download still requires the user to click Download themselves —
// it's not started automatically — but Close/titlebar-close are wired
// (below) to quit the whole app rather than just this window, since
// that's the only way out of a mandatory update besides installing it.
const forcedMode = new URLSearchParams(window.location.search).get("forced") === "1";
const forcedBanner = document.getElementById("forcedBanner");

if (forcedMode) {
    document.body.classList.add("forced-mode");
    if (forcedBanner) forcedBanner.style.display = "flex";
}

window.beabots.onUpdateProgress((data) => {

    downloadSection.style.display = "block";

    downloadProgressBar.style.width = data.percent + "%";

    const downloadedMB =
        (data.downloadedBytes / 1024 / 1024).toFixed(1);

    const totalMB =
        (data.totalBytes / 1024 / 1024).toFixed(1);

    downloadPercent.textContent =
        `${data.percent}% (${downloadedMB} MB / ${totalMB} MB)`;

    // Mirror the percentage onto the button itself. The detailed progress
    // bar lives inside the scrollable content area and can end up off-screen
    // (e.g. if the left column pushes the panel taller than the window), but
    // this button sits in the footer, which is always visible, so the user
    // never loses track of an in-progress download.
    if (data.percent < 100) {
        btnDownload.textContent = `⬇ Downloading... ${data.percent}%`;
    }

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

        // Safety valve: this window was only opened in forced mode because
        // the main process's own check found a version mismatch. If this
        // independent check disagrees, don't leave the user stuck looking
        // at an unclosable "up to date" screen — lift the lock and let the
        // dashboard open normally.
        if (forcedMode) {
            window.beabots?.releaseForceLock?.();
        }

    } else {

        updateStatusEl.textContent = "▲ Update Available";
        updateStatusEl.className = "update-status update";

        if (update.alreadyDownloaded) {

            // Already fully downloaded in a previous session (e.g. the
            // user picked "Install Later" and relaunched) — main.js found
            // and verified it in the temp folder. No need to fetch it
            // again; just let them confirm installing the copy on disk.
            updateStatusEl.textContent = "✔ Update downloaded — ready to install";
            updateStatusEl.className = "update-status success";

            btnDownload.disabled = false;
            btnDownload.textContent = "Install Update";

            btnDownload.onclick = async () => {

                btnDownload.disabled = true;
                btnDownload.textContent = "Installing...";

                try {

                    const installNow = await window.beabots.installUpdate();

                    if (!installNow) {
                        btnDownload.disabled = false;
                        btnDownload.textContent = "Install Update";
                        return;
                    }

                    console.log("User chose Install");

                } catch (err) {

                    alert(err.message);

                    btnDownload.disabled = false;
                    btnDownload.textContent = "Install Update";

                }

            };

        } else {

            btnDownload.disabled = false;
            btnDownload.textContent = "Download Update";

            btnDownload.onclick = async () => {

                btnDownload.disabled = true;
                btnDownload.textContent = "Downloading...";

                downloadSection.style.display = "block";
                downloadProgressBar.style.width = "0%";
                downloadPercent.textContent = "0%";
                downloadText.textContent = "Downloading update...";

                // Bring the detailed progress panel on screen in case it's
                // currently scrolled out of view — a courtesy on top of the
                // always-visible percentage now shown on this button.
                downloadSection.scrollIntoView({ behavior: "smooth", block: "nearest" });

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

    }

    releaseNotesEl.innerHTML = "";

    for (const note of update.notes) {

        const li = document.createElement("li");
        li.textContent = note;
        releaseNotesEl.appendChild(li);

    }

}

document.getElementById("btnHome")?.addEventListener("click", () => {
    if (forcedMode) return;
    window.beabots?.goHome();
});

// In forced mode there's no dashboard to return to, so Close doesn't just
// close this window — it exits the app entirely. This is the only way out
// if the user picks "Install Later" after downloading. Relaunching will
// re-trigger this same locked screen until they actually install.
if (forcedMode) {
    btnClose.textContent = "Exit App";
    btnClose.title = "Quit Beabots";
}

btnClose.addEventListener("click", () => {
    if (forcedMode) {
        window.beabots?.quitApp?.();
        return;
    }
    window.close();
});

btnTitlebarClose.addEventListener("click", () => {
    if (forcedMode) {
        window.beabots?.quitApp?.();
        return;
    }
    window.close();
});

loadAbout();