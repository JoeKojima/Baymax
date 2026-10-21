#!/usr/bin/env python3
"""
Ember fleet agent. Checks in with the fleet console (tadashirobotics.com/admin)
once a minute: it reports this unit's health and carries out what the console
sends back (restart, update now, roll back, new settings, and the release its
channel runs). It runs as root on the robot itself (ember-agent.service), not
in the Ember image, so it keeps working while Ember is stopped or updating.

The check-in contract is in the Company-Website repo, src/worker/routes/fleet.js.

    sudo systemctl status ember-agent          # running?
    sudo journalctl -u ember-agent -n 50       # what it did
    sudo cat /var/lib/ember-agent/state.json   # pending results and settings

Settings from the console are applied at once (Ember restarts). If the unit
can't reach the console within 5 minutes after that, the agent puts the
previous device.toml back and restarts Ember again.

Releases: the agent writes the console's choice for this unit's channel to
<state>/target, and ember-update.sh installs it in the nightly window (or at
once for "Update now"). The channel itself is copied into device.toml.

The agent's key lives in /etc/ember/fleet.env, which only root can read and
the containers never see. Giving a unit a fleet key therefore doesn't change
where the AI core sends patient data: that is BAYMAX_DEVICE_KEY in
secrets.env, a separate decision.

Python standard library only (Debian 13's python3). Paths can be changed with
environment variables; unit 001 (venv runtime) sets them in a drop-in.

    python3 ember-agent.py --self-test   # builds one report offline; ember-update.sh
                                         # runs this on a new release's agent before installing it
"""
import hashlib
import http.client
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, timezone

AGENT_VERSION = "1"

EMBER_HOME = os.getenv("EMBER_HOME", "/opt/ember")              # ember-update.sh, REVISION, device_config.py
KEY_FILE = os.getenv("EMBER_FLEET_ENV", "/etc/ember/fleet.env")   # EMBER_FLEET_KEY, optional EMBER_CLOUD_URL
CONFIG = os.getenv("EMBER_DEVICE_CONFIG", "/etc/ember/device.toml")
STATE_DIR = os.getenv("EMBER_AGENT_STATE", "/var/lib/ember-agent")
LOG_DIR = os.getenv("EMBER_LOG_DIR", "/var/log/ember")            # startup_boot.log, update.log
APP_DIR = os.getenv("APP_DIR", "")                                # venv runtime: the git checkout
DEFAULT_CLOUD = "https://tadashirobotics.com"
# The only image a unit runs, whatever the console says: a compromised or
# mistaken console can't make units run (as root) an image from elsewhere.
RELEASE_IMAGE = os.getenv("EMBER_RELEASE_IMAGE", "ghcr.io/joekojima/baymax")
RELEASE_TAG = re.compile(r"sha-[0-9a-f]{7,40}")
# Settings the console may not change: they decide whether Ember can start at
# all, and a unit that can't start Ember may also refuse the fix.
LOCKED_SETTINGS = ("device.serial", "system.runtime", "system.user", "update.image")

DEFAULT_INTERVAL_S = 60
SETTINGS_CONFIRM_S = 120   # a check-in this long after the settings restart confirms the new file
SETTINGS_REVERT_S = 300    # no check-in this long after it: put the previous file back
MAX_SETTINGS_BYTES = 32 * 1024
READY_PATTERN = "System ready"   # printed by the AI core once Gemini is connected (as in ember-update.sh)
CORE_PROCESS = "python3 realtime_gemini_"  # venv runtime: the AI core process (not the bash -c that starts it)

STATE_FILE = os.path.join(STATE_DIR, "state.json")
TARGET_FILE = os.path.join(STATE_DIR, "target")             # "image:tag", read by ember-update.sh
LAST_CHECKIN_FILE = os.path.join(STATE_DIR, "last-checkin")  # its age tells ember-update.sh the agent works
LAST_UPDATE_FILE = os.path.join(STATE_DIR, "last-update.json")  # written by ember-update.sh

sys.path.insert(0, EMBER_HOME)
import device_config  # noqa: E402  (from EMBER_HOME)


def log(message):
    print(message, flush=True)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_atomic(path, text, mode=0o644):
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def run(*cmd, timeout=60):
    """@return (exit code, stdout+stderr). A missing program counts as exit 127."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, f"{' '.join(cmd)} timed out"


def cfg(key):
    """A device.toml value, read fresh (the file changes under us)."""
    device_config._config = None
    return device_config.get(key)


def read_env_file(path):
    values = {}
    try:
        with open(path) as f:
            for line in f:
                m = re.match(r"\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$", line)
                if m:
                    values[m.group(1)] = m.group(2).strip("'\"")
    except OSError:
        pass
    return values


# ── State (shared by the check-in loop and the command worker) ──────────

class State:
    """state.json: what the console still has to hear, and settings awaiting confirmation."""

    def __init__(self):
        self.lock = threading.RLock()
        self.data = {
            "settings": {"version": 0, "state": None, "message": None},  # last outcome, as reported
            "pending_settings": None,  # {"version", "applied_at", "backup"} until confirmed or reverted
            "results": [],             # command results not yet delivered
            "running": None,           # command in progress {"id", "kind"}
            "seen": [],                # recent command ids, so none runs twice
        }
        try:
            with open(STATE_FILE) as f:
                self.data.update(json.load(f))
        except (OSError, ValueError):
            pass

    def save(self):
        with self.lock:
            write_atomic(STATE_FILE, json.dumps(self.data, indent=1), 0o600)

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        with self.lock:
            self.data[key] = value
            self.save()

    def add_result(self, command_id, ok, message):
        with self.lock:
            self.data["results"].append({"id": command_id, "status": "done" if ok else "failed",
                                         "message": (message or "")[:500]})
            self.data["running"] = None
            self.save()


# ── What the unit reports ──────────────────────────────────────────────

_ready_seen = {}  # core container id+start -> True once READY_PATTERN was in its log


def service_active(name):
    return run("systemctl", "is-active", "--quiet", name)[0] == 0


def startup_waiting_for_internet():
    """True if startup.sh's last word was that there is no internet."""
    path = os.path.join(LOG_DIR, "startup_boot.log")
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 4096))
            lines = f.read().decode(errors="replace").strip().splitlines()
    except OSError:
        return False
    return bool(lines) and ("no internet" in lines[-1].lower())


def ember_state(runtime):
    if runtime != "docker":
        if not service_active("baymax.service"):
            return "stopped"
        if run("pgrep", "-f", CORE_PROCESS)[0] == 0:
            return "running"
        return "waiting_for_internet" if startup_waiting_for_internet() else "starting"

    if not service_active("ember.service"):
        return "stopped"
    code, out = run("docker", "ps", "-q", "--filter", "label=com.docker.compose.project=ember",
                    "--filter", "label=com.docker.compose.service=core")
    core = out.split()[0] if code == 0 and out else ""
    if not core:
        return "waiting_for_internet" if startup_waiting_for_internet() else "starting"
    code, started = run("docker", "inspect", "-f", "{{.State.StartedAt}}", core)
    key = f"{core}@{started}"
    if key not in _ready_seen:
        code, logs = run("docker", "logs", "--since", started, core, timeout=30)
        if READY_PATTERN in logs:
            _ready_seen.clear()
            _ready_seen[key] = True
    return "running" if key in _ready_seen else "starting"


def image_info():
    """@return {"tag", "id"} of ember:current. The tag is the release name (sha-1a2b3c4)."""
    code, out = run("docker", "image", "inspect", "ember:current", "--format",
                    '{{.Id}}|{{index .Config.Labels "org.opencontainers.image.revision"}}|{{join .RepoTags ","}}')
    if code != 0:
        return None
    image_id, revision, tags = (out.split("|") + ["", ""])[:3]
    release = next((t.rsplit(":", 1)[1] for t in tags.split(",") if re.search(r":sha-[0-9a-f]{7}$", t)), None)
    if not release and re.fullmatch(r"[0-9a-f]{7,40}", revision):
        release = f"sha-{revision[:7]}"
    if not release:
        release = next((t.rsplit(":", 1)[1] for t in tags.split(",") if t and not t.startswith("ember:")), None)
    return {"tag": release, "id": image_id}


def host_revision(runtime):
    if runtime == "docker":
        try:
            with open(os.path.join(EMBER_HOME, "REVISION")) as f:
                return f.read().strip() or None
        except OSError:
            return None
    if APP_DIR:
        code, out = run("git", "-c", "safe.directory=*", "-C", APP_DIR, "rev-parse", "--short", "HEAD")
        return out if code == 0 else None
    return None


def build_report(state, runtime):
    report = {
        "agent_version": AGENT_VERSION,
        "runtime": runtime,
        "hostname": socket.gethostname(),
        "revision": host_revision(runtime),
        "ember": ember_state(runtime),
    }
    usage = shutil.disk_usage("/")
    report["disk"] = {"free": usage.free, "total": usage.total}
    try:
        with open("/proc/uptime") as f:
            report["uptime"] = int(float(f.read().split()[0]))
    except OSError:
        pass
    if runtime == "docker":
        image = image_info()
        if image:
            report["image"] = image
        try:
            with open(LAST_UPDATE_FILE) as f:
                report["last_update"] = json.load(f)
        except (OSError, ValueError):
            pass
    with state.lock:
        settings = dict(state["settings"])
        report["commands"] = list(state["results"])
    try:
        with open(CONFIG) as f:
            toml_text = f.read()
        if len(toml_text.encode()) <= MAX_SETTINGS_BYTES:
            settings["toml"] = toml_text
    except OSError:
        pass
    report["settings"] = settings
    return report


# ── Restarting Ember ───────────────────────────────────────────────────

def restart_ember(runtime):
    """@return (ok, message). The AI core gets SIGINT first, so it saves its session."""
    if runtime == "docker":
        # ember.service stops the containers with SIGINT (ExecStopPost), then
        # startup.sh runs again: audio levels, hotspot, containers once online.
        code, out = run("systemctl", "restart", "ember.service", timeout=300)
        return code == 0, "Ember restarted" if code == 0 else f"restart failed: {out[-300:]}"
    # venv (unit 001): the core's restart loop would keep it running, so
    # interrupt it, give it time to save, then restart the whole service.
    if run("pkill", "-INT", "-f", CORE_PROCESS)[0] == 0:
        for _ in range(60):
            if run("pgrep", "-f", CORE_PROCESS)[0] != 0:
                break
            time.sleep(2)
    code, out = run("systemctl", "restart", "baymax.service", timeout=300)
    return code == 0, "Ember restarted" if code == 0 else f"restart failed: {out[-300:]}"


# ── Settings ───────────────────────────────────────────────────────────

def set_channel(text, channel):
    """@return the device.toml text with [update] channel set (added if missing)."""
    lines = text.splitlines(keepends=True)
    section, insert_at = None, None
    for i, line in enumerate(lines):
        m = re.match(r"\s*\[([^\]]+)\]", line)
        if m:
            if section == "update":
                insert_at = i
            section = m.group(1).strip()
            continue
        if section == "update" and re.match(r"\s*channel\s*=", line):
            lines[i] = f'channel = "{channel}"\n'
            return "".join(lines)
    if section == "update":
        insert_at = len(lines)
    if insert_at is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        return "".join(lines) + f'\n[update]\nchannel = "{channel}"\n'
    # Keep the new line next to the section's last key, before blank lines.
    while insert_at > 0 and not lines[insert_at - 1].strip():
        insert_at -= 1
    if insert_at and not lines[insert_at - 1].endswith("\n"):
        lines[insert_at - 1] += "\n"
    lines.insert(insert_at, f'channel = "{channel}"\n')
    return "".join(lines)


def check_settings(text):
    """@return why this device.toml can't be used, or None if it can."""
    try:
        new = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return f"not valid TOML: {e}"
    for section, values in new.items():
        if not isinstance(values, dict):
            return f"{section} must be a [section], not a single value"
    for key in LOCKED_SETTINGS:
        section, name = key.split(".")
        # What the unit would actually use: a line left out means the
        # built-in default, which may differ from the current value.
        effective = new.get(section, {}).get(name, device_config.DEFAULTS[section][name])
        if effective != cfg(key):
            return f'{key} must stay "{cfg(key)}" (it can\'t be changed remotely; keep the line in the file)'
    return None


def apply_settings(state, version, text, channel, runtime):
    problem = check_settings(text)
    if problem:
        log(f"settings v{version} rejected: {problem}")
        state["settings"] = {"version": version, "state": "rejected", "message": problem}
        return
    if channel:
        text = set_channel(text, channel)
    with state.lock:
        pending = state["pending_settings"]
        # While an earlier change is unconfirmed, keep the last confirmed file
        # as the one to go back to.
        backup = pending["backup"] if pending else os.path.join(STATE_DIR, "device.toml.confirmed")
        if not pending:
            shutil.copy2(CONFIG, backup)
        write_atomic(CONFIG, text, 0o644)
        state["pending_settings"] = {"version": version, "applied_at": None, "backup": backup}
    log(f"settings v{version} written; restarting Ember")
    ok, message = restart_ember(runtime)
    with state.lock:
        state["pending_settings"] = {"version": version, "applied_at": time.time(), "backup": backup}
    if not ok:
        log(f"settings v{version}: {message}")


def confirm_or_revert_settings(state, worker, checked_in, runtime):
    """After a settings restart: a check-in confirms the new file; none for 5 minutes reverts it."""
    with state.lock:
        pending = state["pending_settings"]
        if not pending or pending["applied_at"] is None:
            return
        age = time.time() - pending["applied_at"]
        if checked_in and age >= SETTINGS_CONFIRM_S:
            state["settings"] = {"version": pending["version"], "state": "applied", "message": None}
            state["pending_settings"] = None
            log(f"settings v{pending['version']} confirmed")
            try:
                os.remove(pending["backup"])
            except OSError:
                pass
            return
        if checked_in or age < SETTINGS_REVERT_S:
            return
        shutil.copy2(pending["backup"], CONFIG)
        os.remove(pending["backup"])
        message = f"No contact with the console for {SETTINGS_REVERT_S // 60} minutes after applying it"
        state["settings"] = {"version": pending["version"], "state": "reverted", "message": message}
        state["pending_settings"] = None
    log(f"settings v{pending['version']} reverted: {message}")
    worker.add("revert", restart_ember, runtime)


# ── Commands ───────────────────────────────────────────────────────────

def update_last_line():
    """ember-update.sh ends every run with a line saying what happened, kept in <state>/last-run."""
    try:
        with open(os.path.join(STATE_DIR, "last-run")) as f:
            return f.read().strip()
    except OSError:
        return ""


def run_update_script(mode):
    """ember-update.sh in its own transient unit, so restarting the agent can't cut it off."""
    script = os.path.join(EMBER_HOME, "ember-update.sh")
    try:
        os.remove(os.path.join(STATE_DIR, "last-run"))   # so a stale line is never reported
    except OSError:
        pass
    code, out = run("systemd-run", f"--unit=ember-{mode.strip('-')}-{int(time.time())}", "--wait", "--quiet",
                    "--collect", "-p", "RuntimeMaxSec=45min", script, mode, timeout=50 * 60)
    message = update_last_line() or out[-300:]
    return code == 0, message


def run_command(state, command, runtime):
    kind = command["kind"]
    log(f"command {kind} ({command['id']})")
    state["running"] = {"id": command["id"], "kind": kind}
    if kind == "restart":
        ok, message = restart_ember(runtime)
    elif kind in ("update_now", "rollback") and runtime != "docker":
        ok, message = False, "This unit runs from a git checkout; it can't be updated remotely"
    elif kind == "update_now":
        ok, message = run_update_script("--now")
    elif kind == "rollback":
        ok, message = run_update_script("--rollback")
    else:
        ok, message = False, f"unknown command {kind}"
    log(f"command {kind}: {'done' if ok else 'failed'}: {message}")
    state.add_result(command["id"], ok, message)


class Worker(threading.Thread):
    """Runs commands and settings changes one at a time, off the check-in loop."""

    def __init__(self, state):
        super().__init__(daemon=True)
        self.state = state
        self.jobs = queue.Queue()
        self.queued = set()   # settings versions / command ids waiting or running

    def busy(self):
        return bool(self.queued)

    def add(self, key, fn, *args):
        if key in self.queued:
            return
        self.queued.add(key)
        self.jobs.put((key, fn, args))

    def run(self):
        while True:
            key, fn, args = self.jobs.get()
            try:
                fn(*args)
            except Exception as e:  # keep the agent alive whatever a job does
                log(f"job {key} failed: {e!r}")
                if isinstance(key, str) and key.startswith("command:"):
                    self.state.add_result(key.split(":", 1)[1], False, f"agent error: {e}")
            finally:
                self.queued.discard(key)


# ── Check-in ───────────────────────────────────────────────────────────

class KeyRejected(Exception):
    pass


def post_checkin(cloud, key, report):
    request = urllib.request.Request(
        f"{cloud.rstrip('/')}/api/device/checkin",
        data=json.dumps(report).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": f"ember-agent/{AGENT_VERSION}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise KeyRejected(f"HTTP {e.code}") from e
        raise


def handle_response(state, worker, response, runtime):
    channel = response.get("channel")
    if channel and re.fullmatch(r"[a-z0-9._-]+", channel):
        try:
            current = cfg("update.channel")
        except KeyError:
            current = None
        if current != channel:
            with state.lock, open(CONFIG) as f:
                write_atomic(CONFIG, set_channel(f.read(), channel), 0o644)
            log(f"channel set to {channel} (from the console)")

    target = response.get("target")
    if target and (target.get("image") != RELEASE_IMAGE or not RELEASE_TAG.fullmatch(str(target.get("tag")))):
        log(f"ignoring release {target.get('image')}:{target.get('tag')}: only {RELEASE_IMAGE}:sha-… is allowed")
        target = None
    if target:
        wanted = f"{target['image']}:{target['tag']}\n"
        try:
            with open(TARGET_FILE) as f:
                same = f.read() == wanted
        except OSError:
            same = False
        if not same:
            write_atomic(TARGET_FILE, wanted)
            log(f"release for this unit: {wanted.strip()}")
    elif os.path.exists(TARGET_FILE):
        os.remove(TARGET_FILE)   # no release for the channel: keep what's installed

    settings = response.get("settings")
    if settings and isinstance(settings.get("version"), int) and isinstance(settings.get("toml"), str):
        version = settings["version"]
        with state.lock:
            pending = state["pending_settings"]
            handled = max(state["settings"]["version"], pending["version"] if pending else 0)
        if version > handled:
            worker.add(f"settings:{version}", apply_settings, state, version, settings["toml"], channel, runtime)

    for command in response.get("commands") or []:
        cid = command.get("id")
        if not cid or cid in state["seen"]:
            continue
        with state.lock:
            state["seen"] = (state["seen"] + [cid])[-50:]
        worker.add(f"command:{cid}", run_command, state, command, runtime)


def file_hash(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def check_in_once(state, worker, failing):
    """@return (seconds until the next check-in, the problem now logged or None)."""
    interval = DEFAULT_INTERVAL_S
    runtime = cfg("system.runtime")
    fleet = read_env_file(KEY_FILE)
    key = fleet.get("EMBER_FLEET_KEY", "")
    cloud = fleet.get("EMBER_CLOUD_URL") or DEFAULT_CLOUD
    checked_in = False

    if not key:
        problem = f"no EMBER_FLEET_KEY in {KEY_FILE}; not connected to the console"
        interval = 600
    elif key.split(".", 1)[0] != str(cfg("device.serial")):
        problem = f"the device key is for unit {key.split('.', 1)[0]}, but this is unit {cfg('device.serial')}"
        interval = 600
    else:
        problem = None
        report = build_report(state, runtime)
        try:
            response = post_checkin(cloud, key, report)
            checked_in = True
            write_atomic(LAST_CHECKIN_FILE, now_iso() + "\n")
            delivered = {r["id"] for r in report["commands"]}
            with state.lock:
                state["results"] = [r for r in state["results"] if r["id"] not in delivered]
            handle_response(state, worker, response, runtime)
            if isinstance(response.get("interval"), int):
                interval = min(max(response["interval"], 15), 3600)
        except KeyRejected as e:
            problem = f"the console refused this unit's device key ({e}); it was replaced or revoked"
            interval = 600
        except (OSError, ValueError, http.client.HTTPException) as e:   # network errors, bad JSON
            problem = f"can't reach {cloud}: {e}"

    if problem != failing:
        log(problem or f"checking in with {cloud} as unit {cfg('device.serial')}")
    confirm_or_revert_settings(state, worker, checked_in, runtime)
    return interval, problem



def self_test():
    """Build one report without the network or the real state, as a smoke test."""
    report = build_report(State(), cfg("system.runtime"))
    json.dumps(report)
    check_settings(open(CONFIG).read())
    set_channel("[update]\n", "stable")
    print("ok")


def main():
    if sys.argv[1:] == ["--self-test"]:
        return self_test()
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    state = State()
    if state["running"]:
        # The agent stopped while a command ran; the console still waits for it.
        state.add_result(state["running"]["id"], False, "The agent restarted while this ran; check the unit")
    pending = state["pending_settings"]
    if pending and pending["applied_at"] is None:
        # Stopped during a settings restart: start the 5-minute clock now.
        state["pending_settings"] = dict(pending, applied_at=time.time())
    worker = Worker(state)
    worker.start()
    me = os.path.realpath(__file__)
    my_hash = file_hash(me)
    log(f"ember-agent {AGENT_VERSION} started")

    failing = None  # last error, so the log says it once instead of every minute
    while True:
        try:
            interval, failing = check_in_once(state, worker, failing)
        except Exception as e:  # keep checking in whatever went wrong
            log(f"check-in failed: {e!r}")
            interval = DEFAULT_INTERVAL_S

        # A newer agent arrived with a release (ember-update.sh installs it):
        # switch to it once nothing is in progress.
        if not worker.busy() and os.path.exists(me) and file_hash(me) != my_hash:
            log("agent file changed; restarting into the new version")
            os.execv(sys.executable, [sys.executable, me] + sys.argv[1:])
        time.sleep(interval)

if __name__ == "__main__":
    main()
