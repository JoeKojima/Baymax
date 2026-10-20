#!/bin/bash
# Registers a unit with the fleet console and prints its new device key
# (serial.key) on stdout. Used by prepare_unit.sh and make_connect.sh.
#
#   sudo bash deploy/provision/register_unit.sh --serial 002 [--channel stable]
#
# Needs the site's ADMIN_TOKEN in a root-only file on this machine:
#   sudo install -m 600 /dev/null /etc/ember/admin-token
#   sudo nano /etc/ember/admin-token        # paste the token, save
#
# Registering a serial again gives it a new key and the old one stops
# working, so the unit with the old key must get the new one.
set -euo pipefail

SERIAL="" CHANNEL="" TOKEN_FILE=/etc/ember/admin-token SECRETS=/etc/ember/secrets.env
while [ $# -gt 0 ]; do
    case $1 in
        --serial)      SERIAL=$2 ;;
        --channel)     CHANNEL=$2 ;;
        --admin-token) TOKEN_FILE=$2 ;;
        --secrets)     SECRETS=$2 ;;
        *) echo "unknown option $1" >&2; exit 2 ;;
    esac
    shift 2
done
die() { echo "ERROR: $*" >&2; exit 1; }
[[ $SERIAL =~ ^[A-Za-z0-9-]{1,32}$ ]] || die "serial may only contain letters, digits and -"
[ -z "$CHANNEL" ] || [[ $CHANNEL =~ ^(dev|beta|stable)$ ]] || die "the console's channels are dev, beta and stable"
[ -r "$TOKEN_FILE" ] || die "cannot read $TOKEN_FILE (the site's ADMIN_TOKEN; see the top of $0)"
TOKEN=$(tr -d ' \r\n' < "$TOKEN_FILE")
[ -n "$TOKEN" ] || die "$TOKEN_FILE is empty"

CLOUD=$(sed -n 's/^BAYMAX_CLOUD_URL=//p' "$SECRETS" 2>/dev/null | tail -n1 | tr -d "\"'")
CLOUD=${CLOUD:-https://tadashirobotics.com}
BODY=$(python3 -c 'import json, sys; print(json.dumps({k: v for k, v in (("serial", sys.argv[1]), ("channel", sys.argv[2])) if v}))' "$SERIAL" "$CHANNEL")

# The token goes in a header file, not on the command line (visible in ps).
RESPONSE=$(curl -sS --max-time 30 -w '\n%{http_code}' -X POST "$CLOUD/api/admin/devices" \
    -H @<(printf 'Authorization: Bearer %s\n' "$TOKEN") -H 'Content-Type: application/json' -d "$BODY") \
    || die "cannot reach $CLOUD"
CODE=${RESPONSE##*$'\n'}
RESPONSE=${RESPONSE%$'\n'*}
[ "$CODE" = 201 ] || die "$CLOUD refused to register unit $SERIAL (HTTP $CODE): $RESPONSE"
python3 -c '
import json, sys
r = json.load(sys.stdin)
assert r["device_key"].startswith(sys.argv[1] + ".")
print("Registered unit %s in the fleet console (channel %s)" % (r["serial"], r["channel"]), file=sys.stderr)
print(r["device_key"])' "$SERIAL" <<<"$RESPONSE"
