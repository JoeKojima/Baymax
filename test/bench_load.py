"""
How much load does people-awareness add? Run this ON THE ROBOT to get real
numbers (it uses only what's already installed there):

    EMBER_FACE_TEST_DATA=/path/to/lfw_funneled python3 test/bench_load.py

It measures, with the real models:
  1. face recognition per camera frame (0–3 faces), and as CPU % at the 2 fps
     the AI core runs it — next to the pose model it already runs at 15 fps;
  2. memory recall per utterance (embedding + search) and the other memory
     operations at 100 / 1,000 / 10,000 stored memories;
  3. the people file (matching, saving);
  4. how much the prompt and live status messages add for Gemini;
  5. the face thread running for real for 30 s: CPU and RAM;
  6. whether it delays audio: how late a 21 ms "audio callback" and the
     asyncio loop wake up, without and with the face thread (3 faces).

Run it with the robot otherwise idle (stop the Ember service first), or
the timings will include whatever else is running.

CPU % is "% of one core" (100% = one core fully busy). Without
EMBER_FACE_TEST_DATA the face parts use a synthetic frame with no faces.
"""
import glob
import os
import resource
import shutil
import statistics
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BAYMAX = os.path.join(HERE, "..", "Baymax-main")
sys.path.insert(0, BAYMAX)
sys.path.insert(0, HERE)

import numpy as np  # noqa: E402
import cv2  # noqa: E402
cv2.setNumThreads(1)        # as the AI core runs it

import face_id  # noqa: E402
import person_memory  # noqa: E402
import ember_self  # noqa: E402

DATA = os.getenv("EMBER_FACE_TEST_DATA")
FALL_DETECTION_FPS = 15     # what realtime_gemini_10 runs pose at
FACE_ID_FPS = 2


def rss_mb():
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024 if sys.platform == "darwin" else 1024)   # peak (macOS)


def cpu_ms(fn, repeat):
    """Median CPU ms (all threads) and wall ms per call."""
    fn()                                     # warm-up
    cpu, wall = [], []
    for _ in range(repeat):
        c0, w0 = time.process_time(), time.perf_counter()
        fn()
        cpu.append((time.process_time() - c0) * 1000)
        wall.append((time.perf_counter() - w0) * 1000)
    return statistics.median(cpu), statistics.median(wall)


def frames(models=None, store=None):
    """Robot-like 640x480 frames with 0..3 faces at ~1.5 m (from real photos).
    With a store, those people are enrolled from other photos of them."""
    if not DATA:
        return {0: np.full((480, 640, 3), 90, np.uint8)}
    import test_face_models as T
    T.DATA = DATA
    people = [(name, photos) for name, photos in T._people(8)][:3]
    if store is not None:
        for name, photos in people:
            embs = [e for e in (face_id.embeddings_from_photo(models, open(p, "rb").read())[0]
                                for p in photos[1:7]) if e is not None]
            store.add_samples(name, embs, source="app")
    return {n: T.robot_frame([p[0] for _, p in people[:n]], face_px=90) for n in range(4)}


def row(label, value):
    print(f"  {label:<52} {value}")


def main():
    print(f"Python {sys.version.split()[0]}, OpenCV {cv2.__version__}, "
          f"{os.cpu_count()} CPUs, OpenCV threads {cv2.getNumThreads()}")
    base_rss = rss_mb()

    # ── 1. Face recognition per frame ────────────────────────────────────────
    print("\n1. Camera frame (640x480)")
    models = face_id.FaceModels(face_id.ensure_models())
    rss_models = rss_mb()
    tmp = tempfile.mkdtemp()
    store = face_id.PeopleStore(os.path.join(tmp, "people.json"))
    rng = np.random.default_rng(0)
    for i in range(30):                      # a household plus visitors
        store.add_samples(f"Person {i}", [face_id._normalize(rng.normal(size=128))
                                          for _ in range(face_id.MAX_SAMPLES)], source="app")
    ident = face_id.FaceIdentifier(models, store, profile={"user_name": "Person 0"})
    fr = frames(models, store)
    face_cost = {}
    for n, frame in fr.items():
        c, w = cpu_ms(lambda: ident.process(frame), 40)
        face_cost[n] = c
        row(f"face recognition, {n} face(s)", f"{c:6.1f} ms CPU  ({w:5.1f} ms wall)")

    pose_cost = None
    pose_path = os.path.join(BAYMAX, "pose_landmarker_full.task")
    try:
        import mediapipe as mp
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision
        opts = vision.PoseLandmarkerOptions(
            base_options=mp_python.BaseOptions(model_asset_path=pose_path),
            running_mode=vision.RunningMode.VIDEO, num_poses=1)
        landmarker = vision.PoseLandmarker.create_from_options(opts)
        ts = [0]
        frame = fr[max(fr)]
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        def pose():
            ts[0] += 66
            landmarker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), ts[0])
        pose_cost, w = cpu_ms(pose, 40)
        row("pose (already running, for comparison)", f"{pose_cost:6.1f} ms CPU  ({w:5.1f} ms wall)")
    except Exception as e:
        row("pose (already running, for comparison)", f"not measured ({e})")

    print(f"\n   Average load at {FACE_ID_FPS} fps, by situation (% of one core):")
    def load(frame, seconds=60):
        """CPU per second of camera time, following the robot's real logic."""
        clock = [1e6]
        ident = face_id.FaceIdentifier(models, store, clock=lambda: clock[0], wall=time.time,
                                       profile={"user_name": "Person 0"})
        c0 = time.process_time()
        for _ in range(int(seconds * FACE_ID_FPS)):
            ident.process(frame)
            clock[0] += 1 / FACE_ID_FPS
        return (time.process_time() - c0) / seconds * 100
    def old_design(frame):
        # before: detect at full size and identify every face, every frame
        c0 = time.process_time()
        for _ in range(20):
            for d in models.detect(frame):
                if d.size >= face_id.MIN_FACE_PX:
                    models.embed(frame, d)
        return (time.process_time() - c0) / 20 * FACE_ID_FPS * 100
    probe = cpu_ms(lambda: models.detect(fr[0], scale=face_id.DETECT_SCALE), 20)[0]
    row("empty room (a quick look every 2 s)",
        f"{probe / 2 / 10:5.1f}%   (before: {old_design(fr[0]):5.1f}%)")
    for n in sorted(k for k in fr if k):
        row(f"{n} known face(s) sitting still", f"{load(fr[n]):5.1f}%   (before: {old_design(fr[n]):5.1f}%)")
    # worst case: faces that keep changing (moving, new people) are identified every frame
    moving = [fr[max(fr)], np.roll(fr[max(fr)], 40, axis=1)]
    clock = [1e6]
    ident2 = face_id.FaceIdentifier(models, store, clock=lambda: clock[0], wall=time.time, profile={})
    c0 = time.process_time()
    for i in range(40):
        ident2.process(moving[i % 2])
        clock[0] += 1 / FACE_ID_FPS
    row(f"{max(fr)} faces all moving (worst case)",
        f"{(time.process_time() - c0) / 20 * 100:5.1f}%   (before: {old_design(fr[max(fr)]):5.1f}%)")
    if pose_cost:
        row(f"pose @ {FALL_DETECTION_FPS} fps (existing, for comparison)",
            f"{pose_cost * FALL_DETECTION_FPS / 10:5.1f}%")
    row("RAM for the face models", f"{rss_models - base_rss:5.0f} MB")

    # ── 2. Memory ────────────────────────────────────────────────────────────
    print("\n2. Memory (real all-MiniLM-L6-v2 + ChromaDB)")
    model_dir = os.path.join(BAYMAX, "all-MiniLM-L6-v2-onnx")
    if not os.path.exists(os.path.join(model_dir, "model.onnx")):
        row("skipped", "no embedding model (python3 convert_model.py)")
    else:
        from semantic_embedder import SemanticEmbedder
        before = rss_mb()
        db = SemanticEmbedder(model_dir, verbose=False, chroma_dir=os.path.join(tmp, "chroma"),
                              collection_name="bench")
        row("RAM for the embedding model + store (existed before)", f"{rss_mb() - before:5.0f} MB")
        profile = {"user_name": "Margaret", "household": [{"name": "Sarah", "relationship": "daughter"}]}
        names = ["Margaret", "Sarah", "Tom", "Dave", "Ruth"]
        have = 0
        for size in (100, 1000, 10000):
            batch = []
            while have < size:
                who = [names[have % 5], names[(have * 7) % 5]]
                batch.append((f"Memory {have}: {who[0]} talked about the garden, item {have}.", who))
                have += 1
            for i in range(0, len(batch), 200):
                chunk = batch[i:i + 200]
                db.save([t for t, _ in chunk],
                        ids=[f"m{have - len(batch) + i + j}" for j in range(len(chunk))],
                        metadatas=[person_memory._metadata(w, time.time()) for _, w in chunk])
            print(f"   {size:,} memories:")
            c, w = cpu_ms(lambda: db.search("how is the garden doing", n_results=5), 15)
            row("  recall while someone talks (embed + search)", f"{c:6.1f} ms CPU  ({w:5.1f} ms wall)")
            c, w = cpu_ms(lambda: person_memory.memories_involving(db, "Sarah", profile, limit=5), 5)
            row("  memories about a person (greeting / opener)", f"{c:6.1f} ms CPU  ({w:5.1f} ms wall)")
            c, w = cpu_ms(lambda: person_memory.memory_block(db, profile, focus=["Sarah"]), 5)
            row("  prompt memory list (once per new session)", f"{c:6.1f} ms CPU  ({w:5.1f} ms wall)")
        disk = sum(os.path.getsize(p) for p in glob.glob(os.path.join(tmp, "chroma", "**"), recursive=True)
                   if os.path.isfile(p))
        row("disk for 10,000 memories", f"{disk / 1e6:5.1f} MB")

    # ── 3. People file ───────────────────────────────────────────────────────
    print("\n3. People file (30 people x 20 samples)")
    emb = face_id._normalize(rng.normal(size=128))
    c, w = cpu_ms(lambda: store.match(emb), 200)
    row("match one face", f"{c * 1000:6.0f} µs CPU")
    c, w = cpu_ms(lambda: store.reload_if_changed(), 200)
    row("check for dashboard changes (every frame)", f"{c * 1000:6.0f} µs CPU")
    pid = store.people()[0]["id"]
    c, w = cpu_ms(lambda: store.touch({pid: time.time()}), 20)
    row("save (locked write + fsync)", f"{c:6.1f} ms CPU  ({w:5.1f} ms wall)")
    row("file size", f"{os.path.getsize(store.path) / 1e3:5.0f} kB")
    row("saves per hour while someone is in view",
        f"~{3600 // face_id.AUTO_LEARN_INTERVAL + 3600 // face_id.LAST_SEEN_SAVE_INTERVAL:.0f} "
        "(learned look + last-seen), + a few for appearance")

    # ── 4. Gemini ────────────────────────────────────────────────────────────
    print("\n4. Gemini (text added; ~4 characters per token)")
    profile = {"user_name": "Margaret", "about": "Retired teacher, loves her garden.",
               "household": [{"name": "Sarah", "relationship": "daughter", "notes": "visits Sundays"}]}
    caps = [ember_self.Capability("Notice falls and call for help.")]
    identity = ember_self.build_identity(caps, profile)
    row("identity + people rules (once per session)",
        f"~{(len(identity) + len(face_id.PEOPLE_INSTRUCTIONS)) // 4:,} tokens")
    if os.path.exists(os.path.join(model_dir, "model.onnx")):
        block = person_memory.memory_block(db, profile, limit=50, focus=["Sarah"])
        row("memory list in the prompt (50 newest + who's here)", f"~{len(block) // 4:,} tokens")
    state = ember_self.SelfState(debounce=0, min_interval=0, refresh_interval=3600)
    state.set("in_view", "Margaret and Sarah (Margaret's daughter) are in front of you.")
    state.set("visits", "Sarah just arrived; Margaret has been here for about 2 hours.")
    update = state.pending_update() or ""
    row("one [SELF] status update", f"~{len(update) // 4} tokens")
    row("Flash calls (text)", "1 per conversation (summary); 1 per shared memory on a 'forget'")
    row("Flash calls (with a photo)", "≤ 1 per person per 2 h they're around (how they look)")

    # ── 5. The face thread for real ──────────────────────────────────────────
    print(f"\n5. Face thread at {FACE_ID_FPS} fps for 30 s, one face in view")
    frame = fr.get(1, fr[0])
    stop = threading.Event()
    def face_thread():
        while not stop.is_set():
            t0 = time.monotonic()
            ident.process(frame)
            time.sleep(max(0.0, 1 / FACE_ID_FPS - (time.monotonic() - t0)))
    r0, c0, w0 = rss_mb(), time.process_time(), time.perf_counter()
    t = threading.Thread(target=face_thread, daemon=True)
    t.start()
    time.sleep(30)
    stop.set()
    t.join()
    load = (time.process_time() - c0) / (time.perf_counter() - w0) * 100
    row("CPU, averaged", f"{load:5.1f}% of one core")
    row("RAM change over 30 s", f"{rss_mb() - r0:+5.1f} MB")

    # ── 6. Does it delay audio? ──────────────────────────────────────────────
    print("\n6. Audio timing (how late each wake-up is; p50 / p99 / worst)")
    busy = fr[max(fr)]
    for with_faces in (False, True):
        cb, lp = jitter(ident, busy if with_faces else None)
        row(f"{'with' if with_faces else 'without'} face thread — audio callback",
            "%5.1f / %5.1f / %5.1f ms" % cb)
        row(f"{'with' if with_faces else 'without'} face thread — event loop",
            "%5.1f / %5.1f / %5.1f ms" % lp)
    shutil.rmtree(tmp, ignore_errors=True)


def jitter(ident, frame, seconds=20):
    """Lateness of a thread waking every 1024 samples @ 48 kHz (like the
    speaker callback) and of the asyncio loop, while the face thread runs."""
    import asyncio
    stop = threading.Event()
    cb_late, loop_late = [], []

    def faces():
        while not stop.is_set():
            t0 = time.monotonic()
            ident.process(frame)
            time.sleep(max(0.0, 1 / FACE_ID_FPS - (time.monotonic() - t0)))

    def audio():
        period = 1024 / 48000
        nxt = time.perf_counter() + period
        while not stop.is_set():
            time.sleep(max(0.0, nxt - time.perf_counter()))
            cb_late.append((time.perf_counter() - nxt) * 1000)
            np.repeat(np.zeros(1024, np.int16), 2)
            nxt += period

    async def loop():
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            t0 = time.perf_counter()
            await asyncio.sleep(0.01)
            loop_late.append((time.perf_counter() - t0 - 0.01) * 1000)

    threads = [threading.Thread(target=audio, daemon=True)]
    if frame is not None:
        threads.append(threading.Thread(target=faces, daemon=True))
    for t in threads:
        t.start()
    asyncio.run(loop())
    stop.set()
    for t in threads:
        t.join()
    q = lambda xs: (statistics.median(xs), float(np.percentile(xs, 99)), max(xs))
    return q(cb_late), q(loop_late)


if __name__ == "__main__":
    main()
