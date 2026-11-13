"""Ask a question, then a second one while Gemini is still thinking; time the answer.

Compares BAYMAX_INTERRUPT_WHILE_THINKING=0 (NO_INTERRUPTION: Gemini finishes the
first reply) with =1 (START_OF_ACTIVITY_INTERRUPTS: new speech cancels it) against
the real Gemini Live model. The session's VAD and activity handling come from
Ember's own `processing.extend_config` with the robot's settings (enhancement on,
interruptions off); the prompt is the MVP persona plus the [LATEST REMARK] section.
Tools, memory and the address gate are left out.

It simulates the robot: espeak-ng speech sent as a 48 kHz mic in real-time 20 ms
chunks, reply audio "played" in real time, the mic muted (zeros) while Ember's
voice plays plus 0.25 s, and playback flushed on `interrupted`. Only synthetic
speech is sent; no robot audio, camera or memory leaves the machine.

    python evals/ask_again_latency.py                     # Q2 0.5 s after Q1, 3 runs per setting
    python evals/ask_again_latency.py --mode gaps --trials 3   # Q2 0.5 / 1.0 / 1.5 s after Q1
    python evals/ask_again_latency.py --mode single       # one question, latency baseline
    python evals/ask_again_latency.py --out results.json  # also keep every turn and event

Needs espeak-ng and GOOGLE_API_KEY (environment or .env). Results from 2026-11-13
are in features/conversational_ability/README.md ("Asking again while Gemini is
thinking").
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
RATE, CHUNK = 48000, 960            # 20 ms mic chunks; Ember's batches are ~21 ms
MUTE_TAIL_S = 0.25                  # Ember keeps the mic muted this long after its voice
Q1 = "Ember, what is the tallest mountain in Africa?"
Q2 = "Actually, never mind that. What is the capital of Japan?"
SINGLE = "Ember, what is the capital of Japan?"
Q1_WORDS, Q2_WORDS = ("kilimanjaro",), ("tokyo",)
SETTINGS = {"NO_INTERRUPTION": "0", "START_OF_ACTIVITY_INTERRUPTS": "1"}


def api_key():
    """From the environment, this checkout's .env or, in a git worktree, the main checkout's."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from live_eval import api_key as live_eval_key
    return live_eval_key(ROOT)


def speech(text):
    """espeak-ng speech resampled to 48 kHz int16."""
    with tempfile.NamedTemporaryFile(suffix=".wav") as f:
        subprocess.run(["espeak-ng", "-v", "en-us", "-s", "150", "-w", f.name, text], check=True)
        with wave.open(f.name) as w:
            src = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
            rate = w.getframerate()
    t = np.arange(int(len(src) * RATE / rate)) / RATE
    return (np.interp(t, np.arange(len(src)) / rate, src) * 0.8).astype(np.int16)


def config(handling):
    """Ember's realtime input settings for this activity handling, via its own code."""
    from features.conversational_ability import latest_reply, processing
    from features.persona import PERSONA
    os.environ.update(BAYMAX_AUDIO_ENHANCEMENT="1", BAYMAX_INTERRUPTION_ENABLED="0",
                      BAYMAX_INTERRUPT_WHILE_THINKING=SETTINGS[handling])
    os.environ.pop("BAYMAX_MUTE_MIC_DURING_REPLY", None)
    result = processing.extend_config({
        "response_modalities": ["AUDIO"],
        "input_audio_transcription": {}, "output_audio_transcription": {},
        "speech_config": {"voice_config": {"prebuilt_voice_config": {"voice_name": "Autonoe"}}},
        "thinking_config": {"thinking_budget": 0},
        "proactivity": {"proactive_audio": True},
        "system_instruction": PERSONA + latest_reply.INSTRUCTION,
    })
    actual = result["realtime_input_config"]["activity_handling"]
    if actual != handling:
        raise SystemExit(f"extend_config gave {actual}, expected {handling}")
    return result


async def trial(client, handling, q1, q2, gap_s):
    """One session. Mic: 0.5 s silence, Q1 (optional), gap, Q2, 14 s silence."""
    from google.genai import types
    lead = np.zeros(RATE // 2, np.int16)
    first = q1 if q1 is not None else np.zeros(0, np.int16)
    script = np.concatenate([lead, first, np.zeros(int(gap_s * RATE), np.int16), q2,
                             np.zeros(RATE * 14, np.int16)])
    q2_start = (len(lead) + len(first) + int(gap_s * RATE)) / RATE
    q2_end = q2_start + len(q2) / RATE
    turns, heard, events = [], [], []
    state = {"play_until": 0.0, "muted_q2": 0}
    t0 = 0.0

    def now():
        return time.perf_counter() - t0

    async with client.aio.live.connect(model=MODEL, config=config(handling)) as session:
        t0 = time.perf_counter()

        async def send():
            for i in range(0, len(script), CHUNK):
                due = i / RATE
                if due > now():
                    await asyncio.sleep(due - now())
                chunk = script[i:i + CHUNK]
                if now() < state["play_until"] + MUTE_TAIL_S:      # Ember's voice playing
                    if q2_start <= due < q2_end:
                        state["muted_q2"] += len(chunk)
                    chunk = np.zeros_like(chunk)
                await session.send_realtime_input(
                    audio=types.Blob(data=chunk.tobytes(), mime_type=f"audio/pcm;rate={RATE}"))

        async def receive():
            turn, early = None, ""   # A reply's transcript can arrive just before its audio.
            while True:
                async for message in session.receive():
                    content = message.server_content
                    if content is None:
                        continue
                    if content.input_transcription and content.input_transcription.text:
                        heard.append(content.input_transcription.text)
                    if content.interrupted:
                        events.append((round(now(), 2), "interrupted"))
                        if turn is not None:
                            turn["interrupted"] = round(now(), 2)
                        state["play_until"] = now()                  # Ember flushes playback
                        turn = None
                    for part in (content.model_turn.parts if content.model_turn else []):
                        if part.inline_data and part.inline_data.mime_type.startswith("audio/pcm"):
                            if turn is None:
                                turn = {"first_audio": round(now(), 2), "audio_s": 0.0, "text": early}
                                early = ""
                                turns.append(turn)
                            seconds = len(part.inline_data.data) / 2 / 24000
                            turn["audio_s"] += seconds
                            state["play_until"] = max(state["play_until"], now()) + seconds
                    if content.output_transcription and content.output_transcription.text:
                        if turn is not None:
                            turn["text"] += content.output_transcription.text
                        else:
                            early += content.output_transcription.text
                    if content.turn_complete:
                        events.append((round(now(), 2), "turn_complete"))
                        turn, early = None, ""

        receiver = asyncio.create_task(receive())
        await send()
        receiver.cancel()

    def says(turn, words):
        return any(word in turn["text"].lower() for word in words)

    q1_turn = next((t for t in turns if says(t, Q1_WORDS) and not says(t, Q2_WORDS)), None)
    q2_turn = next((t for t in turns if says(t, Q2_WORDS)), None)
    reply = q2_turn if q1 is not None else (turns[0] if turns else None)
    return {
        "handling": handling, "gap_s": gap_s, "q2_end": round(q2_end, 2),
        # Seconds from the end of the latest question to the first audio answering it.
        "latency": round(reply["first_audio"] - q2_end, 2) if reply else None,
        "q2_answered": q2_turn is not None,
        "q1_answered": q1_turn is not None,
        "q1_audio_arrived_s": round(q1_turn["audio_s"], 1) if q1_turn else 0.0,
        "q2_muted_s": round(state["muted_q2"] / RATE, 2),
        "heard": "".join(heard).strip(),
        "turns": [{k: round(v, 2) if isinstance(v, float) else v for k, v in t.items()} for t in turns],
        "events": events,
    }


async def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--mode", choices=("pair", "gaps", "single"), default="pair")
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--out", help="write every result, with turns and events, to this JSON file")
    args = parser.parse_args(argv)
    key = api_key()
    if not key:
        raise SystemExit("No GOOGLE_API_KEY in the environment or .env.")
    from google import genai
    client = genai.Client(api_key=key, http_options={"api_version": "v1alpha"})
    q1, q2 = speech(Q1), speech(Q2)
    if args.mode == "single":
        q2 = speech(SINGLE)
        cases = [(h, None, 0.0) for h in SETTINGS]
    else:
        gaps = (0.5, 1.0, 1.5) if args.mode == "gaps" else (0.5,)
        cases = [(h, q1, gap) for gap in gaps for h in SETTINGS]
    results = []
    for _ in range(args.trials):
        for handling, first, gap in cases:
            try:
                result = await trial(client, handling, first, q2, gap)
            except Exception as error:  # Keep going; record the failure.
                result = {"handling": handling, "gap_s": gap, "error": str(error)[:200]}
            results.append(result)
            print(json.dumps({k: result.get(k) for k in (
                "handling", "gap_s", "latency", "q2_answered", "q1_answered", "q1_audio_arrived_s",
                "q2_muted_s", "heard", "error")}), flush=True)
            await asyncio.sleep(1)
    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=1), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
