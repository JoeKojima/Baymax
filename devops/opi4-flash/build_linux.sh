#!/bin/bash
set -Eeuo pipefail
umask 077
WORK=$(realpath "$1")
[ -f "$WORK/payload/image-settings.private.json" ]
[ ! -b "$WORK/prepared.img" ]
cd "$WORK"
echo 'ca4b8e8a26fa314cacdc7e3dc403a41536d962a5cd2808fa80917dc9e1bb4e3f  base.img.xz' | sha256sum -c -
xz -dc base.img.xz > prepared.img
python3 - <<'PY'
from pathlib import Path
import struct
image=Path('prepared.img')
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
    length=max(image.stat().st_size,2147483648)
    stream.truncate(length)
    struct.pack_into('<I',mbr,458,length//512-start)
    stream.seek(0); stream.write(mbr)
PY
LOOP=$(losetup --find --show --partscan "$WORK/prepared.img")
ROOT="$WORK/mount"
mkdir "$ROOT"
cleanup() { mountpoint -q "$ROOT" && umount "$ROOT"; losetup -d "$LOOP"; }
trap cleanup EXIT
PART="${LOOP}p1"
sleep 1
e2fsck -pf "$PART" || [ "$?" = 1 ]
resize2fs "$PART"
mount "$PART" "$ROOT"
python3 inject_image.py "$ROOT" "$WORK/payload"
python3 validate_image.py "$ROOT" "$WORK/payload/image-settings.private.json"
for SCRIPT in ember-update ember-run ember-device-settings; do bash -n "$ROOT/usr/local/bin/$SCRIPT"; done
bash -n "$ROOT/usr/local/sbin/ember-provision"
python3 -m unittest discover -s "$WORK" -p 'test_*.py'
sync
umount "$ROOT"
e2fsck -pf "$PART" || [ "$?" = 1 ]
losetup -d "$LOOP"
trap - EXIT
