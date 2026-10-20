#!/bin/bash
# Connects an installed unit to the fleet console: installs the fleet agent,
# this unit's device key and the current host files, then waits for the first
# check-in. Made by make_connect.sh; run it at the robot:
#
#   sudo bash /media/<user>/<stick>/ember-connect-002/connect.sh
#
# The key is deleted from the stick once it's installed. Safe to run again
# with a new folder from make_connect.sh.
set -euo pipefail

B=$(cd "$(dirname "$0")" && pwd)
die() { echo "ERROR: $*"; exit 1; }
[ "$(id -u)" = 0 ] || die "run with sudo"
. "$B/unit.env"     # SERIAL
[ -s "$B/device-key" ] || die "no device key in $B (already used?). Make a new folder with make_connect.sh."
KEY=$(cat "$B/device-key")
[[ $KEY == "$SERIAL".* ]] || die "the key in $B isn't for unit $SERIAL"

cfg() { /usr/bin/python3 "$B/host/device_config.py" "$1"; }
[ -f /etc/ember/device.toml ] || die "no /etc/ember/device.toml: this isn't a set-up Ember unit"
HERE=$(cfg device.serial)
[ "$HERE" = "$SERIAL" ] || die "this is unit $HERE, but the folder is for unit $SERIAL"
RUNTIME=$(cfg system.runtime)
echo "Unit $SERIAL ($RUNTIME runtime)"

# Replace or add BAYMAX_DEVICE_KEY, keeping the file's owner and mode.
set_key() {
    local file=$1 tmp
    tmp=$(mktemp "$file.XXXXXX")
    grep -v '^BAYMAX_DEVICE_KEY=' "$file" > "$tmp" || true
    echo "BAYMAX_DEVICE_KEY=$KEY" >> "$tmp"
    chmod --reference="$file" "$tmp"
    chown --reference="$file" "$tmp"
    mv -f "$tmp" "$file"
}
# Replace by rename: startup.sh is read by a running bash.
put() {    # mode file
    install -m "$1" "$B/host/$2" "/opt/ember/.$2.new"
    mv -f "/opt/ember/.$2.new" "/opt/ember/$2"
}

install -d -m 755 /opt/ember
install -d -m 700 /var/lib/ember-agent
if [ "$RUNTIME" = docker ]; then
    for f in startup.sh ember-update.sh ember-agent.py; do put 755 $f; done
    for f in baymax_portal.py device_config.py compose.yaml REVISION; do put 644 $f; done
    install -m 644 "$B/host/ember.service" "$B/host/ember-update.service" "$B/host/ember-update.timer" \
        "$B/host/ember-agent.service" /etc/systemd/system/
    set_key /etc/ember/secrets.env
else
    # Unit 001: runs from its git checkout, which holds the key in .env and
    # the boot logs. Only the agent is installed; nothing else changes.
    CORE_HOME=$(getent passwd "$(cfg system.user)" | cut -d: -f6)
    APP_DIR=${APP_DIR:-$CORE_HOME/Baymax/Baymax/Baymax-main}
    BAYMAX_HOME=${BAYMAX_HOME:-$CORE_HOME/Baymax}
    [ -f "$APP_DIR/.env" ] || die "no $APP_DIR/.env (set APP_DIR=<the Baymax-main checkout>)"
    put 755 ember-agent.py
    put 644 device_config.py
    install -m 644 "$B/host/ember-agent.service" /etc/systemd/system/
    mkdir -p /etc/systemd/system/ember-agent.service.d
    cat > /etc/systemd/system/ember-agent.service.d/venv.conf <<CONF
# This unit runs from a git checkout (venv runtime): its key and logs are there.
[Service]
Environment=EMBER_KEY_FILE=$APP_DIR/.env APP_DIR=$APP_DIR EMBER_LOG_DIR=$BAYMAX_HOME
CONF
    set_key "$APP_DIR/.env"
fi
rm -f "$B/device-key"
sync
echo "Device key installed (and removed from $B)"

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

if [ "$RUNTIME" = docker ]; then
    # The AI core reads the key at start, for its reports to the site.
    echo "Restarting Ember so the AI core picks up the key (about 1 minute)..."
    systemctl restart --no-block ember.service
else
    echo "The AI core picks up the key at its next restart (sudo systemctl restart baymax.service)."
fi
