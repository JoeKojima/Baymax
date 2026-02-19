"""
Gemini Live API - Realtime S2S + Video (SoundDevice Version)
Model: gemini-2.5-flash-native-audio-preview-12-2025
"""
import asyncio
import os
import cv2
import sounddevice as sd
import numpy as np
from google import genai
from dotenv import load_dotenv

# Load API Key
load_dotenv()
API_KEY = os.getenv("GOOGLE_API_KEY")

# Configuration
MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"

CONFIG = {
    "response_modalities": ["AUDIO"],
    "system_instruction": "You are a helpful and friendly AI assistant. You can see what I show you."
}

# Audio Config
SEND_SAMPLE_RATE = 16000
RECEIVE_SAMPLE_RATE = 24000
CHANNELS = 1
CHUNK_SIZE = 1024 

# Queues
audio_queue_mic = asyncio.Queue()
audio_queue_output = asyncio.Queue()

async def listen_audio():
    """Captures audio from the default microphone using SoundDevice."""
    # Capture the main thread's asyncio loop BEFORE entering the callback
    loop = asyncio.get_running_loop()

    def audio_callback(indata, frames, time, status):
        """Callback for SoundDevice input stream."""
        if status:
            print(status, flush=True)
        
        # Safely hand off the raw bytes from the PortAudio thread to the asyncio loop
        loop.call_soon_threadsafe(
            audio_queue_mic.put_nowait, 
            {"data": bytes(indata), "mime_type": f"audio/pcm;rate={SEND_SAMPLE_RATE}"}
        )

    # We use a non-blocking input stream
    stream = sd.InputStream(
        samplerate=SEND_SAMPLE_RATE,
        channels=CHANNELS,
        dtype='int16',
        blocksize=CHUNK_SIZE,
        callback=audio_callback
    )
    
    with stream:
        while True:
            await asyncio.sleep(1) # Keep task alive

async def play_audio():
    """Plays audio received from Gemini using SoundDevice."""
    # Create a blocking OutputStream (we write to it from this async task)
    stream = sd.OutputStream(
        samplerate=RECEIVE_SAMPLE_RATE,
        channels=CHANNELS,
        dtype='int16'
    )
    stream.start()

    try:
        while True:
            bytestream = await audio_queue_output.get()
            # Convert raw bytes back to numpy array
            data_array = np.frombuffer(bytestream, dtype=np.int16)
            stream.write(data_array)
    except Exception as e:
        print(f"Playback error: {e}")
    finally:
        stream.stop()
        stream.close()

async def send_audio_realtime(session):
    """Sends audio from the queue to Gemini."""
    while True:
        msg = await audio_queue_mic.get()
        try:
            # Use the new realtime method, wrapped in a list as expected by the SDK
            await session.send_realtime_input([msg])
        except Exception as e:
            print(f"Error sending audio: {e}")

async def send_video_realtime(session):
    """Captures and sends video frames."""
    cap = cv2.VideoCapture(0)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    
    print("Camera active.")
    try:
        while True:
            await asyncio.sleep(0.5) # ~2 FPS
            ret, frame = cap.read()
            if not ret: continue

            # Compress to JPEG
            _, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 50])
            jpg_bytes = buffer.tobytes()

            # Send visual input via the updated realtime streaming method
            await session.send_realtime_input({"media": {"data": jpg_bytes, "mime_type": "image/jpeg"}})
            
            # Show preview
            cv2.imshow('Gemini Vision', frame)
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break
    except asyncio.CancelledError:
        cap.release()
        cv2.destroyAllWindows()

async def receive_audio(session):
    """Receives audio from Gemini and puts it in the playback queue."""
    while True:
        try:
            async for response in session.receive():
                server_content = response.server_content
                if server_content is None:
                    continue
                
                model_turn = server_content.model_turn
                if model_turn:
                    for part in model_turn.parts:
                        if part.inline_data and part.inline_data.mime_type.startswith("audio/pcm"):
                            audio_queue_output.put_nowait(part.inline_data.data)
        except Exception as e:
            print(f"Receive error: {e}")
            break

async def run():
    client = genai.Client(api_key=API_KEY, http_options={"api_version": "v1alpha"})

    print(f"Connecting to {MODEL}...")
    async with client.aio.live.connect(model=MODEL, config=CONFIG) as live_session:
        print("Connected. System ready.")
        
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(listen_audio())
                tg.create_task(send_audio_realtime(live_session))
                tg.create_task(send_video_realtime(live_session))
                tg.create_task(receive_audio(live_session))
                tg.create_task(play_audio())
        except asyncio.CancelledError:
            pass

if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("Interrupted by user.")