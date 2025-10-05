import json
import pyaudio
import wave
import io
import os
from datetime import datetime, UTC
from typing import TypedDict, List, Dict
from openai import OpenAI
import pyttsx3
import webrtcvad
import collections
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configuration
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SCRATCHPAD_PATH = "scratchpad.json"
SILENCE_DURATION = 2  # seconds
SAMPLE_RATE = 16000
CHUNK_DURATION_MS = 30
CHUNK_SIZE = int(SAMPLE_RATE * CHUNK_DURATION_MS / 1000)

# Initialize clients
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# Initialize scratchpad
open(SCRATCHPAD_PATH, "w").write("[]")

class State(TypedDict):
    scratchpad: List[Dict[str, str]]
    text: str
    img_path: str | None

def load_pad(path=SCRATCHPAD_PATH):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_pad(pad, path=SCRATCHPAD_PATH):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pad, f, ensure_ascii=False, indent=2)

def call_bigLLM(input_text):
    response = openai_client.chat.completions.create(
        messages=[
            {
                "role": "system",
                "content": (
                    """You are a personal assistant robot designed to aid the elderly population with everyday tasks. You need to provide 3 things:
(1) a boolean value for whether movement is necessary to follow the user's command
(2) appropriate, helpful verbal output
(3) Motion information - provide detailed step-by-step navigation/guidance instructions when movement is required (e.g., "Move forward 3 steps, turn left at the door, walk 5 steps to the cabinet"). If no movement is needed, put "N/A".

Organize these three outputs as a list separated by %,%
Format: <boolean> %,% <verbal output> %,% <motion plan>
Example: True %,% Let me guide you to the cabinet %,% Move forward 3 steps, turn left at the door, walk 5 steps to the cabinet

Do not put the boolean in quotations. List should be THREE elements long."""
                )
            },
            {
                "role": "user",
                "content": "Provide a response to this input: " + input_text
            }
        ],
        model="gpt-4o",
    )
    return response.choices[0].message.content

def parse_triplet(raw: str):
    parts = [p.strip().strip('"').strip("'") for p in raw.split("%,%")]
    while len(parts) < 3:
        parts.append("N/A")
    parts = parts[:3]
    movement = parts[0].lower() in ("true", "yes", "1")
    return movement, parts[1], parts[2]

def transcribe_audio(audio_file):
    """Convert speech to text using OpenAI Whisper"""
    transcript = openai_client.audio.transcriptions.create(
        model="whisper-1",
        file=audio_file
    )
    return transcript.text

def text_to_speech(text: str):
    """Convert text to speech using pyttsx3 (offline TTS)"""
    engine = pyttsx3.init()
    engine.setProperty('rate', 150)  # Speed of speech
    engine.setProperty('volume', 1.0)  # Volume (0.0 to 1.0)
    engine.say(text)
    engine.runAndWait()

def record_until_silence():
    """Record audio until 2 seconds of silence detected"""
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
    voiced_frames = []
    ring_buffer = collections.deque(maxlen=int(SILENCE_DURATION * 1000 / CHUNK_DURATION_MS))
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

def run_voice_assistant():
    """Main loop for voice assistant"""
    state: State = {"scratchpad": load_pad(), "text": "", "img_path": None}
    print("Voice assistant started. Listening continuously...")

    while True:
        try:
            # Record audio until silence
            audio_file = record_until_silence()

            # Transcribe to text
            user_text = transcribe_audio(audio_file)
            print(f"You: {user_text}")

            if not user_text.strip():
                continue

            # Process with LLM
            raw = call_bigLLM(user_text)
            movement, say, motion = parse_triplet(raw)

            print(f"Movement: {'Yes' if movement else 'No'}")
            print(f"Audio output: {say or 'N/A'}")
            print(f"Motion plan: {motion or 'N/A'}")

            # Speak response
            if say and say != "N/A":
                text_to_speech(say)

            # Update scratchpad
            entry = {
                "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                "user_text": user_text,
                "movement_required": str(movement),
                "verbal_output": say,
                "motion_info": motion
            }
            state["scratchpad"].append(entry)
            save_pad(state["scratchpad"])
            print(f"[Scratchpad entries: {len(state['scratchpad'])}]\n")

        except KeyboardInterrupt:
            print("\nShutting down...")
            break
        except Exception as e:
            print(f"Error: {e}")
            continue

if __name__ == "__main__":
    run_voice_assistant()
