"""
Baymax Mobile Web App — Fall Detection Log + Voice Analysis
Runs a Flask server accessible from any device on the local network.
Receives fall events and transcript updates from realtime_gemini_6.py via POST.
Sends email notifications on fall detection via Gmail SMTP.
"""
import json
import os
import smtplib
import threading
import time
import glob
import wave
import tempfile
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

from flask import Flask, jsonify, request, send_from_directory
from dotenv import load_dotenv

load_dotenv()

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FALL_LOG_PATH = os.path.join(SCRIPT_DIR, "fall_log.json")
TRANSCRIPT_LOG_PATH = os.path.join(SCRIPT_DIR, "transcript_log.json")
VOICE_RESULTS_PATH = os.path.join(SCRIPT_DIR, "voice_analysis_results.json")
VOICE_ALERTS_PATH = os.path.join(SCRIPT_DIR, "voice_alerts.json")
STATIC_DIR = os.path.join(SCRIPT_DIR, "static")

EMAIL_FROM = os.getenv("BAYMAX_EMAIL_FROM", "")
EMAIL_PASSWORD = os.getenv("BAYMAX_EMAIL_PASSWORD", "")
EMAIL_TO = os.getenv("BAYMAX_EMAIL_TO", "")

app = Flask(__name__, static_folder=STATIC_DIR)

_fall_log_lock = threading.Lock()
_transcript_lock = threading.Lock()
_voice_results_lock = threading.Lock()
_voice_alerts_lock = threading.Lock()

# ─── On-demand voice analysis state ─────────────────────────────────────────
_analysis_running = False
_analysis_lock = threading.Lock()
DAY_UTTERANCE_DIR = os.path.join(SCRIPT_DIR, "day_utterance")

# ─── Boot status tracking ────────────────────────────────────────────────────
_boot_status = {
    "stage": "waiting",
    "message": "Waiting for Baymax to start...",
    "ready": False,
    "stages_completed": [],
    "stages_failed": {},
}
_boot_lock = threading.Lock()


def _load_json(path):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _send_email_alert(fall_entry):
    if not all([EMAIL_FROM, EMAIL_PASSWORD, EMAIL_TO]):
        print("[EMAIL] Email not configured — skipping notification.")
        return

    def _send():
        try:
            msg = MIMEMultipart()
            msg["From"] = EMAIL_FROM
            msg["To"] = EMAIL_TO
            msg["Subject"] = "BAYMAX ALERT: Fall Detected"

            body = (
                f"A fall was detected at {fall_entry['time']} on {fall_entry['date']}.\n\n"
                f"Trunk angle: {fall_entry.get('trunk_angle', 'N/A')}\n"
                f"Angular velocity: {fall_entry.get('angular_vel', 'N/A')}\n"
                f"Hip descent velocity: {fall_entry.get('hip_descent_vel', 'N/A')}\n\n"
                "Please check on the user immediately."
            )
            msg.attach(MIMEText(body, "plain"))

            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(EMAIL_FROM, EMAIL_PASSWORD)
                server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
            print(f"[EMAIL] Fall alert sent to {EMAIL_TO}")
        except Exception as e:
            print(f"[EMAIL] Failed to send alert: {e}")

    threading.Thread(target=_send, daemon=True).start()


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/api/boot-status", methods=["GET"])
def get_boot_status():
    with _boot_lock:
        return jsonify(_boot_status)


@app.route("/api/boot-status", methods=["POST"])
def post_boot_status():
    data = request.get_json(silent=True) or {}
    with _boot_lock:
        if "stage" in data:
            _boot_status["stage"] = data["stage"]
        if "message" in data:
            _boot_status["message"] = data["message"]
        if "ready" in data:
            _boot_status["ready"] = data["ready"]
        stage = data.get("stage")
        if stage:
            if data.get("error"):
                _boot_status["stages_failed"][stage] = data.get("message", "Error")
            else:
                if stage not in _boot_status["stages_completed"]:
                    _boot_status["stages_completed"].append(stage)
                _boot_status["stages_failed"].pop(stage, None)
    print(f"[BOOT] {data.get('message', '')}")
    return jsonify({"status": "ok"}), 200


@app.route("/api/falls", methods=["GET"])
def get_falls():
    with _fall_log_lock:
        falls = _load_json(FALL_LOG_PATH)
    return jsonify(falls)


@app.route("/api/fall", methods=["POST"])
def report_fall():
    data = request.get_json(silent=True) or {}
    now = datetime.now()
    entry = {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "timestamp": now.isoformat(),
        "trunk_angle": data.get("trunk_angle"),
        "angular_vel": data.get("angular_vel"),
        "hip_descent_vel": data.get("hip_descent_vel"),
    }

    with _fall_log_lock:
        falls = _load_json(FALL_LOG_PATH)
        falls.insert(0, entry)
        _save_json(FALL_LOG_PATH, falls)

    _send_email_alert(entry)
    print(f"[FALL] Logged fall at {entry['time']}")
    return jsonify({"status": "ok"}), 201


@app.route("/api/transcript", methods=["GET"])
def get_transcript():
    with _transcript_lock:
        entries = _load_json(TRANSCRIPT_LOG_PATH)
    limit = request.args.get("limit", 50, type=int)
    return jsonify(entries[:limit])


@app.route("/api/transcript", methods=["POST"])
def post_transcript():
    data = request.get_json(silent=True) or {}
    speaker = data.get("speaker", "unknown")
    text = data.get("text", "")
    if not text.strip():
        return jsonify({"status": "empty"}), 400

    now = datetime.now()
    entry = {
        "speaker": speaker,
        "text": text.strip(),
        "time": now.strftime("%H:%M:%S"),
        "date": now.strftime("%Y-%m-%d"),
        "timestamp": now.isoformat(),
    }

    with _transcript_lock:
        entries = _load_json(TRANSCRIPT_LOG_PATH)
        entries.insert(0, entry)
        if len(entries) > 500:
            entries = entries[:500]
        _save_json(TRANSCRIPT_LOG_PATH, entries)

    return jsonify({"status": "ok"}), 201


@app.route("/api/voice-analysis", methods=["GET"])
def get_voice_analysis():
    with _voice_results_lock:
        results = _load_json(VOICE_RESULTS_PATH)
    limit = request.args.get("limit", len(results), type=int)
    return jsonify(results[-limit:] if limit < len(results) else results)


@app.route("/api/voice-analysis", methods=["POST"])
def post_voice_analysis():
    data = request.get_json(silent=True) or {}
    session = data.get("session", {})
    summary = data.get("summary", {})

    if session:
        with _voice_results_lock:
            results = _load_json(VOICE_RESULTS_PATH)
            results.append(session)
            _save_json(VOICE_RESULTS_PATH, results)

    alerts = summary.get("alerts", [])
    if alerts:
        with _voice_alerts_lock:
            existing = _load_json(VOICE_ALERTS_PATH)
            existing.extend(alerts)
            _save_json(VOICE_ALERTS_PATH, existing)
        _send_voice_alert_email(alerts)

    print(f"[VOICE] Session results saved. Alerts: {len(alerts)}")
    return jsonify({"status": "ok"}), 201


@app.route("/api/voice-alerts", methods=["GET"])
def get_voice_alerts():
    with _voice_alerts_lock:
        alerts = _load_json(VOICE_ALERTS_PATH)
    limit = request.args.get("limit", 50, type=int)
    return jsonify(alerts[-limit:] if limit < len(alerts) else alerts)


def _send_voice_alert_email(alerts):
    if not all([EMAIL_FROM, EMAIL_PASSWORD, EMAIL_TO]):
        return

    def _send():
        try:
            msg = MIMEMultipart()
            msg["From"] = EMAIL_FROM
            msg["To"] = EMAIL_TO
            msg["Subject"] = "BAYMAX ALERT: Voice Biomarker Pattern Detected"

            body = "Voice biomarker analysis has detected the following patterns:\n\n"
            for alert in alerts:
                body += f"Pattern: {alert['name']}\n"
                body += f"Matching indicators: {alert['matching_indicators']}/{alert['total_indicators']}\n"
                body += f"Severity: {alert.get('severity', 'warning')}\n"
                for detail in alert.get("details", []):
                    body += f"  - {detail}\n"
                body += "\n"
            body += (
                "This is a screening observation, not a diagnosis. "
                "Please consult a healthcare professional for evaluation."
            )
            msg.attach(MIMEText(body, "plain"))

            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(EMAIL_FROM, EMAIL_PASSWORD)
                server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
            print(f"[EMAIL] Voice alert sent to {EMAIL_TO}")
        except Exception as e:
            print(f"[EMAIL] Failed to send voice alert: {e}")

    threading.Thread(target=_send, daemon=True).start()


def _run_on_demand_analysis():
    global _analysis_running
    try:
        from voice_analyzer import VoiceAnalyzer

        # Collect completed segments — skip the most-recently-modified file
        # (it may still be open and being written by the recording thread)
        seg_files = sorted(glob.glob(os.path.join(DAY_UTTERANCE_DIR, "seg_*.wav")))
        if not seg_files:
            print("[VOICE-OD] No audio segments found.")
            return

        now = time.time()
        safe_files = [f for f in seg_files if (now - os.path.getmtime(f)) > 5]
        if not safe_files:
            print("[VOICE-OD] No completed segments available yet.")
            return

        # Merge safe segments into a temporary WAV
        merged_path = os.path.join(SCRIPT_DIR, f"od_session_{int(now)}.wav")
        try:
            VoiceAnalyzer.concatenate_wavs_list(safe_files, merged_path)
        except AttributeError:
            # Fall back: merge manually if the helper doesn't accept a list
            with wave.open(merged_path, "wb") as out_wf:
                params_set = False
                for path in safe_files:
                    try:
                        with wave.open(path, "rb") as wf:
                            if not params_set:
                                out_wf.setparams(wf.getparams())
                                params_set = True
                            out_wf.writeframes(wf.readframes(wf.getnframes()))
                    except Exception as e:
                        print(f"[VOICE-OD] Skipping {path}: {e}")
            if not params_set:
                print("[VOICE-OD] Could not read any segment files.")
                return

        # Build transcript from saved log (user utterances only)
        with _transcript_lock:
            entries = _load_json(TRANSCRIPT_LOG_PATH)
        user_lines = [e["text"] for e in reversed(entries) if e.get("speaker") == "user"]
        transcript = " ".join(user_lines)

        analyzer = VoiceAnalyzer()
        results = analyzer.analyze_session(merged_path, transcript)
        summary = analyzer.save_session_results(results)

        with _voice_results_lock:
            existing = _load_json(VOICE_RESULTS_PATH)
            existing.append(results)
            _save_json(VOICE_RESULTS_PATH, existing)

        alerts = summary.get("alerts", [])
        if alerts:
            with _voice_alerts_lock:
                existing_alerts = _load_json(VOICE_ALERTS_PATH)
                existing_alerts.extend(alerts)
                _save_json(VOICE_ALERTS_PATH, existing_alerts)
            _send_voice_alert_email(alerts)

        print(f"[VOICE-OD] Analysis complete. Sessions: {summary.get('session_count')}, "
              f"Alerts: {len(alerts)}")
    except Exception as e:
        print(f"[VOICE-OD] Analysis failed: {e}")
        import traceback
        traceback.print_exc()
    finally:
        try:
            os.remove(merged_path)
        except Exception:
            pass
        with _analysis_lock:
            _analysis_running = False


@app.route("/api/analyze-voice", methods=["POST"])
def trigger_voice_analysis():
    global _analysis_running
    with _analysis_lock:
        if _analysis_running:
            return jsonify({"status": "running", "message": "Analysis already in progress"}), 409
        if not os.path.isdir(DAY_UTTERANCE_DIR) or not glob.glob(
            os.path.join(DAY_UTTERANCE_DIR, "seg_*.wav")
        ):
            return jsonify({"status": "error", "message": "No audio recordings found yet"}), 400
        _analysis_running = True

    threading.Thread(target=_run_on_demand_analysis, daemon=True).start()
    return jsonify({"status": "started"}), 202


@app.route("/api/analyze-voice/status", methods=["GET"])
def voice_analysis_status():
    with _analysis_lock:
        running = _analysis_running
    return jsonify({"running": running})


if __name__ == "__main__":
    os.makedirs(STATIC_DIR, exist_ok=True)
    print(f"[APP] Fall log: {FALL_LOG_PATH}")
    print(f"[APP] Voice analysis: {VOICE_RESULTS_PATH}")
    print(f"[APP] Email alerts: {'ENABLED' if all([EMAIL_FROM, EMAIL_PASSWORD, EMAIL_TO]) else 'DISABLED'}")
    app.run(host="0.0.0.0", port=5000, debug=False)
