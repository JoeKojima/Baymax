"""Apply the reviewed desktop-memory code to an existing extracted checkout.

Only an explicit source-file manifest is copied. Credentials, environments,
recordings, model downloads and databases are never read/copied/replaced.
Changed source files receive a dated backup and a hash manifest first.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import time

SOURCE = Path(__file__).resolve().parents[2]
FILES = ["Baymax-main/" + name for name in (
    "realtime_gemini_10.py", "device_config.py", "semantic_embedder.py", "ember_memory.py",
    "ember_timing.py", "ember_topics.py", "run_pc.py", "devUI.py", "dev_settings.py",
    "runtime_monitor.py", "check_release.py", "requirements-pc.txt", "requirements-memory-pc.txt",
    "pytest.ini", ".gitignore", "static/devUI.html", "static/emberdevUI.html")]
DIRECTORIES = ("Baymax-main/toolkit", "Baymax-main/memory_tests", "Baymax-main/tests",
               "Baymax-main/tools", "Baymax-main/docs", "Baymax-main/fall_detection", "devops/clickables")


def candidates():
    names = set(FILES)
    for directory in DIRECTORIES:
        for path in (SOURCE / directory).rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix in (
                    ".py", ".md", ".sh", ".command", ".cmd", ".ps1"):
                names.add(str(path.relative_to(SOURCE)))
    return sorted(names)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def plan(target):
    target = Path(target).resolve()
    if target == SOURCE.resolve() or not (target / "devops/clickables/macOS/1 - Setup.command").is_file():
        raise ValueError("Choose the existing extracted desktop project folder.")
    result = []
    for name in candidates():
        source, destination = SOURCE / name, target / name
        if destination.is_symlink() or not destination.resolve().is_relative_to(target):
            raise ValueError("A destination escapes the selected project folder.")
        before, after = digest(destination), digest(source)
        if before != after:
            result.append({"path": name, "before": before, "after": after})
    return target, result


def install(target, changes):
    status = target / "Baymax-main/.runtime-status.json"
    if status.is_file():
        value = json.loads(status.read_text())
        if value.get("state") not in ("offline", "stopped") and time.time()-value.get("heartbeat", 0) < 5:
            raise ValueError("Stop Ember before updating its source files.")
    backup = target / ".memory-integration-backup" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup.mkdir(parents=True, exist_ok=False)
    for change in changes:
        destination = target / change["path"]
        if digest(destination) != change["before"]:
            raise ValueError("A source file changed since the plan was reviewed; rerun installation.")
        if destination.is_file():
            saved = backup / change["path"]
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, saved)
    (backup / "manifest.json").write_text(json.dumps(changes, indent=2))
    for change in changes:
        source, destination = SOURCE / change["path"], target / change["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
        try:
            shutil.copy2(source, temporary)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    return backup


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    target, changes = plan(args.target)
    print(f"Reviewed code update: {len(changes)} files in {target}")
    if args.apply and changes:
        print("Source backup:", install(target, changes))
    elif not args.apply:
        print("Dry run only. No files changed.")
