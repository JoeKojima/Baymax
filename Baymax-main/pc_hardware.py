"""PC device selection only. Gemini session and processing stay in upstream core."""
import glob
import os
import re
import sys


def performance_cpus(sysfs="/sys/devices/system/cpu"):
    """The fast cores of a big.LITTLE CPU this process may use (on the Orange Pi 5's
    RK3588: the four Cortex-A76s, cpu4-7), or None when the cores are all alike or
    it can't tell. Linux only; reads each core's relative speed from sysfs."""
    if not sys.platform.startswith("linux") or not hasattr(os, "sched_getaffinity"):
        return None
    speed = {}
    for path in glob.glob(os.path.join(sysfs, "cpu[0-9]*")):
        cpu = int(os.path.basename(path)[3:])
        for name in ("cpu_capacity", "cpufreq/cpuinfo_max_freq"):
            try:
                with open(os.path.join(path, name)) as f:
                    speed[cpu] = int(f.read().strip())
                break
            except (OSError, ValueError):
                continue
    if len(speed) < 2 or len(set(speed.values())) < 2:
        return None
    fastest = max(speed.values())
    fast = {cpu for cpu, value in speed.items() if value >= 0.8 * fastest}
    fast &= os.sched_getaffinity(0)
    return fast if fast and len(fast) < len(speed) else None


def use_performance_cpus():
    """Keep Ember on the fast cores of a big.LITTLE board (Orange Pi 5): MediaPipe runs
    pose on one thread, about 3x slower on an A55 than an A76. Call before the runtime
    starts its threads; they inherit it. BAYMAX_CPU_PINNING=off turns it off."""
    if os.getenv("BAYMAX_CPU_PINNING", "auto").strip().lower() in ("off", "0", "false", "no"):
        return None
    cpus = performance_cpus()
    if cpus:
        os.sched_setaffinity(0, cpus)
        print(f"[CPU] Running on the fast cores {','.join(map(str, sorted(cpus)))} "
              "(BAYMAX_CPU_PINNING=off to use all).", flush=True)
    return cpus


def camera_candidates():
    value = os.getenv("BAYMAX_CAMERA_DEVICE", "").strip()
    if value == "none":
        return []
    if sys.platform == "darwin":
        from macos_devices import camera_devices, resolve_camera
        if not value:
            return [device["index"] for device in camera_devices()]
        try:
            index = int(value)
            if index < 0:
                raise ValueError()
        except ValueError:
            raise RuntimeError("Choose a camera in devUI and save before starting Gemini.") from None
        return [resolve_camera(index, os.getenv("BAYMAX_CAMERA_UID", ""), os.getenv("BAYMAX_CAMERA_NAME", ""))]
    if sys.platform.startswith("linux") and value.startswith("/dev/"):
        # A udev link such as /dev/v4l/by-id/...-video-index0 survives reboots
        # that renumber /dev/videoN.
        return [value]
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
    if sys.platform == "darwin":
        from macos_devices import check_camera_permission
        check_camera_permission()
    backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_AVFOUNDATION if sys.platform == "darwin" else cv2.CAP_ANY
    if isinstance(index, str):
        backend = cv2.CAP_V4L2
    return cv2.VideoCapture(index, backend)


def latest_frames_only(cv2, capture):
    """Linux (V4L2) queues about four frames, so reading at 15 fps from a 30 fps camera
    returns pictures ~0.1 s old. Keep one, so pose and fall detection see the present."""
    if sys.platform.startswith("linux"):
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)


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


def _without_alsa_card(name):
    return re.sub(r"\s*\(hw:\d+,\d+\)$", "", name)


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
                if not matches:
                    # ALSA card numbers in "(hw:N,M)" can change between boots.
                    stable = _without_alsa_card(saved_name)
                    matches = [i for i, device in enumerate(devices) if device[channels] > 0
                               and _without_alsa_card(device["name"]) == stable
                               and hosts[device["hostapi"]]["name"] == saved_host]
                    if len(matches) > 1:
                        matches = []  # Identical renumbered devices: the saved index may be the other one.
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
