"""Record short voice clips for one person into clips/<name>/.

Usage:
    python record_clips.py --name stella
    python record_clips.py --name stella --n 8 --seconds 4

Each clip is 16 kHz mono WAV. The first 3 are saved as enroll_XX.wav (used to
build the voiceprint); the rest are test_XX.wav (used to check recognition).
"""
import argparse
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf

SR = 16000
SENTENCES = [
    "Hi Ember, it's me. How are you doing today?",
    "I had a pretty long day, but the weather was really nice outside.",
    "Can you remind me what we talked about yesterday afternoon?",
    "My favorite food is probably kimchi stew, especially when it's cold.",
    "I'm going to the library later to study for my exam.",
    "Ember, who am I?",
    "Could you play some quiet music while I read for a bit?",
    "Tomorrow I have class in the morning and a meeting at four thirty.",
    "I think I left my keys somewhere in the kitchen.",
    "Thanks Ember, talk to you later.",
]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--name", required=True, help="person's name, e.g. stella")
    p.add_argument("--n", type=int, default=8, help="number of clips")
    p.add_argument("--seconds", type=float, default=4.0, help="length of each clip")
    p.add_argument("--n-enroll", type=int, default=3, help="how many clips are enrollment")
    p.add_argument("--out", default="clips")
    args = p.parse_args()

    folder = Path(args.out) / args.name.lower()
    folder.mkdir(parents=True, exist_ok=True)
    print(f"Saving to {folder}/  (mic: {sd.query_devices(kind='input')['name']})\n")

    for i in range(args.n):
        kind = "enroll" if i < args.n_enroll else "test"
        sentence = SENTENCES[i % len(SENTENCES)]
        input(f"[{i + 1}/{args.n}] Press Enter, then read: \"{sentence}\"")
        audio = sd.rec(int(args.seconds * SR), samplerate=SR, channels=1, dtype="float32")
        sd.wait()
        audio = audio[:, 0]
        level = float(np.sqrt(np.mean(audio ** 2)))
        path = folder / f"{kind}_{i:02d}.wav"
        sf.write(path, audio, SR)
        warn = "  <-- very quiet, check your mic" if level < 0.005 else ""
        print(f"    saved {path.name}  (level {level:.4f}){warn}")
    print("\nDone.")


if __name__ == "__main__":
    main()
