from flask import Flask, request, render_template_string, redirect, url_for
from html import escape
import subprocess
import threading
import time

app = Flask(__name__)

# The AP radio. The hotspot lives here permanently and must never be torn down --
# it is the only way back in if the client connection fails.
AP_IFACE = "wlo1"
HOTSPOT = "Baymax_Hotspot"

# Last attempt's outcome, shown on the status page.
last_status = {"ssid": None, "state": "idle", "detail": ""}


def uplink_iface():
    """Pick the Wi-Fi device to join the user's network with: any managed wifi
    device that is NOT the AP radio (i.e. the USB dongle). Never AP_IFACE, or we
    would kill the hotspot we are being reached over."""
    try:
        out = subprocess.run(
            ["nmcli", "-t", "-f", "DEVICE,TYPE", "device", "status"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        for line in out.splitlines():
            parts = line.split(":")
            if len(parts) >= 2 and parts[1] == "wifi" and parts[0] != AP_IFACE:
                return parts[0]
    except Exception as e:
        print(f"uplink_iface lookup failed: {e}", flush=True)
    return None


def profile_exists(name):
    try:
        out = subprocess.run(
            ["nmcli", "-t", "-f", "NAME", "connection", "show"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        return name in out.splitlines()
    except Exception:
        return False


SETUP_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <title>Baymax Setup</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { font-family: -apple-system, sans-serif; padding: 30px; max-width: 400px; margin: auto; text-align: center; }
        input { width: 90%; padding: 12px; margin: 10px 0; border-radius: 8px; border: 1px solid #ccc; font-size: 16px; }
        button { width: 95%; padding: 15px; background: #E03C31; color: white; border: none; border-radius: 8px; font-size: 18px; font-weight: bold; cursor: pointer; margin-top: 15px;}
        .note { color: #666; font-size: 14px; margin-top: 20px; }
    </style>
</head>
<body>
    <h1 style="color: #333;">Hello, I am Baymax.</h1>
    <p style="color: #666; margin-bottom: 25px;">Please connect me to your Wi-Fi network.</p>
    <form action="/setup" method="POST">
        <input type="text" name="ssid" placeholder="Wi-Fi Network Name" required>
        <input type="password" name="password" placeholder="Wi-Fi Password (leave blank if none)">
        <button type="submit">Connect Baymax</button>
    </form>
    <p class="note">This hotspot stays up the whole time, so you will not be
    disconnected. Check <a href="/status">status</a> after submitting.</p>
</body>
</html>
"""


def switch_network(ssid, password):
    """Join the user's Wi-Fi on the uplink radio. The hotspot is left running
    throughout, so whoever submitted this form keeps their connection and can
    read the result on /status."""
    last_status.update({"ssid": ssid, "state": "connecting", "detail": ""})
    time.sleep(2)

    iface = uplink_iface()
    if not iface:
        msg = f"no uplink Wi-Fi device found (all wifi devices are {AP_IFACE})"
        print(f"Connect to {ssid} failed: {msg}", flush=True)
        last_status.update({"state": "failed", "detail": msg})
        return

    print(f"Connecting to {ssid} on {iface}...", flush=True)

    # Reuse an existing profile of the same name instead of letting nmcli mint
    # "<ssid> 1", "<ssid> 2", ... on every attempt.
    if profile_exists(ssid):
        cmds = []
        if password:
            cmds.append(["nmcli", "connection", "modify", ssid,
                         "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password])
        cmds.append(["nmcli", "connection", "up", ssid, "ifname", iface])
    else:
        base = ["nmcli", "device", "wifi", "connect", ssid, "ifname", iface]
        if password:
            base += ["password", password]
        cmds = [base]

    result = None
    for cmd in cmds:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        if result.returncode != 0:
            break

    if result is not None and result.returncode == 0:
        print(f"Baymax is online on {iface}!", flush=True)
        last_status.update({"state": "connected", "detail": f"joined {ssid} on {iface}"})
    else:
        err = (result.stderr or result.stdout or "unknown error").strip() if result else "no command ran"
        print(f"Connection to {ssid} failed: {err}", flush=True)
        last_status.update({"state": "failed", "detail": err})
    # NOTE: the hotspot is never taken down or brought back up here. It is
    # always-on via NetworkManager (connection.autoconnect=yes) and runs on a
    # different radio from the uplink, so the two coexist.


@app.route('/', methods=['GET'])
def home():
    return render_template_string(SETUP_PAGE)


@app.route('/status', methods=['GET'])
def status():
    s = last_status
    return f"""
    <div style="font-family: -apple-system, sans-serif; text-align: center; padding: 40px;">
        <h2>Status: {escape(str(s['state']))}</h2>
        <p>Network: <b>{escape(str(s['ssid'] or '-'))}</b></p>
        <p style="color:#666;">{escape(str(s['detail']))}</p>
        <p><a href="/">Back</a> &middot; <a href="/status">Refresh</a></p>
    </div>
    """


@app.route('/setup', methods=['POST'])
def setup():
    ssid = request.form.get('ssid', '').strip()
    # Use .strip() just in case the phone's keyboard added an accidental space
    password = request.form.get('password', '').strip()
    if not ssid:
        return redirect(url_for('home'))
    threading.Thread(target=switch_network, args=(ssid, password), daemon=True).start()
    safe_ssid = escape(ssid)
    return f"""
    <div style="font-family: -apple-system, sans-serif; text-align: center; padding: 40px;">
        <h2 style="color: #28a745;">Connecting...</h2>
        <p>Attempting to connect to <b>{safe_ssid}</b>.</p>
        <p>You will stay on the Baymax hotspot while this happens.</p>
        <p><a href="/status">Check status</a></p>
    </div>
    """


@app.errorhandler(404)
def page_not_found(e):
    return redirect(url_for('home'))


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=80)
