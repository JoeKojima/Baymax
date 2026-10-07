# Click to use Ember

On Windows, double-click the `.cmd` files. The Python environment must already be installed (install it using Baymax-main/PC_SETUP.md).

| Clickable | What happens |
|---|---|
| **1 - Setup** | Opens the key/device settings and device tests in your browser. |
| **2 - Monitor** | Opens the live Ember activity page in your browser. |
| **3 - Start Ember** | Starts the Gemini conversation in a terminal. Speak after “Connected”. |
| **4 - Stop Ember** | Requests a graceful stop of this runtime, including local session cleanup. |

Setup and Monitor leave the local UI server running. Closing a browser tab does not stop Ember. Use Stop Ember or Ctrl+C in the Start Ember terminal. Stop does not delete your key, settings or recordings. If you launched an older runtime before these launchers were added, stop it with Ctrl+C once and use the new launcher afterward.

Start uses your saved Gemini key and devices. It sends microphone/camera data to Gemini and records audio locally as the existing runtime does. Enabled location/weather tools also contact IPWho (public-IP location) and Open-Meteo (approximate coordinates/weather); set `BAYMAX_TOOLKIT_ENABLED=0` in your private `.env` to disable these tools.

For Mac use the `.command` files after making them executable (`chmod +x *.command`). For Linux use the `.sh` files after `chmod +x *.sh`; double-click behavior depends on the file manager. These launchers use the existing environment; they are not installers. Windows is verified; Mac/Linux launchers still require platform testing.

Keep this folder inside `devops/` so relative paths resolve correctly. The same folder layout is included in `baymax-pc.zip`.
