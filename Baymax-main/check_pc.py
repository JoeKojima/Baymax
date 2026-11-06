"""Read-only preflight. No recording, camera capture, API calls, or secret output."""
from pathlib import Path
import sys
from dotenv import load_dotenv
import os
import sounddevice as sd
from pc_hardware import select_audio_devices

load_dotenv(Path(__file__).with_name(".env"), override=True, interpolate=False)
print("Python:", sys.version.split()[0])
print("Gemini key configured:", bool(os.getenv("GOOGLE_API_KEY")))
print("\nAvailable audio devices:")
print(sd.query_devices())
try:
    mic, speaker = select_audio_devices(sd)
    print(f"\nSelected microphone: {mic}; speaker: {speaker}; 48 kHz supported.")
except Exception as exc:
    print("\nAudio setup needs attention:", exc)
    sys.exit(1)
