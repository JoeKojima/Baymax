"""Realtime speech-to-speech voice assistant (WebSocket) with scratchpad logging."""
import os
import json
import base64
import asyncio
import websockets
from dotenv import load_dotenv

from openai import OpenAI  # for REST TTS

from audio_processing import MicrophoneStream, SpeakerStream
from agent import INSTRUCTIONS as BASE_INSTRUCTIONS, parse_agent_response
from scratchpad import (
    load_scratchpad,
    save_scratchpad,
    create_entry,
    initialize_scratchpad,
)v

# ---- config ----
REALTIME_MODEL = "gpt-realtime"
OPENAI_REALTIME_WS = f"wss://api.openai.com/v1/realtime?model={REALTIME_MODEL}"

# pick any built-in voice that your account supports
SESSION_VOICE = "vers

# TTS config
TTS_MODEL = "gpt-4o-mini-tts"   
TTS_VOICE = "verse"
# we'll request raw pcm from TTS, which is 24 kHz by default → matches your SpeakerStream

load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# REST client for TTS
tts_client = OpenAI(api_key=OPENAI_API_KEY)


def evt(type_: str, **kwargs) -> str:
    payload = {"type": type_}
    payload.update(kwargs)
    return json.dumps(payload)


async def run_session():
    # scratchpad
    initialize_scratchpad()
    scratch = load_scratchpad()

    # audio I/O
    mic = MicrophoneStream()   # 16k/24k, mono, pcm16, ~30 ms
    spk = SpeakerStream()      # 24k, mono, pcm16

    # connect
    headers = [
        ("Authorization", f"Bearer {OPENAI_API_KEY}"),
        ("OpenAI-Beta", "realtime=v1"),
    ]

    # extra rule so the TEXT stays in your 3-part format
    realtime_instructions = (
        BASE_INSTRUCTIONS
        + "\n\n"
        "Return EXACTLY 3 parts separated by %,% (boolean, verbal output, motion plan)."
    )

    async with websockets.connect(
        OPENAI_REALTIME_WS, extra_headers=headers, ping_interval=20
    ) as ws:
        print("Connected to Realtime. Speak anytime. Ctrl+C to exit.")

        # 1) configure session
        await ws.send(
            evt(
                "session.update",
                session={
                    "instructions": realtime_instructions,
                    "modalities": ["audio", "text"],
                    "voice": SESSION_VOICE,
                    # server requires one of these → keep pcm16
                    "input_audio_format": "pcm16",
                    "output_audio_format": "pcm16",
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.30,
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": 600,
                        "create_response": False,  # WE create responses
                    },
                    "input_audio_transcription": {
                        "model": "whisper-1",
                    },
                },
            )
        )

        # shared state
        text_buffer: list[str] = []
        awaiting_text = False   # we have asked for a text response and are waiting

        async def sender():
            """Stream mic → Realtime; server VAD will commit."""
            try:
                for frame in mic:
                    if not frame:
                        await asyncio.sleep(0.0)
                        continue
                    b64 = base64.b64encode(frame).decode("ascii")
                    await ws.send(evt("input_audio_buffer.append", audio=b64))
                    await asyncio.sleep(0.0)
            except asyncio.CancelledError:
                pass

        async def receiver():
            nonlocal text_buffer, awaiting_text
            try:
                async for raw in ws:
                    data = json.loads(raw)
                    t = data.get("type", "")

                    # --- VAD events ---
                    if t == "input_audio_buffer.speech_started":
                        continue
                    if t == "input_audio_buffer.speech_stopped":
                        continue
                    if t == "input_audio_buffer.committed":
                        print("[VAD] committed")
                        # after VAD commit → ask for TEXT ONLY
                        if not awaiting_text:
                            await ws.send(
                                evt(
                                    "response.create",
                                    response={
                                        "modalities": ["text"],  # <--- text only
                                    },
                                )
                            )
                            awaiting_text = True
                        continue

                    # --- text deltas ---
                    if t in (
                        "response.output_text.delta",
                        "response.text.delta",
                        "response.audio_transcript.delta",
                    ):
                        delta = data.get("delta", "")
                        if delta:
                            text_buffer.append(delta)
                        continue

                    # --- audio deltas from server (we ignore them)
                    # the server may still send them because output_audio_format=pcm16
                    if t in ("response.audio.delta", "response.output_audio.delta"):
                        # IGNORE → we are doing our own TTS
                        continue

                    # --- response finished (text) ---
                    if t in (
                        "response.completed",
                        "response.done",
                        "response.failed",
                        "response.canceled",
                    ):
                        if awaiting_text:
                            full_text = "".join(text_buffer).strip()
                            text_buffer = []
                            awaiting_text = False

                            say = None
                            movement = False
                            motion = "N/A"

                            if full_text:
                                # parse into (movement, verbal output, motion)
                                try:
                                    movement, say, motion = parse_agent_response(full_text)
                                except Exception:
                                    # fall back to whatever the model said
                                    say = full_text
                                    movement = False
                                    motion = "N/A"

                                # scratchpad log
                                user_text = "(voice input)"
                                entry = create_entry(
                                    user_text,
                                    movement,
                                    say or "",
                                    motion or "N/A",
                                )
                                scratch.append(entry)
                                save_scratchpad(scratch)
                                print(f"\nYou: {user_text}")
                                print(
                                    f"Assistant (parsed): movement={movement}, say={say!r}, motion={motion!r}"
                                )

                            # ---- local TTS: speak ONLY the verbal output ----
                            if say:
                                try:
                                    speech = tts_client.audio.speech.create(
                                        model=TTS_MODEL,
                                        voice=TTS_VOICE,
                                        input=say,
                                        response_format="pcm",   # raw 24 kHz PCM
                                    )
                                    # in the current SDK this returns a streaming-like object
                                    audio_bytes = speech.read()
                                    spk.play(audio_bytes)
                                except Exception as e:
                                    print("[TTS] error:", e)

                        continue

                    if t == "error":
                        print("Realtime error:", data)
                        continue

                    # uncomment to debug all events:
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