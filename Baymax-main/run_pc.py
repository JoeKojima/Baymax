"""Validate Gemini access, then run the explicitly selected desktop core."""
import os
from pathlib import Path
import runpy
import sys
from dotenv import load_dotenv
from gemini_preflight import PreflightError, validate_key

ROOT = Path(__file__).resolve().parent


def main():
    os.chdir(ROOT)
    load_dotenv(ROOT / ".env", override=True, interpolate=False)
    os.environ["BAYMAX_HARDWARE_PROFILE"] = "pc"
    from toolkit.memory_desktop import configure_runtime
    core = configure_runtime(ROOT, os.environ)
    key = os.getenv("GOOGLE_API_KEY", "")
    if not key:
        print("Open Setup, paste your Gemini key, and Save configuration. Then start again. Do not paste the key into chat.", file=sys.stderr)
        return 1
    if any(os.getenv(name) for name in ("BAYMAX_EMAIL_FROM", "BAYMAX_EMAIL_PASSWORD", "BAYMAX_EMAIL_TO")):
        print("Email credentials detected. Remove them from this development config/environment before starting.", file=sys.stderr)
        return 1
    print("Checking Gemini key and configured model access with Google (no audio/video sent)...", flush=True)
    try:
        validate_key(key, core)
    except PreflightError as error:
        print(f"Startup stopped: {error}", file=sys.stderr)
        return 1
    print("Google accepted the key; configured models are accessible. Live-session quota/access will be checked when connecting.", flush=True)
    print(f"Starting {core.name} with PC audio devices.", flush=True)
    print("Microphone audio and camera frames are sent to Gemini; upstream also records session audio locally.", flush=True)
    runpy.run_path(str(core), run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
