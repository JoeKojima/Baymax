import sounddevice as sd
import numpy as np
import time

# Uncomment and change this if you want to test a specific hardware device
sd.default.device = [13, None] 

SAMPLE_RATE = 48000
TEST_DURATION = 15  # How many seconds to run the test

def audio_callback(indata, frames, time_info, status):
    if status:
        print(f"Status: {status}", flush=True)
   
    # Calculate the root mean square (RMS) to get a volume level
    volume_norm = np.linalg.norm(indata) * 10
   
    # Create a visual volume bar in the terminal
    bar = "█" * int(volume_norm)
    print(f"Mic Level: |{bar.ljust(50)}|", end="\r", flush=True)

print("🎤 Microphone Test Started...")
print(f"Listening for {TEST_DURATION} seconds. Speak into the mic!")
print("-" * 60)

try:
    # We use channels=1 (mono) as that is standard for testing vocal mics
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, callback=audio_callback):
        time.sleep(TEST_DURATION)
except Exception as e:
    print(f"\n❌ Failed to open microphone: {e}")

print("\n\n" + "-" * 60)
print("✅ Mic test complete.")