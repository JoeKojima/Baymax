"""
True Realtime Speech-to-Speech Assistant with Visual Perception.
- Audio In/Out: OpenAI Realtime API (WebSocket)
- Vision: Background loop snapshots -> GPT-4o (REST) -> Text Description -> Realtime Context
prerquisite: pip install opencv-python openai websockets python-dotenv
"""
import os
import json
import base64
import asyncio
import websockets
import cv2  # OpenCV for camera
from dotenv import load_dotenv

from openai import AsyncOpenAI  # Use Async client for the vision loop

# Reuse your existing modules
from face_display import FaceDisplay
from audio_processing import MicrophoneStream, SpeakerStream
from agent import INSTRUCTIONS as BASE_INSTRUCTIONS

# ---- Config ----
REALTIME_MODEL = "gpt-4o-realtime-preview-2024-10-01" 
VISION_MODEL = "gpt-4o"  # The model that describes images
OPENAI_REALTIME_WS = f"wss://api.openai.com/v1/realtime?model={REALTIME_MODEL}"
SESSION_VOICE = "verse"

# How often (seconds) to check the camera
VISION_INTERVAL = 5.0 

load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Async client for the Vision/Image processing
rest_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

def evt(type_: str, **kwargs) -> str:
    payload = {"type": type_}
    payload.update(kwargs)
    return json.dumps(payload)

async def vision_loop(ws_connection):
    """
    Captures an image, converts to text via GPT-4o, and updates Realtime context.
    """
    cap = cv2.VideoCapture(0) # Index 0 is usually the default webcam
    
    print("[Vision] Camera initialized.")
    
    try:
        while True:
            await asyncio.sleep(VISION_INTERVAL)
            
            # 1. Capture Frame
            ret, frame = cap.read()
            if not ret:
                continue

            # 2. Encode to Base64
            _, buffer = cv2.imencode('.jpg', frame)
            base64_image = base64.b64encode(buffer).decode('utf-8')

            # 3. Send to GPT-4o (Vision) to get a text description
            try:
                response = await rest_client.chat.completions.create(
                    model=VISION_MODEL,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "Briefly describe what you see in this image in one sentence. Focus on changes or people."},
                                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}},
                            ],
                        }
                    ],
                    max_tokens=60
                )
                description = response.choices[0].message.content
                
                if description:
                    print(f"[Vision] See: {description}")
                    
                    # 4. Inject into Realtime Session as a 'System' note
                    # We treat this as a user message that gives context, or a conversation item
                    event = {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "message",
                            "role": "user", 
                            "content": [
                                {
                                    "type": "input_text", 
                                    "text": f"(System Visual Update: The camera currently sees: {description})"
                                }
                            ]
                        }
                    }
                    await ws_connection.send(json.dumps(event))
                    
                    # Optional: Force a response if something urgent is seen? 
                    # Usually better to let the model decide when to speak based on the update.

            except Exception as e:
                print(f"[Vision] Error processing image: {e}")

    except asyncio.CancelledError:
        print("[Vision] Stopping camera...")
    finally:
        cap.release()

async def run_session():
    # Audio Hardware
    mic = MicrophoneStream()
    spk = SpeakerStream() # Ensure this supports 24kHz raw PCM if using Realtime default
    face = FaceDisplay(width=480, height=320)

    # Headers for Realtime API
    headers = [
        ("Authorization", f"Bearer {OPENAI_API_KEY}"),
        ("OpenAI-Beta", "realtime=v1"),
    ]

    # Simplified Instructions for Speech-to-Speech (removed the 3-part formatting)
    realtime_instructions = (
        BASE_INSTRUCTIONS + 
        "\n\nYou have eyes. You will receive periodic visual updates about your surroundings. "
        "Incorporate this visual information naturally into your conversation if relevant."
    )

    async with websockets.connect(OPENAI_REALTIME_WS, additional_headers=headers) as ws:
        print("Connected to Realtime S2S. Speak anytime.")

        # 1. Configure Session (ENABLE AUDIO OUTPUT)
        await ws.send(evt("session.update", session={
            "instructions": realtime_instructions,
            "modalities": ["text", "audio"], # <--- ENABLE AUDIO OUTPUT
            "voice": SESSION_VOICE,
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.5,
                "prefix_padding_ms": 300,
                "silence_duration_ms": 600,
                "create_response": True  # <--- Auto-reply when user stops speaking
            }
        }))

        # Start the background tasks
        vision_task = asyncio.create_task(vision_loop(ws))

        async def sender():
            """Stream Mic Audio -> WebSocket"""
            try:
                for frame in mic:
                    if not frame:
                        await asyncio.sleep(0)
                        continue
                    # Append audio to buffer
                    b64 = base64.b64encode(frame).decode("ascii")
                    await ws.send(evt("input_audio_buffer.append", audio=b64))
                    await asyncio.sleep(0)
            except asyncio.CancelledError:
                pass

        async def receiver():
            """Handle Audio Output & Events"""
            try:
                async for raw in ws:
                    data = json.loads(raw)
                    t = data.get("type", "")

                    # --- Speech Started (Interruption Handling) ---
                    if t == "input_audio_buffer.speech_started":
                        face.set_state("listening")
                        # Important: If user interrupts, stop playing current audio
                        # spk.stop() # Uncomment if your SpeakerStream supports clearing the buffer
                        # Send cancel to server to stop generation
                        await ws.send(evt("response.cancel"))
                        continue

                    # --- Native Audio Output ---
                    if t == "response.audio.delta":
                        # Decode base64 PCM and send to speaker
                        delta_b64 = data.get("delta", "")
                        if delta_b64:
                            audio_bytes = base64.b64decode(delta_b64)
                            spk.play(audio_bytes)
                        continue

                    # --- Logging Transcripts ---
                    if t == "response.audio_transcript.delta":
                        print(data.get("delta"), end="", flush=True)
                    
                    if t == "response.output_item.done":
                        # Newline after response finishes
                        print("\n")
                        face.set_state("idle")

                    if t == "error":
                        print("Error:", data)

            except asyncio.CancelledError:
                pass

        send_task = asyncio.create_task(sender())
        recv_task = asyncio.create_task(receiver())

        try:
            await asyncio.gather(send_task, recv_task, vision_task)
        except Exception as e:
            print(f"Session Error: {e}")
        finally:
            send_task.cancel()
            recv_task.cancel()
            vision_task.cancel()
            mic.close()
            spk.close()
            face.stop()

def run_voice_assistant():
    try:
        asyncio.run(run_session())
    except KeyboardInterrupt:
        print("\nShutting down...")

if __name__ == "__main__":
    run_voice_assistant()