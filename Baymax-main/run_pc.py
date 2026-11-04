"""Validate Gemini access, then run the version-8 runtime with PC hardware.

BAYMAX_CORE picks the implementation: "legacy" runs realtime_gemini_8.py as it
is; "ember" runs the same behaviour from ember/ (see docs/architecture/).
"""
import os
from pathlib import Path
import runpy
import sys
from dotenv import load_dotenv
from secret_config import CredentialError, gemini_key
from gemini_preflight import PreflightError, validate_key

ROOT = Path(__file__).resolve().parent
CORES = {"legacy": ROOT / "realtime_gemini_8.py", "ember": ROOT / "ember/session/config.py"}


def core():
    name = os.getenv("BAYMAX_CORE", "legacy").strip().lower() or "legacy"
    if name not in CORES:
        raise ValueError(f"BAYMAX_CORE must be one of {', '.join(CORES)}")
    return name


def main():
    os.chdir(ROOT)
    load_dotenv(ROOT / ".env", override=True, interpolate=False)
    os.environ["BAYMAX_HARDWARE_PROFILE"] = "pc"
    os.environ.setdefault("BAYMAX_AUDIO_ENHANCEMENT", "1")
    try:
        key = gemini_key()
    except CredentialError as error:
        print(f"Startup stopped: {error}", file=sys.stderr)
        return 1
    if not key:
        print("Open Setup, paste your Gemini key, and Save configuration. Then start again. Do not paste the key into chat.", file=sys.stderr)
        return 1
    if any(os.getenv(name) for name in ("BAYMAX_EMAIL_FROM", "BAYMAX_EMAIL_PASSWORD", "BAYMAX_EMAIL_TO")):
        print("Email credentials detected. Remove them from this development config/environment before starting.", file=sys.stderr)
        return 1
    try:
        chosen = core()
    except ValueError as error:
        print(f"Startup stopped: {error}", file=sys.stderr)
        return 1
    print("Checking Gemini key and configured model access with Google (no audio/video sent)...", flush=True)
    try:
        validate_key(key, CORES[chosen])
    except PreflightError as error:
        print(f"Startup stopped: {error}", file=sys.stderr)
        return 1
    print("Google accepted the key; configured models are accessible. Live-session quota/access will be checked when connecting.", flush=True)
    print("Starting upstream Gemini Live version 8 with PC audio devices"
          + (" (ember core)." if chosen == "ember" else "."), flush=True)
    print("Microphone audio and camera frames are sent to Gemini; upstream also records session audio locally.", flush=True)
    os.environ["GOOGLE_API_KEY"] = key  # Process-local only; never copy a key file into .env.
    if chosen == "ember":
        from ember.main import run
        return run("v8", core="ember")
    runpy.run_path(str(CORES["legacy"]), run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
