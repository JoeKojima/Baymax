"""
realtime_gemini_9_stat_test.py — instrumented harness around realtime_gemini_9.py

Does NOT modify realtime_gemini_9.py. It imports it as a module, reproduces the
real __main__ startup sequence, and measures CPU and RAM per component while the
whole system runs for real — including the live Gemini Live session.

Attribution is exact, not inferred:
  * per-THREAD CPU comes from /proc/self/task/<tid>/stat (utime+stime, jiffies).
    Every component that owns a thread (fall detection, MJPEG, audio writer,
    cloud uplink, PortAudio) is measured directly.
  * per-ASYNCIO-TASK CPU comes from a custom Task subclass that accumulates
    time.thread_time() across each step, so the coroutines sharing the main
    thread (send_audio, receive_audio, send_video, initiation, ...) are split
    apart instead of lumped together.
  * RAM is sampled as process RSS, with a milestone recorded after each
    subsystem loads so per-subsystem deltas are attributable.

Usage:
    python3 realtime_gemini_9_stat_test.py [seconds] [--gpu] [--no-record]

    seconds      how long to run the live session (default 180)
    --gpu        force the MediaPipe GPU delegate (default: as v9 ships, CPU)
    --no-record  disable the WAV writer thread (no room audio hits disk)
"""
import os
import sys
import time
import json
import glob
import queue
import asyncio
import threading
import collections

os.environ.setdefault("EGL_PLATFORM", "surfaceless")

DURATION = 180
USE_GPU = "--gpu" in sys.argv
NO_RECORD = "--no-record" in sys.argv
USE_PERSON = "--person" in sys.argv
FIRE_FALL = "--fall" in sys.argv
MEM_SEARCH = "--memsearch" in sys.argv
FALL_INTERVAL = 60.0
MEM_INTERVAL = 10.0
TALK = "--talk" in sys.argv
TALK_INTERVAL = 40.0
PERSON_JPG = "/home/meowmax/.claude/jobs/eed0dae6/tmp/person.jpg"
for a in sys.argv[1:]:
    if a.isdigit():
        DURATION = int(a)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)

import psutil

PROC = psutil.Process()
TICKS = os.sysconf("SC_CLK_TCK")


def rss_mb():
    return PROC.memory_info().rss / 1e6


MILESTONES = []


def milestone(label):
    MILESTONES.append((label, rss_mb()))
    print(f"[STAT] milestone {label:<28s} RSS {rss_mb():8.0f} MB", flush=True)


milestone("interpreter+psutil")

# ─── Import the real module (module-level side effects run here) ─────────────
print("[STAT] importing realtime_gemini_9 ...", flush=True)
import realtime_gemini_9 as R
milestone("import realtime_gemini_9")

# ─── Optional: force GPU delegate without touching v9 ───────────────────────
if USE_GPU:
    _orig_base_options = R.mp_tasks.BaseOptions

    def _gpu_base_options(*args, **kw):
        kw.setdefault("delegate", _orig_base_options.Delegate.GPU)
        return _orig_base_options(*args, **kw)

    _gpu_base_options.Delegate = _orig_base_options.Delegate
    R.mp_tasks.BaseOptions = _gpu_base_options
    print("[STAT] GPU delegate forced for pose inference", flush=True)

# ─── Count pose frames: _update_face_visibility runs once per fall-loop frame ─
_pose_frames = collections.Counter()
_orig_face = R._update_face_visibility


def _counting_face(landmarks):
    _pose_frames["n"] += 1
    if landmarks:
        _pose_frames["with_person"] += 1
    return _orig_face(landmarks)


R._update_face_visibility = _counting_face

# ─── Count mic blocks reaching the queue ────────────────────────────────────
_mic_blocks = collections.Counter()
_orig_qput = R.audio_queue_mic.put_nowait


def _counting_qput(item):
    _mic_blocks["n"] += 1
    return _orig_qput(item)


R.audio_queue_mic.put_nowait = _counting_qput

# ─── Optional: synthetic person in frame (forces the full landmark path) ────
if USE_PERSON:
    import numpy as _np

    _person = R.cv2.imread(PERSON_JPG)
    if _person is None:
        raise SystemExit(f"[STAT] --person: cannot read {PERSON_JPG}")
    _person = R.cv2.resize(_person, (640, 480))
    print(f"[STAT] --person: injecting {PERSON_JPG} as the camera feed", flush=True)

    class _FakeCap:
        """Minimal cv2.VideoCapture stand-in: isOpened/read/set/get/release."""

        def __init__(self, idx=0, *a, **kw):
            self._ok = (idx == 0)
            self._n = 0

        def isOpened(self):
            return self._ok

        def read(self):
            if not self._ok:
                return False, None
            # Jitter the frame a few px so MediaPipe cannot coast on a frozen
            # image; this keeps the tracker doing real work every frame.
            self._n += 1
            dx = (self._n % 7) - 3
            dy = (self._n % 5) - 2
            return True, _np.roll(_np.roll(_person, dx, axis=1), dy, axis=0)

        def set(self, *a, **kw):
            return True

        def get(self, *a, **kw):
            return 0.0

        def release(self):
            pass

    R.cv2.VideoCapture = _FakeCap

# ─── Optional: drive a real conversation so receive_audio/playback are used ─
_session_box = {}
if TALK:
    _orig_send_audio = R.send_audio_realtime

    async def _capturing_send_audio(session):
        _session_box["s"] = session
        return await _orig_send_audio(session)

    R.send_audio_realtime = _capturing_send_audio
    print(f"[STAT] --talk: will prompt Gemini every {TALK_INTERVAL:.0f}s", flush=True)

_TALK_PROMPTS = [
    "Hi Baymax, how are you doing today? Please answer in two or three sentences.",
    "Can you tell me a short story about a robot who loves gardening?",
    "What are three simple things someone can do to feel better after a long day?",
    "Describe what you can see and hear right now, in a few sentences.",
]

_talk_stats = collections.Counter()


_MEM_QUERIES = [
    "how are you feeling today",
    "what did we talk about yesterday",
    "tell me about your family",
    "do I have any appointments coming up",
    "what medication am I taking",
]
_feature_stats = collections.Counter()
_mem_latency = []


def _fall_driver():
    """Set the fall event on a fixed cadence so the alert path really runs."""
    while not R._shutdown_event.is_set():
        for _ in range(int(FALL_INTERVAL * 10)):
            if R._shutdown_event.is_set():
                return
            time.sleep(0.1)
        R._fall_detected_event.set()
        _feature_stats["falls_fired"] += 1
        print(f"[STAT] --fall: event #{_feature_stats['falls_fired']} set", flush=True)


def _mem_driver():
    """Query the memory store during the live session."""
    i = 0
    while not R._shutdown_event.is_set():
        for _ in range(int(MEM_INTERVAL * 10)):
            if R._shutdown_event.is_set():
                return
            time.sleep(0.1)
        emb = R._memory_embedder
        if emb is None:
            continue
        t0 = time.perf_counter()
        try:
            emb.search(_MEM_QUERIES[i % len(_MEM_QUERIES)], n_results=3)
            _mem_latency.append((time.perf_counter() - t0) * 1000)
            _feature_stats["mem_searches"] += 1
        except Exception as e:
            print(f"[STAT] --memsearch failed: {type(e).__name__}: {e}", flush=True)
        i += 1


async def _talk_driver():
    """Sends real text turns into the live session on a fixed cadence."""
    i = 0
    while True:
        await asyncio.sleep(TALK_INTERVAL)
        s = _session_box.get("s")
        if s is None:
            continue
        try:
            await s.send_client_content(
                turns={"role": "user",
                       "parts": [{"text": _TALK_PROMPTS[i % len(_TALK_PROMPTS)]}]},
                turn_complete=True,
            )
            _talk_stats["sent"] += 1
            print(f"[STAT] --talk: prompt #{_talk_stats['sent']} sent", flush=True)
        except Exception as e:
            print(f"[STAT] --talk: send failed {type(e).__name__}: {e}", flush=True)
        i += 1

# ─── Per-asyncio-task CPU accounting ────────────────────────────────────────
TASK_CPU = collections.defaultdict(float)
TASK_STEPS = collections.Counter()
_task_factory_ok = True

try:
    _PyTask = asyncio.tasks._PyTask

    class _TimedTask(_PyTask):
        def __init__(self, coro, **kw):
            self._stat_label = getattr(coro, "__qualname__", None) or repr(coro)
            super().__init__(coro, **kw)

        def _Task__step(self, *args, **kw):
            t0 = time.thread_time()
            try:
                return super()._Task__step(*args, **kw)
            finally:
                TASK_CPU[self._stat_label] += time.thread_time() - t0
                TASK_STEPS[self._stat_label] += 1

    def _factory(loop, coro, **kw):
        return _TimedTask(coro, loop=loop, **kw)

except Exception as e:  # pragma: no cover
    _task_factory_ok = False
    print(f"[STAT] per-task accounting unavailable: {e}", flush=True)

# ─── Per-thread CPU sampling from /proc ─────────────────────────────────────
_thread_seen = {}


def _thread_snapshot():
    out = {}
    try:
        tids = os.listdir("/proc/self/task")
    except OSError:
        return out
    for tid in tids:
        try:
            with open(f"/proc/self/task/{tid}/stat") as f:
                data = f.read()
            rest = data[data.rindex(")") + 2:].split()
            utime, stime = int(rest[11]), int(rest[12])
            with open(f"/proc/self/task/{tid}/comm") as f:
                comm = f.read().strip()
        except (OSError, ValueError, IndexError):
            continue
        out[tid] = (comm, utime + stime)
        _thread_seen[tid] = comm
    return out


RSS_SAMPLES = []
SYS_SAMPLES = []
_stop_sampler = threading.Event()


def _sampler():
    PROC.cpu_percent(None)
    while not _stop_sampler.is_set():
        time.sleep(0.5)
        try:
            RSS_SAMPLES.append((PROC.cpu_percent(None) / 100.0, rss_mb()))
            vm = psutil.virtual_memory()
            SYS_SAMPLES.append((vm.total - vm.available) / 1e6)
        except Exception:
            pass


# ─── Reproduce the real __main__ startup ────────────────────────────────────

# ─── Real OS thread names (Python's Thread(name=) does not set /proc comm) ───
import ctypes as _ctypes

try:
    _libc = _ctypes.CDLL("libc.so.6", use_errno=True)
    _PR_SET_NAME = 15
except Exception:
    _libc = None


def _set_thread_name(name):
    """prctl(PR_SET_NAME) -- kernel truncates to 15 chars + NUL."""
    if _libc is None:
        return
    try:
        _libc.prctl(_PR_SET_NAME, name.encode()[:15] + b"\x00", 0, 0, 0)
    except Exception:
        pass


def _named(name, fn):
    """Wrap a thread target so it names itself at the OS level first."""
    def _run(*a, **kw):
        _set_thread_name(name)
        return fn(*a, **kw)
    return _run


def main():
    _set_thread_name("asyncio-main")
    print("=" * 74)
    print(f"STAT TEST — {DURATION}s live run | pose delegate: "
          f"{'GPU' if USE_GPU else 'CPU (as v9 ships)'} | "
          f"recording: {'OFF' if NO_RECORD else 'ON'} | "
          f"person: {'INJECTED' if USE_PERSON else 'real camera'} | "
          f"talk: {'ON' if TALK else 'OFF'} | "
          f"fall: {'ON' if FIRE_FALL else 'OFF'} | "
          f"memsearch: {'ON' if MEM_SEARCH else 'OFF'}")
    print("=" * 74, flush=True)

    wav_before = set(glob.glob(os.path.join(R.DAY_UTTERANCE_DIR, "seg_*.wav")))

    R._boot_status("audio", "Configuring audio devices...")

    R._boot_status("memory", "Loading memory system...")
    R._memory_embedder = R._load_memory_embedder()
    milestone("memory embedder + chroma")

    R._boot_status("pose_model", "Loading fall detection model...")
    try:
        R._ensure_pose_model()
    except Exception as e:
        print(f"[BOOT] Pose model load failed: {e}")

    threading.Thread(target=_named("stat-sampler", _sampler), name="stat-sampler", daemon=True).start()

    R._boot_status("camera", "Starting camera & fall detection...")
    threading.Thread(target=_named("fall-detect", R._fall_detection_thread),
                     name="fall-detect", daemon=True).start()

    R._boot_status("video_stream", "Starting video stream server...")
    threading.Thread(target=_named("mjpeg-srv", R._start_mjpeg_server), args=(8080,),
                     name="mjpeg-srv", daemon=True).start()

    if not NO_RECORD:
        threading.Thread(target=_named("wav-writer", R._audio_writer_thread),
                         name="wav-writer", daemon=True).start()
    else:
        def _drain():
            while not R._shutdown_event.is_set():
                try:
                    R._audio_record_queue.get(timeout=1.0)
                except queue.Empty:
                    pass
        threading.Thread(target=_named("wav-drain", _drain), name="wav-drain", daemon=True).start()

    time.sleep(6)
    milestone("threads running (pre-session)")

    base_threads = _thread_snapshot()
    t_start = time.time()

    async def _bounded():
        loop = asyncio.get_running_loop()
        if _task_factory_ok:
            loop.set_task_factory(_factory)
        driver = asyncio.ensure_future(_talk_driver()) if TALK else None
        try:
            await R.run()
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        finally:
            if driver is not None:
                driver.cancel()

    def _finish():
        elapsed = time.time() - t_start
        end_threads = _thread_snapshot()
        R._shutdown_event.set()
        _stop_sampler.set()
        time.sleep(1.5)
        report(base_threads, end_threads, elapsed)
        _cleanup_wavs(wav_before)

    def _watchdog():
        # v9's run() swallows CancelledError and reconnects forever, so the only
        # reliable way to end a timed measurement is to report and hard-exit.
        time.sleep(DURATION)
        print(f"\n[STAT] duration {DURATION}s reached — writing report", flush=True)
        try:
            _finish()
        except Exception as e:
            print(f"[STAT] report failed: {type(e).__name__}: {e}", flush=True)
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)

    threading.Thread(target=_named("stat-watchdog", _watchdog), name="stat-watchdog", daemon=True).start()

    if FIRE_FALL:
        threading.Thread(target=_named("fall-driver", _fall_driver),
                         name="fall-driver", daemon=True).start()
    if MEM_SEARCH:
        threading.Thread(target=_named("mem-search", _mem_driver),
                         name="mem-search", daemon=True).start()

    try:
        asyncio.run(_bounded())
    except KeyboardInterrupt:
        print("\n[STAT] interrupted early")
    except Exception as e:
        print(f"[STAT] session ended: {type(e).__name__}: {e}")

    _finish()


def _cleanup_wavs(wav_before):
    wav_after = set(glob.glob(os.path.join(R.DAY_UTTERANCE_DIR, "seg_*.wav")))
    created = wav_after - wav_before
    freed = 0
    for p in created:
        try:
            freed += os.path.getsize(p)
            os.remove(p)
        except OSError:
            pass
    print(f"\n[STAT] removed {len(created)} WAV segment(s) written by this test "
          f"({freed/1e6:.1f} MB)")


NICE_NAMES = {
    "fall-detect":  "Fall detection thread (camera + pose + overlay)",
    "mjpeg-srv":    "MJPEG dashboard server",
    "wav-writer":   "Audio recording writer",
    "wav-drain":    "Audio queue drain (recording disabled)",
    "stat-sampler": "measurement overhead (this harness)",
}


def report(base, end, elapsed):
    print("\n" + "=" * 74)
    print(f"RESULTS — {elapsed:.0f}s live run")
    print("=" * 74)

    print("\nRAM — process RSS milestones")
    prev = 0.0
    for label, mb in MILESTONES:
        print(f"  {label:<34s} {mb:8.0f} MB   (+{mb - prev:7.0f})")
        prev = mb
    if RSS_SAMPLES:
        peak = max(s[1] for s in RSS_SAMPLES)
        mean = sum(s[1] for s in RSS_SAMPLES) / len(RSS_SAMPLES)
        print(f"  {'during live session (mean)':<34s} {mean:8.0f} MB")
        print(f"  {'during live session (peak)':<34s} {peak:8.0f} MB")
    if SYS_SAMPLES:
        print(f"  {'system-wide used (peak)':<34s} {max(SYS_SAMPLES)/1000:8.2f} GB")

    print("\nCPU — per thread (exact, from /proc/self/task)")
    rows = []
    for tid, (comm, jiff) in end.items():
        j0 = base.get(tid, (comm, 0))[1]
        cores = (jiff - j0) / TICKS / elapsed
        if cores <= 0.0005:
            continue
        rows.append((cores, NICE_NAMES.get(comm, comm if comm != "python"
                                   else "python (unattributed thread)")))
    rows.sort(reverse=True)
    total = 0.0
    for cores, name in rows:
        print(f"  {name:<52s} {cores:6.3f} cores")
        total += cores
    print(f"  {'-'*52} {'-'*12}")
    print(f"  {'TOTAL (all threads)':<52s} {total:6.3f} cores")
    if RSS_SAMPLES:
        m = sum(s[0] for s in RSS_SAMPLES) / len(RSS_SAMPLES)
        print(f"  {'cross-check: psutil whole-process mean':<52s} {m:6.3f} cores")

    if TASK_CPU:
        print("\nCPU — per asyncio task (all share the main thread)")
        for label, secs in sorted(TASK_CPU.items(), key=lambda kv: -kv[1]):
            short = label.split(".")[-1]
            print(f"  {short:<52s} {secs/elapsed:6.3f} cores"
                  f"   ({secs:6.2f}s over {TASK_STEPS[label]:,} steps)")
        print(f"  {'-'*52} {'-'*12}")
        print(f"  {'TOTAL (main thread coroutines)':<52s} "
              f"{sum(TASK_CPU.values())/elapsed:6.3f} cores")

    print("\nThroughput")
    n = _pose_frames['n']
    print(f"  pose frames                {n:6d}   -> {n/elapsed:5.2f} fps "
          f"(target {R.FALL_DETECTION_FPS})")
    print(f"  frames with a person       {_pose_frames['with_person']:6d}   "
          f"({100*_pose_frames['with_person']/max(n,1):.0f}% of frames)")
    if FIRE_FALL or MEM_SEARCH:
        print(f"  fall alerts fired          {_feature_stats['falls_fired']:6d}")
        print(f"  memory searches            {_feature_stats['mem_searches']:6d}", end="")
        if _mem_latency:
            s = sorted(_mem_latency)
            print(f"   mean {sum(s)/len(s):5.1f} ms  p95 {s[int(len(s)*.95)]:5.1f} ms")
        else:
            print()
    print(f"  mic blocks queued          {_mic_blocks['n']:6d}   -> "
          f"{_mic_blocks['n']/elapsed:5.1f}/s (expect ~46.9)")
    with R._transcript_lock:
        print(f"  transcript: user={len(R._transcript_user)} "
              f"gemini={len(R._transcript_gemini)}")

    for t in (R.tracker_first_audio, R.tracker_send_audio, R.tracker_receive):
        if t.samples:
            import numpy as np
            a = np.array(t.samples)
            print(f"  {t.name:<26s} n={len(a):4d} mean={a.mean():7.1f} ms "
                  f"p95={np.percentile(a,95):7.1f} ms")

    out = {
        "duration_s": elapsed,
        "gpu_delegate": USE_GPU,
        "person_injected": USE_PERSON,
        "talk_prompts_sent": _talk_stats["sent"],
        "falls_fired": _feature_stats["falls_fired"],
        "mem_searches": _feature_stats["mem_searches"],
        "mem_search_ms_mean": (sum(_mem_latency)/len(_mem_latency))
                              if _mem_latency else None,
        "mem_search_ms_p95": (sorted(_mem_latency)[int(len(_mem_latency)*.95)])
                             if _mem_latency else None,
        "recording": not NO_RECORD,
        "milestones_mb": MILESTONES,
        "threads_cores": {n2: c for c, n2 in rows},
        "asyncio_tasks_cores": {k.split('.')[-1]: v / elapsed
                                for k, v in TASK_CPU.items()},
        "rss_peak_mb": max((s[1] for s in RSS_SAMPLES), default=0),
        "pose_fps": n / elapsed,
        "pose_frames_with_person_pct":
            100 * _pose_frames["with_person"] / max(n, 1),
    }
    path = os.path.join(SCRIPT_DIR, "stat_test_results.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\n[STAT] machine-readable results -> {path}")




if __name__ == "__main__":
    main()
