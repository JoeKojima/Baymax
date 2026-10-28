"""PC device selection only. Gemini session and processing stay in upstream core."""
import glob
import os
import platform
import re
import sys


def arm_linux():
    """Linux on 64-bit ARM (the Orange Pi 5's RK3588). Fast-core pinning and the 1-frame
    camera buffer were measured only there, so they apply only there."""
    return sys.platform.startswith("linux") and platform.machine().lower() in ("aarch64", "arm64")


def performance_cpus(sysfs="/sys/devices/system/cpu"):
    """The fast cores of an ARM big.LITTLE CPU this process may use (on the Orange Pi 5's
    RK3588: the four Cortex-A76s, cpu4-7), or None when the cores are all alike, fewer
    than two fast cores are allowed, or it can't tell. Linux on 64-bit ARM only (x86
    hybrid CPUs, e.g. Intel P/E cores, are left to the kernel's scheduler); reads each
    core's relative speed from sysfs."""
    if not arm_linux() or not hasattr(os, "sched_getaffinity"):
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
    # One fast core alone (e.g. under taskset -c 0-4) would squeeze audio, pose and
    # Gemini onto it: leave such a limit as it is.
    return fast if len(fast) >= 2 and len(fast) < len(speed) else None


def device_tree_compatible(path="/proc/device-tree/compatible"):
    """The board's device-tree 'compatible' strings (Linux ARM boards); () elsewhere."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return ()
    return tuple(part.decode("ascii", "replace") for part in data.split(b"\0") if part)


def is_rk3588(compatible=None):
    """Rockchip RK3588/RK3588S (Orange Pi 5 family), the SoC with the 6-TOPS NPU."""
    compatible = device_tree_compatible() if compatible is None else compatible
    return any(entry.startswith("rockchip,rk3588") for entry in compatible)


def npu_drivers(drivers="/sys/bus/platform/drivers"):
    """Loaded Rockchip NPU kernel drivers: 'RKNPU' (vendor kernel; what RKNN needs) and/or
    'rocket' (mainline accel driver, which librknnrt cannot use). Empty if neither."""
    return sorted(os.path.basename(path) for path in glob.glob(os.path.join(drivers, "*"))
                  if os.path.basename(path).lower() in ("rknpu", "rocket"))


def thread_ids(tasks="/proc/self/task"):
    """This process's Linux thread ids; [0] (the calling thread) if they can't be listed."""
    try:
        return sorted(int(name) for name in os.listdir(tasks) if name.isdigit()) or [0]
    except OSError:
        return [0]


def pin_threads(cpus, tasks="/proc/self/task"):
    """Put every existing thread of this process on cpus. On Linux the affinity is per
    thread (os.sched_setaffinity(0, ...) moves only the caller); threads started later
    inherit it from the thread that starts them. A thread that exits meanwhile is
    skipped; a thread started meanwhile is caught by listing again."""
    done = set()
    for _ in range(3):
        pending = [tid for tid in thread_ids(tasks) if tid not in done]
        if not pending:
            return
        for tid in pending:
            try:
                os.sched_setaffinity(tid, cpus)
            except OSError:          # exited meanwhile (ProcessLookupError) or not ours to move
                pass
            done.add(tid)


def use_performance_cpus():
    """Keep Ember on the fast cores of an ARM big.LITTLE board (Orange Pi 5): MediaPipe
    runs pose on one thread, about 5x slower on an A55 than an A76 (measured on the
    RK3588). Moves every thread that already exists and the caller; threads started later
    inherit it. run_mvp.py and run_pc.py call it first thing; a second call changes
    nothing and prints nothing. BAYMAX_CPU_PINNING=off turns it off."""
    if os.getenv("BAYMAX_CPU_PINNING", "auto").strip().lower() in ("off", "0", "false", "no"):
        return None
    cpus = performance_cpus()
    if not cpus:
        return None
    already = os.sched_getaffinity(0) == cpus
    try:
        os.sched_setaffinity(0, cpus)
    except OSError as error:
        print(f"[CPU] Could not keep Ember on the fast cores ({error}); using all.", flush=True)
        return None
    pin_threads(cpus)
    if not already:
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
    """OpenCV's V4L2 backend queues four frames, so reading at 15 fps from a 30 fps camera
    returns pictures up to ~0.2 s old. Keep one, so pose and fall detection see the
    present. Linux on 64-bit ARM only (the Orange Pi; the runtime calls it for the pc
    profile): unmeasured on x86 Linux cameras (LattePanda, Docker), which keep OpenCV's
    default."""
    if arm_linux():
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
