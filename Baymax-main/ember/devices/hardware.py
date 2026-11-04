"""Device selection and streams, as the version-8 runtime does them, behind one object.

`Devices` takes a sounddevice-compatible backend (the real module, or
devices.virtual.VirtualSoundDevice) and a camera opener. Nothing is opened or
queried when it is created; the runtime calls select() at startup, like v8 did
at import. Log lines are v8's, so robot logs read the same.
"""
import subprocess
import time

SEND_SAMPLE_RATE = 48000
RECEIVE_SAMPLE_RATE = 48000
OUTPUT_CHANNELS = 1  # Gemini returns mono
CHUNK_SIZE = 1024

# The USB speaker (Jieli Technology "UACDemoV1.0") is not exposed as its own
# PortAudio device — PortAudio only sees the 'pipewire'/'default' nodes, which
# follow whatever PipeWire's default sink happens to be. So we pin the default
# sink to the USB speaker first, then open the pipewire node.
SPEAKER_SINK_MATCH = ("UACDemo", "Jieli")


def pin_pipewire_sink(match=SPEAKER_SINK_MATCH, run=subprocess.run):
    """Point PipeWire's default sink at the USB speaker. Returns its name, or None."""
    try:
        listing = run(["pactl", "list", "sinks", "short"], capture_output=True, text=True, timeout=5).stdout
    except Exception as e:
        print(f"[AUDIO] Could not query PipeWire sinks: {e}")
        return None

    for line in listing.splitlines():
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        sink_name = fields[1]
        if not any(m.lower() in sink_name.lower() for m in match):
            continue
        try:
            run(["pactl", "set-default-sink", sink_name], check=True, capture_output=True, timeout=5)
            print(f"[AUDIO] Default sink pinned to USB speaker: {sink_name}")
            return sink_name
        except Exception as e:
            print(f"[AUDIO] Failed to pin default sink to {sink_name}: {e}")
            return None

    print("[AUDIO] USB speaker sink not found; leaving PipeWire default sink as-is.")
    return None


class Devices:
    def __init__(self, sd, settings, cv2=None, camera_opener=None, pin_sink=pin_pipewire_sink):
        self.sd, self.settings, self.cv2 = sd, settings, cv2
        self.camera_opener = camera_opener
        self.pin_sink = pin_sink
        self.microphone = self.speaker = None
        self.input_channels = 2  # Hardware demands 2 channels

    # ── Audio ────────────────────────────────────────────────────────────────
    def default_ids(self):
        """(microphone, speaker) device ids: pc_hardware on desktops, PipeWire on the robot."""
        sd = self.sd
        if self.settings.pc:
            from pc_hardware import select_audio_devices
            return select_audio_devices(sd)
        self.pin_sink()

        devices = sd.query_devices()
        microphone = None

        for i, dev in enumerate(devices):
            if dev['name'] == 'pipewire':
                microphone = i
                print(f"FOUND PIPEWIRE AT {microphone}")
                break

        def find_output(predicate):
            for i, dev in enumerate(devices):
                if dev['max_output_channels'] > 0 and predicate(dev):
                    return i
            return None

        # Prefer the USB speaker's own PortAudio device if ALSA ever exposes it,
        # otherwise route through pipewire (and fall back to 'default').
        speaker = find_output(lambda d: any(m.lower() in d['name'].lower() for m in SPEAKER_SINK_MATCH))
        if speaker is not None:
            print(f"[AUDIO] USB speaker exposed directly at device {speaker}: {devices[speaker]['name']}")
        else:
            speaker = find_output(lambda d: d['name'] == 'pipewire')
            if speaker is None:
                speaker = find_output(lambda d: d['name'] == 'default')

        if speaker is None:
            speaker = 0
        return microphone, speaker

    def select(self):
        """Startup selection (v8 module level): choose, log, make default, set input channels."""
        sd = self.sd
        self.microphone, self.speaker = self.default_ids()
        print(f"[AUDIO] Mapping input to device ID: {self.microphone}, and output to device ID: "
              f"{self.speaker} ({sd.query_devices()[self.speaker]['name']})")
        sd.default.device = [self.microphone, self.speaker]
        self.input_channels = 2
        if self.settings.pc:
            self.input_channels = min(2, int(sd.query_devices(self.microphone)["max_input_channels"]))
        return self.microphone, self.speaker

    def input_stream(self, callback):
        return self.sd.InputStream(samplerate=SEND_SAMPLE_RATE, channels=self.input_channels, dtype="int16",
                                   blocksize=CHUNK_SIZE, callback=callback)

    def output_stream(self, callback):
        return self.sd.OutputStream(samplerate=RECEIVE_SAMPLE_RATE, channels=OUTPUT_CHANNELS, dtype="int16",
                                    blocksize=1024, callback=callback)

    def reset(self, sleep=time.sleep):
        """After a failed connection: reset PortAudio and re-select devices (v8 run() recovery)."""
        sd = self.sd
        print("[AUDIO] Resetting PortAudio state...")
        try:
            sd._terminate()
        except Exception:
            pass
        sleep(5)
        try:
            sd._initialize()
            mic, spk = self.default_ids()
            sd.default.device = [mic, spk]
            print(f"[AUDIO] PortAudio reset. Input→{mic}, Output→{spk}")
        except Exception as reinit_err:
            print(f"[AUDIO] PortAudio reinit failed: {reinit_err}")

    # ── Camera ───────────────────────────────────────────────────────────────
    def open_camera(self):
        """First camera that returns a frame, or None (v8 camera thread probe)."""
        if self.camera_opener is not None:
            return self.camera_opener()
        cv2 = self.cv2
        from pc_hardware import camera_candidates, open_camera
        pc = self.settings.pc
        for cam_idx in (camera_candidates() if pc else [0, 1, 2]):
            test = open_camera(cv2, cam_idx) if pc else cv2.VideoCapture(cam_idx)
            if test.isOpened():
                ret, _ = test.read()
                if ret:
                    print(f"[CAM] Camera found at index {cam_idx}")
                    return test
            test.release()
        return None


def real(settings):
    import cv2
    import sounddevice as sd
    return Devices(sd, settings, cv2=cv2)


def virtual(settings, sd=None, camera=None):
    """Virtual mic/speaker (devices.virtual) and a virtual camera, selected like a desktop."""
    from dataclasses import replace
    from .virtual import VirtualCamera, VirtualSoundDevice
    sd = sd or VirtualSoundDevice()
    capture = camera if camera is not None else VirtualCamera()

    def opener():
        if capture is False:
            return None
        print("[CAM] Camera found at index 0")
        return capture
    return Devices(sd, replace(settings, hardware_profile="pc"), camera_opener=opener)
