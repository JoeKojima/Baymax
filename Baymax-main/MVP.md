# MVP — integrated POC

This branch combines PR #7 (setup/device compatibility), #11 (toolkits and
feature folders), and #12 (Conversational Ability). `run_mvp.py` starts their
shared v8 runtime through the existing key/model preflight in `run_pc.py`.
Target: Debian 13 x86_64 LattePanda ember-002, running a desktop session with
working audio and camera permissions. Actual target-device validation is pending.

## Included features

- One-time local configuration of Gemini key, microphone, speaker and camera.
  Choose explicit devices; use **No camera** for audio-only operation.
- Echo cancellation, noise reduction, automatic gain and responsive turn-taking,
  with the existing safe fallback when the audio processor cannot initialize.
- Location/weather and opt-in public information tools, monitor, device tests,
  graceful stop and the existing optional CPU fall-detection worker.
- Organized implementations in `features/conversational_ability/` and
  `features/toolkits/`. Memory/person packages remain reserved namespaces.
- Optional automatic startup at each Linux desktop login using a dedicated
  user service. This does not replace the existing production Ember service.
- LED eyes using the user's normal blink and six-position thinking-dot patterns.
  Normal blinking continues during listening/speaking; thinking begins only
  after more than 2s since the latest user-transcription fragment. Reply audio,
  turn end/interruption and reconnect restore normal blinking. No servo.
- One short local “hmm” once per wait over 2s, through the existing speaker
  stream/AEC reference. Reply audio or detected near-end speech cancels it.
  Requires working echo cancellation; set `BAYMAX_WAIT_CUE_ENABLED=0` to disable.
  It is a bundled synthetic cue, not Gemini speech or another model request.

PR #8 people awareness, #9 latency optimizations, #13 Memory Palace,
new reminder/audiobook work and the fleet agent are outside this MVP.
See [LED eye setup](features/eyes/README.md) for serial access, enabling/disabling
and wiring. Physical second-matrix behavior still needs validation with MVP.
The older v10 runtime remains in the repository but is not the MVP entry point.

## Install and configure on ember-002

Use the complete `MVP` branch checkout or extracted ZIP, with `Baymax-main/`
and `devops/` together. From the project root:

On a fresh LattePanda, stop the separate eye demo first, then download the PR:

```bash
sudo apt update
sudo apt install -y git
git clone --branch MVP --single-branch https://github.com/JoeKojima/Baymax.git ~/Ember-MVP
cd ~/Ember-MVP
```

For an existing checkout instead, use `cd ~/Ember-MVP && git pull --ff-only`.
Run the commands below as the desktop user; the prerequisite script requests
administrator access for system packages. Do not run Ember itself with sudo.

```bash
# Debian system libraries; displays packages and asks for administrator access.
bash "devops/clickables/Linux/0 - Debian Prerequisites.sh"

# Installs private Python 3.12, dependencies/model and runs offline checks.
# Then opens Setup; save explicit devices/key and close Setup with Ctrl+C.
bash "devops/clickables/Linux/1 - Setup.sh"

# Starts MVP. If configuration is incomplete, Setup opens once and saving
# explicit devices plus a key starts the runtime automatically.
Baymax-main/.venv/bin/python Baymax-main/run_mvp.py
```

Save choices once. Subsequent MVP launches reuse `.env` without opening Setup.
Audio choices retain device names and host APIs to handle index reordering.
Linux camera selection retains its explicit OpenCV index; keep the same USB
ports and verify camera enumeration across reboot. A disconnected device or
rejected key is an error, not a reason to silently choose a different device or
reopen setup. Explicitly change configuration with `run_mvp.py --setup`.
Do not run a second runtime from the older Start Ember shortcut simultaneously.

Starting Ember sends microphone audio and enabled camera frames to Gemini and
records session audio locally. Preflight sends only key/model metadata requests.
Toolkits contact their documented providers; public tools and fall detection
remain opt-in. Setup device discovery opens cameras to check availability;
device-test buttons deliberately record a short sample/play a tone/take a snapshot.
No extra capture streams are added to the runtime.

## Automatic startup

Stop any manually started runtime with Ctrl+C first. Then, as `meowmax` in the
robot's desktop session, without sudo:

```bash
python3 devops/mvp/install_autostart.py
# Deliberately start now, or log out and back in to exercise autostart:
bash devops/mvp/start-session.sh
```

If an earlier install reports `Loaded: bad-setting`, update and regenerate the
service (the installer fixes the previously quoted WorkingDirectory):

```bash
git pull --ff-only
python3 devops/mvp/install_autostart.py
systemctl --user reset-failed ember-mvp.service
bash devops/mvp/start-session.sh
systemctl --user status ember-mvp.service -l --no-pager
```

The installer writes only `ember-mvp.service` under the user's configuration
`systemd/user/` directory and `ember-mvp.desktop` under `autostart/`. It reloads
the user service manager but does not start capture. At desktop login, the
autostart entry imports the desktop environment and starts the user service.
The service uses the installed private Python directly: no package installation,
upgrade or model-install prompt is performed on subsequent boots.

Boot-to-conversation without intervention requires Debian desktop **automatic
login** for `meowmax`. This installer does not change display-manager settings,
enable lingering, or configure a headless boot service. A remote-desktop session
alone does not establish that boot auto-login is configured. Do not enable this
alongside an already running production Ember service.

```bash
systemctl --user status ember-mvp.service
journalctl --user -u ember-mvp.service -b
systemctl --user stop ember-mvp.service
```

SIGTERM follows the runtime's existing Ctrl+C cleanup path, with up to 180s for
session cleanup. Failed starts retry after 15s; saved settings are retained.
To remove autostart, stop the service, delete only the two generated files from
the user's configuration directory, then run `systemctl --user daemon-reload`.
Removing autostart does not remove `.env` or recordings.

## Validation and remaining checks

Windows/Python 3.12.15: **152 core + 70 toolkit offline tests pass**, with zero
failures, errors or skips, using `python check_release.py`. New checks cover
first setup, subsequent startup without setup, explicit devices/no-camera,
cancellation, preserving settings after failure, concurrent launch protection,
HTTP save/shutdown and user-scoped autostart generation with spaces in paths.
Fifteen eye tests verify timing/state transitions, normal blinking, decoded
MAX7219 transfers, mounting, hardware failure and lifecycle using fakes.
Twelve waiting-cue checks cover PCM priority, microphone cancellation, echo
reference, timing, one-shot behavior and invalid assets. Saved feature overrides
are checked before feature startup.
These use simulated devices/providers and do not measure conversation latency.

Pending on ember-002: clean install/native audio processor imports, actual
mic/speaker/camera, echo/interruptions/short answers, key rejection before
capture, public tools/provider recovery, graceful stop, desktop auto-login,
first-run configuration and two cold boots with no repeated setup prompt.
Verify both eyes on real hardware, default blinking while listening/speaking,
no thinking for waits <=2s, thinking dots on longer waits, and reset/cleanup.
Verify the local cue's voice/volume, once-only playback, no echo-triggered reply,
and cancellation on real reply/user interruption with working native AEC.
Confirm no duplicate production runtime. Follow `TESTING_PROTOCOL.md` for live
core/toolkit evidence and software-lead approval before merge/deployment.
