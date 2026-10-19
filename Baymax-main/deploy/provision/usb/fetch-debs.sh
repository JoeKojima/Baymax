#!/bin/bash
# Runs inside a fresh debian:trixie container (make_usb.sh), with the Debian
# DVD at /dvd.iso and the output folder at /out. Fetches the packages named as
# arguments plus everything they depend on (alternatives included) into /out
# as a small apt repository.
#
# Packages the DVD has are taken in the DVD's exact version, never a newer
# one from the mirror: the unit's system is installed from the DVD, and a
# newer library can clash with what's installed. (Seen in testing: the
# security update bind9-libs 9.20.29 against the installed bind9-dnsutils,
# which needs exactly 9.20.26.)
set -euo pipefail
apt-get update -qq
apt-get install -y -qq apt-utils xorriso >/dev/null

# The DVD's package lists, preferred over everything else (priority > 1000
# also wins over newer versions).
xorriso -osirrox on -indev /dvd.iso -extract /dists /dvd/dists 2>/dev/null
echo "deb [trusted=yes] file:/dvd trixie main contrib non-free-firmware" > /etc/apt/sources.list.d/dvd.list
printf 'Package: *\nPin: origin ""\nPin-Priority: 1001\n' > /etc/apt/preferences.d/dvd
apt-get update -qq

apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts --no-breaks \
    --no-replaces --no-enhances "$@" 2>/dev/null | grep -E '^[a-z0-9]' | sort -u > /tmp/list

# A DVD version that the mirror no longer has is copied off the DVD.
apt-get download --print-uris $(cat /tmp/list) 2>/dev/null \
    | sed -n "s|^'file:/dvd/\([^']*\)'.*|\1|p" > /tmp/dvd-only
if [ -s /tmp/dvd-only ]; then
    paths=()
    while read -r p; do paths+=("/$(printf '%b' "${p//%/\\x}")"); done < /tmp/dvd-only
    xorriso -osirrox on -indev /dvd.iso -extract_l / /dvd/ "${paths[@]}" -- 2>/dev/null
    echo "${#paths[@]} packages copied from the DVD"
fi

cd /out
apt-get download -qq $(cat /tmp/list) 2>&1 | grep -v '^W:' || true
apt-ftparchive packages . 2>/dev/null | gzip -9 > Packages.gz
