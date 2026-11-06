"""Proactive greetings with fake faces, clocks and sessions; no camera, model or Gemini."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import runtime_monitor as monitor
from features.toolkits import fall_detection, proactive
from features.toolkits.proactive import EyeContact, Proactive, looking


def face(yaw=0.0, iris=0.5, span=0.14):
    """478 fake landmarks: eyes `span` apart, nose offset by `yaw`, irises at `iris` across each eye."""
    points = [SimpleNamespace(x=0.5, y=0.5) for _ in range(478)]
    left, right = 0.5 - span / 2, 0.5 + span / 2
    width = span * 0.3
    points[33].x, points[133].x = left, left + width          # Right eye (image left).
    points[362].x, points[263].x = right - width, right       # Left eye.
    points[468].x = left + width * iris
    points[473].x = right - width + width * iris
    points[1].x = 0.5 + yaw * span
    return points


class LookingTests(unittest.TestCase):
    def test_facing_camera_with_centred_irises_is_eye_contact(self):
        self.assertTrue(looking(face()))
        self.assertFalse(looking(None))
        self.assertFalse(looking(face()[:468]))      # No iris points.
        self.assertFalse(looking(face(yaw=0.3)))     # Head turned away.
        self.assertFalse(looking(face(iris=0.15)))   # Eyes looking sideways.
        self.assertFalse(looking(face(span=0.03)))   # Too far away to be talking to Ember.

    def test_new_eye_contact_needs_a_held_look_after_time_away(self):
        contact = EyeContact(hold=0.8, away=30)
        self.assertFalse(contact.update(0.0, True))
        self.assertTrue(contact.update(1.0, True))    # Held long enough: one event.
        self.assertFalse(contact.update(2.0, True))
        self.assertFalse(contact.update(3.0, False))  # Glanced away briefly...
        self.assertFalse(contact.update(4.0, True))
        self.assertFalse(contact.update(6.0, True))   # ...looking back is not new.
        contact.update(7.0, False)
        contact.update(40.0, True)
        self.assertTrue(contact.update(41.0, True))   # Back after 30 s away: new again.


    def test_started_and_ended_survive_a_blink(self):
        contact = EyeContact(hold=0.8, away=30, grace=1.0)
        changes = []
        for t, seen in [(0, True), (0.5, True), (1.0, True), (1.25, False), (1.5, True),
                        (2.0, False), (2.5, False), (3.1, False), (3.5, False)]:
            contact.update(t, seen)
            if contact.change:
                changes.append((t, contact.change))
        self.assertEqual(changes, [(1.0, "started"), (3.1, "ended")])


class ProactiveTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.book = False
        self.talked = 0.0
        self.greeter = Proactive(landmarks_factory=lambda: None, clock=lambda: self.now,
                                 wall=lambda: self.now, book_playing=lambda: self.book,
                                 last_conversation=lambda: self.talked)
        patcher = patch.object(monitor, "state", "listening")
        patcher.start()
        self.addCleanup(patcher.stop)

    def look(self, seconds=1.0):
        for _ in range(int(seconds / 0.25) + 1):
            self.greeter.step(face())
            self.now += 0.25

    def test_greets_on_new_eye_contact_only_when_quiet_and_not_in_a_book(self):
        self.talked = self.now - 30           # Talking recently: no greeting.
        self.look()
        self.assertFalse(self.greeter.wanted)
        self.greeter.step(None)
        self.now += 60
        self.talked = self.now - 600
        self.book = True                      # Book playing: no greeting.
        self.look()
        self.assertFalse(self.greeter.wanted)
        self.greeter.step(None)
        self.now += 60
        self.book = False
        self.look()
        self.assertTrue(self.greeter.wanted)

    def test_cooldown_and_busy_states_block_greetings(self):
        self.greeter.last_greeting = self.now - 60
        self.assertFalse(self.greeter.allowed())
        self.greeter.last_greeting = None
        with patch.object(monitor, "state", "speaking"):
            self.assertFalse(self.greeter.allowed())
        self.assertTrue(self.greeter.allowed())

    def test_session_task_says_hello_once_and_delivers_greetings(self):
        sent = []
        session = SimpleNamespace(send_client_content=AsyncMock(
            side_effect=lambda **kw: sent.append(kw["turns"]["parts"][0]["text"])))

        async def run_once(want=False):
            shutdown = asyncio.Event()
            self.greeter.wanted = want
            self.greeter.sleep = AsyncMock(side_effect=lambda s: shutdown.set())
            await self.greeter.session_task(session, shutdown)

        with patch.dict("os.environ", {"BAYMAX_STARTUP_GREETING": "1"}):
            asyncio.run(run_once())
            asyncio.run(run_once())           # Reconnect: no second hello.
        self.assertEqual(sent, [proactive.STARTUP_NOTE])
        self.greeter.last_greeting = None     # Past the cooldown.
        asyncio.run(run_once(want=True))
        self.assertEqual(sent[-1], proactive.PRESENCE_NOTE)

    def test_hello_retries_once_when_the_session_drops_right_after_connecting(self):
        sent = []
        session = SimpleNamespace(send_client_content=AsyncMock(
            side_effect=lambda **kw: sent.append(kw["turns"]["parts"][0]["text"])))

        async def dropped_session():
            # The connection fails a moment after the hello; the runtime cancels session tasks.
            shutdown = asyncio.Event()

            async def sleep(seconds):
                if seconds == 5.0:
                    raise asyncio.CancelledError
                if seconds == 0.2:
                    shutdown.set()  # Third session: just the idle loop, end it.
            self.greeter.sleep = sleep
            try:
                await self.greeter.session_task(session, shutdown)
            except asyncio.CancelledError:
                pass

        with patch.dict("os.environ", {"BAYMAX_STARTUP_GREETING": "1"}):
            for _ in range(3):
                asyncio.run(dropped_session())
        self.assertEqual(sent, [proactive.STARTUP_NOTE] * 2)  # Retried once, then gives up.
        self.assertFalse(self.greeter.greeted_startup)

    def test_eye_contact_is_off_unless_enabled(self):
        import os
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("BAYMAX_PRESENCE_GREETING", None)  # Default: no face model, no thread.
            self.greeter.observe(object())
        with patch.dict("os.environ", {"BAYMAX_PRESENCE_GREETING": "0"}):
            self.greeter.observe(object())
        self.assertIsNone(self.greeter.thread)
        self.assertIsNone(self.greeter.frame)

    def test_unavailable_model_leaves_conversation_alone(self):
        def broken():
            raise ImportError("mediapipe")
        greeter = Proactive(landmarks_factory=broken)
        greeter._run()
        self.assertTrue(greeter.state.startswith("Unavailable"))


class HookTests(unittest.TestCase):
    def test_registered_on_existing_camera_frames_and_session(self):
        import features.toolkits.gemini_tools  # noqa: F401  (imports and registers proactive)
        self.assertIn(proactive.proactive.observe, fall_detection.frame_observers)
        self.assertIn(proactive.proactive.session_task, fall_detection.session_tasks)

    def test_no_session_task_blocks_the_event_loop_with_the_runtime_shutdown_event(self):
        # The runtime passes a threading.Event. A task that called its wait() froze
        # Ember's whole loop on ember-002: no hello, mic never reached Gemini.
        import threading
        import features.toolkits.gemini_tools  # noqa: F401  (registers every task)
        session = SimpleNamespace(send_client_content=AsyncMock())
        self.assertGreaterEqual(len(fall_detection.session_tasks), 2)
        for task in list(fall_detection.session_tasks):
            ticks = []

            async def run():
                shutdown = threading.Event()
                running = asyncio.create_task(task(session, shutdown))
                for _ in range(10):
                    await asyncio.sleep(0.01)
                    ticks.append(1)
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)

            asyncio.run(asyncio.wait_for(run(), 3))
            self.assertEqual(len(ticks), 10, getattr(task, "__qualname__", task))

    def test_fall_worker_forwards_frames_and_runs_session_tasks(self):
        seen, started = [], []

        def broken(frame):
            raise RuntimeError

        async def task(session, shutdown):
            started.append(session)
            await asyncio.sleep(10)

        worker = fall_detection.FallWorker()
        with patch.object(fall_detection, "frame_observers", [seen.append, broken]), \
                patch.object(fall_detection, "session_tasks", [task]):
            worker.submit("frame")
            worker.submit("frame2")
            self.assertEqual(seen, ["frame", "frame2"])
            self.assertEqual(fall_detection.frame_observers, [seen.append])  # Broken one removed.

            async def run():
                shutdown = asyncio.Event()
                asyncio.get_running_loop().call_later(0.3, shutdown.set)
                await worker.alert_monitor("session", shutdown)
            asyncio.run(run())
        self.assertEqual(started, ["session"])


if __name__ == "__main__":
    unittest.main()
