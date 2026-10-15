# Click to use Ember

Open the folder for your OS first: **Windows/** (`.cmd`), **macOS/** (`.command`), or **Linux/** (`.sh`). On Windows, double-click the `.cmd` files. The Python environment must already be installed (install it using Baymax-main/PC_SETUP.md).

| Clickable | What happens |
|---|---|
| **1 - Setup** | Opens the key/device settings and device tests in your browser. |
| **2 - Monitor** | Opens the live Ember activity page in your browser. |
| **3 - Start Ember** | Starts the Gemini conversation in a terminal. Speak after “Connected”. |
| **4 - Stop Ember** | Requests a graceful stop of this runtime, including local session cleanup. |

Setup and Monitor leave the local UI server running. Closing a browser tab does not stop Ember. Use Stop Ember or Ctrl+C in the Start Ember terminal. Stop does not delete your key, settings or recordings. If you launched an older runtime before these launchers were added, stop it with Ctrl+C once and use the new launcher afterward.

Start uses your saved Gemini key and devices. It sends microphone/camera data to Gemini and records audio locally as the existing runtime does. Enabled location/weather tools also contact IPWho (public-IP location) and Open-Meteo (approximate coordinates/weather); set `BAYMAX_TOOLKIT_ENABLED=0` in your private `.env` to disable these tools.

For Mac open `macOS/` and use the `.command` files after making them executable (`chmod +x *.command`). For Linux open `Linux/` and use the `.sh` files after `chmod +x *.sh`; double-click behavior depends on the file manager. These launchers use the existing environment; they are not installers. Windows is verified; Mac/Linux launchers still require platform testing.

Keep the whole repository layout intact: OS folders depend on the shared `devops/clickables/launch.py` and `Baymax-main` environment. Do not move individual launchers out of their OS folders.

## Startup key verification

Start Ember contacts Google's HTTPS model-metadata endpoint with your saved key before loading the conversation runtime or opening devices. It checks the exact Live and summary models configured in the core; no model is silently substituted. The key is sent in a header, never printed or put in the request URL. No audio, images or conversation content is sent by this check.

Rejected credentials, denied access, unavailable models, quota errors, network failures and unreadable responses stop startup with guidance. Update the key in Setup and Save, or resolve the reported connection/project problem. A metadata pass confirms key acceptance/model visibility, not Live-session quota, successful generation or post-session analysis. Those can still fail when used. Setup, Monitor, Stop and the offline tests do not make this verification request.
