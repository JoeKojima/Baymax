"""
Baymax on-device job agent.

Cloudflare Workers cannot run voice_analyzer.py — it needs numpy and the raw
WAV segments in day_utterance/, and a full merge-plus-analyze pass takes far
longer than a Worker invocation may live. So the "Analyze Voice Now" button in
the dashboard only records a request; this agent is what actually does the work.

    dashboard  ──POST /api/devices/:serial/analyze──►  job row (pending)
    this agent ──GET  /api/device/jobs────────────►  claims it (running)
               ── runs VoiceAnalyzer locally ──
               ──POST /api/device/voice-analysis──►  results + job_id (done)

This is a direct port of _run_on_demand_analysis() from baymax_app.py.

Run alongside realtime_gemini_9.py:
    python3 baymax_agent.py
"""
import glob
import os
import time
import wave

import requests

import baymax_cloud
from baymax_cloud import CLOUD_URL, DEVICE_KEY

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DAY_UTTERANCE_DIR = os.path.join(SCRIPT_DIR, "day_utterance")
TRANSCRIPT_LOG_PATH = os.path.join(SCRIPT_DIR, "transcript_log.json")

POLL_INTERVAL = 15          # seconds between job checks
SEGMENT_SETTLE_SECONDS = 5  # skip files the recorder may still have open


def _collect_segments():
    """
    Completed segments only. The most recently modified file may still be open
    and being written by the recording thread, which would truncate the merge.
    """
    seg_files = sorted(glob.glob(os.path.join(DAY_UTTERANCE_DIR, "seg_*.wav")))
    if not seg_files:
        return []

    now = time.time()
    return [f for f in seg_files if (now - os.path.getmtime(f)) > SEGMENT_SETTLE_SECONDS]


def _merge_segments(paths, out_path):
    """@return True if at least one segment was written."""
    from voice_analyzer import VoiceAnalyzer

    try:
        VoiceAnalyzer.concatenate_wavs_list(paths, out_path)
        return True
    except AttributeError:
        pass  # older analyzer without the list helper — merge manually

    params_set = False
    with wave.open(out_path, "wb") as out_wf:
        for path in paths:
            try:
                with wave.open(path, "rb") as wf:
                    if not params_set:
                        out_wf.setparams(wf.getparams())
                        params_set = True
                    out_wf.writeframes(wf.readframes(wf.getnframes()))
            except Exception as e:
                print(f"[AGENT] Skipping {path}: {e}")

    return params_set


def _build_transcript():
    """User utterances only, oldest first — matches the original behaviour."""
    import json

    try:
        with open(TRANSCRIPT_LOG_PATH) as f:
            entries = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return ""

    lines = [e["text"] for e in reversed(entries) if e.get("speaker") == "user"]
    return " ".join(lines)


def _run_analysis(job_id):
    from voice_analyzer import VoiceAnalyzer

    merged_path = None
    try:
        safe_files = _collect_segments()
        if not safe_files:
            _report_failure(job_id, "No completed audio segments available yet.")
            return

        merged_path = os.path.join(SCRIPT_DIR, f"od_session_{int(time.time())}.wav")
        if not _merge_segments(safe_files, merged_path):
            _report_failure(job_id, "Could not read any segment files.")
            return

        analyzer = VoiceAnalyzer()
        results = analyzer.analyze_session(merged_path, _build_transcript())
        summary = analyzer.save_session_results(results)

        baymax_cloud.notify(
            "/api/voice-analysis",
            {"session": results, "summary": summary, "job_id": job_id},
        )

        alerts = summary.get("alerts", [])
        print(
            f"[AGENT] Analysis complete. Sessions: {summary.get('session_count')}, "
            f"Alerts: {len(alerts)}"
        )

    except Exception as e:
        import traceback

        traceback.print_exc()
        _report_failure(job_id, f"Analysis failed: {e}")

    finally:
        if merged_path:
            try:
                os.remove(merged_path)
            except OSError:
                pass


def _report_failure(job_id, message):
    print(f"[AGENT] {message}")
    # Posting with job_id and no session closes the job out as errored so the
    # dashboard button does not stay disabled.
    baymax_cloud.notify("/api/voice-analysis", {"job_id": job_id, "error": message})


def _poll_jobs():
    try:
        res = requests.get(
            f"{CLOUD_URL}/api/device/jobs",
            headers={"Authorization": f"Bearer {DEVICE_KEY}"},
            timeout=10,
        )
    except requests.RequestException:
        return []  # offline; the uplink queue handles the rest

    if res.status_code != 200:
        if res.status_code in (401, 403):
            print(f"[AGENT] Device rejected: HTTP {res.status_code}. Check BAYMAX_DEVICE_KEY.")
        return []

    try:
        return res.json().get("jobs", [])
    except ValueError:
        return []


def main():
    if not DEVICE_KEY:
        print("[AGENT] BAYMAX_DEVICE_KEY is not set — nothing to do.")
        return

    baymax_cloud.start()
    print(f"[AGENT] Polling {CLOUD_URL} for analysis jobs every {POLL_INTERVAL}s")

    while True:
        for job in _poll_jobs():
            if job.get("type") == "voice_analysis":
                print(f"[AGENT] Claimed job {job['id']}")
                _run_analysis(job["id"])
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
