"""Try voice_id.py on recorded clips: enroll everyone from their enroll_*.wav
files, then identify their test_*.wav files.

Usage (from voice_proto/):
    python try_voice_id.py --clips clips
    python try_voice_id.py --clips clips --reset     # start from an empty store

Uses a development people store (dev_people.json in this folder), never the
robot's ember_people.json.
"""
import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))          # so "import voice_id" finds Baymax-main/

import soundfile as sf                        # noqa: E402

import voice_id                               # noqa: E402
from dev_store import DevPeopleStore          # noqa: E402


def read(path):
    audio, sr = sf.read(str(path), dtype="float32")
    return audio, sr


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--clips", default="clips")
    p.add_argument("--store", default=str(HERE / "dev_people.json"))
    p.add_argument("--reset", action="store_true")
    args = p.parse_args()

    if args.reset and os.path.exists(args.store):
        os.remove(args.store)
    print("Loading voice model...")
    ids = voice_id.VoiceIdentifier(voice_id.VoiceModel(), DevPeopleStore(args.store))

    people = sorted(d for d in Path(args.clips).iterdir() if d.is_dir())
    print("\n== Enroll ==")
    for d in people:
        clips = [read(f) for f in sorted(d.glob("enroll*.wav"))]
        if not clips:
            continue
        sr = clips[0][1]
        result = ids.enroll(d.name.capitalize(), [a for a, _ in clips], sr, consent=True,
                            allow_new=True)
        print(f"{d.name:<12} {result}")

    print("\n== Identify ==")
    print(f"{'true':<12}{'file':<14}{'secs':>5}  {'result':<10}{'name':<12}{'score':>6}{'ms':>7}")
    right = wrong = unsure = 0
    for d in people:
        for f in sorted(d.glob("test*.wav")):
            audio, sr = read(f)
            for secs in (None, 1.5):     # whole clip, and a short ~"Ember, who am I?" length
                clip = audio if secs is None else audio[: int(secs * sr)]
                m = ids.identify(clip, sr)
                ok = m.name and m.name.lower() == d.name.lower()
                if m.level == "sure":
                    right += bool(ok)
                    wrong += not ok
                else:
                    unsure += 1
                flag = "" if m.level != "sure" or ok else "   <-- WRONG"
                print(f"{d.name:<12}{f.name:<14}{m.seconds:>5.1f}  {m.level:<10}"
                      f"{(m.name or '-'):<12}{m.score:>6.3f}{m.ms:>7.0f}{flag}")
    total = right + wrong + unsure
    print(f"\nSure & right: {right}/{total}   Sure & WRONG: {wrong}/{total}   "
          f"Maybe/unknown: {unsure}/{total}")
    print(f"\nexample message for Gemini: {m.describe()}")


if __name__ == "__main__":
    main()
