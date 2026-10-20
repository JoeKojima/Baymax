"""Start the v10 Memory Palace runtime with desktop device adapters."""
import argparse
import json
import time
import os
from pathlib import Path
import runpy
import sys

from dotenv import load_dotenv
from gemini_preflight import PreflightError, validate_key
from secret_config import CredentialError, gemini_key

ROOT = Path(__file__).resolve().parent


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stop", action="store_true", help="Request graceful shutdown of the local monitored runtime")
    args = parser.parse_args(argv)
    os.chdir(ROOT)
    load_dotenv(ROOT / '.env', override=True, interpolate=False)
    data = Path(os.environ.setdefault('BAYMAX_DATA_DIR', str(ROOT / '.local-memory')))
    os.environ.setdefault('BAYMAX_MONITOR_DIR', str(data))
    if args.stop:
        import runtime_monitor as monitor
        status = monitor.read_status()
        if status.get('state') in ('offline', 'stopped'):
            print('Ember is already stopped.')
            return 0
        temporary = monitor.STOP_PATH.with_suffix('.tmp')
        temporary.write_text(json.dumps({'pid': status['pid'], 'requested_at': time.time()}), encoding='utf-8')
        os.replace(temporary, monitor.STOP_PATH)
        print('Stop requested. Ember will save the pending conversation before exiting.')
        return 0
    if any(os.getenv(n) for n in ('BAYMAX_EMAIL_FROM', 'BAYMAX_EMAIL_PASSWORD', 'BAYMAX_EMAIL_TO', 'BAYMAX_DEVICE_KEY')):
        print('Remove email/production device credentials from this local test configuration before starting.', file=sys.stderr)
        return 1
    try:
        key = gemini_key()
        if not key:
            raise CredentialError('Configure a Gemini key file in local settings first.')
        validate_key(key, ROOT / 'realtime_gemini_10.py')
    except (CredentialError, PreflightError) as error:
        print(f'Startup stopped: {error}', file=sys.stderr)
        return 1
    os.environ['GOOGLE_API_KEY'] = key
    os.environ['BAYMAX_HARDWARE_PROFILE'] = 'pc'
    os.environ['BAYMAX_DEBUG_MONITOR'] = '1'
    os.environ.setdefault('BAYMAX_AUDIO_ENHANCEMENT', '1')
    data.mkdir(parents=True, exist_ok=True)
    print('Starting local Memory Palace. Microphone and camera go to Gemini; local recordings and memories persist.', flush=True)
    runpy.run_path(str(ROOT / 'realtime_gemini_10.py'), run_name='__main__')
    return 0


if __name__ == '__main__':
    sys.exit(main())
