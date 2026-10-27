import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[2] / "devops/mvp/update.py"
spec = importlib.util.spec_from_file_location("mvp_update", SOURCE)
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


@unittest.skipUnless(shutil.which("git"), "git is required")
class UpdaterTests(unittest.TestCase):
    """Real git repos: a bare origin, a developer clone that pushes, and a robot clone."""

    def setUp(self):
        quiet = patch.object(update, "log")
        quiet.start()
        self.addCleanup(quiet.stop)
        # These repos change requirements-pc.txt; on an Orange Pi the updater follows
        # requirements-orange-pi.txt, so pin the profile for the test.
        profile = patch.object(update, "REQUIREMENTS", "Baymax-main/requirements-pc.txt")
        profile.start()
        self.addCleanup(profile.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.origin, self.dev, self.robot = root / "origin.git", root / "dev", root / "robot"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.dev)], check=True, capture_output=True)
        git(self.dev, "config", "user.email", "t@example.com")
        git(self.dev, "config", "user.name", "Test")
        (self.dev / "Baymax-main").mkdir()
        (self.dev / ".gitignore").write_text(".env\n.update-state.*\n.runtime-status.json\n")
        self.v1 = self.commit("requirements-pc.txt", "numpy==1\n")
        git(self.dev, "push", "-q", "origin", "HEAD:main", "HEAD:release")
        subprocess.run(["git", "clone", "-q", "-b", "main", str(self.origin), str(self.robot)],
                       check=True, capture_output=True)
        self.env = {"BAYMAX_AUTO_UPDATE": "1"}
        self.checks, self.pip = [], []
        self.checks_pass = self.pip_pass = True
        self.now = 1000.0
        self.updater = update.Updater(self.robot, run_checks=self.fake_checks,
                                      install_requirements=self.fake_pip, clock=lambda: self.now)

    def commit(self, name, text):
        (self.dev / "Baymax-main" / name).write_text(text)
        git(self.dev, "add", "-A")
        git(self.dev, "commit", "-q", "-m", name)
        return git(self.dev, "rev-parse", "HEAD")

    def release(self, name="feature.py", text="x = 1\n"):
        sha = self.commit(name, text)
        git(self.dev, "push", "-q", "--force", "origin", "HEAD:release")
        return sha

    def fake_checks(self):
        self.checks.append(self.head())
        return self.checks_pass

    def fake_pip(self):
        self.pip.append(self.head())
        return self.pip_pass

    def head(self):
        return git(self.robot, "rev-parse", "HEAD")

    def state(self):
        return json.loads((self.robot / "Baymax-main/.update-state.json").read_text())

    def ember_reached_listening(self, at):
        (self.robot / "Baymax-main/.runtime-status.json").write_text(
            json.dumps({"events": [{"state": "listening", "time": at, "message": "Connected"}]}))

    def test_disabled_by_default_and_with_invalid_branch(self):
        self.assertEqual(self.updater.run({}), "disabled")
        self.assertEqual(self.updater.run({**self.env, "BAYMAX_UPDATE_BRANCH": "../evil"}), "invalid-branch")
        self.assertEqual(self.head(), self.v1)

    def test_new_release_is_checked_switched_then_kept_once_listening(self):
        v2 = self.release()
        self.assertEqual(self.updater.run(self.env), "updated")
        self.assertEqual(self.head(), v2)
        self.assertEqual(self.checks, [v2])
        self.assertEqual(self.pip, [])  # Requirements unchanged: no pip.
        self.assertEqual(git(self.robot, "rev-parse", "--abbrev-ref", "HEAD"), "release")
        self.ember_reached_listening(self.now + 30)
        self.assertEqual(self.updater.run(self.env), "current")
        self.assertEqual(self.state()["good"], v2)
        self.assertIsNone(self.state()["pending"])

    def test_listening_from_before_the_switch_does_not_confirm(self):
        self.release()
        self.ember_reached_listening(self.now - 5)
        self.updater.run(self.env)
        self.assertEqual(self.updater.run(self.env), "waiting")

    def test_failed_checks_roll_back_and_skip_that_commit(self):
        v2 = self.release()
        self.checks_pass = False
        self.assertEqual(self.updater.run(self.env), "rolled-back")
        self.assertEqual(self.head(), self.v1)
        self.assertIn(v2, self.state()["bad"])
        self.checks_pass = True
        self.assertEqual(self.updater.run(self.env), "known-bad")
        self.assertEqual(self.head(), self.v1)
        v3 = self.release("fix.py")
        self.assertEqual(self.updater.run(self.env), "updated")
        self.assertEqual(self.head(), v3)

    def test_changed_requirements_are_installed_and_failure_rolls_back(self):
        v2 = self.release("requirements-pc.txt", "numpy==2\n")
        self.pip_pass = False
        self.assertEqual(self.updater.run(self.env), "rolled-back")
        # Installed for the new version, then reinstalled for the old one.
        self.assertEqual(self.pip, [v2, self.v1])
        self.assertEqual(self.checks, [])
        self.assertEqual(self.head(), self.v1)

    def test_version_that_never_reaches_listening_rolls_back_after_three_starts(self):
        v2 = self.release()
        self.assertEqual(self.updater.run(self.env), "updated")   # Start 1 of v2.
        self.assertEqual(self.updater.run(self.env), "waiting")   # Start 2 after a crash.
        self.assertEqual(self.updater.run(self.env), "waiting")   # Start 3.
        self.assertEqual(self.updater.run(self.env), "rolled-back")
        self.assertEqual(self.head(), self.v1)
        self.assertIn(v2, self.state()["bad"])

    def test_release_moved_back_is_followed(self):
        self.release()
        self.updater.run(self.env)
        self.ember_reached_listening(self.now + 1)
        self.updater.run(self.env)
        git(self.dev, "push", "-q", "--force", "origin", f"{self.v1}:release")
        self.now += 100
        self.assertEqual(self.updater.run(self.env), "updated")
        self.assertEqual(self.head(), self.v1)

    def test_local_edits_and_missing_network_leave_ember_alone(self):
        self.release()
        (self.robot / "Baymax-main/requirements-pc.txt").write_text("edited on robot\n")
        self.assertEqual(self.updater.run(self.env), "dirty")
        git(self.robot, "checkout", "--", ".")
        # .env and other ignored files never count as local edits.
        (self.robot / "Baymax-main/.env").write_text("BAYMAX_AUTO_UPDATE=1\n")
        git(self.robot, "remote", "set-url", "origin", str(Path(self.temp.name) / "missing.git"))
        self.assertEqual(self.updater.run(self.env), "offline")
        self.assertEqual(self.head(), self.v1)

    def test_other_branch_can_be_followed_for_rapid_testing(self):
        sha = self.commit("wip.py", "y = 2\n")
        git(self.dev, "push", "-q", "origin", "HEAD:MVP")
        self.assertEqual(self.updater.run({**self.env, "BAYMAX_UPDATE_BRANCH": "MVP"}), "updated")
        self.assertEqual(self.head(), sha)

    def test_main_never_fails_ember_startup(self):
        with patch.object(update.Updater, "run", side_effect=RuntimeError("boom")):
            self.assertEqual(update.main(), 0)

    def test_env_reader_handles_quotes_comments_and_missing_file(self):
        path = Path(self.temp.name) / "x.env"
        path.write_text("# comment\nBAYMAX_AUTO_UPDATE='1'\nBAYMAX_UPDATE_BRANCH = \"MVP\"\nbad line\n")
        self.assertEqual(update.read_env(path), {"BAYMAX_AUTO_UPDATE": "1", "BAYMAX_UPDATE_BRANCH": "MVP"})
        self.assertEqual(update.read_env(Path(self.temp.name) / "missing"), {})


class RequirementsProfileTests(unittest.TestCase):
    def test_updater_installs_the_same_package_list_as_setup(self):
        cases = [("linux", "aarch64", "requirements-orange-pi.txt"), ("linux", "arm64", "requirements-orange-pi.txt"),
                 ("linux", "x86_64", "requirements-pc.txt"), ("darwin", "x86_64", "requirements-macos-intel.txt"),
                 ("darwin", "arm64", "requirements-pc.txt"), ("win32", "AMD64", "requirements-pc.txt")]
        for system, machine, expected in cases:
            with patch.object(update.sys, "platform", system), patch.object(update.platform, "machine", return_value=machine):
                self.assertEqual(update._requirements(), "Baymax-main/" + expected, (system, machine))


if __name__ == "__main__":
    unittest.main()
