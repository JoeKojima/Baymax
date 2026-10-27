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
    """Paths of the 'rknn' or 'onnx' models whose SHA-256 matches the manifest."""
    entries = manifest.get(kind) or {}
    paths = {}
    for name in NAMES:
        entry = entries.get(name)
        if not isinstance(entry, dict) or not entry.get("file") or not entry.get("sha256"):
            raise ModelError(f"manifest has no {kind} entry for {name}; re-run the converter")
        path = Path(directory) / Path(entry["file"]).name
        if not path.is_file():
            raise ModelError(f"{path.name} is missing from {directory}; re-run the converter")
        if sha256(path) != entry["sha256"]:
            raise ModelError(f"{path.name} does not match the SHA-256 recorded at conversion; re-run the converter")
        paths[name] = path
    return paths


def check_passed(manifest):
    check = manifest.get("check") or {}
    return check.get("passed") is True
