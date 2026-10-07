"""PC device selection only. Gemini session and processing stay in upstream core."""
import os
import sys


def camera_candidates():
    value = os.getenv("BAYMAX_CAMERA_DEVICE", "").strip()
    if value == "none":
        return []
    if not value:
        return [0, 1, 2]
    try:
        index = int(value)
        if index < 0:
            raise ValueError()
        name = os.getenv("BAYMAX_CAMERA_NAME", "")
        if name and sys.platform == "win32":
            import comtypes
            from pygrabber.dshow_graph import FilterGraph
            comtypes.CoInitialize()
            try:
                names = FilterGraph().get_input_devices()
            finally:
                comtypes.CoUninitialize()
            if index >= len(names) or names[index] != name:
                matches = [i for i, candidate in enumerate(names) if candidate == name]
                if len(matches) != 1:
                    raise RuntimeError("Saved camera is unavailable or ambiguous. Reopen devUI and choose a camera.")
                index = matches[0]
        return [index]
    except ValueError:
        raise RuntimeError("Choose a camera in devUI and save before starting Gemini.") from None


def open_camera(cv2, index):
    backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_AVFOUNDATION if sys.platform == "darwin" else cv2.CAP_ANY
    return cv2.VideoCapture(index, backend)


def configure_audio_backend(sd, microphone=None, speaker=None):
    if sys.platform != "win32":
        return
    hosts = sd.query_hostapis()
    settings = []
    for index, direction in ((microphone, "input"), (speaker, "output")):
        device = sd.query_devices(index, direction)
        settings.append(sd.WasapiSettings(auto_convert=True)
                        if hosts[device["hostapi"]]["name"] == "Windows WASAPI" else None)
    sd.default.extra_settings = tuple(settings)


def select_audio_devices(sd):
    devices = sd.query_devices()
    default_in, default_out = sd.default.device
    chosen = []
    for name, default, channels in (
        ("BAYMAX_MIC_DEVICE", default_in, "max_input_channels"),
        ("BAYMAX_SPEAKER_DEVICE", default_out, "max_output_channels"),
    ):
        raw = os.getenv(name, "").strip()
        if raw:
            try:
                index = int(raw)
            except ValueError:
                raise RuntimeError(f"{name} must be a device number from python check_pc.py.") from None
            prefix = "BAYMAX_MIC" if channels == "max_input_channels" else "BAYMAX_SPEAKER"
            saved_name = os.getenv(prefix + "_NAME", "")
            saved_host = os.getenv(prefix + "_HOSTAPI", "")
            if saved_name and saved_host:
                hosts = sd.query_hostapis()
                matches = [i for i, device in enumerate(devices) if device[channels] > 0
                           and device["name"] == saved_name and hosts[device["hostapi"]]["name"] == saved_host]
                if index not in matches:
                    if len(matches) == 1:
                        index = matches[0]
                    else:
                        raise RuntimeError(f"Saved {prefix} device is unavailable or ambiguous. Reopen devUI and choose a device.")
        else:
            index = int(default)
            if index < 0:
                candidates = [i for i, device in enumerate(devices) if device[channels] > 0]
                preferred = "microphone" if channels == "max_input_channels" else "speakers"
                index = next((i for i in candidates if preferred in devices[i].get("name", "").lower()),
                             candidates[0] if candidates else -1)
        if index < 0 or index >= len(devices) or devices[index][channels] < 1:
            raise RuntimeError(f"No usable device for {name}. Run python check_pc.py and set {name} in .env.")
        chosen.append(index)
    if sys.platform == "win32" and hasattr(sd, "WasapiSettings"):
        configure_audio_backend(sd, *chosen)
    sd.check_input_settings(device=chosen[0], channels=min(2, devices[chosen[0]]["max_input_channels"]),
                            samplerate=48000, dtype="int16")
    sd.check_output_settings(device=chosen[1], channels=1, samplerate=48000, dtype="int16")
    return tuple(chosen)
