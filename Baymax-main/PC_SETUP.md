# Ember desktop development

The Monitor's **Latency & audio diagnostics** link opens runtime timing, a manual
Gemini connection check, and microphone recording/replay and speaker-test links.
See [diagnostic measurements and limits](DIAGNOSTICS.md).

Windows Gemini conversation, device setup and monitor were tested on the developer PC. Mac/Linux hardware and clean installs still require verification. This is a development extension of version 8; confirm the robot's actual deployed version/commit before deployment.

## Clickable installation (recommended)

Extract the whole repository, open `devops/clickables` -> your OS folder -> **1 - Setup**. It downloads Python 3.12, installs packages/model, runs offline tests, creates Desktop clickables and opens key/device configuration. No terminal commands are needed on supported configurations; see [platform details](../devops/clickables/README.md).

## Manual installation (advanced)

Install Python 3.12, clone this branch, and open a terminal in `Baymax-main`.

Windows:
```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-pc.txt
.\.venv\Scripts\python.exe -m spacy download en_core_web_sm
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe devUI.py
```

Mac/Linux (not yet hardware-verified):
```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-pc.txt
.venv/bin/python -m spacy download en_core_web_sm
.venv/bin/python -m unittest discover -s tests
.venv/bin/python devUI.py
```

On Intel macOS, use `requirements-macos-intel.txt` instead of `requirements-pc.txt` for manual installation. The macOS Setup clickable selects it automatically. Actual Mac installation/device testing remains pending.

Paste your own Gemini key in devUI, select/test microphone, speaker and camera, then Save. Never copy production email credentials. Windows audio uses full WASAPI endpoint names; cameras use DirectShow names. Saved names resolve device IDs again at startup. Mac cameras now use native AVFoundation names and saved unique IDs. This code has simulation coverage; actual Mac hardware still needs testing. See [Mac verification](MACOS_TESTING.md).

## Click to run

Open your OS folder under `devops/clickables` (`Windows`, `macOS` or `Linux`) and use the numbered launchers: Setup, Monitor, Start Ember, Stop Ember. `.cmd` is for Windows, `.command` for Mac and `.sh` for Linux; Unix launchers may need `chmod +x`. Setup, Monitor and Start install the environment automatically when needed. Keep the whole repository together; Setup creates Desktop shortcuts to its OS launchers. Alternatively run `python run_pc.py` using the environment interpreter.

Monitor: http://127.0.0.1:8766/monitor. It shows live transcript fragments, compact toolkit start/end messages, reply timing within speech bubbles, and separate Listening messages. New messages appear at the bottom; scroll up to pause following. Thinking means waiting for Gemini, not access to reasoning. Transcription timing depends on Gemini delivery; it is not guaranteed to arrive one word at a time.

Stop requests graceful shutdown and existing voice analysis. Recent transcripts remain in the ignored local `.runtime-status.json` until the next run replaces it. Closing the browser does not stop Ember.

## Data and parity

The runtime sends microphone audio/camera frames to Gemini and records audio locally. The optional upstream Flask dashboard is available through `run_dashboard_pc.py`; its local accounts are separate from robot credentials. Memory retrieval and pose/fall inference remain disabled in the upstream version-8 demo.

Location/weather tools contact IPWho with the device public IP, Open-Meteo with approximate coordinates or a city query, and return results to Gemini. IP estimates can be wrong. A user-confirmed city lasts for the process only. Use `BAYMAX_TOOLKIT_ENABLED=0` in your private `.env` to disable tool declarations/instructions/preloading. See [toolkit documentation](features/toolkits/README.md) for service terms and commercial deployment limitations.

`upstream/realtime_gemini_8.py` is the pinned unmodified baseline for parity checks. Tests normalize only documented hardware/toolkit/monitor hooks before comparing original processing; the base prompt, models and audio rates remain unchanged. Windows uses WASAPI shared-mode format conversion so the core keeps 48 kHz. v9 matches main, and v10 changes here only add toolkit registration/dispatch. Person/setting and memory-recall changes are scoped to PR #12. Production cloud integration and device startup are unchanged.

## Paste into a coding assistant

> Help me set up this branch of JoeKojima/Baymax on my computer. Read AGENTS.md and Baymax-main/PC_SETUP.md. Create the Python 3.12 environment, install requirements-pc.txt and the spaCy English model, run offline tests, and open devUI. Let me enter my own key locally. Preserve the Gemini model and shared conversation processing. Do not copy robot credentials or start microphone/camera capture without my request. Document platform failures and feature changes for review.

## Core and toolkit review gate

After installing the environment, run `python check_release.py --report .local-review/report.md` with its interpreter. This runs offline core and toolkit checks separately and prints a pending live checklist. Follow [TESTING_PROTOCOL.md](TESTING_PROTOCOL.md) before submitting a feature PR; an offline pass does not certify hardware or live Gemini behavior.

Start Ember verifies the saved Gemini key and visibility of the configured Live/summary models with Google before loading hardware. This sends the key to Google in a request header, without audio/video. A failed check stops startup with guidance; a pass does not guarantee Live-session quota. See [launcher instructions](../devops/clickables/README.md).

## Orange Pi and fall detection

Native Linux ARM64 automatically selects a separate dependency profile. On Debian 13, use Linux/0 - Debian Prerequisites before Setup if system libraries/build tools are missing. See [Orange Pi steps](ORANGE_PI_SETUP.md). Fall detection is an explicit experimental CPU toggle in Setup and remains unverified on Pi hardware.


## Conversational ability

Enabled by the desktop launcher by default; Setup exposes a feature toggle and
noise-reduction level. Save/restart to apply. See
[Conversational ability](features/conversational_ability/README.md). This uses the
existing capture/playback streams and does not require Memory Palace or faces.
`GOOGLE_API_KEY_FILE` optionally points to a private external UTF-8 key file; the
application reads it at startup without copying its contents into `.env` or logs.
A configured file takes precedence over `GOOGLE_API_KEY`. Keep it outside Git.
