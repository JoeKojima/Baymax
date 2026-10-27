"""Where the converted pose models live and how they are checked.

The converter (convert_rknn.py) writes, into the model directory:

    pose_detector.onnx, pose_landmarks_detector.onnx     derived from the .task's TFLite
    pose_detector.rknn, pose_landmarks_detector.rknn     FP16, target rk3588
    manifest.json                                        versions, SHA-256s, accuracy check

The directory defaults to Baymax-main/.models/pose_npu (ignored by Git and kept by
the updater's checkout); BAYMAX_POSE_MODEL_DIR overrides it. Standard library only.
"""
import hashlib
import json
import os
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[2]          # Baymax-main
DEFAULT_DIR = ROOT / ".models" / "pose_npu"
MANIFEST = "manifest.json"
MANIFEST_FORMAT = 1
NAMES = ("pose_detector", "pose_landmarks_detector")

# The same pose_landmarker_full.task (float16) the runtime downloads. Version 1 is
# pinned here; "latest" is a different zip of byte-identical TFLite files, so the
# inner hashes below are what identify the weights.
TASK_URL = ("https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
            "pose_landmarker_full/float16/1/pose_landmarker_full.task")
TASK_SHA256 = "5134a3aad27a58b93da0088d431f366da362b44e3ccfbe3462b3827a839011b1"
TASK_MAX_BYTES = 12 * 1024 * 1024
TFLITE_SHA256 = {
    "pose_detector": "04f5a96483f8ce5913f8730ad2aa0c4c999c80a4b75653bc0e4712de18e104b2",
    "pose_landmarks_detector": "82be6d591b9dad7d29fe21dc9fd892bf8b9602c458fb05209283de8282a0c488",
}


class ModelError(Exception):
    """The converted models cannot be used; the message says why (one line)."""


def model_dir(env=None):
    env = os.environ if env is None else env
    value = env.get("BAYMAX_POSE_MODEL_DIR", "").strip()
    return Path(value).expanduser() if value else DEFAULT_DIR


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def load_manifest(directory):
    path = Path(directory) / MANIFEST
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ModelError(f"no converted models in {directory} (run features/pose/convert_rknn.py on the board)") from None
    except (OSError, ValueError) as error:
        raise ModelError(f"unreadable {path}: {error}") from None
    if not isinstance(manifest, dict) or manifest.get("format") != MANIFEST_FORMAT:
        raise ModelError(f"{path} has an unknown format; re-run the converter")
    return manifest


def write_manifest(directory, manifest):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / (MANIFEST + ".tmp")
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, directory / MANIFEST)


def verified_files(directory, manifest, kind):
    """Paths of the 'rknn' or 'onnx' models whose SHA-256 matches the manifest. Any
    problem (missing, changed, unreadable, malformed manifest) is a ModelError."""
    entries = manifest.get(kind) if isinstance(manifest, dict) else None
    if not isinstance(entries, dict):
        raise ModelError(f"manifest has no {kind} section; re-run the converter")
    paths = {}
    for name in NAMES:
        entry = entries.get(name)
        if not (isinstance(entry, dict) and isinstance(entry.get("file"), str) and entry["file"]
                and isinstance(entry.get("sha256"), str) and entry["sha256"]):
            raise ModelError(f"manifest has no {kind} entry for {name}; re-run the converter")
        path = Path(directory) / Path(entry["file"]).name
        try:
            if not path.is_file():
                raise ModelError(f"{path.name} is missing from {directory}; re-run the converter")
            digest = sha256(path)
        except OSError as error:
            raise ModelError(f"cannot read {path}: {type(error).__name__}: {error}") from None
        if digest != entry["sha256"]:
            raise ModelError(f"{path.name} does not match the SHA-256 recorded at conversion; re-run the converter")
        paths[name] = path
    return paths


def check_section(manifest):
    """The converter's accuracy-check record, {} if absent or malformed."""
    check = manifest.get("check") if isinstance(manifest, dict) else None
    return check if isinstance(check, dict) else {}


def check_passed(manifest):
    return check_section(manifest).get("passed") is True


def task_matches_pinned(path):
    """True if the .task at `path` holds the two TFLite files pinned in TFLITE_SHA256 (the
    weights the converted models were made from), False if it holds others, None if it
    can't be read."""
    try:
        with zipfile.ZipFile(path) as bundle:
            for name in NAMES:
                with bundle.open(name + ".tflite") as handle:
                    digest = hashlib.sha256()
                    for block in iter(lambda: handle.read(1 << 20), b""):
                        digest.update(block)
                if digest.hexdigest() != TFLITE_SHA256[name]:
                    return False
    except (OSError, KeyError, zipfile.BadZipFile):
        return None
    return True
