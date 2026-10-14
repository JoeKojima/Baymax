"""
Long-running behaviour, compressed: a whole day in front of the camera, a
month of conversations, and the dashboard and the AI core writing the people
file at the same time. These catch what short tests can't — state that grows
without limit, disk writes that add up, slowly creeping cost, rare races.

    python3 -m unittest test/test_soak.py -v
"""
import json
import multiprocessing
import os
import sys
import tempfile
import threading
import time
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "Baymax-main"))
sys.path.insert(0, HERE)
import face_id as fi  # noqa: E402
import person_memory as pm  # noqa: E402
from test_face_id import FakeModels, Clock, face, identity, sample, unit  # noqa: E402

HOUR = 3600


class DayInFrontOfTheCamera(unittest.TestCase):
    """24 hours at 1 frame/s: Margaret most of the day, Sarah visiting twice,
    five strangers passing by (one says no, one says yes), empty nights."""

    def test_a_whole_day(self):
        d = tempfile.mkdtemp()
        clock = Clock()
        clock.t = 1_000_000.0
        store = fi.PeopleStore(os.path.join(d, "people.json"), clock=clock)
        writes = []
        real_write = store._write_file
        store._write_file = lambda data: (writes.append(clock.t), real_write(data))
        ident = fi.FaceIdentifier(FakeModels(), store, clock=clock,
                                  profile={"user_name": "Margaret", "household": []})
        m, s = identity(), identity()
        store.add_samples("Margaret", [sample(m, .75) for _ in range(5)], source="app")
        store.add_samples("Sarah", [sample(s, .75) for _ in range(5)], source="app")
        strangers = [identity() for _ in range(5)]
        writes.clear()

        start = clock.t                         # 7:00
        def at(h):
            return start + (h - 7) * HOUR

        def who(t):
            out = []
            if at(7) <= t < at(12) or at(13) <= t < at(18) or at(19) <= t < at(22):
                out.append(face(sample(m, .7), x=100))
            if at(10) <= t < at(11) or at(15) <= t < at(15.5):
                out.append(face(sample(s, .7), x=350))
            for i, x in enumerate(strangers):
                if at(14 + i * 0.5) <= t < at(14 + i * 0.5) + 120:
                    out.append(face(sample(x, .9), x=500))
            return out

        greetings, intros, peaks, costs = [], 0, {"tracks": 0, "visitors": 0, "arrivals": 0}, []
        for second in range(24 * HOUR):
            t = start + second
            clock.t = t
            frame = who(t)
            t0 = time.perf_counter()
            ident.process(frame)
            costs.append(time.perf_counter() - t0)
            # what self_awareness_monitor does every second
            arrival = ident.pending_arrival()
            if arrival:
                greetings.append((round((t - start) / HOUR + 7, 2), arrival["name"]))
            if ident.pending_introduction():
                intros += 1
                if intros == 1:
                    ident.handle_tool_call("decline_to_be_remembered", {})
                elif intros == 2:
                    self.assertEqual(ident.handle_tool_call("remember_person", {"name": "Ruth"})["status"],
                                     "remembered")
            peaks["tracks"] = max(peaks["tracks"], len(ident._tracks))
            peaks["visitors"] = max(peaks["visitors"], len(ident._visitors))
            peaks["arrivals"] = max(peaks["arrivals"], len(ident._arrivals))

        # Who was greeted, and when
        self.assertEqual([n for _, n in greetings if n == "Margaret"], ["Margaret"])   # breaks < 3 h
        self.assertEqual([h for h, n in greetings if n == "Sarah"], [10.0, 15.0])
        self.assertEqual(intros, 5, "each stranger asked exactly once")
        # Nothing grows without limit
        self.assertLessEqual(peaks["tracks"], 4)
        self.assertLessEqual(peaks["visitors"], 5)
        self.assertLessEqual(peaks["arrivals"], 2)
        self.assertEqual(len(ident._tracks), 0, "night: nobody in view")
        for p in store.people():
            self.assertLessEqual(len(p["embeddings"]), fi.MAX_SAMPLES)
        self.assertEqual(sorted(p["name"] for p in store.people()), ["Margaret", "Ruth", "Sarah"])
        # Disk writes stay modest (an SD card / eMMC lasts years at this rate)
        self.assertLess(len(writes), 150, f"{len(writes)} people-file writes in a day")
        busiest = max(sum(1 for w in writes if h <= w < h + HOUR) for h in range(int(start), int(start) + 24 * HOUR, HOUR))
        # last-seen every 10 min + a learned look per person every 15 min
        per_hour = HOUR / fi.LAST_SEEN_SAVE_INTERVAL + 2 * HOUR / fi.AUTO_LEARN_INTERVAL
        self.assertLessEqual(busiest, per_hour + 2)
        # Per-frame work doesn't creep up over the day (same scene: 7:00 vs 21:00)
        early = np.median(costs[0:1800])
        late = np.median(costs[14 * HOUR:14 * HOUR + 1800])
        self.assertLess(late, early * 3 + 1e-4, f"per-frame cost {early * 1e6:.0f} → {late * 1e6:.0f} µs")
        print(f"\n  day: {len(writes)} file writes (busiest hour {busiest}), greetings {greetings}, "
              f"peaks {peaks}, frame {early * 1e6:.0f}→{late * 1e6:.0f} µs")


class MonthOfConversations(unittest.TestCase):
    """30 days x 6 conversations x 20 lines, saved as they end, with a
    'forget me' in every 10th: every line saved exactly once, in its own
    conversation; nothing piles up in the pending file."""

    def test_a_month(self):
        d = tempfile.mkdtemp()
        clock = Clock()
        clock.t = 1_700_000_000.0
        log = pm.ConversationLog(os.path.join(d, "pending.jsonl"), clock=clock)
        saved, expected, n = [], [], 0
        for day in range(30):
            for c in range(6):
                lines = [f"day{day} conv{c} line{i}" for i in range(20)]
                expected.append(lines)
                for i, text in enumerate(lines):
                    log.add("user" if i % 2 == 0 else "gemini", text, ["Margaret"])
                    clock.t += 15
                    if i == 10 and n % 10 == 0:
                        log.forget(f"Visitor{n}")
                n += 1
                clock.t += pm.CONVERSATION_GAP + 60
                while True:                                  # the memory thread
                    turns = log.finished()
                    if not turns:
                        break
                    forgotten = log.forgotten_for(turns)
                    saved.append(([t["text"] for t in turns], forgotten))
                    log.mark_saved(turns)
                clock.t += 2 * HOUR
            clock.t += 8 * HOUR                              # night
        self.assertEqual([s for s, _ in saved], expected)
        for k, (_, forgotten) in enumerate(saved):
            self.assertEqual(forgotten, {f"Visitor{k}"} if k % 10 == 0 else set(), k)
        self.assertEqual(log.pending(), [])
        self.assertEqual(log.forgotten, {})
        self.assertLess(os.path.getsize(log.path), 10)
        # …and a restart reads back exactly that
        self.assertEqual(pm.ConversationLog(log.path, clock=clock).pending(), [])


def _dashboard(path, seconds, out):
    """Family adds and removes people in the dashboard."""
    store = fi.PeopleStore(path)
    rng = np.random.default_rng(1)
    added, removed, i = [], [], 0
    end = time.time() + seconds
    while time.time() < end:
        name = f"Dash{i}"
        pid, _ = store.add_samples(name, [unit(rng.normal(size=128))], source="app")
        added.append(name)
        if i % 3 == 0:
            store.remove(pid, forget_memories=True)
            removed.append(name)
        i += 1
    out.put(("dashboard", added, removed))


def _core(path, seconds, out):
    """The AI core: last-seen, learning, appearance, forget requests, reloads."""
    store = fi.PeopleStore(path)
    rng = np.random.default_rng(2)
    pid, _ = store.add_samples("Margaret", [unit(rng.normal(size=128))], source="app")
    end = time.time() + seconds
    n = 0
    while time.time() < end:
        store.reload_if_changed()
        store.touch({pid: time.time()})
        store.add_samples_to(pid, [unit(rng.normal(size=128))], check_consistency=False)
        store.set_appearance(pid, f"look {n}", today="a scarf")
        for name in store.forget_requests()[:2]:
            store.forget_done(name)
        n += 1
    out.put(("core", n, None))


class DashboardAndCoreTogether(unittest.TestCase):
    def test_simultaneous_writers_never_lose_or_corrupt(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "people.json")
        ctx = multiprocessing.get_context("spawn")
        out = ctx.Queue()
        procs = [ctx.Process(target=_dashboard, args=(path, 6, out)),
                 ctx.Process(target=_core, args=(path, 6, out))]
        bad_reads, reads = [], [0]
        stop = threading.Event()

        def reader():                       # e.g. the dashboard listing people
            while not stop.is_set():
                try:
                    with open(path) as f:
                        json.load(f)
                    reads[0] += 1
                except FileNotFoundError:
                    pass
                except ValueError as e:
                    bad_reads.append(str(e))
        t = threading.Thread(target=reader, daemon=True)
        t.start()
        for p in procs:
            p.start()
        results = {}
        for _ in procs:
            who, a, b = out.get(timeout=60)
            results[who] = (a, b)
        for p in procs:
            p.join(timeout=30)
        stop.set()
        t.join()
        self.assertEqual([p.exitcode for p in procs], [0, 0])
        self.assertEqual(bad_reads, [], "a reader saw a half-written file")
        added, removed = results["dashboard"]
        names = {p["name"] for p in fi.PeopleStore(path).people()}
        self.assertEqual(names, (set(added) - set(removed)) | {"Margaret"})
        self.assertGreater(len(added), 20)
        self.assertGreater(results["core"][0], 20)
        print(f"\n  writers: dashboard {len(added)} adds / {len(removed)} removes, "
              f"core {results['core'][0]} rounds, {reads[0]} clean reads")


if __name__ == "__main__":
    unittest.main()
