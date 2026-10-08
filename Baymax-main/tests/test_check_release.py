import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from check_release import collect_suites, render_report, run_sections


class ReviewGateTests(unittest.TestCase):
    def test_missing_required_files_cannot_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                collect_suites(Path(directory))

    def test_failed_and_empty_sections_cannot_pass(self):
        class Broken(unittest.TestCase):
            def runTest(self):
                self.fail("simulated regression")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            outcomes = run_sections({"core": unittest.TestSuite([Broken()]), "toolkit": unittest.TestSuite()})
        self.assertFalse(outcomes["core"]["passed"])
        self.assertFalse(outcomes["toolkit"]["passed"])
        self.assertEqual(outcomes["core"]["failures"], 1)

    def test_offline_pass_does_not_grant_live_or_lead_approval(self):
        outcomes = {section: {"passed": True, "tests": 1, "failures": 0, "errors": 0, "skipped": 0}
                    for section in ("core", "toolkit")}
        with patch("check_release.subprocess.run", side_effect=OSError):
            report = render_report(outcomes)
        self.assertIn("core: PASS", report)
        self.assertIn("toolkit: PASS", report)
        self.assertIn("not live validation or release approval", report)
        self.assertIn("[ ] Start Ember", report)
        self.assertIn("approval: PENDING", report)
