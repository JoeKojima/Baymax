"""Run the original version-8 Gemini entry point with PC hardware selection."""
import os
from pathlib import Path
import runpy
import sys
from dotenv import load_dotenv

root = Path(__file__).resolve().parent
os.chdir(root)
load_dotenv(root / ".env", override=True, interpolate=False)
os.environ["BAYMAX_HARDWARE_PROFILE"] = "pc"
if not os.getenv("GOOGLE_API_KEY"):
    sys.exit("Open devUI, paste your Gemini key, and Save configuration. Then start again. Do not paste the key into chat.")
if any(os.getenv(name) for name in ("BAYMAX_EMAIL_FROM", "BAYMAX_EMAIL_PASSWORD", "BAYMAX_EMAIL_TO")):
    sys.exit("Email credentials detected. Remove them from this development config/environment before starting.")
print("Starting upstream Gemini Live version 8 with PC audio devices.", flush=True)
print("Microphone audio and camera frames are sent to Gemini; upstream also records session audio locally.", flush=True)
runpy.run_path(str(root / "realtime_gemini_8.py"), run_name="__main__")
