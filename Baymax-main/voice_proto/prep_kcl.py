"""Turn the MDVR-KCL dataset into speaker-ID test clips (no recording needed).

For each speaker: 3 enrollment clips from the read-text recording and
5 test clips from the spontaneous-dialogue recording (different session/task,
which is a fairer test than reusing the same recording).

Usage:
    python prep_kcl.py --data ../kcl_baseline/data/MDVR-KCL --out kcl_clips --max-speakers 10
"""
import argparse
from collections import defaultdict
from pathlib import Path

import numpy as np
import soundfile as sf

from identify import SR, load_audio


def chunks(audio, n, seconds, skip_start=1.0):
    """Take n evenly spaced chunks, skipping near-silent ones."""
    size = int(seconds * SR)
    start0 = int(skip_start * SR)
    if len(audio) < start0 + size:
        return []
    starts = np.linspace(start0, len(audio) - size, num=n * 3).astype(int)
    out = []
    for s in starts:
        c = audio[s:s + size]
        if np.sqrt(np.mean(c ** 2)) > 0.01:  # skip silence
            out.append(c)
        if len(out) == n:
            break
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data", required=True)
    p.add_argument("--out", default="kcl_clips")
    p.add_argument("--max-speakers", type=int, default=10)
    p.add_argument("--seconds", type=float, default=4.0)
    args = p.parse_args()

    files = defaultdict(dict)
    for f in Path(args.data).rglob("*.wav"):
        task = "read" if "read" in str(f).lower() else "dialogue"
        files[f.name.split("_")[0]][task] = f

    speakers = [s for s in sorted(files) if len(files[s]) == 2][: args.max_speakers]
    print(f"Using {len(speakers)} speakers with both recordings")
    for spk in speakers:
        enroll = chunks(load_audio(files[spk]["read"]), 3, args.seconds)
        test = chunks(load_audio(files[spk]["dialogue"]), 5, args.seconds)
        if len(enroll) < 3 or not test:
            print(f"  skip {spk}: not enough speech")
            continue
        folder = Path(args.out) / spk
        folder.mkdir(parents=True, exist_ok=True)
        for i, c in enumerate(enroll):
            sf.write(folder / f"enroll_{i:02d}.wav", c, SR)
        for i, c in enumerate(test):
            sf.write(folder / f"test_{i:02d}.wav", c, SR)
        print(f"  {spk}: {len(enroll)} enroll, {len(test)} test")


if __name__ == "__main__":
    main()
