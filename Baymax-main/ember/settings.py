"""The core's own settings, read once when the runtime is built.

The variable names, defaults and meanings are the version-8 runtime's, which read
them with scattered os.getenv calls at import time. Features (toolkits, eyes,
audio cleanup) still read their own variables themselves, when they need them.
"""
from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    api_key: str = None                 # None, like v8's os.getenv: genai then looks in the environment
    hardware_profile: str = ""          # "pc" on desktops (pc_hardware selects devices); robot otherwise
    audio_retention_days: float = 1.0   # day_utterance/ segments older than this are pruned; <=0 keeps all
    voice_analysis: bool = True         # post-session voice biomarker analysis (run_mvp.py turns it off)
    webapp_url: str = "http://localhost:5000"
    mjpeg_port: int = 8080

    @property
    def pc(self):
        return self.hardware_profile == "pc"

    @classmethod
    def load(cls, environ=None):
        env = os.environ if environ is None else environ
        return cls(
            api_key=env.get("GOOGLE_API_KEY"),
            hardware_profile=env.get("BAYMAX_HARDWARE_PROFILE", ""),
            audio_retention_days=float(env.get("BAYMAX_AUDIO_RETENTION_DAYS", "1")),
            voice_analysis=env.get("BAYMAX_VOICE_ANALYSIS", "1") != "0",
        )
