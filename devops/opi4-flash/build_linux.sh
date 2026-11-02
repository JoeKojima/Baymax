#!/bin/bash
set -Eeuo pipefail
umask 077
WORK=$(realpath "$1")
CACHE=$(realpath "$2")
HERE=$(dirname "$(realpath "$0")")
[ -f "$WORK/payload/image-settings.private.json" ]
[ ! -b "$WORK/prepared.img" ]
# Packages are installed by running the image's ARM64 apt/Python inside a chroot.
if [ "$(uname -m)" != aarch64 ] && [ ! -e /proc/sys/fs/binfmt_misc/qemu-aarch64 ]; then
  echo 'Install ARM64 emulation first: sudo apt-get install qemu-user-static binfmt-support (newer Ubuntu/Debian: qemu-user-binfmt)' >&2
  exit 1
fi
cd "$WORK"
ROOT="$WORK/mount"
LOOP=
mkdir "$ROOT"
cleanup() { if mountpoint -q "$ROOT"; then umount -R "$ROOT"; fi; if [ -n "$LOOP" ]; then losetup -d "$LOOP"; fi; }
trap cleanup EXIT
attach() {
  LOOP=$(losetup --find --show --partscan "$1")
  PART="${LOOP}p1"
  sleep 1
}
detach() {
  sync
  umount -R "$ROOT"
  e2fsck -pf "$PART" || [ "$?" = 1 ]
  losetup -d "$LOOP"
  LOOP=
}
chroot_up() {
  for dir in proc sys dev dev/pts; do mount --bind "/$dir" "$ROOT/$dir"; done
  # The board mounts a fresh tmpfs on /tmp; the image's own /tmp is not writable by apt.
  mount -t tmpfs -o mode=1777 tmpfs "$ROOT/tmp"
  if [ -e "$ROOT/etc/resolv.conf" ] || [ -L "$ROOT/etc/resolv.conf" ]; then mv "$ROOT/etc/resolv.conf" "$ROOT/etc/resolv.conf.ember-build"; fi
  cp -L /etc/resolv.conf "$ROOT/etc/resolv.conf"
  chmod 644 "$ROOT/etc/resolv.conf"
  # Package scripts must not start services inside the build chroot.
  printf '#!/bin/sh\nexit 101\n' >"$ROOT/usr/sbin/policy-rc.d"
  chmod 755 "$ROOT/usr/sbin/policy-rc.d"
}
chroot_down() {
  rm -f "$ROOT/usr/sbin/policy-rc.d" "$ROOT/etc/resolv.conf"
  if [ -e "$ROOT/etc/resolv.conf.ember-build" ] || [ -L "$ROOT/etc/resolv.conf.ember-build" ]; then mv "$ROOT/etc/resolv.conf.ember-build" "$ROOT/etc/resolv.conf"; fi
  for dir in tmp dev/pts dev sys proc; do umount "$ROOT/$dir"; done
}

# Stage A, cached across cards: base image with every Debian package and AnyDesk installed.
# Only the script above Stage B shapes this image, so later edits keep the cache.
# Wheels only feed Stage B, so a new wheel does not rebuild the package base.
KEY=$( { python3 -c 'import json, sys; print({k: v for k, v in json.load(open(sys.argv[1])).items() if not k.endswith(".whl")})' "$HERE/assets.json"; sed '/^# Stage B/,$d' "$HERE/build_linux.sh"; cat "$WORK/payload/system-packages.txt"; } | sha256sum | cut -c1-16)
PACKAGES="$CACHE/packages-$KEY.img"
if [ ! -f "$PACKAGES" ]; then
  echo "Preinstalling system packages into $PACKAGES (slow under emulation; done once)..."
  echo 'ca4b8e8a26fa314cacdc7e3dc403a41536d962a5cd2808fa80917dc9e1bb4e3f  base.img.xz' | sha256sum -c -
  xz -dc base.img.xz > "$PACKAGES.partial"
  python3 - "$PACKAGES.partial" <<'PY'
from pathlib import Path
import struct
import sys
image=Path(sys.argv[1])
with image.open('r+b') as stream:
    mbr=bytearray(stream.read(512))
    assert mbr[510:] == b'\x55\xaa' and mbr[450] == 0x83
    assert all(mbr[446+i*16+4] == 0 for i in range(2,4))
    assert mbr[466] == 0x0c and struct.unpack_from('<II',mbr,470) == (1294336,2048)
    # Match the boot-tested layout: inject /boot directly, replacing the factory
    # 1 MiB setup partition before enlarging the Linux partition.
    mbr[462:478]=bytes(16)
    start=struct.unpack_from('<I',mbr,454)[0]
    assert start == 65536
    # 7 GiB holds the desktop, AnyDesk and the Ember venv and still fits an 8 GB card.
    length=max(image.stat().st_size,7516192768)
    stream.truncate(length)
    struct.pack_into('<I',mbr,458,length//512-start)
    stream.seek(0); stream.write(mbr)
PY
  attach "$PACKAGES.partial"
  e2fsck -pf "$PART" || [ "$?" = 1 ]
  resize2fs "$PART"
  mount "$PART" "$ROOT"
  chroot_up
  install -m 644 "$WORK/payload/system-packages.txt" "$ROOT/tmp/system-packages.txt"
  install -m 644 "$WORK/payload/anydesk-arm64.deb" "$ROOT/tmp/anydesk-arm64.deb"
  # umask 022: the builder's private umask must not leak into installed system files.
  chroot "$ROOT" /bin/bash -Eeuo pipefail -c '
    umask 022
    export DEBIAN_FRONTEND=noninteractive
    echo "lightdm shared/default-x-display-manager select lightdm" | debconf-set-selections
    apt-get -o Acquire::Retries=3 update
    apt-get -o Acquire::Retries=3 install -y --no-install-recommends $(cat /tmp/system-packages.txt)
    apt-get install -y /tmp/anydesk-arm64.deb
    apt-get clean
    rm /tmp/system-packages.txt /tmp/anydesk-arm64.deb
    install -d -m 755 /var/lib/ember
    touch /var/lib/ember/packages-baked'
  chroot_down
  detach
  mv "$PACKAGES.partial" "$PACKAGES"
fi

# Stage B, per card: copy the cached base, add this card's settings, preinstall Ember's venv.
cp --sparse=always "$PACKAGES" prepared.img
attach prepared.img
mount "$PART" "$ROOT"
python3 inject_image.py "$ROOT" "$WORK/payload"
python3 validate_image.py "$ROOT" "$WORK/payload/image-settings.private.json"
for SCRIPT in ember-update ember-run ember-device-settings; do bash -n "$ROOT/usr/local/bin/$SCRIPT"; done
bash -n "$ROOT/usr/local/sbin/ember-provision"
echo 'Preinstalling the Ember release and Python packages (built wheels are cached)...'
# Numeric IDs via chown: the host has no "ember" user and uutils install rejects -o 1001.
install -d -m 755 "$CACHE/uv" "$ROOT/home/ember/.cache" "$ROOT/home/ember/.cache/uv"
chown 1001:1001 "$CACHE/uv" "$ROOT/home/ember/.cache" "$ROOT/home/ember/.cache/uv"
chroot_up
mount --bind "$CACHE/uv" "$ROOT/home/ember/.cache/uv"
if ! chroot "$ROOT" runuser -u ember -- env HOME=/home/ember UV_LINK_MODE=copy /usr/local/bin/ember-update; then
  tail -n 80 "$ROOT/home/ember/ember-deployment/logs/update.log"
  exit 1
fi
umount "$ROOT/home/ember/.cache/uv"
chroot_down
ls "$ROOT"/home/ember/ember-deployment/releases/*/Baymax-main/.venv/.ember-ready.json
python3 -m unittest discover -s "$HERE" -p 'test_*.py'
detach
trap - EXIT
