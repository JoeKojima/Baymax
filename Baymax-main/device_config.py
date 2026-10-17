"""
Per-robot settings, read from /etc/ember/device.toml.

Every value has a default matching the original LattePanda (unit 001), so a
robot without the file behaves exactly as before. A file that is missing a
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
