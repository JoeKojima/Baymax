#!/bin/bash
# Makes the setup bundle for one new unit. make_usb.sh calls it to build an
# installer stick (the usual way); on its own it makes a bundle for a board
# that already runs Debian. Run it on any machine with this repo checked out
# (unit 001 works), with sudo so it can write the unit records:
#
#   sudo bash deploy/provision/prepare_unit.sh --serial 002 --out /media/$USER/USBSTICK
#
# Options:
#   --serial N        unit serial (required). Hotspot becomes Ember-<last 4>.
#   --out DIR         where to create ember-unit-<serial>/ (required)
#   --secrets FILE    Gemini key, email login etc. (default /etc/ember/secrets.env)
#   --timezone TZ     robot-local time zone (default America/New_York)
#   --user NAME       desktop user on the new board (default meowmax; provision.sh
#                     falls back to the board's first user if it doesn't exist)
#   --channel NAME    release channel the unit follows (default stable)
#   --records FILE    root-only list of units and their passwords
#                     (default /etc/ember/units.csv); a serial already in it is
#                     refused unless --force
#   --login-password PW  (make_usb.sh) password for the board's login account
#   --admin-token FILE   the site's ADMIN_TOKEN, used to register the unit in the
#                     fleet console and get its device key (default
#                     /etc/ember/admin-token; see register_unit.sh)
#   --no-console      don't register; the unit runs without the console and
#                     follows its channel's GHCR tag
#
# On the new unit (Debian 13 desktop, online over Ethernet or a USB Wi-Fi
# dongle, never wlo1):
#
#   sudo bash /media/<user>/<stick>/ember-unit-002/provision.sh
#
# The bundle holds the shared secrets until provision.sh installs and deletes
# them, so keep the stick safe until then.
set -euo pipefail

REPO=$(cd "$(dirname "$0")/../.." && pwd)     # Baymax-main
SERIAL="" OUT="" SECRETS=/etc/ember/secrets.env TZ_NAME=America/New_York
CORE_USER=meowmax CHANNEL=stable RECORDS=/etc/ember/units.csv LOGIN_PASSWORD="" FORCE=0
CONSOLE=1 TOKEN_FILE=/etc/ember/admin-token
while [ $# -gt 0 ]; do
    case $1 in
        --force)    FORCE=1; shift; continue ;;
        --no-console) CONSOLE=0; shift; continue ;;
        --admin-token) TOKEN_FILE=$2 ;;
        --records)  RECORDS=$2 ;;
        --login-password) LOGIN_PASSWORD=$2 ;;
        --serial)   SERIAL=$2 ;;
        --out)      OUT=$2 ;;
        --secrets)  SECRETS=$2 ;;
        --timezone) TZ_NAME=$2 ;;
        --user)     CORE_USER=$2 ;;
        --channel)  CHANNEL=$2 ;;
        *) echo "unknown option $1"; exit 2 ;;
    esac
    shift 2
done
[ -n "$SERIAL" ] && [ -n "$OUT" ] || { sed -n '2,20p' "$0"; exit 2; }
[[ $SERIAL =~ ^[A-Za-z0-9-]+$ ]] || { echo "serial may only contain letters, digits and -"; exit 2; }
[[ $CORE_USER =~ ^[a-z_][a-z0-9_-]*$ ]] || { echo "bad user name $CORE_USER"; exit 2; }
[[ $CHANNEL =~ ^[a-z0-9._-]+$ ]] || { echo "bad channel $CHANNEL"; exit 2; }
[ -r "$SECRETS" ] || { echo "cannot read $SECRETS"; exit 1; }
grep -q '^GOOGLE_API_KEY=.' "$SECRETS" || { echo "$SECRETS has no GOOGLE_API_KEY"; exit 1; }
[ -e "/usr/share/zoneinfo/$TZ_NAME" ] || { echo "unknown time zone $TZ_NAME"; exit 1; }
if [ "$CONSOLE" = 1 ]; then
    [[ $CHANNEL =~ ^(dev|beta|stable)$ ]] || { echo "the console's channels are dev, beta and stable"; exit 2; }
    [ -r "$TOKEN_FILE" ] || { echo "cannot read $TOKEN_FILE (the site's ADMIN_TOKEN, to register the unit; or use --no-console)"; exit 1; }
fi

# Unit records: one line per unit with its passwords. Root-only.
if [ ! -e "$RECORDS" ]; then
    (umask 077; mkdir -p "$(dirname "$RECORDS")" && \
     echo "date,serial,hostname,hotspot_ssid,hotspot_password,login_user,login_password,revision" > "$RECORDS") \
        || { echo "cannot create $RECORDS (run with sudo)"; exit 1; }
fi
[ -w "$RECORDS" ] || { echo "cannot write $RECORDS (run with sudo)"; exit 1; }
if cut -d, -f2 "$RECORDS" | grep -qx "$SERIAL" && [ "$FORCE" = 0 ]; then
    echo "unit $SERIAL is already in $RECORDS; use another serial, or --force to set it up again"
    exit 1
fi

DEST="$OUT/ember-unit-$SERIAL"
[ -e "$DEST" ] && { echo "$DEST already exists; remove it first"; exit 1; }

# Fleet console: register the unit and get its own device key. Registering a
# serial again replaces its key.
DEVICE_KEY=""
if [ "$CONSOLE" = 1 ]; then
    DEVICE_KEY=$(bash "$REPO/deploy/provision/register_unit.sh" --serial "$SERIAL" --channel "$CHANNEL" \
        --admin-token "$TOKEN_FILE" --secrets "$SECRETS") || exit 1
fi
mkdir -p "$DEST/host"

# Last 4 characters of the serial, zero-padded: 002 -> Ember-0002.
SUFFIX=$(printf '000%s' "$SERIAL" | tail -c 4)
SSID="Ember-$SUFFIX"
PASSWORD=$(python3 -c "import secrets; a='abcdefghjkmnpqrstuvwxyz23456789'; print(''.join(secrets.choice(a) for _ in range(12)))")

# device.toml: the example with this unit's values filled in.
python3 - "$REPO/deploy/device.example.toml" "$DEST/device.toml" <<EOF
import re, sys
text = open(sys.argv[1]).read()
values = {
    ("device", "serial"): '"$SERIAL"',
    ("device", "name"): '"Ember Unit $SERIAL"',
    ("device", "model"): '"Ember (LattePanda 3 Delta)"',
    ("system", "user"): '"$CORE_USER"',
    ("system", "runtime"): '"docker"',
    ("system", "timezone"): '"$TZ_NAME"',
    ("network", "hotspot_connection"): '"$SSID"',
    ("update", "channel"): '"$CHANNEL"',
    # Off until the seated false alarms are fixed (2026-10-15).
    ("fall_detection", "enabled"): "false",
}
section, out = None, []
for line in text.splitlines():
    m = re.match(r"\[(\w+)\]", line)
    if m:
        section = m.group(1)
    m = re.match(r"(\w+) = ", line)
    if m and (section, m.group(1)) in values:
        line = f"{m.group(1)} = {values.pop((section, m.group(1)))}"
    out.append(line)
assert not values, f"not in device.example.toml: {values}"
open(sys.argv[2], "w").write("\n".join(out) + "\n")
EOF

# The shared secrets plus this unit's own key (never another unit's).
# The shared secrets never carry a device key: with one, the AI core would
# send patient data to the site, which is a separate decision per unit.
(umask 077; grep -v '^BAYMAX_DEVICE_KEY=' "$SECRETS" > "$DEST/secrets.env" || true)
# The fleet agent's own key: root-only on the unit, never seen by the containers.
if [ -n "$DEVICE_KEY" ]; then
    (umask 077; bash "$REPO/deploy/provision/register_unit.sh" --fleet-env "$DEVICE_KEY" --secrets "$SECRETS" > "$DEST/fleet.env")
fi
(umask 077; printf 'SSID=%s\nPASSWORD=%s\n' "$SSID" "$PASSWORD" > "$DEST/hotspot.env")
cp "$REPO/deploy/provision/provision.sh" "$DEST/"
cp "$REPO/deploy/host/startup.sh" "$REPO/deploy/host/baymax_portal.py" "$REPO/device_config.py" \
   "$REPO/deploy/docker/compose.yaml" "$REPO/deploy/provision/ember-update.sh" \
   "$REPO/deploy/provision/ember.service" "$REPO/deploy/provision/ember-update.service" \
   "$REPO/deploy/provision/ember-update.timer" "$REPO/deploy/provision/ember-agent.py" \
   "$REPO/deploy/provision/ember-agent.service" "$DEST/host/"
git -C "$REPO" rev-parse --short HEAD > "$DEST/host/REVISION" 2>/dev/null || true
REVISION=$(cat "$DEST/host/REVISION" 2>/dev/null || echo "?")

echo "$(date -Is),$SERIAL,ember-${SERIAL,,},$SSID,$PASSWORD,$CORE_USER,${LOGIN_PASSWORD:-unchanged},$REVISION" >> "$RECORDS"

cat <<EOF
Bundle ready: $DEST

Label for unit $SERIAL:
  Wi-Fi hotspot: $SSID
  Password:      $PASSWORD
${LOGIN_PASSWORD:+  Login:         $CORE_USER / $LOGIN_PASSWORD
}
Recorded in $RECORDS.
$([ "$CONSOLE" = 1 ] && echo "Fleet console: registered, channel $CHANNEL." || echo "Fleet console: not registered (--no-console).")

On the new unit:
  sudo bash $DEST/provision.sh
EOF
