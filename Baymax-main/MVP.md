# MVP — integrated POC

This branch combines PR #7 (setup/device compatibility), #11 (toolkits and
feature folders), and #12 (Conversational Ability). `run_mvp.py` starts their
shared runtime, `realtime_gemini_10.py`, through the existing key/model preflight
in `run_pc.py`. (The original MVP, PR #15, ran v8; this copy runs v10, which adds
fall detection, robot-started conversations and `device.toml` settings.)
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
- Optional (off by default; `BAYMAX_WAIT_CUE_ENABLED=1`): one short local
  “hmm” once per wait over 2s, through the existing speaker stream/AEC reference.
  Reply audio or detected near-end speech cancels it. Requires working echo cancellation.
  It is a bundled synthetic cue, not Gemini speech or another model request.

PR #8 people awareness, #9 latency optimizations, #13 Memory Palace,
new reminder/audiobook work and the fleet agent are outside this MVP.
See [LED eye setup](features/eyes/README.md) for serial access, enabling/disabling
and wiring. Physical second-matrix behavior still needs validation with MVP.
v8 remains in the repository but is no longer the MVP entry point. Ember answers
only speech meant for it (`conversation.only_when_addressed` in `device.toml`): say
"Ember" to start, then keep talking within 20 seconds of its reply (see
[addressing](features/conversational_ability/README.md#speaking-only-when-addressed-addressingpy)).
Transcripts never leave the robot.

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

The installer writes `ember-mvp.service` and `ember-monitor.service` under the
user's configuration `systemd/user/` directory, `ember-mvp.desktop` under
`autostart/`, and Monitor launchers in Applications and the localized Desktop.
It reloads the user service manager and enables both services at `default.target`
but does not start capture. At desktop login, the autostart entry imports the
desktop environment and starts both services.
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
To remove autostart, disable and stop both services with
`systemctl --user disable --now ember-mvp.service ember-monitor.service`, delete
the generated service/autostart files and Monitor launchers, then run
`systemctl --user daemon-reload`.
Removing autostart does not remove `.env` or recordings.

## Debugging Monitor and interruption tuning

All supplied eye poses now work through explicit Gemini expression commands:
happy, sad, excited, confused spinner, surprised, curious, love, sleep bubbles
and dead/X eyes. Ask "sound sad" or "act sleepy" to request matching voice/eyes.
Normal blinking remains the default; >2s thinking dots remain automatic. Actual
fall-alert dispatch forces surprise over other eye modes. This does not enable
fall detection or infer emotion from speech audio. Expressions remain through
buffered playback, reset afterward, and have a safety expiry. See
[`features/eyes/README.md`](features/eyes/README.md) for priority and timing limits.

MVP now defaults to `BAYMAX_INTERRUPTION_ENABLED=1`: healthy echo processing keeps
microphone audio flowing during replies, and speech activity can interrupt
the current model response. Set this to `0` in `.env` for non-interruptible
replies. AEC/residual subtraction remain enabled. This does not guarantee
that leaked robot speech will never be recognized or answered afterward; deleting
transcript words cannot retract already-sent audio. Real mixed-voice behavior on
the LattePanda remains pending. Failed DSP retains its half-duplex protection.

After updating, reinstall the startup files and start the desktop session:

```bash
cd ~/Ember-MVP
python3 devops/mvp/install_autostart.py
bash devops/mvp/start-session.sh
```

It opens `http://127.0.0.1:8766/monitor` on the LattePanda, showing transcripts,
listening/thinking/speaking state, microphone/DSP metrics and tool activity.
Monitor opens automatically at login when a physical display is connected
(Linux DRM connector status). Without one, it remains available locally: use
the **Ember Monitor** desktop/Applications launcher, including in remote desktop.
GNOME may require right-click **Allow Launching** once; desktop icons depend on
the desktop's icon support. Display hot-plug after login uses the launcher.
First-run Setup uses port 8767, so it can coexist with Monitor. The Monitor
service remains available before configuration is saved and never opens sensor streams.
Closing this UI does not stop either service. Use Monitor while running; device
tests should be performed with Ember stopped so they do not compete for capture.

If the browser freezes, close/reopen it through the launcher. If the server
stalls, use `systemctl --user restart ember-monitor.service`; Ember keeps running.
Requests time out after five seconds and polling retries. Check startup with:

```bash
systemctl --user is-enabled ember-mvp.service ember-monitor.service
systemctl --user status ember-mvp.service ember-monitor.service --no-pager -l
journalctl --user -u ember-mvp.service -u ember-monitor.service -b -n 60 --no-pager
```

For unattended boot on GNOME/GDM, enable **Automatic Login** for `meowmax` in
Settings → System → Users (unlock first). This allows access to that account
at the physical console without a login password. The installer does not change
GDM. Verify a real cold boot; remote desktop login alone does not prove it works.

Conversational Ability now defaults to low speech-start sensitivity to reduce
false interruptions. MVP waits for 900 ms of end-of-turn silence, configurable
with `BAYMAX_TURN_END_SILENCE_MS` (300-2000). Intentional interruptions
may need clearer speech. `BAYMAX_SPEECH_START_SENSITIVITY=high` in private `.env`
restores the earlier detection setting; restart the service after changes.

Echo protection removes positive AGC boost while the speaker/device buffer and
a 250 ms echo tail are active, so cancelled robot speech is not re-amplified.
Healthy processing continues to pass user speech during replies; failed DSP
uses a visible half-duplex fallback through the echo tail. Monitor shows the
protection state and AEC delay, using callback ADC/DAC timing where available.
These status indicators do not certify physical echo cancellation. Verify in
silence first, then with deliberate interruptions at normal speaking volume.

Residual echo detection now searches recent actual speaker audio over changing
delays and subtracts a confidently fitted component while keeping unexplained
user audio. Uncertain/strong double-talk frames pass unchanged. It does not
require a fixed mic/speaker distance. To compare primary WebRTC alone, set
`BAYMAX_RESIDUAL_ECHO_ENABLED=0` in `.env` and restart.

Monitor also warns when a substantial user-transcript prefix overlaps recent
Ember speech. The text is always retained; this late diagnostic never blocks
an interruption or assumes a person repeating Ember is an echo. Check the new
residual correlation/state alongside the warning rather than treating either
as proof of speaker identity.

## Validation and remaining checks

Windows/Python 3.12.15: **180 core + 73 toolkit offline tests pass**, with zero
failures, errors or skips, using `python check_release.py`. New checks cover
first setup, subsequent startup without setup, explicit devices/no-camera,
cancellation, preserving settings after failure, concurrent launch protection,
HTTP save/shutdown and user-scoped autostart generation with spaces in paths.
Twenty-two eye tests verify timing/state transitions, expressions, normal blinking, decoded
MAX7219 transfers, mounting, hardware failure and lifecycle using fakes.
Three eye-tool tests cover declaration gating, validation, hardware failures,
and real async dispatch/call IDs; fall check-in tests verify the forced event.
Twelve waiting-cue checks cover PCM priority, microphone cancellation, echo
reference, timing, one-shot behavior and invalid assets. Saved feature overrides
are checked before feature startup.
Five echo regressions cover speaker-active gain compensation without muting
near-end samples, quiet-period gain, device-buffer/tail timing, callback delay
hints and failed-DSP fallback after the Python playback queue drains. The opt-in
native `tests/aec_synthetic_probe.py` reports 11.0 -> 26.1 dB echo reduction on
generated signals and non-muted voiced double talk. Physical validation remains pending.
Ten residual/text tests cover multiple delays, polarity, movement, quiet/strong
double talk, stale/missing references, failure isolation and non-destructive
transcript warnings. Native synthetic behavior remains verified with this layer.
Actual room/movement behavior, quiet interruptions and LattePanda CPU cost remain pending.
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
