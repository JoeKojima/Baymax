# Baymax — AI Companion Robot

A real-time AI companion and health monitoring system built on Google Gemini Live API. Baymax listens, sees, and responds with low latency, while continuously monitoring for falls and tracking voice biomarkers over time.

## Architecture

Two processes run on startup:

| Process | File | Role |
|---|---|---|
| AI Core | `realtime_gemini_10.py` | Gemini Live session, audio/video, fall detection, memory |
| Web App | `baymax_app.py` | Flask server, mobile dashboard, email alerts |

### AI Core (`realtime_gemini_10.py`)

- **Realtime speech-to-speech** via Gemini Live API — no local ASR needed
- **Video streaming** — camera feed sent to Gemini for visual context (one frame every 3 s); annotated MJPEG stream on `127.0.0.1:8080`, shown in the dashboard behind its login
- **Fall detection** — MediaPipe Pose runs in a dedicated thread, shares the camera with Gemini. A fall must start from upright and the person must stay down for 2 s (`fall_detection/detector.py`); then Gemini checks on them and the web app emails an alert. Can be switched off per robot in `device.toml`
- **Semantic memory** — on each user turn, retrieves similar past memories from ChromaDB (via `semantic_embedder.py`) and injects them as context; on session end, Gemini Flash summarises the conversation and embeds it back into ChromaDB
- **Voice biomarker analysis** — on session end, `voice_analyzer.py` analyses the recorded audio for vocal quality, prosody, lexical density, and syntactic complexity to track health trends over time
- **Session audio recording** — full session audio saved for voice analysis
- **Identity** (`ember_self.py`, `ember_identity.md`) — Ember knows it's a companion robot for older adults: its body, its priorities (safety, wellbeing, help with daily life, companionship), what it can and can't do on this robot (generated from the features running, e.g. fall detection on/off), and who it cares for from the profile their family sets in the app. Live `[SELF]` updates tell it the time, whether someone is in view, and when they last talked; profile edits reach it within a few minutes, even mid-conversation. Edit `ember_identity.md` in plain English to change how Ember sees itself
- **Face recognition** (`face_id.py`) — Ember knows who is in front of it by name ("Margaret and Sarah (Margaret's daughter) are in front of you") and, when someone new stays in view, asks who they are and whether it may remember their face. Nobody is remembered without a yes (or photos added by the family). Unsure matches are checked ("Is that you, Sarah?"); people can say "forget me". Uses OpenCV's built-in YuNet detector + SFace recognizer (MIT / Apache-2.0; downloaded once into `face_models/`, hash-checked), 2 frames/s. Only face measurements are stored — on the robot, in `ember_people.json` — never photos, and never uploaded

### Web App (`baymax_app.py`)

- Mobile-accessible dashboard served on port 5000. Every page needs a login, and the robot's data needs an account that has paired this robot
- Live conversation transcript feed
- Fall event log with timestamps
- Voice analysis dashboard — tracks metrics across sessions, shows progress toward baseline (5 sessions required)
- **People Ember recognizes** (Profile tab) — the family can add 1–10 photos of someone (after confirming they have that person's permission), see who Ember knows and how it learned them, and remove anyone (`/api/people`)
- **Profile** tab — the family enters the name of the person Ember cares for, a few words about them, and the people in their life (`/api/profile`, saved to `ember_profile.json`). When the robot is linked to the cloud (`BAYMAX_DEVICE_KEY`), the cloud's copy from `GET /api/device/profile` takes precedence
- Email notifications on falls and voice-biomarker alerts via Gmail SMTP, sent to the registered email of each account paired to this robot

## Setup

1. **Create and activate virtual environment**:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

2. **Install dependencies** from the lock file (exact versions, CPU-only PyTorch):
   ```bash
   pip install -r requirements.lock
   ```
   `requirements.txt` is the older, loose list. Regenerate the lock from a working venv with `python deploy/make_lock.py > requirements.lock`.

3. **Configure environment variables** — copy `.env.example` to `.env` and fill in:
   - `GOOGLE_API_KEY` — Gemini API key
   - `BAYMAX_EMAIL_FROM` — Gmail address alerts are sent from
   - `BAYMAX_EMAIL_PASSWORD` — Gmail app password
   - `BAYMAX_CLOUD_URL`, `BAYMAX_DEVICE_KEY` — optional, for the tadashirobotics.com uplink (`baymax_cloud.py`)

   (`.env.example` is out of date. `BAYMAX_EMAIL_TO` is no longer used: alerts go to the paired accounts.)

   Per-robot settings (serial, speaker, camera, Wi-Fi radio, fall detection on/off) live in `/etc/ember/device.toml`; see `deploy/device.example.toml`. Without it, the defaults match the original LattePanda.

4. **Download the pose landmarker model** (first run downloads automatically):
   The MediaPipe pose model (`pose_landmarker_full.task`) is downloaded on first run if not present. It is not tracked in git due to its size (~9 MB).

5. **ONNX embedding model** — the `all-MiniLM-L6-v2-onnx/` directory must be present for semantic memory. Run `convert_model.py` once to generate it if missing.

## Running

Baymax starts automatically on boot via `startup.sh` (configured as a systemd service). To run manually:

```bash
source venv/bin/activate
python3 realtime_gemini_10.py  # AI core
python3 baymax_app.py          # Web dashboard (separate terminal)
```

Press `Ctrl+C` to end a session. On exit, Baymax will:
1. Summarise the conversation and embed memories into ChromaDB
2. Run voice biomarker analysis on the session audio
3. Restart automatically (when launched via `startup.sh`)

## Tests

```bash
python3 -m unittest discover -s test -p "test_*.py"     # no hardware needed
```

`test/test_face_models.py` also runs the real face models on real photos when `EMBER_FACE_TEST_DATA` points to a folder of face photos with one sub-folder per person (e.g. the public LFW benchmark); otherwise it is skipped.

## Data Files (runtime, not tracked in git)

| File | Contents |
|---|---|
| `chroma_store/` | ChromaDB semantic memory database |
| `voice_analysis_results.json` | Per-session voice biomarker data |
| `transcript_log.json` | Rolling conversation transcript |
| `fall_log.json` | Fall detection event log |
| `day_utterance/` | Segmented audio clips for voice analysis |
| `ember_profile.json` | Who Ember cares for, as set by the family in the app |
| `ember_people.json` | Face measurements of people Ember recognizes (biometric — stays on the robot, never committed) |
| `face_models/` | Downloaded YuNet + SFace models (~39 MB) |
