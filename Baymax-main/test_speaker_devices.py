"""Iterate through all output-capable sound devices and announce each one."""
import sounddevice as sd
import numpy as np
import pyttsx3
import wave
import tempfile
import os
import time

engine = pyttsx3.init()
engine.setProperty('rate', 150)

devices = sd.query_devices()
output_devices = [
    (i, d) for i, d in enumerate(devices) if d['max_output_channels'] > 0
]

print(f"Found {len(output_devices)} output-capable devices:\n")
for i, d in output_devices:
    print(f"  {i}: {d['name']} ({d['max_output_channels']} out, default_sr={d['default_samplerate']})")
print()

for idx, dev in output_devices:
    name = dev['name']
    sr = int(dev['default_samplerate']) or 44100
    channels = min(dev['max_output_channels'], 2)

    print(f"--- Trying device {idx}: {name} (sr={sr}, ch={channels}) ---")

    tmp = tempfile.mktemp(suffix='.wav')
    try:
        engine.save_to_file(f"This is sound device {idx}, {name}", tmp)
        engine.runAndWait()

        with wave.open(tmp, 'rb') as wf:
            raw = wf.readframes(wf.getnframes())
            wav_sr = wf.getframerate()
            wav_ch = wf.getnchannels()
            audio = np.frombuffer(raw, dtype=np.int16)
            if wav_ch > 1:
                audio = audio.reshape(-1, wav_ch)[:, 0]

        if wav_sr != sr:
            ratio = sr / wav_sr
            n_out = int(len(audio) * ratio)
            indices = (np.arange(n_out) / ratio).astype(int)
            indices = np.clip(indices, 0, len(audio) - 1)
            audio = audio[indices]

        if channels > 1:
            audio = np.column_stack([audio] * channels)

        sd.play(audio, samplerate=sr, device=idx)
        sd.wait()
        print(f"  Played on device {idx}.")
    except Exception as e:
        print(f"  FAILED on device {idx}: {e}")
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

    time.sleep(1.5)

print("\nDone. Which device did you hear?")
