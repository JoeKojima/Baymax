#!/bin/bash
# Builds the installer stick for one new unit: Debian 13 with the GNOME
# desktop, the extra packages Ember needs, the Ember image and the unit's
# settings, all on the stick, so the unit installs with no network.
#
#   sudo bash deploy/provision/make_usb.sh --serial 002 --usb /dev/sdX
#
# Options:
#   --serial N      unit serial (required)
#   --usb DEV       write the result to this USB stick (8 GB or more). Without
#                   it, the image is left in the work folder to write yourself.
#   --timezone TZ   robot-local time zone (default America/New_York)
#   --channel NAME  release channel to install and follow (default stable)
#   --dvd FILE      Debian 13 DVD-1 image to start from (default: downloaded
#                   into $CACHE once and reused)
#   --force         set up a serial that is already in the unit records again
#                   (it gets a new device key; the old install stops reporting)
#   --no-console    don't register the unit in the fleet console
#   --admin-token F the site's ADMIN_TOKEN (default /etc/ember/admin-token);
#                   used to register the unit and put its device key on the stick
#
# Needs: an x86-64 Linux machine with docker, xorriso and ~15 GB free. The unit's
# passwords go into /etc/ember/units.csv (root-only) and on the printed label.
# The stick holds the shared secrets: keep it safe and re-flash it for the
# next unit.
#
# At the robot: plug in the stick, keyboard and monitor; boot from the stick
# (boot menu key at power-on); type ERASE when asked. It installs and powers
# off. Remove the stick and power on: the first boot sets up Ember (~10 min)
# and reboots into it.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)           # deploy/provision
REPO=$(cd "$HERE/../.." && pwd)                # Baymax-main
CACHE=${EMBER_USB_CACHE:-/var/cache/ember-usb}
DEBIAN_CD=https://cdimage.debian.org/debian-cd/current/amd64/iso-dvd

SERIAL="" USB="" TZ_NAME=America/New_York CHANNEL=stable DVD="" FORCE=() CONSOLE=()
while [ $# -gt 0 ]; do
    case $1 in
        --force)    FORCE=(--force); shift; continue ;;
        --no-console) CONSOLE=(--no-console); shift; continue ;;
        --admin-token) CONSOLE+=(--admin-token "$2") ;;
        --serial)   SERIAL=$2 ;;
        --usb)      USB=$2 ;;
        --timezone) TZ_NAME=$2 ;;
        --channel)  CHANNEL=$2 ;;
        --dvd)      DVD=$2 ;;
        *) echo "unknown option $1"; exit 2 ;;
    esac
    shift 2
done
[ -n "$SERIAL" ] || { sed -n '2,30p' "$0"; exit 2; }

die() { echo "ERROR: $*"; exit 1; }
step() { echo; echo "== $*"; }

# ── Checks ──────────────────────────────────────────────────────────────
[ "$(id -u)" = 0 ] || die "run with sudo"
for c in xorriso docker openssl curl python3; do
    command -v $c >/dev/null || die "$c is missing (sudo apt install xorriso)"
done
[ "$(uname -m)" = x86_64 ] || die "build on an x86-64 machine (the packages and image are amd64)"
if [ -n "$USB" ]; then
    [ -b "$USB" ] || die "$USB is not a disk"
    [ "$(lsblk -dno TRAN "$USB")" = usb ] || die "$USB is not a USB device; refusing to overwrite it"
    lsblk -nro MOUNTPOINT "$USB" | grep -qx / && die "$USB holds this system's root"
    [ "$(lsblk -bdno SIZE "$USB")" -ge 7000000000 ] || die "$USB is smaller than 8 GB"
fi

WORK=$(mktemp -d /var/tmp/ember-usb.XXXXXX)
chmod 700 "$WORK"
TREE=$WORK/tree/ember                          # becomes /ember on the stick
mkdir -p "$TREE" "$CACHE"
ISO=$WORK/ember-unit-$SERIAL.iso
cleanup() { [ -n "${KEEP_ISO:-}" ] && rm -rf "$WORK/tree" || rm -rf "$WORK"; }
trap cleanup EXIT

# ── Debian DVD (downloaded once) ────────────────────────────────────────
step "Debian DVD"
if [ -z "$DVD" ]; then
    curl -fsSL "$DEBIAN_CD/SHA256SUMS" -o "$CACHE/SHA256SUMS"
    NAME=$(grep -oE 'debian-[0-9.]+-amd64-DVD-1\.iso' "$CACHE/SHA256SUMS" | head -n1)
    DVD=$CACHE/$NAME
    if [ ! -f "$DVD" ]; then
        echo "downloading $NAME (~4 GB, once)"
        curl -fL -C - -o "$DVD.part" "$DEBIAN_CD/$NAME" && mv "$DVD.part" "$DVD"
        (cd "$CACHE" && grep " $NAME\$" SHA256SUMS | sha256sum -c -) || { rm -f "$DVD"; die "checksum mismatch"; }
    fi
fi
[ -f "$DVD" ] || die "no DVD image at $DVD"
xorriso -indev "$DVD" -find /pool/main/g/gnome-shell -name 'gnome-shell_*' 2>/dev/null | grep -q gnome-shell \
    || die "$DVD doesn't contain the GNOME desktop (need DVD-1)"
echo "using $DVD"

# ── Unit bundle ─────────────────────────────────────────────────────────
step "Unit $SERIAL"
LOGIN_USER=meowmax
LOGIN_PASSWORD=$(python3 -c "import secrets; a='abcdefghjkmnpqrstuvwxyz23456789'; print(''.join(secrets.choice(a) for _ in range(12)))")
bash "$HERE/prepare_unit.sh" --serial "$SERIAL" --out "$WORK" --timezone "$TZ_NAME" \
    --channel "$CHANNEL" --user "$LOGIN_USER" --login-password "$LOGIN_PASSWORD" "${FORCE[@]}" "${CONSOLE[@]}" \
    | tee "$WORK/label.txt" | grep -v -e '^On the new unit' -e 'provision.sh$'
mv "$WORK/ember-unit-$SERIAL" "$TREE/bundle"
IMAGE="$(EMBER_DEVICE_CONFIG=$TREE/bundle/device.toml python3 "$REPO/device_config.py" update.image):$CHANNEL"

# ── Ember image ─────────────────────────────────────────────────────────
step "Ember image $IMAGE"
had_image=$(docker image inspect -f '{{.Id}}' "$IMAGE" 2>/dev/null || true)
docker pull -q "$IMAGE"
docker save "$IMAGE" | gzip -1 > "$TREE/ember-image.tar.gz"
docker image inspect -f '{{.Id}}' "$IMAGE" > "$TREE/bundle/host/IMAGE_ID"
# Don't keep 3 GB in this machine's Docker unless it was already there.
[ -z "$had_image" ] && docker rmi -f "$IMAGE" >/dev/null
ls -lh "$TREE/ember-image.tar.gz" | awk '{print $5, "compressed"}'

# ── Extra packages ──────────────────────────────────────────────────────
# Everything provision.sh installs, with every dependency (alternatives
# included), as a small apt repository; apt on the unit picks what it needs.
# Fetched inside a fresh Debian 13 container, so this machine's own package
# state doesn't matter, in the DVD's versions wherever the DVD has them.
step "Extra packages"
PACKAGES="docker.io docker-cli docker-compose python3-flask pulseaudio-utils iw bind9-host wget curl psmisc"
mkdir -p "$TREE/debs"
docker pull -q debian:trixie >/dev/null
# Host networking: robots run Docker with "iptables": false (for the hotspot),
# which leaves bridged containers without internet.
docker run --rm --network host -e DEBIAN_FRONTEND=noninteractive \
    -v "$DVD:/dvd.iso:ro" -v "$TREE/debs:/out" -v "$HERE/usb/fetch-debs.sh:/fetch-debs.sh:ro" \
    debian:trixie sh -c "bash /fetch-debs.sh $PACKAGES && chown -R $(id -u):$(id -g) /out"
echo "$(ls "$TREE/debs" | grep -c '\.deb$') packages, $(du -sh "$TREE/debs" | cut -f1)"
# Proof the set is complete: install it on a bare Debian 13 from this folder
# alone, with no network, and run the commands Ember uses. The unit (a full desktop) needs at most that.
docker run --rm --network none -e DEBIAN_FRONTEND=noninteractive -v "$TREE/debs:/debs:ro" debian:trixie sh -c "
    echo 'deb [trusted=yes] file:/debs ./' > /etc/apt/sources.list.d/ember.list
    rm -f /etc/apt/sources.list.d/debian.sources
    apt-get update -qq && apt-get install -y -qq --no-install-recommends $PACKAGES >/dev/null &&
    docker compose version >/dev/null" \
    || die "the extra packages don't install offline; see above"
echo "checked: installs offline on a bare Debian 13"
# The installer scans the whole stick for package lists and would offer these
# alongside the DVD; renamed here, late.sh names it back on the unit's disk.
mv "$TREE/debs/Packages.gz" "$TREE/debs/ember-index.gz"

# ── Installer files ─────────────────────────────────────────────────────
step "Installer"
cp "$HERE/usb/erase-confirm.sh" "$HERE/usb/late.sh" "$HERE/usb/ember.templates" \
   "$HERE/usb/ember-firstboot.service" "$TREE/"
echo "SERIAL=$SERIAL" > "$TREE/unit.env"
HOSTNAME_=ember-${SERIAL,,}
HASH=$(printf '%s' "$LOGIN_PASSWORD" | openssl passwd -6 -stdin)
python3 - "$HERE/usb/preseed.cfg.in" "$TREE/preseed.cfg" <<EOF
import sys
text = open(sys.argv[1]).read()
for key, value in {"HOSTNAME": "$HOSTNAME_", "USER": "$LOGIN_USER",
                   "PASSWORD_HASH": r"""$HASH""", "TIMEZONE": "$TZ_NAME"}.items():
    text = text.replace(f"@{key}@", value)
import re
assert not re.search(r"@[A-Z_]+@", text), "unfilled placeholder"
open(sys.argv[2], "w").write(text)
EOF

# Boot menu: our entry first and default after 5 s. UEFI boots read this file.
xorriso -osirrox on -indev "$DVD" -extract /boot/grub/grub.cfg "$WORK/grub.cfg" 2>/dev/null
chmod u+w "$WORK/grub.cfg"
python3 - "$WORK/grub.cfg" "$SERIAL" "$HOSTNAME_" <<'EOF'
import sys
path, serial, hostname = sys.argv[1:]
text = open(path).read()
entry = f"""set default=0
set timeout=5
menuentry 'Install Ember unit {serial} (asks before erasing the disk)' {{
    set background_color=black
    linux /install.amd/vmlinuz auto=true priority=critical preseed/file=/cdrom/ember/preseed.cfg locale=en_US.UTF-8 keymap=us hostname={hostname} domain= --- quiet
    initrd /install.amd/initrd.gz
}}
"""
i = text.index("menuentry ")
open(path, "w").write(text[:i] + entry + text[i:])
EOF

# ── Stick image ─────────────────────────────────────────────────────────
step "Building the stick image"
xorriso -indev "$DVD" -outdev "$ISO" -boot_image any replay \
    -map "$TREE" /ember -map "$WORK/grub.cfg" /boot/grub/grub.cfg \
    2>&1 | grep -E "FAILURE|SORRY|completed successfully" || true
[ -s "$ISO" ] || die "xorriso didn't produce $ISO"
ls -lh "$ISO" | awk '{print $5, $9}'

if [ -n "$USB" ]; then
    step "Writing to $USB ($(lsblk -dno MODEL,SIZE "$USB" | tr -s ' '))"
    for p in $(lsblk -nro PATH "$USB" | tail -n +2); do umount "$p" 2>/dev/null || true; done
    dd if="$ISO" of="$USB" bs=4M conv=fsync status=progress
    sync
else
    KEEP_ISO=1
    echo "Not written to a stick. Image (it holds the secrets; delete it after use):"
    echo "  $ISO"
fi

step "Done"
cat "$WORK/label.txt" | grep -v -e '^Bundle ready' -e '^On the new unit' -e 'provision.sh$'
cat <<EOF
At the robot: plug in the stick, keyboard and monitor; power on and pick the
USB stick in the boot menu; type ERASE when asked. It installs (~20 min) and
powers off. Remove the stick and power on: the first boot sets up Ember
(~10 min) and reboots into it.
EOF
