import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('status', Path(__file__).parent / 'payload/ember-status.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ProgressTests(unittest.TestCase):
    def test_waiting_before_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = module.progress(root/'stage', root/'log', root/'started', 'inactive', now=10000)
            self.assertEqual(result, ['  Progress: waiting to start'])

    def test_live_activity_does_not_expose_log_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ('stage', 'started', 'log'):
                (root/name).write_text('')
                os.utime(root/name, (9900, 9900))
            (root/'log').write_text('password=DO-NOT-SHOW\nGOOGLE_API_KEY=DO-NOT-SHOW\nSetting up lightdm (1.0) ...\n')
            os.utime(root/'log', (9990, 9990))
            result = '\n'.join(module.progress(root/'stage', root/'log', root/'started', 'activating', now=10000))
            self.assertIn('1m 40s', result)
            self.assertIn('10s ago', result)
            self.assertIn('Setting up lightdm', result)
            self.assertNotIn('DO-NOT-SHOW', result)
            self.assertNotIn('CHECK:', result)

    def test_quiet_running_installer_flagged_but_completed_one_is_not(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ('stage', 'log'):
                (root/name).write_text('')
                os.utime(root/name, (8000, 8000))
            active = '\n'.join(module.progress(root/'stage', root/'log', root/'started', 'activating', now=10000))
            done = '\n'.join(module.progress(root/'stage', root/'log', root/'started', 'inactive', now=10000))
            self.assertIn('CHECK:', active)
            self.assertIn('does not prove a stall', active)
            self.assertNotIn('CHECK:', done)


if __name__ == '__main__':
    unittest.main()
