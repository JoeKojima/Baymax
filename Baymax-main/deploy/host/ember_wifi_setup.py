"""Start an Ember-Setup hotspot and phone page when no saved Wi-Fi works.

wlan0 has one radio, so the hotspot and the Wi-Fi client take turns: the board
stays a normal ifupdown/wpa_supplicant client while it is online, and only
after GRACE seconds offline does it switch wlan0 to a hostapd access point.
"""
import hashlib
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time
from urllib.parse import parse_qs

IFACE = 'wlan0'
ADDRESS = '192.168.4.1'
SUPPLICANT = Path('/etc/wpa_supplicant/wpa_supplicant.conf')
SECRET = Path('/etc/ember/wifi-setup.json')
RUN = Path('/run/ember-wifi-setup')
GRACE = 90          # seconds offline before the hotspot starts
JOIN_WAIT = 60      # seconds to wait for a saved network after leaving the hotspot
RETRY_EVERY = 300   # hotspot minutes with no phone before retrying saved networks


def log(message):
    print(time.strftime('%H:%M:%S'), message, flush=True)


def run(*command, timeout=60):
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        log(f'{command[0]} failed: {error}')
        return subprocess.CompletedProcess(command, 1, '', str(error))


def online():
    # Any default route counts: Ethernet or a saved Wi-Fi network.
    return bool(run('ip', '-4', 'route', 'show', 'default').stdout.strip())


def validate(ssid, password):
    if not 1 <= len(ssid.encode('utf-8')) <= 32 or any(ord(c) < 32 for c in ssid):
        raise ValueError('The network name must be 1-32 characters.')
    if any(ord(c) < 32 for c in password):
        raise ValueError('The password contains an unsupported character.')
    if password and not (8 <= len(password.encode('utf-8')) <= 63 or re.fullmatch(r'[0-9a-fA-F]{64}', password)):
        raise ValueError('Wi-Fi passwords are 8-63 characters; leave it blank for an open network.')


def network_block(ssid, password):
    """A wpa_supplicant entry storing a derived PSK, never the typed password."""
    lines = ['network={', '    ssid=' + ssid.encode('utf-8').hex(), '    scan_ssid=1', '    priority=200']
    if password:
        psk = password.lower() if re.fullmatch(r'[0-9a-fA-F]{64}', password) else hashlib.pbkdf2_hmac(
            'sha1', password.encode('utf-8'), ssid.encode('utf-8'), 4096, 32).hex()
        lines += ['    key_mgmt=WPA-PSK', '    psk=' + psk]
    else:
        lines += ['    key_mgmt=NONE']
    return '\n'.join(lines + ['}']) + '\n'


def block_ssid(block):
    match = re.search(r'^\s*ssid=(?:"(.*)"|([0-9a-fA-F]+))\s*$', block, re.M)
    if not match:
        return None
    return match[1] if match[1] is not None else bytes.fromhex(match[2]).decode('utf-8', 'replace')


def with_network(text, ssid, password):
    """Replace any saved entry for ssid and append the new one; other networks stay."""
    kept = [block for block in re.findall(r'network=\{.*?\n\}\n?', text, re.S) if block_ssid(block) != ssid]
    header = re.sub(r'network=\{.*?\n\}\n?', '', text, flags=re.S).rstrip('\n') + '\n'
    return header + ''.join(block if block.endswith('\n') else block + '\n' for block in kept) + network_block(ssid, password)


def save_network(ssid, password, path=SUPPLICANT):
    validate(ssid, password)
    temporary = path.with_name(path.name + '.ember-new')
    temporary.write_text(with_network(path.read_text(), ssid, password))
    temporary.chmod(0o600)
    temporary.replace(path)


def scan_ssids(output):
    names = []
    for line in output.splitlines():
        match = re.match(r'\s*SSID: (.+)$', line)
        if not match or match[1].startswith('\\x00'):
            continue  # hidden network
        # iw prints non-ASCII SSID bytes as \xNN.
        name = re.sub(rb'\\x([0-9a-fA-F]{2})', lambda m: bytes.fromhex(m[1].decode()), match[1].encode()).decode('utf-8', 'replace')
        if name not in names:
            names.append(name)
    return names


def hostapd_config(ssid, password):
    if not 8 <= len(password) <= 63:
        raise ValueError('The setup hotspot password must be 8-63 characters.')
    return '\n'.join([f'interface={IFACE}', 'driver=nl80211', f'ssid={ssid}', 'country_code=US',
                      'hw_mode=g', 'channel=6', 'ieee80211n=1', 'wpa=2', 'wpa_key_mgmt=WPA-PSK',
                      'rsn_pairwise=CCMP', f'wpa_passphrase={password}', ''])


def page(networks, message=''):
    options = ''.join(f'<option>{escape(name)}</option>' for name in networks)
    notice = f'<p class="note">{escape(message)}</p>' if message else ''
    return f'''<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Ember Wi-Fi setup</title>
<style>body{{font-family:-apple-system,sans-serif;max-width:420px;margin:auto;padding:24px}}
label{{display:block;margin-top:16px}}select,input,button{{width:100%;box-sizing:border-box;padding:12px;font-size:16px;margin-top:6px}}
button{{background:#E03C31;color:#fff;border:0;border-radius:8px;margin-top:20px}}.note{{background:#fff3cd;padding:12px;border-radius:8px}}</style>
</head><body><h1>Connect Ember to Wi-Fi</h1>{notice}
<form method="post" action="/connect">
<label>Network<select name="choice">{options}<option value="">Other (type below)</option></select></label>
<label>Or type the network name<input name="ssid" autocapitalize="none" autocorrect="off"></label>
<label>Password (blank for open Wi-Fi)<input name="password" type="password"></label>
<button type="submit">Connect</button></form>
<p>Ember will leave this hotspot to try the network. If <b>{escape(hotspot_name())}</b> comes back
in about two minutes, the network could not be joined: reconnect and try again.</p></body></html>'''


def hotspot_name():
    return 'Ember-Setup-' + socket.gethostname()[-4:].upper()


class Portal:
    def __init__(self):
        self.networks, self.message = [], ''
        self.submitted = threading.Event()
        self.server = None

    def handler(portal):
        class Handler(BaseHTTPRequestHandler):
            def reply(self, status, body):
                data = body.encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                # Every URL gets the form, so phones open it as a sign-in page.
                self.reply(200, page(portal.networks, portal.message))

            def do_POST(self):
                length = max(0, min(int(self.headers.get('Content-Length') or 0), 4096))
                form = parse_qs(self.rfile.read(length).decode('utf-8', 'replace'))
                ssid = (form.get('ssid', [''])[0] or form.get('choice', [''])[0]).strip()
                password = form.get('password', [''])[0]
                try:
                    save_network(ssid, password)
                except (ValueError, OSError) as error:
                    return self.reply(400, page(portal.networks, str(error)))
                log(f'Saved Wi-Fi network {ssid!r} from the setup page')
                portal.message = f'Could not join "{ssid}". Check the password and try again.'
                portal.submitted.set()
                self.reply(200, f'<!DOCTYPE html><meta name="viewport" content="width=device-width, initial-scale=1">'
                                f'<body style="font-family:sans-serif;padding:24px"><h2>Saved</h2><p>Ember is joining '
                                f'<b>{escape(ssid)}</b> now and this hotspot will close.</p></body>')

            def log_message(self, *args):
                pass
        return Handler

    def serve(self):
        self.server = ThreadingHTTPServer((ADDRESS, 80), self.handler())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


def client_mode():
    run('ifup', IFACE, timeout=90)


def join_saved(wait=JOIN_WAIT):
    client_mode()
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if online():
            return True
        time.sleep(3)
    return online()


def start_hotspot(portal, secret):
    run('ifdown', '--force', IFACE, timeout=30)
    run('pkill', '-f', f'wpa_supplicant.*{IFACE}')
    run('ip', 'addr', 'flush', 'dev', IFACE)
    run('ip', 'link', 'set', IFACE, 'up')
    portal.networks = scan_ssids(run('iw', 'dev', IFACE, 'scan', timeout=30).stdout)
    run('ip', 'addr', 'add', ADDRESS + '/24', 'dev', IFACE)
    RUN.mkdir(mode=0o700, exist_ok=True)
    config = RUN / 'hostapd.conf'
    config.write_text(hostapd_config(hotspot_name(), secret['password']))
    config.chmod(0o600)
    processes = [
        subprocess.Popen(['hostapd', str(config)]),
        subprocess.Popen(['dnsmasq', '--keep-in-foreground', '--conf-file=/dev/null', '--no-resolv',
                          f'--interface={IFACE}', '--bind-interfaces', '--dhcp-range=192.168.4.10,192.168.4.100,10m',
                          f'--address=/#/{ADDRESS}', '--dhcp-option=option:router,' + ADDRESS]),
    ]
    portal.serve()
    log(f'Hotspot {hotspot_name()} started with {len(portal.networks)} nearby networks')
    return processes


def stop_hotspot(portal, processes):
    portal.close()
    for process in processes:
        process.terminate()
    for process in processes:
        try:
            process.wait(10)
        except subprocess.TimeoutExpired:
            process.kill()
    run('ip', 'addr', 'flush', 'dev', IFACE)
    log('Hotspot stopped')


def phones_connected():
    return bool(run('iw', 'dev', IFACE, 'station', 'dump').stdout.strip())


def setup_until_online(secret):
    portal = Portal()
    while True:
        portal.submitted.clear()
        processes = start_hotspot(portal, secret)
        started = time.monotonic()
        while not portal.submitted.wait(10):
            if time.monotonic() - started >= RETRY_EVERY and not phones_connected():
                break
        time.sleep(2)  # let the confirmation page reach the phone
        stop_hotspot(portal, processes)
        if join_saved():
            log('Online; setup hotspot closed')
            return
        if not portal.submitted.is_set():
            portal.message = ''
        log('No saved Wi-Fi network joined; reopening the hotspot')


def main():
    secret = json.loads(SECRET.read_text())
    client_mode()  # recover wlan0 if a previous run stopped mid-hotspot
    offline_since = None
    while True:
        if online():
            offline_since = None
        elif offline_since is None:
            offline_since = time.monotonic()
        elif time.monotonic() - offline_since >= GRACE:
            log(f'Offline for {GRACE} s; starting the setup hotspot')
            setup_until_online(secret)
            offline_since = None
        time.sleep(5)


if __name__ == '__main__':
    main()
