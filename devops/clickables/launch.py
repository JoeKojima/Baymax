"""Launch local Ember tools relative to this folder; no installation or uploads."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

BASE = Path(__file__).resolve().parents[2]
RUNTIME = BASE if (BASE / 'run_pc.py').exists() else BASE / 'Baymax-main'
sys.path.insert(0, str(RUNTIME))
from runtime_monitor import read_status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['setup', 'monitor', 'start', 'stop'])
    action = parser.parse_args().action
    if action in ('setup', 'monitor'):
        command = [sys.executable, str(RUNTIME / 'devUI.py')]
        if action == 'monitor':
            command.append('--monitor')
        return subprocess.call(command, cwd=RUNTIME)
    status = read_status(RUNTIME / '.runtime-status.json')
    active = status.get('state') not in ('offline', 'stopped')
    if action == 'start':
        if active:
            print('Ember is already running. Open Monitor to see its activity.')
            return 0
        print('Starting Ember. Speak when it says Connected. Ctrl+C also stops it.')
        print('Location/weather tools contact IPWho and Open-Meteo if enabled in your configuration.')
        try:
            return subprocess.call([sys.executable, str(RUNTIME / 'run_pc.py')], cwd=RUNTIME)
        except KeyboardInterrupt:
            return 0
    if not active:
        print('Ember is already stopped.')
        return 0
    if status.get('state') == 'stopping':
        print('Ember is already shutting down. Check Monitor for progress.')
        return 0
    path = RUNTIME / '.runtime-stop.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'pid': status['pid'], 'requested_at': time.time()}), encoding='utf-8')
    temporary.replace(path)
    print('Stop requested. Ember will finish local session analysis before exiting.')
    for _ in range(20):
        time.sleep(.5)
        if read_status(RUNTIME / '.runtime-status.json').get('state') in ('stopped', 'offline'):
            print('Ember is stopped.')
            return 0
    print('Shutdown is still finishing. Check Monitor or the Start Ember window.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
