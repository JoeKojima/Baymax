"""Audio recording and transcription module."""
import pyaudio
import wave
import io
import os
import webrtcvad
import collections
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# Configuration
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SAMPLE_RATE = 16000
CHUNK_DURATION_MS = 30
CHUNK_SIZE = int(SAMPLE_RATE * CHUNK_DURATION_MS / 1000)
SILENCE_DURATION = 2  # seconds

# Initialize OpenAI client
openai_client = OpenAI(api_key=OPENAI_API_KEY)


def record_until_silence(silence_duration: int = SILENCE_DURATION):
    vad = webrtcvad.Vad(3)  # Aggressiveness mode 3 (most aggressive)
    audio = pyaudio.PyAudio()

    stream = audio.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=CHUNK_SIZE
    )

    print("Listening...")
    frames = []
    ring_buffer = collections.deque(maxlen=int(silence_duration * 1000 / CHUNK_DURATION_MS))
    triggered = False

    while True:
        chunk = stream.read(CHUNK_SIZE, exception_on_overflow=False)
        is_speech = vad.is_speech(chunk, SAMPLE_RATE)

        if not triggered:
            ring_buffer.append((chunk, is_speech))
            num_voiced = len([f for f, speech in ring_buffer if speech])
            if num_voiced > 0.7 * ring_buffer.maxlen:  # Increased threshold to reduce false triggers
                triggered = True
                print("Speaking detected...")
                for f, s in ring_buffer:
                    frames.append(f)
                ring_buffer.clear()
        else:
            frames.append(chunk)
            ring_buffer.append((chunk, is_speech))
            num_unvoiced = len([f for f, speech in ring_buffer if not speech])

            if num_unvoiced > 0.9 * ring_buffer.maxlen:
                print("Silence detected, processing...")
                break

    stream.stop_stream()
    stream.close()
    audio.terminate()

    # Save to WAV file in memory
    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(audio.get_sample_size(pyaudio.paInt16))
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b''.join(frames))

    wav_buffer.seek(0)
    wav_buffer.name = "audio.wav"
    return wav_buffer


def transcribe_audio(audio_file):
    transcript = openai_client.audio.transcriptions.create(
        model="whisper-1",
        file=audio_file
    )
    return transcript.text
