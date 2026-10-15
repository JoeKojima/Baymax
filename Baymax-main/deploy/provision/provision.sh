#!/bin/bash
# Turns a fresh Debian 13 desktop LattePanda into an Ember unit, unattended.
# Runs from the bundle that prepare_unit.sh made (usually on a USB stick):
#
#   sudo bash /media/<user>/<stick>/ember-unit-002/provision.sh
#
# The board must be online over Ethernet or a USB Wi-Fi dongle: wlo1 becomes
# the unit's hotspot. Safe to run again. Reboots when done (--no-reboot to skip).
# DRY_RUN=1 prints the changes instead of making them.
set -euo pipefail

B=$(cd "$(dirname "$0")" && pwd)
REBOOT=1
[ "${1:-}" = "--no-reboot" ] && REBOOT=0
DRY_RUN=${DRY_RUN:-0}

LOG=/var/log/ember/provision.log
[ "$DRY_RUN" = 1 ] && LOG=/dev/null
mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
exec > >(tee -a "$LOG") 2>&1

step() { echo; echo "== $*"; }
die()  { echo "ERROR: $*"; exit 1; }
run()  { if [ "$DRY_RUN" = 1 ]; then echo "+ $*"; else "$@"; fi; }
cfg()  { EMBER_DEVICE_CONFIG="$B/device.toml" /usr/bin/python3 "$B/host/device_config.py" "$1"; }

# ── Checks ──────────────────────────────────────────────────────────────
step "Checks"
[ "$(id -u)" = 0 ] || [ "$DRY_RUN" = 1 ] || die "run with sudo"
[ -f "$B/device.toml" ] && [ -d "$B/host" ] || die "run this from an ember-unit-* bundle"
. /etc/os-release
[ "${VERSION_CODENAME:-}" = trixie ] || echo "WARNING: built for Debian 13 (trixie); this is ${PRETTY_NAME:-unknown}"
[ "$(uname -m)" = x86_64 ] || die "the Ember image is x86-64 only for now (this is $(uname -m))"

SERIAL=$(cfg device.serial)
CORE_USER=$(cfg system.user)
TZ_NAME=$(cfg system.timezone)
AP_IFACE=$(cfg network.ap_iface)
HOTSPOT=$(cfg network.hotspot_connection)
IMAGE="$(cfg update.image):$(cfg update.channel)"
echo "Unit $SERIAL, image $IMAGE, hotspot $HOTSPOT on $AP_IFACE"

if ! id "$CORE_USER" >/dev/null 2>&1; then
    # The board's desktop account has another name: use the first regular user.
    OTHER=$(getent passwd 1000 | cut -d: -f1)
    [ -n "$OTHER" ] || die "no user $CORE_USER and no uid 1000 user"
    echo "No user $CORE_USER; using $OTHER"
    CORE_USER=$OTHER
    run sed -i "/^\[system\]/,/^\[/ s/^user = .*/user = \"$CORE_USER\"/" "$B/device.toml"
fi
CORE_UID=$(id -u "$CORE_USER")
[ "$CORE_UID" = 1000 ] || echo "WARNING: $CORE_USER has uid $CORE_UID; the containers expect 1000 (EMBER_UID)"

curl -s -o /dev/null --max-time 10 https://ghcr.io/v2/ || die "no internet (connect Ethernet or a USB Wi-Fi dongle)"
UPLINK=$(ip route show default | awk '{print $5; exit}')
HOTSPOT_NOW=1
if ! ip link show "$AP_IFACE" >/dev/null 2>&1; then
    echo "WARNING: no $AP_IFACE; hotspot not set up"
    HOTSPOT_NOW=0
elif [ "$UPLINK" = "$AP_IFACE" ]; then
    echo "WARNING: internet is on $AP_IFACE, which the hotspot needs. The hotspot is created"
    echo "         but starts only after reboot; give the unit a USB Wi-Fi dongle or Ethernet."
    HOTSPOT_NOW=0
fi

# ── Time zone ───────────────────────────────────────────────────────────
step "Time zone $TZ_NAME"
run timedatectl set-timezone "$TZ_NAME"

# ── Packages ────────────────────────────────────────────────────────────
step "Packages"
# Before Docker's first start: by default it rewrites the firewall and blocks
# forwarding, which breaks the hotspot's shared internet. Host networking
# means the containers don't need Docker's firewall rules.
run mkdir -p /etc/docker
if [ "$DRY_RUN" = 1 ]; then echo "+ write /etc/docker/daemon.json"
else echo '{"iptables": false, "ip6tables": false}' > /etc/docker/daemon.json; fi
run apt-get update
run env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    docker.io docker-compose python3-flask pulseaudio-utils iw bind9-host wget curl psmisc
run systemctl restart docker

# ── Settings, secrets, files ────────────────────────────────────────────
step "Settings and secrets"
run install -d -m 755 /etc/ember
run install -m 644 "$B/device.toml" /etc/ember/device.toml
if [ -f "$B/secrets.env" ]; then
    run install -m 640 -g docker "$B/secrets.env" /etc/ember/secrets.env
    run rm -f "$B/secrets.env"      # don't leave the shared keys on the stick
    echo "secrets installed and removed from the bundle"
elif [ -f /etc/ember/secrets.env ]; then
    echo "bundle has no secrets; keeping the installed /etc/ember/secrets.env"
else
    die "no secrets.env in the bundle and none installed"
fi

step "Robot files in /opt/ember"
run install -d -m 755 /opt/ember /var/log/ember
run install -m 755 "$B/host/startup.sh" "$B/host/ember-update.sh" /opt/ember/
run install -m 644 "$B/host/baymax_portal.py" "$B/host/device_config.py" "$B/host/compose.yaml" /opt/ember/
[ -f "$B/host/REVISION" ] && run install -m 644 "$B/host/REVISION" /opt/ember/
run install -d -m 700 -o "$CORE_USER" -g "$CORE_USER" /var/lib/ember

# ── Desktop session (PipeWire audio) ────────────────────────────────────
step "Auto-login for $CORE_USER"
# The mic and speaker go through the user's PipeWire, which runs in the
# desktop session; auto-login starts it at every boot without a keyboard.
GDM=/etc/gdm3/daemon.conf
if [ -f "$GDM" ]; then
    if [ "$DRY_RUN" = 1 ]; then echo "+ set AutomaticLogin=$CORE_USER in $GDM"
    else
        python3 - "$GDM" "$CORE_USER" <<'EOF'
import re, sys
path, user = sys.argv[1:]
text = open(path).read()
text = re.sub(r"(?m)^#?\s*AutomaticLoginEnable\s*=.*\n", "", text)
text = re.sub(r"(?m)^#?\s*AutomaticLogin\s*=.*\n", "", text)
text = re.sub(r"(?m)^\[daemon\]\n", f"[daemon]\nAutomaticLoginEnable = true\nAutomaticLogin = {user}\n", text, count=1)
open(path, "w").write(text)
EOF
    fi
else
    echo "WARNING: no GDM ($GDM); set up auto-login for $CORE_USER by hand"
fi
run loginctl enable-linger "$CORE_USER"

# ── Image ───────────────────────────────────────────────────────────────
step "Robot software $IMAGE"
run docker pull "$IMAGE"
run docker tag "$IMAGE" ember:current

# ── Hotspot ─────────────────────────────────────────────────────────────
step "Hotspot $HOTSPOT"
. "$B/hotspot.env"
[ "$SSID" = "$HOTSPOT" ] || die "hotspot.env ($SSID) doesn't match device.toml ($HOTSPOT)"
if nmcli -t -f NAME connection show | grep -qx "$HOTSPOT"; then
    run nmcli connection delete "$HOTSPOT"
fi
# Same settings as unit 001's Baymax_Hotspot.
run nmcli connection add type wifi ifname "$AP_IFACE" con-name "$HOTSPOT" ssid "$SSID" \
    connection.autoconnect yes connection.autoconnect-priority 10 \
    802-11-wireless.mode ap 802-11-wireless.band bg 802-11-wireless.channel 1 \
    ipv4.method shared \
    wifi-sec.key-mgmt wpa-psk wifi-sec.proto rsn wifi-sec.pairwise ccmp wifi-sec.pmf disable \
    wifi-sec.psk "$PASSWORD"
[ "$HOTSPOT_NOW" = 1 ] && run nmcli connection up "$HOTSPOT"

# ── Services ────────────────────────────────────────────────────────────
step "Services"
run install -m 644 "$B/host/ember.service" "$B/host/ember-update.service" "$B/host/ember-update.timer" /etc/systemd/system/
run systemctl daemon-reload
run systemctl enable ember.service ember-update.timer

IMAGE_ID=$(docker image inspect -f '{{.Id}}' ember:current 2>/dev/null || echo "?")
[ "$DRY_RUN" = 1 ] || cat > /etc/ember/provisioned <<EOF
serial=$SERIAL
date=$(date -Is)
image=$IMAGE
image_id=$IMAGE_ID
revision=$(cat "$B/host/REVISION" 2>/dev/null || echo "?")
EOF
[ "$DRY_RUN" = 1 ] || cp "$LOG" "$B/provision-$SERIAL.log" 2>/dev/null || true

step "Done: unit $SERIAL"
echo "Hotspot $SSID (password on the label). Dashboard: http://<unit address>:5000"
echo "Updates: $IMAGE, nightly between 2 and 4 AM $TZ_NAME"
if [ "$REBOOT" = 1 ] && [ "$DRY_RUN" != 1 ]; then
    echo "Rebooting in 10 s to start Ember (Ctrl+C to stay)..."
    sleep 10
    systemctl reboot
fi
