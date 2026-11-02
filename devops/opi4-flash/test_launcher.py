"""Exercise launch routing using a fake Python executable; never capture media."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform == 'linux', 'Launcher integration requires Linux/bash')
class LauncherTests(unittest.TestCase):
    def launch(self, argument=None):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            runtime = home / 'ember-deployment/current/Baymax-main'
            (runtime / '.venv/bin').mkdir(parents=True)
            (runtime / '.venv/.ember-ready.json').write_text('{}')
            python = runtime / '.venv/bin/python'
            python.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$HOME/calls"\nexit 0\n')
            python.chmod(0o755)
            source = (Path(__file__).parent / 'payload/ember-run').read_text()
            script = home / 'launch.sh'
            script.write_text(source.replace('BASE=/home/ember/ember-deployment', 'BASE=' + str(home / 'ember-deployment')))
            subprocess.run(['bash', str(script)] + ([argument] if argument else []),
                           env={**os.environ, 'HOME': str(home)}, check=True, timeout=5)
            return (home / 'calls').read_text().splitlines()

    def test_conversation_uses_mvp_wrapper_not_direct_pc_entry(self):
        calls = self.launch()
        self.assertEqual(calls[-1], 'run_mvp.py')
        self.assertNotIn('run_pc.py', calls)
        self.assertTrue(any('ember-hardware.py' in call for call in calls))

    def test_settings_remains_independent_of_conversation_and_capture(self):
        self.assertEqual(self.launch('settings'), ['devUI.py --no-browser'])


if __name__ == '__main__':
    unittest.main()
