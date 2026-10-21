#!/bin/bash
# Connects an installed unit to the fleet console: installs the fleet agent,
# its key and the current host files, then waits for the first check-in.
# Made by make_connect.sh; run it at the robot:
#
#   sudo bash /media/<user>/<stick>/ember-connect-002/connect.sh
#
# The key goes to /etc/ember/fleet.env (root only). The AI core never sees
# it, so where the robot sends patient data doesn't change. The key is deleted
# from the folder once installed. Safe to run again with a new folder.
set -euo pipefail

B=$(cd "$(dirname "$0")" && pwd)
die() { echo "ERROR: $*"; exit 1; }
[ "$(id -u)" = 0 ] || die "run with sudo"
. "$B/unit.env"     # SERIAL
[ -s "$B/fleet.env" ] || die "no key in $B (already used?). Make a new folder with make_connect.sh."
grep -q "^EMBER_FLEET_KEY=$SERIAL\." "$B/fleet.env" || die "the key in $B isn't for unit $SERIAL"

cfg() { /usr/bin/python3 "$B/host/device_config.py" "$1"; }
[ -f /etc/ember/device.toml ] || die "no /etc/ember/device.toml: this isn't a set-up Ember unit"
HERE=$(cfg device.serial)
[ "$HERE" = "$SERIAL" ] || die "this is unit $HERE, but the folder is for unit $SERIAL"
RUNTIME=$(cfg system.runtime)
echo "Unit $SERIAL ($RUNTIME runtime)"

# Replace by rename: startup.sh is read by a running bash.
put() {    # mode file
    install -m "$1" "$B/host/$2" "/opt/ember/.$2.new"
    mv -f "/opt/ember/.$2.new" "/opt/ember/$2"
}

install -d -m 755 /opt/ember
install -d -m 700 /var/lib/ember-agent
if [ "$RUNTIME" = docker ]; then
    # Current host files; startup.sh and compose.yaml apply at Ember's next restart.
    for f in startup.sh ember-update.sh ember-agent.py; do put 755 $f; done
    for f in baymax_portal.py device_config.py compose.yaml REVISION; do put 644 $f; done
    install -m 644 "$B/host/ember.service" "$B/host/ember-update.service" "$B/host/ember-update.timer" \
        "$B/host/ember-agent.service" /etc/systemd/system/
else
    # Unit 001 runs from its git checkout (boot logs there too). Only the
    # agent is installed; nothing else changes.
    CORE_HOME=$(getent passwd "$(cfg system.user)" | cut -d: -f6)
    APP_DIR=${APP_DIR:-$CORE_HOME/Baymax/Baymax/Baymax-main}
    BAYMAX_HOME=${BAYMAX_HOME:-$CORE_HOME/Baymax}
    [ -d "$APP_DIR" ] || die "no $APP_DIR (set APP_DIR=<the Baymax-main checkout>)"
    put 755 ember-agent.py
    put 644 device_config.py
    install -m 644 "$B/host/ember-agent.service" /etc/systemd/system/
    mkdir -p /etc/systemd/system/ember-agent.service.d
    cat > /etc/systemd/system/ember-agent.service.d/venv.conf <<CONF
# This unit runs from a git checkout (venv runtime): its boot logs are there.
[Service]
Environment=APP_DIR=$APP_DIR EMBER_LOG_DIR=$BAYMAX_HOME
CONF
fi
install -d -m 755 /etc/ember
install -m 600 "$B/fleet.env" /etc/ember/fleet.env
rm -f "$B/fleet.env"
sync
echo "Fleet key installed in /etc/ember/fleet.env (and removed from $B)"

systemctl daemon-reload
systemctl enable --quiet ember-agent.service
[ "$RUNTIME" = docker ] && systemctl enable --quiet ember-update.timer
STARTED=$(date +%s)
systemctl restart ember-agent.service

echo "Waiting for the first check-in (up to 2 minutes)..."
CHECKED_IN=0
for _ in $(seq 24); do
    sleep 5
    if [ "$(stat -c %Y /var/lib/ember-agent/last-checkin 2>/dev/null || echo 0)" -ge "$STARTED" ]; then
        CHECKED_IN=1
        break
    fi
done
if [ $CHECKED_IN = 1 ]; then
    echo "Connected: unit $SERIAL shows in the fleet console (tadashirobotics.com/admin)."
else
    echo "No check-in yet. The agent keeps trying every minute; what it says:"
    journalctl -u ember-agent -n 5 --no-pager -o cat || true
fi

# Say so if the AI core sends patient data to the site (a separate choice).
for f in /etc/ember/secrets.env "${APP_DIR:-}/.env"; do
    if [ -f "$f" ] && grep -q '^BAYMAX_DEVICE_KEY=.' "$f"; then
        echo "Note: $f has BAYMAX_DEVICE_KEY, so this unit's AI core sends transcripts, falls"
        echo "      and voice results to the site instead of its own dashboard. The console"
        echo "      doesn't need it; remove that line and restart Ember to keep data local."
    fi
done
