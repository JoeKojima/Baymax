import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import run_memory_pc
import runtime_monitor
from gemini_preflight import PreflightError


class MemoryLauncherTests(unittest.TestCase):
    def test_rejected_key_cannot_start_capture(self):
        with patch.dict(os.environ, {'GOOGLE_API_KEY': 'synthetic-test-key'}, clear=True), patch.object(run_memory_pc, 'load_dotenv'), patch.object(run_memory_pc, 'validate_key', side_effect=PreflightError('Rejected')), patch.object(run_memory_pc.runpy, 'run_path') as run, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run_memory_pc.main([]), 1)
            run.assert_not_called()

    def test_local_start_selects_v10_and_pc_profile_without_saving_key(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'GOOGLE_API_KEY': 'synthetic-test-key', 'BAYMAX_DATA_DIR': folder}, clear=True), patch.object(run_memory_pc, 'load_dotenv'), patch.object(run_memory_pc, 'validate_key'), patch.object(run_memory_pc.runpy, 'run_path') as run, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(run_memory_pc.main([]), 0)
            self.assertEqual(os.environ['BAYMAX_HARDWARE_PROFILE'], 'pc')
            self.assertEqual(os.environ['BAYMAX_DEBUG_MONITOR'], '1')
            self.assertEqual(os.environ['BAYMAX_MONITOR_DIR'], folder)
            self.assertTrue(run.call_args.args[0].endswith('realtime_gemini_10.py'))
            self.assertNotIn('synthetic-test-key', output.getvalue())
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_stop_targets_current_pid_without_key_or_google(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'BAYMAX_DATA_DIR': folder}, clear=True), patch.object(run_memory_pc, 'load_dotenv'), patch.object(run_memory_pc, 'validate_key') as validate, patch.object(runtime_monitor, 'read_status', return_value={'pid': 1234, 'state': 'listening'}), patch.object(runtime_monitor, 'STOP_PATH', Path(folder) / '.runtime-stop.json'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_memory_pc.main(['--stop']), 0)
            self.assertEqual(json.loads(runtime_monitor.STOP_PATH.read_text())['pid'], 1234)
            validate.assert_not_called()
