# Run Ember with clickables

1. Download the PR branch ZIP and **extract the whole folder** to a location you will keep (for example Desktop/Ember). Do not run inside the ZIP.
2. Open `devops/clickables`, then your OS folder: **Windows**, **macOS**, or **Linux**.
3. Double-click **1 - Setup**. First use downloads a private Python 3.12 runtime, installs the full requirements and spaCy model, and runs offline checks. Internet access is needed; wait for completion. No preinstalled Python or terminal commands are needed on supported configurations.
4. Setup creates **Desktop/clickables** shortcuts and opens the local device/key UI. Enter your own Gemini key, select/test your devices, and Save.
5. From Desktop/clickables, click **2 - Monitor**, then **3 - Start Ember** to talk. Use **4 - Stop Ember** when finished. **1 - Setup** reopens settings whenever you need them.

| Clickable | What it does |
|---|---|
| **1 - Setup** | Installs prerequisites if needed, creates Desktop shortcuts and opens configuration. |
| **2 - Monitor** | Opens live transcript/activity. Installs prerequisites on first use if necessary. |
| **3 - Start Ember** | Installs if necessary, opens Setup if no key is saved, then checks key/model access before hardware capture. |
| **4 - Stop Ember** | Requests graceful shutdown; does not delete settings, recordings or dependencies. |

Keep the extracted repository in place: Desktop shortcuts point to it. Moving/deleting it breaks those shortcuts; rerun Setup from the new location to recreate them. Keep OS launchers in their folders because they share the installer/dispatcher in the parent directory.

## Which order do I click?

- **First use:** 1 - Setup -> enter key/select devices -> Save -> 2 - Monitor -> 3 - Start Ember.
- **Later sessions:** 2 - Monitor -> 3 - Start Ember -> 4 - Stop Ember when finished.
- **Change key/devices:** reopen 1 - Setup, Save, then restart Ember to apply changes.

Monitor is optional and can open before or after Start; opening it first shows startup activity. Setup installs only when needed, so you do not need to reinstall for every conversation. Keep the Setup/Monitor server window open while using its pages. Stop ends the conversation runtime; it does not close the separate local UI server.

## Installation behavior

The installer uses the [official uv installer](https://docs.astral.sh/uv/getting-started/installation/) from astral.sh with [unmanaged installation](https://docs.astral.sh/uv/reference/installer/) into the repository's ignored `.devtools` directory. uv downloads Python from its managed Python distribution; it does not replace your existing Python. Python downloads and package caches use uv's per-user locations. Dependencies go into `Baymax-main/.venv`; the English language model is downloaded from spaCy's release infrastructure. No administrator access or persistent shell-policy/PATH changes are requested by these scripts.

Requirements changes trigger installation again. Readiness is recorded only after packages, language model and offline checks succeed. Failed installations remain unready and can be retried by clicking Setup. Simultaneous environment installers are blocked. An incompatible preexisting `.venv` is kept as `.venv.previous-<timestamp>` before a Python 3.12 environment is created. Never commit environments, installer files or private configuration.

Setup and Monitor leave a local server running. Closing a browser tab does not stop Ember; use Stop or Ctrl+C in the Start window. New installations do not include keys, production credentials or saved sessions.

## Platform limits

- **Windows:** automatic fresh-environment installation, offline checks and Desktop shortcut creation were verified on the developer PC. Windows may show a downloaded-file warning; your organization's policies can block scripts/downloads.
- **macOS:** use `.command` launchers. Intel Macs automatically select the experimental `requirements-macos-intel.txt` profile with MediaPipe 0.10.21 and compatible NumPy/OpenCV/spaCy pins; Apple Silicon retains the desktop profile. Use a native Terminal session on Apple Silicon; an x86_64 Python session selects the Intel profile. macOS may ask you to approve opening a downloaded script and allow Camera/Microphone for the launching app. Intel package resolution and simulated installer checks pass; actual Mac installation/devices and older macOS versions remain unverified. See `Baymax-main/MACOS_TESTING.md`.
- **Linux:** `.sh` launchers and Desktop links depend on the file manager allowing execution (Run/Trust). On minimal Debian 13, open **Linux/0 - Debian Prerequisites** first: it lists system libraries/build tools, asks for confirmation and uses administrator authorization for apt. Native ARM64 automatically uses the experimental Orange Pi CPU profile; see [Pi setup](../../Baymax-main/ORANGE_PI_SETUP.md). This is separate from the private Python environment. Hardware/fresh installation remain unverified.

## Key verification and external services

Start checks the saved key with Google's HTTPS model-metadata endpoint before loading the conversation runtime or opening devices. It checks the exact Live/summary models configured in the core; no silent substitutions. The key is a request header, never console output or a URL parameter. No audio/video is sent during this check. Failed verification stops startup with guidance. Metadata acceptance does not guarantee Live-session quota, generation or post-session analysis.

After starting the runtime, microphone/camera data goes to Gemini and audio is recorded locally. Enabled toolkit features contact IPWho for approximate public-IP location and Open-Meteo for weather; set `BAYMAX_TOOLKIT_ENABLED=0` in your private `.env` to disable them. The installer and offline checks never start capture or contact these providers.

Advanced validation without opening a UI: `python devops/clickables/bootstrap.py setup --uv .devtools/uv.exe --install-only` using Python 3.12 (use `.devtools/uv` on Unix). Normal users only need the clickables.

For sharing with the team, copy the three messages in [Discord quick start](../../docs/DISCORD_QUICKSTART.md).

Optional experimental fall detection: enable its checkbox in Setup, select a camera, Save and restart. Model loading/running/error state and actual FPS are shown in Monitor. No external alerts are enabled. It runs on the CPU unless the optional Orange Pi NPU pose setup has been done ([ORANGE_PI_SETUP.md](../../Baymax-main/ORANGE_PI_SETUP.md), "Optional: pose on the NPU"); see [fall toolkit](../../Baymax-main/features/toolkits/FALL_DETECTION.md).
