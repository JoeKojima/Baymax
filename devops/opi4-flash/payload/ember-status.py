"""Read-only LAN status: no keys, passwords, control endpoints, or capture."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from datetime import datetime
import re
import socket
import subprocess
import time

REFRESH_SECONDS = 10
QUIET_SECONDS = 15 * 60


def duration(seconds):
    minutes, seconds = divmod(max(0, int(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours}h {minutes}m' if hours else f'{minutes}m {seconds}s'


def progress(stage, log, started, service_state, now=None):
    """Expose timings and allowlisted package names, never arbitrary log contents."""
    now = time.time() if now is None else now
    if not stage.exists():
        return ['  Progress: waiting to start']
    try:
        start = (started if started.exists() else stage).stat().st_mtime
        phase = stage.stat().st_mtime
        running = service_state in ('active', 'activating')
        elapsed = (now if running else phase) - start
        lines = [f'  Elapsed: {duration(elapsed)}; last stage change: {duration(now - phase)} ago',
                 '  Installer service: ' + service_state]
        if log.exists():
            info = log.stat()
            quiet = max(0, now - info.st_mtime)
            lines.append(f'  Last installer output: {duration(quiet)} ago; log size: {info.st_size:,} bytes')
            with log.open('rb') as stream:
                stream.seek(max(0, info.st_size - 65536))
                tail = stream.read(65536).decode(errors='replace')
            package = re.findall(r'^(Setting up|Unpacking|Downloading|Building) ([a-zA-Z0-9][a-zA-Z0-9+_.:-]{0,100})(?=[\s(]|$)', tail, re.M)
            if package:
                action, name = package[-1]
                lines.append(f'  Latest package activity: {action} {name}')
            if quiet >= QUIET_SECONDS and service_state in ('active', 'activating'):
                lines.append('  CHECK: no installer output for 15+ minutes. Compilation can be quiet; this alone does not prove a stall.')
        if service_state == 'failed':
            lines.append('  FAILED: installer stopped; inspect the saved setup log.')
        return lines
    except OSError:
        return ['  Progress details temporarily unavailable; refreshing shortly']


def command(*args):
    try:
        return subprocess.check_output(args, timeout=5, stderr=subprocess.DEVNULL, text=True).strip()
    except (OSError, subprocess.SubprocessError):
        return 'not available yet'


def anydesk_id(path=Path('/var/lib/ember/anydesk-id')):
    try:
        value = path.read_text().strip()
        return value if re.fullmatch(r'[0-9]{5,20}', value) else 'not available yet'
    except OSError:
        return 'not available yet'


def status():
    lines = ['Ember board status',
             'Updated: ' + datetime.now().astimezone().isoformat(timespec='seconds'),
             f'Automatic refresh: every {REFRESH_SECONDS} seconds', '', 'Board: ' + socket.gethostname(),
             'IP addresses: ' + command('hostname', '-I'),
             'AnyDesk ID: ' + anydesk_id(),
             'AnyDesk service: ' + command('systemctl', 'is-active', 'anydesk'),
             'Desktop service (session not verified): ' + command('systemctl', 'is-active', 'lightdm'), '']
    for title, filename, log, started, unit in [
        ('System setup', '/var/lib/ember/stage', '/var/lib/ember/provision.log', '/var/lib/ember/provision.started', 'ember-provision.service'),
        ('Ember installation', '/home/ember/ember-deployment/update-status', '/home/ember/ember-deployment/logs/update.log', '/home/ember/ember-deployment/update.started', 'ember-update.service')]:
        path = Path(filename)
        try:
            value = path.read_text().strip()
        except OSError:
            value = 'waiting to start'
        lines.append(title + ': ' + value)
        lines.extend(progress(path, Path(log), Path(started), command('systemctl', 'show', unit, '--property=ActiveState', '--value')))
    lines += ['', 'Use the AnyDesk ID above and the password in your private handoff file.',
              'Once connected, open Ember Setup to enter/replace the Gemini key.',
              'If this page is unavailable, check the hotspot connected-device list.',
              'If boot failed, power off and return the SD card to the preparation PC.',
              'Logs on card: /var/lib/ember/provision.log and',
              '/home/ember/ember-deployment/logs/update.log', '']
    return '\n'.join(lines)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != '/':
            self.send_error(404)
            return
        content = status().encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Refresh', str(REFRESH_SECONDS))
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, *_):
        pass


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', 8765), Handler).serve_forever()
