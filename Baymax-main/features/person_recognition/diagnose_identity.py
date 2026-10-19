"""Read-only robot identity diagnostic; no devices, Gemini, credentials or transcripts.

Can also run as a standalone Python 3 script on an older robot checkout.
"""
import argparse
import ast
import json
import os
import re
import sqlite3
from pathlib import Path

LIMIT = 1_000_000
RUNTIME = re.compile(r"realtime_gemini_(8|9|10)\.py$")


def running_cores(proc_root=Path("/proc")):
    """Return only script/pid/path metadata, never complete command lines or env."""
    result = []
    if not proc_root.is_dir():
        return result
    for process in proc_root.iterdir():
        if not process.name.isdigit():
            continue
        try:
            with (process / "cmdline").open("rb") as stream:
                args = stream.read(32_768).decode("utf-8", "replace").split("\0")
            # Only a Python invocation's first script argument; no shell snippets.
            if not args or not re.fullmatch(r"python(?:3(?:\.\d+)?)?", Path(args[0]).name):
                continue
            candidates = [a for a in args[1:3] if RUNTIME.fullmatch(Path(a).name)]
            if not candidates:
                continue
            cwd = (process / "cwd").resolve(strict=True)
            script = Path(candidates[0])
            script = script if script.is_absolute() else cwd / script
            result.append({"pid": int(process.name), "runtime": script.name, "script_path": str(script)})
        except (OSError, ValueError):
            continue
    return result


def source_summary(path, probe):
    result = {"runtime": path.name, "path": str(path), "available": path.is_file()}
    if not result["available"]:
        return result
    try:
        if path.stat().st_size > LIMIT:
            raise ValueError()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        result["probe_in_source_string_count"] = sum(probe.casefold() in s.casefold() for s in strings)
        result["legacy_this_user_prompt_present"] = any("What you know about this user from previous conversations" in s for s in strings)
        result["legacy_do_not_contradict_memory_present"] = any("do NOT second-guess" in s for s in strings)
        build = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_build_config"), None)
        result["startup_unfiltered_collection_get_present"] = bool(build and any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get"
            and isinstance(n.func.value, ast.Attribute) and n.func.value.attr == "_collection"
            and not any(k.arg == "where" for k in n.keywords) for n in ast.walk(build)))
        result["profile_loader_reference_present"] = any(isinstance(n, ast.Attribute) and n.attr == "load_profile" for n in ast.walk(tree))
        result["memory_loader_call_present"] = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                                                    and n.func.id == "_load_memory_embedder" for n in ast.walk(tree))
        result["notice"] = "Static source indicators, not proof of the live session's effective state or incident cause."
    except (OSError, ValueError, SyntaxError, UnicodeError):
        result["error"] = "Cannot inspect runtime source"
    return result


def profile_summary(path, probe):
    result = {"path": str(path), "present": path.is_file()}
    if not result["present"]:
        return result
    try:
        if path.stat().st_size > LIMIT:
            raise ValueError()
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError()
        primary = data.get("user_name")
        result["primary_name_set"] = isinstance(primary, str) and bool(primary.strip())
        result["primary_name_matches_probe"] = isinstance(primary, str) and primary.strip().casefold() == probe.casefold()
        people = data.get("people", [])
        if isinstance(people, list):
            result["stored_people_count"] = len(people)
            result["stored_people_matching_probe"] = sum(isinstance(p, dict) and isinstance(p.get("name"), str)
                                                         and p["name"].strip().casefold() == probe.casefold() for p in people)
    except (OSError, ValueError, UnicodeError):
        result["error"] = "Cannot inspect profile metadata"
    return result


def memory_summary(path, probe):
    result = {"path": str(path), "present": path.is_file()}
    if not result["present"]:
        return result
    result["wal_present"] = Path(str(path) + "-wal").exists()
    result["notice"] = "Read-only immutable committed snapshot; active WAL/recent writes may be omitted. Counts do not establish speaker identity."
    try:
        # immutable avoids creating sidecar files or taking locks in a live store.
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True, timeout=1)
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "embeddings" in tables:
                result["embedding_records"] = connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
            if "embedding_metadata" not in tables:
                result["metadata_schema_supported"] = False
                return result
            columns = {r[1] for r in connection.execute("PRAGMA table_info(embedding_metadata)")}
            if not {"id", "key", "string_value"}.issubset(columns):
                result["metadata_schema_supported"] = False
                return result
            result["metadata_schema_supported"] = True
            escaped = probe.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            result["probe_substring_records"] = connection.execute(
                "SELECT COUNT(DISTINCT id) FROM embedding_metadata WHERE string_value LIKE ? ESCAPE '\\'", ("%" + escaped + "%",)).fetchone()[0]
            for label, key in (("person_id_tagged_records", "person_id"), ("people_name_tagged_records", "people")):
                result[label] = connection.execute("SELECT COUNT(DISTINCT id) FROM embedding_metadata WHERE key=?", (key,)).fetchone()[0]
        finally:
            connection.close()
    except sqlite3.Error:
        result["error"] = "Cannot inspect committed memory metadata"
    return result


def diagnose(app_dir, data_dir, probe="James", processes=None):
    if not isinstance(probe, str) or not probe.strip() or len(probe) > 60:
        raise ValueError("Probe name must contain 1-60 characters")
    app_dir, data_dir = Path(app_dir).resolve(), Path(data_dir).resolve()
    return {
        "probe_name": probe,
        "running_cores": running_cores() if processes is None else processes,
        "inspected_app_dir": str(app_dir), "inspected_data_dir": str(data_dir),
        "runtime_sources": [source_summary(app_dir / f"realtime_gemini_{n}.py", probe) for n in (8, 9, 10)],
        "profiles": [profile_summary(root / name, probe) for root in dict.fromkeys((app_dir, data_dir))
                     for name in ("ember_profile.json", "ember_people.json")],
        "memory_stores": [memory_summary(root / "chroma_store/chroma.sqlite3", probe) for root in dict.fromkeys((app_dir, data_dir))],
        "limitations": ["No remote robot connection was made.",
                        "Host /proc discovery may miss containers, inaccessible processes or custom launchers.",
                        "The inspected source directory may differ from a running image/checkout; compare reported paths.",
                        "No live Gemini session, transcript or face embeddings were inspected.",
                        "A matching name/profile/memory is evidence to investigate, not a confirmed cause."],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-dir", type=Path, help="Actual running robot source directory, not the downloaded test branch")
    parser.add_argument("--data-dir", type=Path, help="Actual runtime data directory; Docker host usually /var/lib/ember, container /data")
    parser.add_argument("--probe-name", default="James")
    args = parser.parse_args()
    processes = running_cores()
    inferred = Path(processes[0]["script_path"]).parent if len(processes) == 1 else Path(__file__).resolve().parents[2]
    app = args.app_dir or inferred
    data = args.data_dir or Path(os.getenv("BAYMAX_DATA_DIR", app))
    report = diagnose(app, data, args.probe_name, processes)
    report["app_dir_basis"] = "explicit" if args.app_dir else "single_detected_process" if len(processes) == 1 else "diagnostic_checkout_fallback_unverified"
    report["data_dir_basis"] = "explicit" if args.data_dir else "diagnostic_environment_or_app_fallback_unverified"
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
