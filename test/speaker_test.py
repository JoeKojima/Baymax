import sounddevice as sd
import numpy as np

# Uncomment and change this if you want to test a specific hardware device
sd.default.device = [None, 13] # [Input, Output]

SAMPLE_RATE = 48000
DURATION = 3.0     # Seconds
FREQUENCY = 440.0  # Hz (A4 note)

print("🔊 Speaker Test Started...")
print(f"Attempting to play a pure {FREQUENCY}Hz tone for {DURATION} seconds...")
print("-" * 50)

try:
    # Generate a time array
    t = np.linspace(0, DURATION, int(SAMPLE_RATE * DURATION), False)
   
    # Generate a mathematical sine wave
    tone = np.sin(FREQUENCY * t * 2 * np.pi)
   
    # Ensure the audio data is in the correct 16-bit format
    audio_data = (tone * 32767).astype(np.int16)
   
    # Play the audio and block the script until it finishes
    sd.play(audio_data, samplerate=SAMPLE_RATE, blocking=True)
   
    print("✅ Test Complete. Did you hear a loud, continuous beep?")

except Exception as e:
    print(f"❌ Failed to play audio: {e}")