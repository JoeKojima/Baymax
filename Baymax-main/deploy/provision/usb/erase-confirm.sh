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
# Shows a stop message for good: Continue just shows it again, so the
# installer can never move on to partitioning. Only power-off gets out.
stop_here() {
    while true; do
        db_fset "$1" seen false
        db_input critical "$1" || true
        db_go || true
    done
}

if [ -z "$best" ]; then
    stop_here ember/nodisk
fi
name=${best#/dev/}
model=$(cat "/sys/block/$name/device/model" "/sys/block/$name/device/name" 2>/dev/null | head -n1 | tr -s ' ')
gb=$((bestsize / 1953125))             # 512-byte sectors -> GB

db_subst ember/erase SERIAL "$SERIAL"
db_subst ember/erase DISK "the internal disk $best ($gb GB${model:+, $model})"
db_set ember/erase ""
db_fset ember/erase seen false
db_input critical ember/erase || true
db_go
db_get ember/erase
if [ "$RET" != ERASE ]; then
    stop_here ember/aborted            # never returns; the disk is untouched
fi

# Confirmed: now, and only now, answer partman's erase questions.
answer() { db_set "$1" "$2"; db_fset "$1" seen true; }
answer partman-auto/disk "$best"
answer partman-lvm/device_remove_lvm true
answer partman-md/device_remove_md true
answer partman-lvm/confirm true
answer partman-lvm/confirm_nooverwrite true
answer partman-partitioning/confirm_write_new_label true
answer partman/confirm true
answer partman/confirm_nooverwrite true
# The boot loader goes on the same disk. (The preseed's "default" means the
# first disk, which is the USB stick when the internal one is eMMC or NVMe.)
answer grub-installer/bootdev "$best"
