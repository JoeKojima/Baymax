# ember: the runtime core, as testable parts

This package runs the version-8 conversation runtime: the same behaviour as `realtime_gemini_8.py`, organised so it can be tested and extended. The design and the reasons are in `docs/architecture/WHY.md`, the rollout in `docs/architecture/MIGRATION.md`.

## Inputs
- **Microphone:** 48 kHz int16, 1024-sample blocks, mono or stereo (downmixed).
- **Camera:** 640×480 at 15 fps.
- **Settings:** env and `.env` (`GOOGLE_API_KEY`, `BAYMAX_HARDWARE_PROFILE`, `BAYMAX_AUDIO_RETENTION_DAYS`, `BAYMAX_VOICE_ANALYSIS`). Features read their own variables, as before.
- **Gemini Live:** server messages.

## Outputs
- **Speaker:** 48 kHz int16 mono (Gemini's 24 kHz upsampled ×2), mixed with feature sources.
- **To Gemini:** mic audio and a 320×240 JPEG every 3 s.
- **Monitor:** `.runtime-status.json` via `runtime_monitor`, and `ember.bus` events.
- **Dashboard:** POSTs to `localhost:5000`.
- **Preview:** MJPEG on port 8080.
- **Recording:** raw mic audio in `day_utterance/` (pruned by retention).
- **Voice analysis:** post-session, unless disabled.

## External data flows
Unchanged from v8:
- Mic audio and camera frames go to Google Gemini Live.
- Dashboard posts stay on localhost.
- Nothing else leaves the device.

`--devices virtual` sends synthetic audio and frames to Gemini instead of real ones.

## Enabling and disabling
- **`run_pc.py` / `run_mvp.py`:** use this package by default (`BAYMAX_CORE=ember`). `BAYMAX_CORE=legacy` runs `realtime_gemini_8.py` unchanged (the rollback).
- **Directly:** `python -m ember --profile v8|mvp`.
- Profiles `v9` and `v10` run their legacy files.

## Dependencies
The desktop and robot requirements already installed: `google-genai`, `numpy`, `opencv`, `sounddevice`, `python-dotenv` and `requests`, plus the features' own.

## Tests (offline, core section)
- **`tests/test_ember_parity.py`:** the new core against the unchanged v8 on virtual devices with the real audio cleanup.
  - Config: 7 setting combinations.
  - Speaker: sample for sample, with and without the waiting cue.
  - Mic path and recording.
  - Send-path drop rules.
  - Receive loop: events, playback, transcripts, tools, dashboard posts.
  - Video: frames sent, camera thread.
  - Files on disk.
  - Whole sessions with a 1011/1006 drop and a reconnect.
- **`tests/test_ember_plugins.py`:** registry order and problems, config steps, bus, profiles, the CLI, and `BAYMAX_CORE` selection.
- **`tests/test_ember_virtual.py`:** virtual devices, the fake Gemini and the legacy loader. The loader blocks real cameras.
