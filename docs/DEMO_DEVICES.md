# Device demos and OPI4 extensions

| | LattePanda Demo (PR #20) | OPI4 Demo |
|---|---|---|
| Branch | `startup-greeting` | `opi4-demo` |
| Review base | `mvp-v10` | `mvp-v10` |
| CPU | Intel x86_64 / AMD64 | Orange Pi 4 Pro ARM64 / aarch64 |
| OS setup | Debian 13 desktop | 64-bit Debian/DietPi; desktop needed for Setup/Monitor |
| Dependencies selected by Setup | `requirements-pc.txt` | `requirements-orange-pi.txt` |
| Eye connection | onboard Leonardo's saved serial path | external Firmata-compatible Arduino; set its real serial path |
| Wi-Fi provisioning | existing NetworkManager path | optional single-radio DietPi hotspot in OPI4 branch |

Both start `Baymax-main/run_mvp.py` -> `run_pc.py` -> `realtime_gemini_10.py`.
Both include the same conversation/personality, Gemini session, audio cleanup,
400 ms turn-end default, reply-time echo protection, local eye expressions,
60-second idle sleep eyes, engage/disengage, reminders/repeats/check-ins,
audiobooks/wake recognition, app integration, public tools, and built-in
pose/fall detection. Existing feature flags, opt-ins and saved settings still
apply; app integration needs its device key and alerts need their recipients.
The OPI4 profile additionally packages Memory Palace and people recognition
code dormant, without activating their runtime hooks. It groups source and
settings and adds latest-frame parallel vision; see `OPI4_FEATURES.md`.
These extensions are not claimed as installed on LattePanda.

The child-process vision fix, lite pose model and reduced inference rate stay
in both demos. These are shared performance fixes, not different features.
The initial split kept shared runtime, feature modules, bootstrap and feature
tests identical. Later OPI4 extensions change that baseline; validate a separate
LattePanda port rather than claiming current byte parity.
Both PRs are sibling demos against `mvp-v10`, not additive feature patches:
review/select the device branch rather than merging the pair into each other.

## Setup

Follow this branch's `Baymax-main/MVP.md`. Setup detects Linux x86_64 versus
ARM64 and installs the corresponding pinned dependency file under Python 3.12.
Both profiles include pywebrtc-audio (audio cleanup), pyfirmata2 (eyes), miniaudio
(audiobook decoding) and Vosk (offline wake). ARM miniaudio may compile from
source; install compiler tools. ARM MediaPipe/NumPy pins differ from x86 pins.

The prerequisite helper supports Debian 13 only. On DietPi/another Debian
version, provision equivalent system libraries first: curl, ca-certificates,
libportaudio2, libgl1, libegl1, libgles2, libglib2.0-0 (or the distro's t64
variant), libsndfile1, libasound2-plugins, build-essential, cmake, pkg-config,
and xdg-utils; a browser/terminal and working desktop audio for Setup/Monitor.
Kernel/audio-server provisioning and SD-card image creation are separate work.
Do not run the root-owned Orange Pi single-radio hotspot service on LattePanda.

Save the actual microphone, speaker and camera in Setup. Never copy another
robot's `.env`, API/device keys or `/etc/ember/device.toml`. Confirm the local
clock/timezone. Stop the older runtime/eye demo before launching this one.
On Orange Pi, connect a Firmata-compatible Arduino with the documented MAX7219
wiring and set `BAYMAX_EYES_PORT` to its actual `/dev/serial/by-id/...` path;
the LattePanda Leonardo default is not an Orange Pi port. Grant the desktop
user serial access (usually `dialout`, followed by logging in again).

For voice sleep, keep audio enhancement healthy and allow the first Vosk model
download (or prefetch with `.venv/bin/python features/toolkits/wakeword.py --download`
from `Baymax-main`). Missing cleanup or wake recognition refuses/undoes sleep.
Disengage withholds Gemini-bound microphone audio; camera frames and existing
raw local recordings continue. This is voice sleep, not a privacy-off switch.

## Validation

Run `python check_release.py` in `Baymax-main` on each branch; preserve separate
core/toolkit results. The profile test checks shared feature dependencies and
the installer architecture selection. Compare shared files between the branch
heads before publishing; only device docs, Wi-Fi provisioning and its tests may
differ for this split. Offline checks do not establish physical equivalence.

On each device, verify installation/native imports, selected devices, audible
multi-turn conversation/echo, engage/disengage (including reminder/fall wake),
eyes, audiobook playback, reminders/app integration, fall behavior, sustained
camera+audio performance, clean stop/restart and cold boot. Orange Pi hotspot
AP mode and phone setup also require board testing. Hardware/provider checks
remain pending; no device, service, saved SD image or production alert is changed
by preparing these branches. Software-lead review remains required for release.
