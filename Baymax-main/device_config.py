"""
Per-robot settings, read from /etc/ember/device.toml.

Hardware values default to the original LattePanda (unit 001). Personal memory
requires an explicit patient ID; caching and instrumentation default off.
A file that is missing a
section or key falls back to the default for it; a file that cannot be parsed
falls back to all defaults with a warning, so a typo can't stop a robot from
booting. deploy/device.example.toml lists every key.

Set EMBER_DEVICE_CONFIG to read a different file.

Python:
    import device_config
    device_config.get("audio.speaker_match")   # -> ["UACDemo", "Jieli"]

Shell (lists print space-separated, booleans as true/false):
    python3 device_config.py audio.speaker_volume   # -> 75%
"""
import copy
import os
import sys
import tomllib

CONFIG_PATH = os.getenv("EMBER_DEVICE_CONFIG", "/etc/ember/device.toml")

DEFAULTS = {
    "device": {
        "serial": "001",
        "name": "Baymax Unit 001",
        "model": "Baymax v1",
    },
    "system": {
        # Runs the AI core; owns the PipeWire session.
        "user": "meowmax",
        # How startup.sh runs the AI core and dashboard: "venv" (the git
        # checkout's virtualenv, unit 001) or "docker" (the published image).
        "runtime": "venv",
        # Set by the provisioning script; robot-local time drives the update window.
        "timezone": "America/New_York",
    },
    "audio": {
        # Substrings of the PipeWire sink name of the speaker to pin as default.
        "speaker_match": ["UACDemo", "Jieli"],
        "speaker_volume": "75%",
        "mic_volume": "85%",
    },
    "camera": {
        # OpenCV indices tried in order; the first that returns a frame wins.
        "indices": [0, 1, 2],
        # false skips MediaPipe Pose (saves CPU). Video still works, but there
        # is no fall detection and no robot-initiated conversation.
        "pose_tracking": True,
    },
    "network": {
        # Radio that hosts the hotspot. The portal never uses it for uplink.
        "ap_iface": "wlo1",
        "hotspot_connection": "Baymax_Hotspot",
    },
    "fall_detection": {
        # Pose tracking still runs when this is off (video and face presence
        # need it); only the fall check and its alerts stop.
        "enabled": True,
    },
    "update": {
        # Docker units pull image:channel nightly (2-4 AM) and roll back if
        # the new version doesn't come up. See deploy/provision/ember-update.sh.
        "image": "ghcr.io/joekojima/baymax",
        "channel": "stable",
    },
    "memory": {
        # false stops the AI core using stored memories (lower latency);
        # session summaries are still saved.
        "retrieval_enabled": True,
        # No default patient: legacy shared memories are never assigned silently.
        "patient_id": "",
        "cache_enabled": False,
        "prepared_cache_enabled": False,
        "cache_comparison_enabled": False,
        "prediction_enabled": False,
        "cache_ttl_seconds": 300,
        "prediction_interval_seconds": 30,
        "prediction_max_topics": 2,
        "calibration_path": "",
        "topic_aliases": {},
    },
    "observability": {
        "enabled": False,
        "trace_sample_rate": 0.1,
        "benchmark": False,
    },
}

_config = None


def load():
    """@return the merged settings (defaults overlaid with the file), cached."""
    global _config
    if _config is not None:
        return _config

    config = copy.deepcopy(DEFAULTS)
    try:
        with open(CONFIG_PATH, "rb") as f:
            overrides = tomllib.load(f)
    except FileNotFoundError:
        overrides = {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"[CONFIG] Could not read {CONFIG_PATH} ({e}); using defaults.", file=sys.stderr)
        overrides = {}

    for section, values in overrides.items():
        if isinstance(values, dict) and isinstance(config.get(section), dict):
            config[section].update(values)
        else:
            config[section] = values

    _config = config
    return _config


def get(key):
    """@param key dotted path such as "audio.speaker_volume". Raises KeyError if unknown."""
    value = load()
    for part in key.split("."):
        value = value[part]
    return value


def _format_for_shell(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return " ".join(str(v) for v in value)
    return str(value)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python3 device_config.py SECTION.KEY")
    try:
        print(_format_for_shell(get(sys.argv[1])))
    except KeyError:
        sys.exit(f"unknown setting: {sys.argv[1]}")
