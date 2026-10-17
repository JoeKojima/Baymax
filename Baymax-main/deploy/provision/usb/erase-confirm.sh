#!/bin/sh
# partman/early_command: pick the disk to install on and get the one
# confirmation before anything is written. Runs inside Debian's installer.
. /usr/share/debconf/confmodule
. /cdrom/ember/unit.env                # SERIAL
debconf-loadtemplate ember /cdrom/ember/ember.templates

# The largest disk that isn't on USB (so never the installer stick or an
# external drive): the LattePanda's eMMC, or an NVMe/SATA disk on other boards.
best="" bestsize=0
for d in $(list-devices disk); do
    name=${d#/dev/}
    readlink -f "/sys/block/$name" | grep -q /usb && continue
    size=$(cat "/sys/block/$name/size" 2>/dev/null || echo 0)
    if [ "$size" -gt "$bestsize" ]; then best=$d; bestsize=$size; fi
done
if [ -z "$best" ]; then
    db_input critical ember/nodisk || true
    db_go
    exit 1
fi
name=${best#/dev/}
model=$(cat "/sys/block/$name/device/model" "/sys/block/$name/device/name" 2>/dev/null | head -n1 | tr -s ' ')
gb=$((bestsize / 1953125))             # 512-byte sectors -> GB

db_set partman-auto/disk "$best"
db_subst ember/erase SERIAL "$SERIAL"
db_subst ember/erase DISK "the internal disk $best ($gb GB${model:+, $model})"
db_set ember/erase ""
db_fset ember/erase seen false
db_input critical ember/erase || true
db_go
db_get ember/erase
if [ "$RET" != ERASE ]; then
    db_input critical ember/aborted || true
    db_go
    exit 1                             # partman stops; the disk is untouched
fi
