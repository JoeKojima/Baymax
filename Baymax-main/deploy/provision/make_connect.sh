#!/bin/bash
# Connects a unit that is already installed (e.g. from an older stick) to the
# fleet console. Registers the unit, which gives it a new device key, and
# writes a folder with the key, the fleet agent, the current host files and
# connect.sh, which you run at the robot. Connecting doesn't change where the
# robot sends patient data (see connect.sh).
#
#   sudo bash deploy/provision/make_connect.sh --serial 002 --out /media/$USER/STICK
#   # at unit 002, with the stick plugged in:
#   sudo bash /media/<user>/<stick>/ember-connect-002/connect.sh
#
#   sudo bash deploy/provision/make_connect.sh --serial 001 --here
#   # connects this machine (unit 001) directly, no stick
#
# Options:
#   --channel NAME      dev, beta or stable (default: keep the unit's channel
#                       in the console; stable for a new one)
#   --admin-token FILE  the site's ADMIN_TOKEN (default /etc/ember/admin-token)
#
# Registering replaces the unit's key at once, so run connect.sh soon after.
# The folder holds the key until connect.sh installs it: keep the stick safe.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)           # deploy/provision
REPO=$(cd "$HERE/../.." && pwd)                # Baymax-main
SERIAL="" OUT="" LOCAL=0 REGISTER=()
while [ $# -gt 0 ]; do
    case $1 in
        --here)        LOCAL=1; shift; continue ;;
        --serial)      SERIAL=$2 ;;
        --out)         OUT=$2 ;;
        --channel)     REGISTER+=(--channel "$2") ;;
        --admin-token) REGISTER+=(--admin-token "$2") ;;
        *) echo "unknown option $1"; exit 2 ;;
    esac
    shift 2
done
[ -n "$SERIAL" ] && { [ -n "$OUT" ] || [ $LOCAL = 1 ]; } || { sed -n '2,21p' "$0"; exit 2; }
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
if [ $LOCAL = 1 ]; then
    THIS=$(python3 "$REPO/device_config.py" device.serial)
    [ "$THIS" = "$SERIAL" ] || { echo "this machine is unit $THIS, not $SERIAL"; exit 1; }
    OUT=$(mktemp -d)
    trap 'rm -rf "$OUT"' EXIT
fi
DEST=$OUT/ember-connect-$SERIAL
[ -e "$DEST" ] && { echo "$DEST already exists; remove it first"; exit 1; }

KEY=$(bash "$HERE/register_unit.sh" --serial "$SERIAL" "${REGISTER[@]}")
mkdir -p "$DEST/host"
(umask 077; bash "$HERE/register_unit.sh" --fleet-env "$KEY" > "$DEST/fleet.env")
echo "SERIAL=$SERIAL" > "$DEST/unit.env"
cp "$HERE/connect.sh" "$DEST/"
cp "$REPO/deploy/host/startup.sh" "$REPO/deploy/host/baymax_portal.py" "$REPO/device_config.py" \
   "$REPO/deploy/docker/compose.yaml" "$HERE/ember-update.sh" "$HERE/ember-agent.py" \
   "$HERE/ember.service" "$HERE/ember-update.service" "$HERE/ember-update.timer" \
   "$HERE/ember-agent.service" "$DEST/host/"
git -c safe.directory='*' -C "$REPO" rev-parse --short HEAD > "$DEST/host/REVISION" 2>/dev/null || echo "?" > "$DEST/host/REVISION"

if [ $LOCAL = 1 ]; then
    bash "$DEST/connect.sh"
else
    sync
    cat <<MSG
Ready: $DEST
At unit $SERIAL (keyboard and monitor), plug in the stick, open a terminal and run:
  sudo bash /media/<user>/<stick>/ember-connect-$SERIAL/connect.sh
The stick holds the unit's key until then; keep it safe.
MSG
fi
