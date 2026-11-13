"""First-run installation for clickables. Standard library only; no device capture."""
import argparse
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

PROJECT = Path(__file__).resolve().parents[2]
RUNTIME = PROJECT / "Baymax-main"
SCHEMA = 2


class SetupError(RuntimeError):
    pass


def environment_python(runtime):
    return runtime / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def requirements_file(runtime):
    if sys.platform == "darwin" and platform.machine().lower() == "x86_64":
        return runtime / "requirements-macos-intel.txt"
    if sys.platform.startswith("linux") and platform.machine().lower() in ("aarch64", "arm64"):
        return runtime / "requirements-orange-pi.txt"
    return runtime / "requirements-pc.txt"


def fingerprint(runtime):
    return {"schema": SCHEMA, "requirements": hashlib.sha256(requirements_file(runtime).read_bytes()).hexdigest(),
            "profile": requirements_file(runtime).name,
            "python": "3.12", "platform": sys.platform, "architecture": platform.machine()}


@contextmanager
def installation_lock(runtime):
    runtime.mkdir(parents=True, exist_ok=True)
    with (runtime / ".bootstrap.lock").open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SetupError("Another Ember setup is running. Let it finish, then click again.") from None
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def run_checked(command, cwd):
    result = subprocess.run([str(item) for item in command], cwd=cwd)
    if result.returncode:
        raise SetupError("An installation/check step failed. Read the error above, fix internet/access issues and click Setup again.")


def ensure_environment(runtime, uv):
    if sys.version_info[:2] != (3, 12):
        raise SetupError("The launcher must select Python 3.12. Reopen Setup from your OS folder.")
    python = environment_python(runtime)
    marker = runtime / ".venv/.ember-ready.json"
    expected = fingerprint(runtime)
    try:
        ready = json.loads(marker.read_text(encoding="utf-8")) == expected
    except (OSError, ValueError):
        ready = False
    if ready and python.is_file():
        return python
    # Remove readiness before repair so a failed install is never accepted later.
    marker.unlink(missing_ok=True)
    if not uv.is_file():
        raise SetupError("Installer tool missing. Reopen the OS Setup clickable to download it.")
    if python.exists():
        version = subprocess.run([str(python), "-c", "import sys; sys.exit(0 if sys.version_info[:2] == (3,12) else 1)"], cwd=runtime)
        if version.returncode:
            previous = (runtime / ".venv").resolve()
            backup = (runtime / (".venv.previous-" + datetime.now().strftime("%Y%m%d-%H%M%S"))).resolve()
            if previous.parent != runtime.resolve() or backup.parent != runtime.resolve():
                raise SetupError("Environment backup must stay inside the runtime folder.")
            previous.rename(backup)
            print("Kept the previous Python environment as " + backup.name, flush=True)
    if not python.is_file():
        print("Creating the private Ember environment...", flush=True)
        run_checked([uv, "venv", "--seed", "--python", sys.executable, runtime / ".venv"], runtime)
    print("Installing " + requirements_file(runtime).name + ". First setup may take several minutes (ARM voice analysis may compile from source)...", flush=True)
    run_checked([uv, "pip", "install", "--python", python, "-r", requirements_file(runtime)], runtime)
    run_checked([python, "-m", "spacy", "download", "en_core_web_sm"], runtime)
    print("Running offline core/toolkit checks (no capture or cloud requests)...", flush=True)
    run_checked([python, "check_release.py", "--report", ".local-review/report.md"], runtime)
    marker.write_text(json.dumps(expected, indent=2), encoding="utf-8")
    return python


def install_shortcuts(project):
    base = project / "devops/clickables"
    if os.name == "nt":
        run_checked(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                     base / "install_shortcuts.ps1", "-ProjectRoot", project], project)
    elif sys.platform == "darwin":
        directory = Path.home() / "Desktop/clickables"
        directory.mkdir(parents=True, exist_ok=True)
        for source in (base / "macOS").glob("*.command"):
            source.chmod(source.stat().st_mode | 0o111)
            target = directory / source.name
            if target.is_symlink():
                target.unlink()
            if not target.exists():
                target.symlink_to(source)
        print("Desktop clickables: " + str(directory), flush=True)
    else:
        directory = Path.home() / "Desktop/clickables"
        directory.mkdir(parents=True, exist_ok=True)
        for source in (base / "Linux").glob("*.sh"):
            source.chmod(source.stat().st_mode | 0o111)
            target = directory / source.name
            if target.is_symlink():
                target.unlink()
            if not target.exists():
                target.symlink_to(source)
        print("Desktop clickables: " + str(directory) + "; choose Run/Trust if your file manager asks.", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("setup", "monitor", "start", "stop"))
    parser.add_argument("--uv", type=Path, required=True)
    parser.add_argument("--install-only", action="store_true", help="Install/check without opening a browser or starting capture.")
    args = parser.parse_args(argv)
    try:
        if args.action == "stop":
            python = environment_python(RUNTIME)
            if not python.is_file():
                print("Ember is not installed here; nothing to stop.")
                return 0
        else:
            with installation_lock(RUNTIME):
                python = ensure_environment(RUNTIME, args.uv)
        if args.action == "setup":
            install_shortcuts(PROJECT)
        if args.install_only:
            print("Ember installation is ready. Open Setup to save your key/devices; Start Ember begins capture.")
            return 0
        return subprocess.call([str(python), str(PROJECT / "devops/clickables/launch.py"), args.action], cwd=RUNTIME)
    except (SetupError, OSError) as error:
        print("Ember setup stopped: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
