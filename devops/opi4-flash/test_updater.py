"""Updater controls must leave an existing release usable without network access."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform == 'linux', 'Updater integration requires Linux/bash')
class UpdaterTests(unittest.TestCase):
    def exercise(self, automatic, manual=False, invalid=False):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            base = home / 'ember-deployment'
            ready = base / 'current/Baymax-main/.venv/.ember-ready.json'
            ready.parent.mkdir(parents=True)
            ready.write_text('{}')
            (base / 'source/.git').mkdir(parents=True)
            config = home / '.config/ember/device.json'
            config.parent.mkdir(parents=True)
            config.write_text('{bad' if invalid else json.dumps({
                'repository': 'https://example.com/Ember.git',
                'branch': 'release/board', 'auto_update': automatic}))
            tools = home / '.local/bin'
            tools.mkdir(parents=True)
            fake_git = tools / 'git'
            fake_git.write_text('#!/bin/sh\necho "$*" >> "$HOME/git-calls"\ncase "$*" in *" fetch "*) exit 1;; esac\nexit 0\n')
            fake_git.chmod(0o755)
            payload = Path(__file__).parent / 'payload'
            script = (payload / 'ember-update').read_text()
            script = script.replace('export HOME=/home/ember', 'export HOME=' + str(home))
            script = script.replace('/opt/ember-python/python/bin/python3.12', sys.executable)
            script = script.replace('/opt/ember-provision/ember-device-config.py', str(payload / 'ember-device-config.py'))
            target = home / 'update.sh'
            target.write_text(script)
            result = subprocess.run(['bash', str(target)] + (['--now'] if manual else []), timeout=10)
            self.assertTrue(ready.exists(), 'Existing release must remain available')
            calls = (home / 'git-calls').read_text() if (home / 'git-calls').exists() else ''
            status = (base / 'update-status').read_text()
            return result.returncode, calls, status

    def test_disabled_updates_do_not_contact_git(self):
        code, calls, status = self.exercise(False)
        self.assertEqual(code, 0)
        self.assertEqual(calls, '')
        self.assertIn('disabled', status)

    def test_manual_update_overrides_disabled_setting(self):
        code, calls, status = self.exercise(False, manual=True)
        self.assertEqual(code, 0)
        self.assertIn('fetch origin release/board', calls)
        self.assertIn('current working release retained', status)

    def test_bad_config_keeps_existing_release(self):
        code, calls, status = self.exercise(True, invalid=True)
        self.assertNotEqual(code, 0)
        self.assertEqual(calls, '')
        self.assertIn('previous release kept', status)

    def first_install(self, matching_bundle=True):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            base = home / 'ember-deployment'
            (base / 'source/.git').mkdir(parents=True)
            config = home / '.config/ember'
            config.mkdir(parents=True)
            (config / 'device.json').write_text(json.dumps({
                'repository': 'https://example.com/Ember.git', 'branch': 'refs/pull/27/head', 'auto_update': True}))
            seed = 'GOOGLE_API_KEY="test-private-key"\nBAYMAX_CAMERA_DEVICE=none\n'
            (config / 'initial.env').write_text(seed)
            bundle = home / 'bundle-source.json'
            bundle.write_text(json.dumps({'repository': 'https://example.com/Ember.git',
                                          'branch': 'refs/pull/27/head' if matching_bundle else 'refs/pull/20/head'}))
            tools = home / '.local/bin'
            tools.mkdir(parents=True)
            bootstrap = home / 'bootstrap.py'
            bootstrap.write_text('from pathlib import Path\np=Path(__file__).parents[2]/"Baymax-main/.venv"\np.mkdir(parents=True)\n(p/".ember-ready.json").write_text("{}")\n')
            (tools / 'git').write_text('''#!/bin/sh
case "$*" in
*" fetch "*) exit 1;;
*" rev-parse "*) echo abc123;;
*" worktree add "*) mkdir -p "$6/devops/clickables" "$6/Baymax-main"; cp "$HOME/bootstrap.py" "$6/devops/clickables/bootstrap.py"; echo 'no v10 runtime' > "$6/Baymax-main/run_pc.py";;
esac
''')
            for name in ('uv', 'systemctl'):
                (tools / name).write_text('#!/bin/sh\nexit 0\n')
            for path in tools.iterdir():
                path.chmod(0o755)
            payload = Path(__file__).parent / 'payload'
            script = (payload / 'ember-update').read_text()
            script = script.replace('export HOME=/home/ember', 'export HOME=' + str(home))
            script = script.replace('/opt/ember-python/python/bin/python3.12', sys.executable)
            script = script.replace('/opt/ember-provision/ember-device-config.py', str(payload / 'ember-device-config.py'))
            script = script.replace('/opt/ember-provision/bundle-source.json', str(bundle))
            target = home / 'update.sh'
            target.write_text(script)
            result = subprocess.run(['bash', str(target)], timeout=10)
            active = base / 'current/Baymax-main/.env'
            if matching_bundle:
                self.assertEqual(result.returncode, 0)
                self.assertEqual(active.read_text(), seed)
                self.assertEqual(active.stat().st_mode & 0o777, 0o600)
            else:
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(active.exists())

    def test_first_boot_uses_matching_pr_bundle_and_preset_key(self):
        self.first_install()

    def test_offline_first_boot_rejects_a_different_pr_bundle(self):
        self.first_install(matching_bundle=False)

    def code_update(self, dependencies_changed):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            base = home / 'ember-deployment'
            old_venv = base / 'releases/old/Baymax-main/.venv'
            old_venv.mkdir(parents=True)
            (old_venv / '.ember-ready.json').write_text('{}')
            (base / 'current').symlink_to(base / 'releases/old')
            (base / 'source/.git').mkdir(parents=True)
            config = home / '.config/ember'
            config.mkdir(parents=True)
            (config / 'device.json').write_text(json.dumps({
                'repository': 'https://example.com/Ember.git', 'branch': 'refs/pull/20/head', 'auto_update': True}))
            tools = home / '.local/bin'
            tools.mkdir(parents=True)
            # A full install leaves "fresh"; a shared environment already has its readiness marker.
            bootstrap = home / 'bootstrap.py'
            bootstrap.write_text('from pathlib import Path\np=Path(__file__).parents[2]/"Baymax-main/.venv"\n'
                                 'if not (p/".ember-ready.json").exists():\n'
                                 '    p.mkdir(parents=True); (p/".ember-ready.json").write_text("{}"); (p/"fresh").write_text("")\n')
            (tools / 'git').write_text(f'''#!/bin/sh
case "$*" in
*" fetch "*) exit 0;;
*" rev-parse "*) echo new;;
*" diff --quiet old new "*) exit {1 if dependencies_changed else 0};;
*" worktree add "*) mkdir -p "$6/devops/clickables" "$6/Baymax-main"; cp "$HOME/bootstrap.py" "$6/devops/clickables/bootstrap.py"; echo 'no v10 runtime' > "$6/Baymax-main/run_pc.py";;
esac
''')
            for name in ('uv', 'systemctl'):
                (tools / name).write_text('#!/bin/sh\nexit 0\n')
            for path in tools.iterdir():
                path.chmod(0o755)
            payload = Path(__file__).parent / 'payload'
            script = (payload / 'ember-update').read_text()
            script = script.replace('export HOME=/home/ember', 'export HOME=' + str(home))
            script = script.replace('/opt/ember-python/python/bin/python3.12', sys.executable)
            script = script.replace('/opt/ember-provision/ember-device-config.py', str(payload / 'ember-device-config.py'))
            target = home / 'update.sh'
            target.write_text(script)
            self.assertEqual(subprocess.run(['bash', str(target)], timeout=10).returncode, 0)
            new_venv = base / 'releases/new/Baymax-main/.venv'
            self.assertEqual((base / 'current').resolve(), (base / 'releases/new').resolve())
            return new_venv.is_symlink(), (new_venv / 'fresh').exists()

    def test_code_only_update_reuses_the_installed_environment(self):
        self.assertEqual(self.code_update(dependencies_changed=False), (True, False))

    def test_changed_dependencies_get_a_fresh_environment(self):
        self.assertEqual(self.code_update(dependencies_changed=True), (False, True))


if __name__ == '__main__':
    unittest.main()
