"""Offline speaker-identification prototype for Ember (Week 3, step 1).

Folder layout (one folder per person):
    clips/
      stella/  enroll_00.wav enroll_01.wav enroll_02.wav test_03.wav ...
      kelly/   ...

Files starting with "enroll" build the person's voiceprint (average embedding).
All other files are test clips. If a folder has no "enroll" files, the first
--n-enroll files (sorted) are used for enrollment.

For every test clip, at full length and cropped to 1 s / 2 s / 3 s, it prints:
  - who the model thinks it is (highest cosine score)
  - true_score: similarity to the correct person's voiceprint
  - best_wrong: highest similarity to anyone else's voiceprint
A good threshold sits above every best_wrong and below most true_scores.

Usage:
    python identify.py --clips clips
    python identify.py --clips kcl_clips --model ecapa
"""
import argparse
import csv
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from scipy.signal import resample_poly

SR = 16000
MODELS = {"ecapa": "speechbrain/spkrec-ecapa-voxceleb"}


def load_audio(path):
    """Read a WAV/FLAC file as 16 kHz mono float32 (no torchaudio needed)."""
    audio, sr = sf.read(str(path), dtype="float32", always_2d=True)
    audio = audio.mean(axis=1)  # stereo -> mono
    if sr != SR:
        g = np.gcd(int(sr), SR)
        audio = resample_poly(audio, SR // g, int(sr) // g).astype("float32")
    return audio


class Embedder:
    def __init__(self, model="ecapa"):
        from speechbrain.inference.speaker import EncoderClassifier
        from speechbrain.utils.fetching import LocalStrategy

        print(f"Loading {MODELS[model]} (first run downloads ~80 MB)...")
        self.model = EncoderClassifier.from_hparams(
            source=MODELS[model],
            savedir=f"pretrained_models/{model}",
            run_opts={"device": "cpu"},
            local_strategy=LocalStrategy.COPY,  # avoids symlink errors on Windows
        )
        self.model.eval()
        self.times_ms = []

    def __call__(self, audio):
        start = time.perf_counter()
        with torch.no_grad():
            emb = self.model.encode_batch(torch.from_numpy(audio).unsqueeze(0))
        self.times_ms.append((time.perf_counter() - start) * 1000)
        emb = emb.squeeze().cpu().numpy()
        return emb / np.linalg.norm(emb)  # unit length, so dot product = cosine


def split_files(folder, n_enroll):
    files = sorted(p for p in folder.iterdir() if p.suffix.lower() in (".wav", ".flac"))
    enroll = [p for p in files if p.name.startswith("enroll")]
    if enroll:
        return enroll, [p for p in files if p not in enroll]
    return files[:n_enroll], files[n_enroll:]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--clips", default="clips")
    p.add_argument("--model", default="ecapa", choices=MODELS)
    p.add_argument("--n-enroll", type=int, default=3)
    p.add_argument("--out", default="results.csv")
    args = p.parse_args()

    people = sorted(d for d in Path(args.clips).iterdir() if d.is_dir())
    if len(people) < 2:
        raise SystemExit("Need at least 2 people (folders) to test identification.")

    embed = Embedder(args.model)

    # 1. Enrollment: average each person's enrollment embeddings into a voiceprint.
    voiceprints, tests = {}, {}
    for d in people:
        enroll, test = split_files(d, args.n_enroll)
        if not enroll or not test:
            print(f"Skipping {d.name}: needs both enrollment and test clips")
            continue
        centroid = np.mean([embed(load_audio(f)) for f in enroll], axis=0)
        voiceprints[d.name] = centroid / np.linalg.norm(centroid)
        tests[d.name] = test
        print(f"Enrolled {d.name}: {len(enroll)} enroll clips, {len(test)} test clips")
    names = list(voiceprints)
    matrix = np.stack([voiceprints[n] for n in names])

    # 2. Identification on test clips, full length and cropped.
    rows = []
    for person, files in tests.items():
        for f in files:
            audio = load_audio(f)
            for crop in (None, 1.0, 2.0, 3.0):
                if crop is not None and len(audio) < crop * SR:
                    continue
                clip = audio if crop is None else audio[: int(crop * SR)]
                scores = matrix @ embed(clip)
                best = int(np.argmax(scores))
                true_score = float(scores[names.index(person)])
                best_wrong = float(max(s for n, s in zip(names, scores) if n != person))
                rows.append({
                    "person": person, "file": f.name,
                    "length": "full" if crop is None else f"{crop:.0f}s",
                    "predicted": names[best], "correct": names[best] == person,
                    "true_score": round(true_score, 3), "best_wrong": round(best_wrong, 3),
                })

    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)

    # 3. Summary.
    print(f"\n{'length':<7}{'accuracy':>10}{'min true':>10}{'max wrong':>11}   separable?")
    for length in ("full", "3s", "2s", "1s"):
        r = [x for x in rows if x["length"] == length]
        if not r:
            continue
        acc = np.mean([x["correct"] for x in r])
        lo = min(x["true_score"] for x in r)
        hi = max(x["best_wrong"] for x in r)
        sep = "yes" if lo > hi else "no (overlap)"
        print(f"{length:<7}{acc:>10.0%}{lo:>10.3f}{hi:>11.3f}   {sep}")
    t = np.array(embed.times_ms[1:])  # skip first call (warm-up)
    print(f"\nEmbedding time on this computer: median {np.median(t):.0f} ms, "
          f"max {t.max():.0f} ms per clip")
    print(f"Per-clip details saved to {args.out}")
    print("\nThreshold hint: 'sure' cutoff should be above the max wrong score; "
          "'might be' zone sits just below it.")


if __name__ == "__main__":
    main()
