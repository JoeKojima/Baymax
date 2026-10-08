# Baymax — AI Companion Robot

A real-time AI companion and health monitoring system built on Google Gemini Live API. Baymax listens, sees, and responds with low latency, while continuously monitoring for falls and tracking voice biomarkers over time.

## Architecture

Two processes run on startup:

| Process | File | Role |
|---|---|---|
| AI Core | `realtime_gemini_8.py` | Gemini Live session, audio/video, fall detection, memory |
| Web App | `baymax_app.py` | Flask server, mobile dashboard, email alerts |

### AI Core (`realtime_gemini_8.py`)

- **Realtime speech-to-speech** via Gemini Live API — no local ASR needed
- **Video streaming** — camera feed sent to Gemini for visual context; MJPEG stream served on port 8080
- **Fall detection** — MediaPipe Pose runs in a dedicated thread, shares the camera with Gemini; alerts Gemini via the live session on detection
- **Semantic memory** — on each user turn, retrieves similar past memories from ChromaDB (via `semantic_embedder.py`) and injects them as context; on session end, Gemini Flash summarises the conversation and embeds it back into ChromaDB
- **Voice biomarker analysis** — on session end, `voice_analyzer.py` analyses the recorded audio for vocal quality, prosody, lexical density, and syntactic complexity to track health trends over time
- **Session audio recording** — full session audio saved for voice analysis

### Web App (`baymax_app.py`)

- Mobile-accessible dashboard served on port 5000
- Live conversation transcript feed
- Fall event log with timestamps
- Voice analysis dashboard — tracks metrics across sessions, shows progress toward baseline (5 sessions required)
- Email notifications on fall detection via Gmail SMTP

## Setup

1. **Create and activate virtual environment**:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure environment variables** — copy `.env.example` to `.env` and fill in:
   - `GOOGLE_API_KEY` — Gemini API key
   - `BAYMAX_EMAIL_FROM` — Gmail address for fall alerts
   - `BAYMAX_EMAIL_PASSWORD` — Gmail app password
   - `BAYMAX_EMAIL_TO` — recipient address for fall alerts

4. **Download the pose landmarker model** (first run downloads automatically):
   The MediaPipe pose model (`pose_landmarker_full.task`) is downloaded on first run if not present. It is not tracked in git due to its size (~9 MB).

5. **ONNX embedding model** — the `all-MiniLM-L6-v2-onnx/` directory must be present for semantic memory. Run `convert_model.py` once to generate it if missing.

## Running

Baymax starts automatically on boot via `startup.sh` (configured as a systemd service). To run manually:

```bash
source venv/bin/activate
python3 realtime_gemini_8.py   # AI core
python3 baymax_app.py          # Web dashboard (separate terminal)
```

Press `Ctrl+C` to end a session. On exit, Baymax will:
1. Summarise the conversation and embed memories into ChromaDB
2. Run voice biomarker analysis on the session audio
3. Restart automatically (when launched via `startup.sh`)

## Data Files (runtime, not tracked in git)

| File | Contents |
|---|---|
| `chroma_store/` | ChromaDB semantic memory database |
| `voice_analysis_results.json` | Per-session voice biomarker data |
| `transcript_log.json` | Rolling conversation transcript |
| `fall_log.json` | Fall detection event log |
| `day_utterance/` | Segmented audio clips for voice analysis |
