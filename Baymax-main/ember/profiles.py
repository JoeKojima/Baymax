"""What a robot runs: which core, which features and which setting defaults.

v8 and mvp run on the new core (ember/), with the built-in features in v8's
order. v9 and v10 still run their own files unchanged: they differ from v8 in
about 1,100 lines each (memory retrieval and summaries, inline fall detection,
conversation initiation, face visibility), and docs/architecture/MIGRATION.md
lists each difference and the plugin it becomes. Until they are ported and
checked side by side the way v8 was, the legacy file is the reference.
"""
from dataclasses import dataclass, field
from typing import Callable, Optional

from ember import registry

MVP_DEFAULTS = {  # run_mvp.py applies these the same way (setdefault) before starting.
    "BAYMAX_WAIT_CUE_ENABLED": "0",      # The fixed "hmm" felt unsettling.
    "BAYMAX_INTERRUPTION_ENABLED": "0",
    "BAYMAX_VOICE_ANALYSIS": "0",
}


@dataclass(frozen=True)
class Profile:
    name: str
    about: str
    core: str                              # "ember" (new core) or "legacy" (run legacy_file as is)
    legacy_file: str
    defaults: dict = field(default_factory=dict)
    plugins: Optional[Callable[[], list]] = None


PROFILES = {
    "v8": Profile("v8", "Version-8 runtime (desktop run_pc.py and robots on v8)", "ember", "realtime_gemini_8.py",
                  plugins=registry.builtin),
    "mvp": Profile("mvp", "v8 with the MVP defaults (no waiting cue, no interruptions, no voice analysis)", "ember",
                   "realtime_gemini_8.py", defaults=MVP_DEFAULTS, plugins=registry.builtin),
    "v9": Profile("v9", "Version 9 (conversation initiation), legacy file", "legacy", "realtime_gemini_9.py"),
    "v10": Profile("v10", "Fleet core: v9 + fall detection + memory, legacy file (deploy/ uses it)", "legacy",
                   "realtime_gemini_10.py"),
}


def get(name):
    try:
        return PROFILES[name]
    except KeyError:
        raise SystemExit(f"Unknown profile {name!r}; choose from {', '.join(PROFILES)}") from None
