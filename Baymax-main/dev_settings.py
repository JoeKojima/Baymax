"""Private local settings for devUI; no conversation/provider logic."""
import json
import os
from pathlib import Path
import re
import tempfile

from dotenv import dotenv_values

MANAGED = {"GOOGLE_API_KEY", "BAYMAX_MIC_DEVICE", "BAYMAX_SPEAKER_DEVICE", "BAYMAX_CAMERA_DEVICE",
           "BAYMAX_MIC_NAME", "BAYMAX_MIC_HOSTAPI", "BAYMAX_SPEAKER_NAME", "BAYMAX_SPEAKER_HOSTAPI", "BAYMAX_CAMERA_NAME"}


def read_configuration(path):
    return dotenv_values(path, interpolate=False) if Path(path).exists() else {}


def public_configuration(path):
    values = read_configuration(path)
    return {"key_configured": bool(values.get("GOOGLE_API_KEY")),
            "microphone": values.get("BAYMAX_MIC_DEVICE", "") or "",
            "speaker": values.get("BAYMAX_SPEAKER_DEVICE", "") or "",
            "camera": values.get("BAYMAX_CAMERA_DEVICE", "") or "",
            "camera_name": values.get("BAYMAX_CAMERA_NAME", "") or "",
            "microphone_name": values.get("BAYMAX_MIC_NAME", "") or "",
            "microphone_hostapi": values.get("BAYMAX_MIC_HOSTAPI", "") or "",
            "speaker_name": values.get("BAYMAX_SPEAKER_NAME", "") or "",
            "speaker_hostapi": values.get("BAYMAX_SPEAKER_HOSTAPI", "") or ""}


def save_configuration(path, payload, catalog):
    """Validate all fields before an atomic write. Blank key preserves existing key."""
    if not isinstance(payload, dict):
        raise ValueError("Invalid settings request.")
    key = payload.get("key", "")
    if not isinstance(key, str) or len(key) > 4096 or any(c.isspace() for c in key):
        raise ValueError("The API key must contain no whitespace. Paste the key value only.")
    old = read_configuration(path)
    values = {"GOOGLE_API_KEY": key or old.get("GOOGLE_API_KEY", "") or ""}
    for field, setting, kind in (("microphone", "BAYMAX_MIC", "microphones"),
                                ("speaker", "BAYMAX_SPEAKER", "speakers")):
        selected = payload.get(field, "")
        if not isinstance(selected, str):
            raise ValueError("Choose devices from the dropdowns.")
        match = next((d for d in catalog[kind] if str(d["index"]) == selected), None)
        if selected and match is None:
            raise ValueError(f"Selected {field} is unavailable. Refresh devices and choose again.")
        values[setting + "_DEVICE"] = selected
        values[setting + "_NAME"] = match["name"] if match else ""
        values[setting + "_HOSTAPI"] = match["hostapi"] if match else ""
    camera = payload.get("camera", "")
    if not isinstance(camera, str) or (camera not in ("", "none") and
                                      camera not in [str(d["index"]) for d in catalog["cameras"]]):
        raise ValueError("Selected camera is unavailable. Refresh devices and choose again.")
    values["BAYMAX_CAMERA_DEVICE"] = camera
    values["BAYMAX_CAMERA_NAME"] = next((d["name"] for d in catalog["cameras"] if str(d["index"]) == camera), "")
    path = Path(path)
    previous = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    keep = [line for line in previous if not (m := re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*=", line))
            or m.group(1) not in MANAGED]
    keep.extend(f"{name}={json.dumps(value, ensure_ascii=False)}" for name, value in values.items())
    fd, temporary = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write("\n".join(keep) + "\n")
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return public_configuration(path)
