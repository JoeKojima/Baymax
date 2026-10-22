"""Opt-in desktop adapter for the existing version-10 memory implementation.

No capture or provider calls at import. Model preparation contacts Hugging Face
only when explicitly requested by Setup. Conversation/prediction still use the
unchanged Gemini models in version 10.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, deque
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from ember_timing import Timeline

FIELDS = {
    "memory_trace": "BAYMAX_MEMORY_TRACE", "memory_exact": "BAYMAX_MEMORY_EXACT",
    "memory_compare": "BAYMAX_MEMORY_COMPARE", "memory_prepared": "BAYMAX_MEMORY_PREPARED",
    "memory_prediction": "BAYMAX_MEMORY_PREDICTION",
}
MODEL_FILES = ("model.onnx", "config.json", "tokenizer.json", "tokenizer_config.json",
               "special_tokens_map.json", "vocab.txt")


def public_settings(values):
    return {"runtime": values.get("BAYMAX_DESKTOP_RUNTIME", "desktop") or "desktop",
            "memory_patient": values.get("BAYMAX_MEMORY_PATIENT", "") or "",
            "memory_calibration": values.get("BAYMAX_MEMORY_CALIBRATION", "") or "",
            **{field: values.get(setting, "0") == "1" for field, setting in FIELDS.items()}}


def validate_settings(payload, old):
    settings = public_settings(old)
    settings.update({k: payload[k] for k in settings if k in payload})
    if settings["runtime"] not in ("desktop", "memory"):
        raise ValueError("Choose the original desktop or memory testing version.")
    patient = settings["memory_patient"]
    if not isinstance(patient, str) or (patient and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", patient)):
        raise ValueError("Use a short test-person ID such as demo01, with letters, digits, underscores or hyphens.")
    if any(not isinstance(settings[field], bool) for field in FIELDS):
        raise ValueError("Memory switches must be on or off.")
    calibration = settings["memory_calibration"]
    if not isinstance(calibration, str) or len(calibration) > 4096 or any(c in calibration for c in "\r\n\x00"):
        raise ValueError("Enter a valid calibration file path.")
    if settings["runtime"] == "memory" and not patient and any(settings[f] for f in FIELDS if f != "memory_trace"):
        raise ValueError("Enter a test-person ID before enabling memory experiments.")
    return {"BAYMAX_DESKTOP_RUNTIME": settings["runtime"], "BAYMAX_MEMORY_PATIENT": patient,
            "BAYMAX_MEMORY_CALIBRATION": calibration,
            **{setting: "1" if settings[field] else "0" for field, setting in FIELDS.items()}}


def model_directory(root=ROOT):
    return Path(root) / "all-MiniLM-L6-v2-onnx"


def ensure_model(root=ROOT):
    """Download official existing ONNX weights once; no export or capture."""
    destination = model_directory(root)
    if all((destination / name).is_file() and (destination / name).stat().st_size for name in MODEL_FILES):
        return destination
    from huggingface_hub import HfApi, hf_hub_download
    repository = "sentence-transformers/all-MiniLM-L6-v2"
    revision = HfApi(token=False).model_info(repository).sha
    print("Preparing MiniLM memory model from Hugging Face (one-time download)...", flush=True)
    destination.mkdir(parents=True, exist_ok=True)
    for name in MODEL_FILES:
        remote = "onnx/model.onnx" if name == "model.onnx" else name
        cached = hf_hub_download(repository, filename=remote, revision=revision, token=False)
        temporary = destination / (name + ".download")
        shutil.copyfile(cached, temporary)
        temporary.replace(destination / name)
    (destination / ".source.json").write_text(json.dumps({"repository": repository, "revision": revision}))
    return destination


def configure_runtime(root, values):
    """Resolve the selected core before key preflight; immutable for this process."""
    root = Path(root)
    settings = public_settings(validate_settings({}, values))
    if settings["runtime"] == "desktop":
        return root / "realtime_gemini_8.py"
    data = root / ".memory-data"
    data.mkdir(exist_ok=True, mode=0o700)
    config = root / ".memory-desktop.toml"
    sections = {"camera": {"pose_tracking": False}, "fall_detection": {"enabled": False},
                "memory": {"retrieval_enabled": True, "patient_id": settings["memory_patient"],
                           "cache_enabled": settings["memory_exact"],
                           "cache_comparison_enabled": settings["memory_compare"],
                           "prepared_cache_enabled": settings["memory_prepared"],
                           "prediction_enabled": settings["memory_prediction"],
                           "calibration_path": settings["memory_calibration"]},
                "observability": {"enabled": settings["memory_trace"], "trace_sample_rate": .1, "benchmark": False}}
    lines = []
    for section, fields in sections.items():
        lines.append(f"[{section}]")
        lines.extend(f"{key} = {json.dumps(value)}" for key, value in fields.items())
        lines.append("")
    fd, temporary = tempfile.mkstemp(prefix=".memory-config-", dir=root)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write("\n".join(lines))
        os.replace(temporary, config)
    finally:
        Path(temporary).unlink(missing_ok=True)
    os.environ["EMBER_DEVICE_CONFIG"] = str(config)
    os.environ["BAYMAX_DATA_DIR"] = str(data)
    os.environ["BAYMAX_MEMORY_MODEL_DIR"] = str(model_directory(root))
    return root / "realtime_gemini_10.py"


def seed_demo(root, values):
    """Explicit local-only operation; never writes the legacy shared database."""
    from ember_memory import PatientContext
    patient = public_settings(values)["memory_patient"]
    context = PatientContext.resolve(Path(root) / ".memory-data", patient)
    if context is None:
        raise ValueError("Save a test-person ID before adding fictional memories.")
    if not (model_directory(root) / "model.onnx").is_file():
        raise ValueError("Run the updated Setup once to prepare the MiniLM model.")
    from semantic_embedder import SemanticEmbedder
    context.prepare()
    embedder = SemanticEmbedder(model_dir=model_directory(root), chroma_dir=context.chroma_dir,
                                collection_name="user_memories", verbose=False)
    facts = ["The fictional test person's daughter Sarah enjoys gardening.",
             "Sarah grows roses and tomatoes in her garden.",
             "The fictional test person visits Sarah's garden on Sundays.",
             "The fictional test person plays piano and enjoys jazz music.",
             "A fictional music club meets on Fridays.",
             "The fictional test person has a doctor appointment on Monday."]
    embedder.save(facts, ids=[f"desktop_demo_{i}" for i in range(len(facts))],
                  metadatas=[{"source": "fictional_desktop_demo"} for _ in facts])
    return {"count": len(facts), "message": "Six fictional memories added. Start Ember to use them."}


async def check_memory(root, values):
    """Exercise real local retrieval twice; no audio, video or Gemini request."""
    from ember_memory import MemoryService, MemorySettings, PatientContext, model_fingerprint
    from semantic_embedder import SemanticEmbedder
    settings = public_settings(values)
    patient = PatientContext.resolve(Path(root) / ".memory-data", settings["memory_patient"])
    if patient is None:
        raise ValueError("Save a test-person ID first.")
    patient.prepare()
    embedder = SemanticEmbedder(model_dir=model_directory(root), chroma_dir=patient.chroma_dir,
                                collection_name="user_memories", verbose=False)
    service = MemoryService(embedder, patient.patient_id, model_fingerprint(model_directory(root)),
        settings=MemorySettings(exact_enabled=settings["memory_exact"],
                                comparison_enabled=settings["memory_compare"],
                                prepared_enabled=settings["memory_prepared"],
                                calibration_path=settings["memory_calibration"]))
    sid = uuid.uuid4().hex
    try:
        await service.start_session(sid)
        first = await service.lookup("Sarah enjoys gardening and grows roses", sid, uuid.uuid4().hex)
        second = await service.lookup("Sarah enjoys gardening and grows roses", sid, uuid.uuid4().hex)
        if first.source in ("error", "obsolete", "disabled") or second.source in ("error", "obsolete", "disabled"):
            raise RuntimeError("Local memory lookup failed")
        return {"message": f"Local check: {first.source} → {second.source}; {len(second.records)} memories. No Gemini call or capture.",
                "first_source": first.source, "second_source": second.source,
                "first_ms": first.retrieval_ms, "second_ms": second.retrieval_ms,
                "count": len(second.records)}
    finally:
        await service.close()


class DesktopTimeline(Timeline):
    """A small numeric monitor summary, independent of sampled trace export."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._summary_lock = threading.Lock()
        self._retrievals = deque(maxlen=128)
        self._sources = Counter()
        self._last_source = "No lookup yet"
        self._last_ms = None

    def event(self, name, session_id="", turn_id="", **attributes):
        if name == "retrieval.ready":
            with self._summary_lock:
                self._last_source = attributes.get("source", "unknown")
                self._sources[self._last_source] += 1
        super().event(name, session_id, turn_id, **attributes)

    def duration(self, name, elapsed_ms, session_id="", turn_id="", **attributes):
        if name == "retrieval.total":
            with self._summary_lock:
                self._last_ms = elapsed_ms
                self._retrievals.append(elapsed_ms)
        super().duration(name, elapsed_ms, session_id, turn_id, **attributes)

    def summary(self):
        with self._summary_lock:
            return {"memory_last_source": self._last_source, "memory_last_ms": self._last_ms,
                    "memory_lookups": sum(self._sources.values()),
                    "memory_exact_hits": self._sources["exact_cache"],
                    "memory_prepared_hits": self._sources["prepared_cache"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "seed", "check"))
    args = parser.parse_args()
    if args.action == "prepare":
        ensure_model()
        print("MiniLM memory model ready.")
    else:
        from dev_settings import read_configuration
        from runtime_monitor import read_status
        if read_status().get("state") not in ("offline", "stopped"):
            raise ValueError("Stop Ember before changing its memory database.")
        values = read_configuration(ROOT / ".env")
        result = seed_demo(ROOT, values) if args.action == "seed" else asyncio.run(check_memory(ROOT, values))
        print(json.dumps(result))


if __name__ == "__main__":
    main()
