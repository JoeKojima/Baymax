"""Realtime speech-to-speech voice assistant (WebSocket) with scratchpad logging."""
import os
import json
import base64
import asyncio
import websockets
from dotenv import load_dotenv

from audio_processing import MicrophoneStream, SpeakerStream, SAMPLE_RATE
from agent import INSTRUCTIONS, parse_agent_response
from scratchpad import load_scratchpad, save_scratchpad, create_entry, initialize_scratchpad

# ---- config ----
REALTIME_MODEL = "gpt-realtime"
OPENAI_REALTIME_WS = f"wss://api.openai.com/v1/realtime?model={REALTIME_MODEL}"

load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")


def evt(type_: str, **kwargs) -> str:
    payload = {"type": type_}
    payload.update(kwargs)
    return json.dumps(payload)


async def run_session():
    initialize_scratchpad()
    scratch = load_scratchpad()

    mic = MicrophoneStream()   # yields PCM16 mono frames (~30 ms) at 16 kHz
    spk = SpeakerStream()

    headers = [
        ("Authorization", f"Bearer {OPENAI_API_KEY}"),
        ("OpenAI-Beta", "realtime=v1"),
    ]

    async with websockets.connect(
        OPENAI_REALTIME_WS, extra_headers=headers, ping_interval=20
    ) as ws:
        print("Connected to Realtime. Speak anytime. Ctrl+C to exit.")

        # --- session configuration ---
        # Use server VAD for speech detection/commit, but WE create the response.
        await ws.send(evt(
            "session.update",
            session={
                "instructions": INSTRUCTIONS,
                "modalities": ["audio", "text"],
                "voice": "verse",
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.30,
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 600,
                    "create_response": False
                },
                "input_audio_transcription": {
                    "model": "whisper-1"
                }
            }
        ))

        # ---- shared state ----
        text_buffer = []
        active_response = False

        async def sender():
            """Only append audio frames. No manual commit; server VAD decides."""
            try:
                sent = 0
                for frame in mic:
                    if not frame:
                        await asyncio.sleep(0.0)
                        continue
                    b64 = base64.b64encode(frame).decode("ascii")
                    await ws.send(evt("input_audio_buffer.append", audio=b64))
                    sent += 1
                    if sent % 50 == 0:
                        print(f"[I/O] pushed {sent} frames")
                    await asyncio.sleep(0.0)
            except asyncio.CancelledError:
                pass

        async def receiver():
            nonlocal text_buffer, active_response
            try:
                async for msg in ws:
                    data = json.loads(msg)
                    t = data.get("type", "")

                    # ---- VAD & commit debug ----
                    if t == "input_audio_buffer.speech_started":
                        print("[VAD] speech_started")
                        continue
                    if t == "input_audio_buffer.speech_stopped":
                        print("[VAD] speech_stopped")
                        continue
                    if t == "input_audio_buffer.committed":
                        print("[VAD] committed")
                        # Explicitly request AUDIO+TEXT and force a voice/format each turn
                        if not active_response:
                            await ws.send(evt("response.create", response={
                                "modalities": ["audio", "text"],
                                "audio": {
                                    "voice": "verse",
                                    "format": "pcm16"   # ensure PCM16 stream back
                                }
                            }))
                            active_response = True
                        continue

                    # ---- Response lifecycle ----
                    if t == "response.created":
                        active_response = True
                        print("[RT] response.created payload:", data)
                        continue

                    # Audio stream from the model (handle both event names)
                    if t in ("response.audio.delta", "response.output_audio.delta"):
                        chunk_b64 = data.get("audio", "")
                        if chunk_b64:
                            spk.play(base64.b64decode(chunk_b64))
                        else:
                            # one-time debug if payload shape differs
                            print("[RT] audio delta without 'audio' field:", data)
                        continue

                    # Various text delta names
                    if t in ("response.output_text.delta",
                             "response.text.delta",
                             "response.audio_transcript.delta"):
                        delta = data.get("delta", "")
                        if delta:
                            text_buffer.append(delta)
                        continue

                    # Completed (also handle 'done')
                    if t in ("response.completed", "response.done", "response.failed", "response.canceled"):
                        print(f"[RT] {t}")
                        full_text = "".join(text_buffer).strip()
                        text_buffer = []

                        if full_text:
                            try:
                                movement, say, motion = parse_agent_response(full_text)
                            except Exception:
                                movement, say, motion = False, full_text, "N/A"

                            user_text = "(voice input)"
                            entry = create_entry(user_text, movement, say, motion)
                            scratch.append(entry)
                            save_scratchpad(scratch)
                            print(f"\nYou: {user_text}")
                            print(f"Assistant (parsed): movement={movement}, say={say!r}, motion={motion!r}")

                        active_response = False
                        continue

                    if t == "error":
                        print("Realtime error:", data)
                        continue

                    # Uncomment to see everything else:
                    # print("[RT] event:", t, data)

            except asyncio.CancelledError:
                pass

        send_task = asyncio.create_task(sender())
        recv_task = asyncio.create_task(receiver())
        try:
            await asyncio.gather(send_task, recv_task)
        finally:
            send_task.cancel()
            recv_task.cancel()
            mic.close()
            spk.close()


def run_voice_assistant():
    try:
        asyncio.run(run_session())
    except KeyboardInterrupt:
        print("\nShutting down...")


if __name__ == "__main__":
    run_voice_assistant()