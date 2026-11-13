"""Settings changed from the Ember app: microphone, camera and speaker volume.

The dashboard is the only place they change (user decision 2026-11-10). The app
link (features/toolkits/family/link.py) reads them from the inbox and calls
`settings.apply`; the result goes back in every sync so the dashboard can show
what Ember is actually doing.

- Microphone off: the capture callback drops every block before anything else
  sees it, so nothing reaches Gemini, the recorder, voice analysis or the wake
  words. Ember can still speak (messages, reminders) but can't hear answers.
- Camera off: the camera thread releases the camera and closes the pose model,
  so there is no fall detection, no MJPEG stream and no video to Gemini.
- Volume: the whole speaker (PipeWire default sink), 10-100 %.
- Daily conversation reports (features/toolkits/family/reports.py): `reports`
  off means nothing about conversations is kept for or sent to the family;
  `report_flags` off keeps the daily summary but drops concern flags and urgent
  alerts. `report_time` ("HH:MM", Ember's local time) is when the day's report
  is written; activity recognition reads the same time.

Saved in device_settings.json, so a camera or microphone switched off stays off
across restarts, before the cloud is reachable. When someone turns the camera
or microphone off or on, Ember says so out loud.
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

# Next to the code, or BAYMAX_DATA_DIR (Docker units, the test rig), like the other runtime data.
PATH = Path(os.getenv("BAYMAX_DATA_DIR") or Path(__file__).resolve().parents[1]) / "device_settings.json"
MIN_VOLUME, MAX_VOLUME = 10, 100   # never fully silent: alerts and check-ins must be heard
DEFAULT_REPORT_TIME = "20:00"
TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

ANNOUNCE = {
    ("camera", False): "{who} turned your camera off from the Ember app. You can't see now, and fall detection is off until it's turned back on in the app.",
    ("camera", True): "{who} turned your camera back on from the Ember app. You can see again and fall detection is back on.",
    ("mic", False): "{who} turned your microphone off from the Ember app. You can't hear the person until it's turned back on in the app.",
    ("mic", True): "{who} turned your microphone back on from the Ember app. You can hear the person again.",
}
SESSION_NOTES = {
    "camera": "[CAMERA OFF] Your camera is switched off from the Ember app: you can't see, and fall detection is off. "
              "If the person asks you to look at something, explain that and that the app can turn it back on.",
    "mic": "[MICROPHONE OFF] Your microphone is switched off from the Ember app: you can't hear the person. "
           "You may still pass on messages and reminders, but don't ask questions or wait for answers.",
}


def set_speaker_volume(percent):
    """Whole-speaker volume through PipeWire. True if a tool accepted it."""
    commands = []
    if shutil.which("pactl"):
        commands.append(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{percent}%"])
    if shutil.which("wpctl"):
        commands.append(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{percent / 100:.2f}"])
    for command in commands:
        try:
            if subprocess.run(command, capture_output=True, timeout=5).returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


class Settings:
    def __init__(self, path=PATH, volume_setter=set_speaker_volume):
        self.path = Path(path)
        self.set_volume = volume_setter
        self.lock = threading.Lock()
        self.state = {"rev": 0, "mic": True, "camera": True, "volume": None, "volume_ok": None,
                      "reports": True, "report_flags": True, "report_time": DEFAULT_REPORT_TIME}
        try:
            saved = json.loads(self.path.read_text())
            self.state.update({k: saved[k] for k in self.state if k in saved})
        except (OSError, ValueError, TypeError):
            pass
        self.camera_wanted = threading.Event()
        if self.state["camera"]:
            self.camera_wanted.set()
        self.announcements = []   # (key, on, who) for the live session to say

    def mic_on(self):
        return self.state["mic"]

    def camera_on(self):
        return self.state["camera"]

    def reports_on(self):
        return self.state["reports"]

    def report_flags_on(self):
        return self.state["reports"] and self.state["report_flags"]

    def report_time(self):
        """(hour, minute) of the daily report, Ember's local time."""
        hour, minute = self.state["report_time"].split(":")
        return int(hour), int(minute)

    def wait_for_camera(self, stop_event, poll=0.5):
        """Block until the camera is wanted (True) or stop_event is set (False)."""
        while not stop_event.is_set():
            if self.camera_wanted.wait(poll):
                return True
        return False

    def start(self):
        """At boot: put the saved volume back (startup.sh resets the speaker)."""
        if self.state["volume"] is not None:
            self._volume(self.state["volume"])
        off = [name for name in ("mic", "camera") if not self.state[name]]
        if off:
            print(f"[SETTINGS] Off from the Ember app: {', '.join(off)}.", flush=True)

    def _volume(self, percent):
        ok = bool(self.set_volume(percent))
        self.state["volume_ok"] = ok
        if not ok:
            print("[SETTINGS] Could not set the speaker volume (no pactl/wpctl?).", flush=True)
        return ok

    def apply(self, remote):
        """Apply the dashboard's settings if they are newer. Returns what changed."""
        if not isinstance(remote, dict):
            return []
        try:
            rev = int(remote.get("rev") or 0)
        except (TypeError, ValueError):
            return []
        with self.lock:
            if rev <= self.state["rev"]:
                return []
            who = str(remote.get("changed_by") or "Someone")[:60]
            changed = []
            for key in ("mic", "camera"):
                if isinstance(remote.get(key), bool) and remote[key] != self.state[key]:
                    self.state[key] = remote[key]
                    changed.append(key)
                    self.announcements.append((key, remote[key], who))
            for key in ("reports", "report_flags"):
                if isinstance(remote.get(key), bool) and remote[key] != self.state[key]:
                    self.state[key] = remote[key]
                    changed.append(key)
            report_time = remote.get("report_time")
            if isinstance(report_time, str) and TIME_PATTERN.match(report_time) and report_time != self.state["report_time"]:
                self.state["report_time"] = report_time
                changed.append("report_time")
            volume = remote.get("volume")
            if isinstance(volume, (int, float)) and not isinstance(volume, bool):
                volume = int(min(max(volume, MIN_VOLUME), MAX_VOLUME))
                if volume != self.state["volume"] or not self.state["volume_ok"]:
                    self.state["volume"] = volume
                    self._volume(volume)
                    changed.append("volume")
            self.state["rev"] = rev
            if self.state["camera"]:
                self.camera_wanted.set()
            else:
                self.camera_wanted.clear()
            self._save()
        if changed:
            print(f"[SETTINGS] {who} changed {', '.join(changed)}: mic {'on' if self.state['mic'] else 'OFF'}, "
                  f"camera {'on' if self.state['camera'] else 'OFF'}, volume {self.state['volume']}.", flush=True)
        return changed

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state))
        os.replace(tmp, self.path)

    def report(self):
        """What Ember is doing now, for the sync."""
        with self.lock:
            return dict(self.state)

    def take_announcements(self):
        with self.lock:
            items, self.announcements = self.announcements, []
        return items


settings = Settings()


def _can_speak():
    try:
        from features.toolkits.everyday.tools import available_to_speak
        return available_to_speak()
    except Exception:
        return True


async def session_task(session, shutdown_event, can_speak=_can_speak):
    """Tell Gemini what is off when the session starts, and say changes out loud."""
    off = [SESSION_NOTES[key] for key in ("camera", "mic") if not getattr(settings, f"{key}_on")()]
    if off:
        try:
            await session.send_client_content(turns={"role": "user", "parts": [{"text": " ".join(off)}]},
                                              turn_complete=False)
        except Exception:
            pass
    while not shutdown_event.is_set():
        await asyncio.sleep(1)
        if not settings.announcements or not can_speak():
            continue
        items = settings.take_announcements()
        # Only the latest state of each switch matters (off then on within a minute says "on").
        latest = {key: (on, who) for key, on, who in items}
        lines = [ANNOUNCE[(key, on)].format(who=who) for key, (on, who) in latest.items()]
        try:
            from features.conversational_ability.engage import engagement
            engagement.engage("settings changed")
        except Exception:
            pass
        try:
            await session.send_client_content(turns={"role": "user", "parts": [{"text":
                "[SETTINGS] " + " ".join(lines) + " Tell the person now in one short, calm sentence."}]},
                turn_complete=True)
        except Exception:
            with settings.lock:
                settings.announcements[:0] = items   # try again next second


def register():
    from features.toolkits.fall_detection import session_tasks
    if session_task not in session_tasks:
        session_tasks.append(session_task)


register()
