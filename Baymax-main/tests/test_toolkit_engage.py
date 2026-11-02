"""Toolkits around 'Ember disengage': greetings stay quiet; reminders and fall check-ins engage Ember."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import runtime_monitor as monitor
from features.conversational_ability.engage import engagement
from features.toolkits.everyday import tools
from features.toolkits.fall_detection import FallWorker
from features.toolkits.proactive import Proactive


class Disengaged(unittest.TestCase):
    def setUp(self):
        patcher = patch.multiple(engagement, disengaged=True, listener=None, tell=lambda text: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_greetings_while_disengaged(self):
        greeter = Proactive(landmarks_factory=lambda: None, clock=lambda: 1000.0, wall=lambda: 1000.0,
                            book_playing=lambda: False, last_conversation=lambda: 0.0)
        with patch.object(monitor, "state", "listening"):
            self.assertEqual(greeter.blocked(), "disengaged")
            engagement.disengaged = False
            self.assertIsNone(greeter.blocked())

    def test_due_reminder_engages_ember_before_it_is_announced(self):
        session = AsyncMock()
        with patch.object(tools, "available_to_speak", return_value=True), \
                patch.object(tools.store, "due", return_value=[{"id": 1, "text": "Pills"}]), \
                patch.object(tools.store, "delivered"), \
                patch.object(tools.store, "now", return_value={"now": "09:00"}), \
                patch.dict("os.environ", {"EMBER_EVERYDAY_ENABLED": "1"}):
            asyncio.run(tools.notify_due(session))
        self.assertFalse(engagement.disengaged)
        session.send_client_content.assert_awaited_once()

    def test_no_due_reminder_leaves_ember_disengaged(self):
        with patch.object(tools, "available_to_speak", return_value=True), \
                patch.object(tools.store, "due", return_value=[]), \
                patch.dict("os.environ", {"EMBER_EVERYDAY_ENABLED": "1"}):
            asyncio.run(tools.notify_due(AsyncMock()))
        self.assertTrue(engagement.disengaged)

    def test_possible_fall_engages_ember_for_the_check_in(self):
        worker = FallWorker()
        alerts = [{"recorded": 0}]
        worker.pop_alert = lambda: alerts.pop() if alerts else None
        session = AsyncMock()

        class Stop:
            calls = 0

            def is_set(self):
                Stop.calls += 1
                return Stop.calls > 2
        with patch.object(monitor, "event_observer", None):
            asyncio.run(worker._alerts(session, Stop()))
        self.assertFalse(engagement.disengaged)
        session.send_client_content.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
