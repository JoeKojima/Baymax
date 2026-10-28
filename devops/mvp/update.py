"""Opt-in robot updater: follow one branch, check before switching, roll back if Ember never starts.

Runs before every ember-mvp.service start (ExecStartPre=-, so a failure here never
blocks Ember). Enable with BAYMAX_AUTO_UPDATE=1 in Baymax-main/.env; the followed
branch is BAYMAX_UPDATE_BRANCH (default: release). Standard library only.
"""
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time

MAX_ATTEMPTS = 3        # Starts allowed for a new version to reach 'listening' before rollback.
FETCH_TIMEOUT = 20      # Boot must not wait long for a missing network.
CHECK_TIMEOUT = 600     # A step that runs out of time fails like any other: rollback.
PIP_TIMEOUT = 900       # All together can exceed the service's TimeoutStartSec (1200 s); an
                        # update stopped that way is rolled back on the next start, and a
                        # rollback stopped during its reinstall is finished then.


def _requirements():
    """The same package list Setup installs (devops/clickables/bootstrap.py)."""
    machine = platform.machine().lower()
    if sys.platform == "darwin" and machine == "x86_64":
        return "Baymax-main/requirements-macos-intel.txt"
    if sys.platform.startswith("linux") and machine in ("aarch64", "arm64"):
        return "Baymax-main/requirements-orange-pi.txt"
    return "Baymax-main/requirements-pc.txt"


REQUIREMENTS = _requirements()


def log(message):
    print(f"[UPDATE] {message}", flush=True)


def read_env(path):
    # Minimal KEY=VALUE reader: the updater must work even if the venv is broken.
    values = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


class Updater:
    def __init__(self, project, run_checks=None, install_requirements=None, clock=time.time):
        self.project = Path(project)
        self.runtime = self.project / "Baymax-main"
        self.state_path = self.runtime / ".update-state.json"
        self.status_path = self.runtime / ".runtime-status.json"
        self.python = self.runtime / ".venv/bin/python"
        self.run_checks = run_checks or self._run_checks
        self.install_requirements = install_requirements or self._install_requirements
        self.clock = clock

    # ── helpers ──────────────────────────────────────────────────────────────
    def git(self, *args, timeout=60):
        result = subprocess.run(["git", "-C", str(self.project), *args], capture_output=True,
                                text=True, timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def load_state(self):
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            return state if isinstance(state, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_state(self, state):
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(temporary, self.state_path)

    def reached_listening(self, since):
        try:
            status = json.loads(self.status_path.read_text(encoding="utf-8"))
            return any(event.get("state") == "listening" and event.get("time", 0) >= since
                       for event in status.get("events", []))
        except (OSError, ValueError, AttributeError, TypeError):
            return False

    def requirements_changed(self, old, new):
        return bool(self.git("diff", "--name-only", old, new, "--", REQUIREMENTS))

    def _run_checks(self):
        result = subprocess.run([str(self.python), "check_release.py"], cwd=self.runtime,
                                capture_output=True, text=True, timeout=CHECK_TIMEOUT)
        return result.returncode == 0

    def _install_requirements(self):
        result = subprocess.run([str(self.python), "-m", "pip", "install", "-q", "-r",
                                 str(self.project / REQUIREMENTS)],
                                capture_output=True, text=True, timeout=PIP_TIMEOUT)
        return result.returncode == 0

    def step(self, name, action):
        """True when an install/check step succeeded. A timeout (e.g. a source build on the
        Orange Pi) or a missing/broken venv python is a failure, not an exception, so the
        caller's rollback runs instead of leaving the robot on an unchecked commit."""
        try:
            return bool(action())
        except (OSError, subprocess.TimeoutExpired) as error:
            log(f"{name} did not finish: {type(error).__name__}: {error}")
            return False

    def switch(self, branch, commit):
        # A named local branch keeps manual `git pull` working; -B also follows a
        # release that was moved back to an older commit.
        self.git("checkout", "--quiet", "-B", branch, commit)

    def rollback(self, state, branch, bad, good, reason):
        log(f"{reason}; rolling back {bad[:7]} -> {good[:7]}.")
        # Recorded before switching back: a rollback stopped before the switch back or
        # during its own reinstall (systemd's start limit after a long pip, power loss)
        # is finished on the next start, and the bad commit is never applied again. A
        # power cut during the checkout itself is not handled: see run().
        state["bad"] = sorted(set(state.get("bad", [])) | {bad})
        state.update(rollback_from=bad, rollback_to=good)
        self.save_state(state)
        changed = self.requirements_changed(bad, good)
        self.switch(branch, good)
        if changed and not self.step("Reinstalling the previous Python packages", self.install_requirements):
            log("Reinstalling the previous Python packages failed; Ember may need a manual fix.")
        state.update(good=good, pending=None, attempts=0, checked=True)
        state.pop("rollback_from", None)
        state.pop("rollback_to", None)
        self.save_state(state)

    # ── main flow ────────────────────────────────────────────────────────────
    def run(self, settings):
        if settings.get("BAYMAX_AUTO_UPDATE", "0") != "1":
            return "disabled"
        branch = settings.get("BAYMAX_UPDATE_BRANCH", "release").strip() or "release"
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch) or ".." in branch:
            log(f"Invalid BAYMAX_UPDATE_BRANCH {branch!r}; not updating.")
            return "invalid-branch"
        state = self.load_state()
        head = self.git("rev-parse", "HEAD")
        state.setdefault("good", head)

        pending = state.get("pending")
        if pending:
            good = state.get("rollback_to") or state.get("previous") or state["good"]
            if state.get("rollback_from") == pending and head in (pending, good):
                self.rollback(state, branch, pending, good,
                              f"The rollback from {pending[:7]} was interrupted; finishing it")
                return "rolled-back"
            if pending != head:
                if head == state.get("previous") and state.get("checked") is False:
                    log(f"The switch to {pending[:7]} was interrupted before it happened; trying again.")
                else:
                    log("Checkout changed by hand; forgetting the pending update.")
                state.update(pending=None, attempts=0, good=head, checked=True)
                state.pop("rollback_from", None)
                state.pop("rollback_to", None)
            elif state.get("checked") is False:
                # Switched, but the package install/checks never finished (power loss, or
                # systemd stopped this pre-start step): never keep an unchecked version.
                self.rollback(state, branch, head, state.get("previous") or state["good"],
                              f"The update to {head[:7]} was interrupted before its checks finished")
                return "rolled-back"
            elif self.reached_listening(state.get("switched_at", 0)):
                log(f"Version {head[:7]} reached listening; keeping it.")
                state.update(good=head, pending=None, attempts=0)
            else:
                state["attempts"] = state.get("attempts", 0) + 1
                if state["attempts"] >= MAX_ATTEMPTS:
                    self.rollback(state, branch, head, state.get("previous") or state["good"],
                                  f"Version {head[:7]} never reached listening in {MAX_ATTEMPTS} starts")
                    return "rolled-back"
                self.save_state(state)
                log(f"Version {head[:7]} not confirmed yet (start {state['attempts'] + 1} of {MAX_ATTEMPTS}).")
                return "waiting"
            self.save_state(state)

        if self.git("status", "--porcelain", "--untracked-files=no"):
            log("Local edits in the checkout; not updating.")
            return "dirty"
        try:
            self.git("fetch", "--quiet", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
                     timeout=FETCH_TIMEOUT)
        except (RuntimeError, subprocess.TimeoutExpired):
            log(f"Could not fetch {branch} (no network or no such branch); starting the current version.")
            return "offline"
        target = self.git("rev-parse", f"refs/remotes/origin/{branch}")
        if target == head:
            return "current"
        if target in state.get("bad", []):
            log(f"{branch} is at {target[:7]}, which failed before; staying on {head[:7]}.")
            return "known-bad"

        log(f"Updating {head[:7]} -> {target[:7]} from {branch}.")
        requirements = self.requirements_changed(head, target)
        # Recorded before the switch, so an update stopped after the checkout (right
        # after it, during pip or the checks) is rolled back on the next start, and one
        # stopped before the checkout started is simply tried again. Known limit (not
        # new): a power cut during the git checkout itself, this one or the rollback's,
        # can leave the tree part old, part new, or a stale .git/index.lock. Later starts
        # can then report local edits or fail to check out, so updates stop and Ember
        # keeps starting on that unchecked mix (or on the failed commit) until it is
        # fixed by hand (devops/mvp/README.md). A forced checkout and removing the lock
        # while `pending` is set would handle it, but need testing on a robot first.
        state.update(pending=target, previous=head, switched_at=self.clock(), attempts=0, checked=False)
        self.save_state(state)
        self.switch(branch, target)
        if requirements and not self.step("Installing new Python packages", self.install_requirements):
            self.rollback(state, branch, target, head, "Installing new Python packages failed")
            return "rolled-back"
        if not self.step("Offline checks", self.run_checks):
            self.rollback(state, branch, target, head, "Offline checks failed")
            return "rolled-back"
        state.update(switched_at=self.clock(), checked=True)
        self.save_state(state)
        log(f"Switched to {target[:7]}; it is kept once Ember reaches listening.")
        return "updated"


def main():
    project = Path(__file__).resolve().parents[2]
    try:
        Updater(project).run(read_env(project / "Baymax-main/.env"))
    except Exception as error:  # Never block Ember from starting.
        log(f"Skipped: {error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
