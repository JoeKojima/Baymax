# Ember desktop development

Windows Gemini conversation, device setup and monitor were tested on the developer PC. Mac/Linux hardware and clean installs still require verification. This is a development extension of version 8; confirm the robot's actual deployed version/commit before deployment.

## Install

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

Paste your own Gemini key in devUI, select/test microphone, speaker and camera, then Save. Never copy production email credentials. Windows audio uses full WASAPI endpoint names; cameras use DirectShow names. Saved names resolve device IDs again at startup. Mac camera identity still needs testing.

## Click to run

Use the numbered launchers in `devops/clickables` at the repository root: Setup, Monitor, Start Ember, Stop Ember. `.cmd` is for Windows, `.command` for Mac and `.sh` for Linux; Unix launchers may need `chmod +x`. The environment must already be installed. These are launchers, not installers. Alternatively run `python run_pc.py` using the environment interpreter.

Monitor: http://127.0.0.1:8766/monitor. It shows live transcript fragments, compact toolkit start/end messages, reply timing within speech bubbles, and separate Listening messages. New messages appear at the bottom; scroll up to pause following. Thinking means waiting for Gemini, not access to reasoning. Transcription timing depends on Gemini delivery; it is not guaranteed to arrive one word at a time.

Stop requests graceful shutdown and existing voice analysis. Recent transcripts remain in the ignored local `.runtime-status.json` until the next run replaces it. Closing the browser does not stop Ember.

## Data and parity

The runtime sends microphone audio/camera frames to Gemini and records audio locally. The optional upstream Flask dashboard is available through `run_dashboard_pc.py`; its local accounts are separate from robot credentials. Memory retrieval and pose/fall inference remain disabled in the upstream version-8 demo.

Location/weather tools contact IPWho with the device public IP, Open-Meteo with approximate coordinates or a city query, and return results to Gemini. IP estimates can be wrong. A user-confirmed city lasts for the process only. Use `BAYMAX_TOOLKIT_ENABLED=0` in your private `.env` to disable tool declarations/instructions/preloading. See [toolkit documentation](toolkit/README.md) for service terms and commercial deployment limitations.

`upstream/realtime_gemini_8.py` is the pinned unmodified baseline for parity checks. Tests remove only documented hardware/toolkit/monitor hooks before comparing original processing. Windows uses WASAPI shared-mode format conversion so the core keeps 48 kHz. No changes are made to version 9, production cloud integration or device startup.

## Paste into a coding assistant

> Help me set up this branch of JoeKojima/Baymax on my computer. Read AGENTS.md and Baymax-main/PC_SETUP.md. Create the Python 3.12 environment, install requirements-pc.txt and the spaCy English model, run offline tests, and open devUI. Let me enter my own key locally. Preserve the Gemini model and shared conversation processing. Do not copy robot credentials or start microphone/camera capture without my request. Document platform failures and feature changes for review.
