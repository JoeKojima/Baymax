"""Original dashboard, bound to localhost with no production email credentials."""
import os
from pathlib import Path
import sys
from dotenv import load_dotenv

root = Path(__file__).resolve().parent
os.chdir(root)
load_dotenv(root / ".env")
if any(os.getenv(name) for name in ("BAYMAX_EMAIL_FROM", "BAYMAX_EMAIL_PASSWORD", "BAYMAX_EMAIL_TO")):
    sys.exit("Remove production email credentials from the PC environment before starting its dashboard.")
from baymax_app import app, STATIC_DIR
os.makedirs(STATIC_DIR, exist_ok=True)
app.run(host="127.0.0.1", port=5000, debug=False)
