#!/bin/bash
# Boots a stick image from make_usb.sh in a virtual machine, so the install
# can be checked before a real board's disk is erased. The VM gets the image
# as a USB stick, an empty 32 GB disk (sparse, uses ~10 GB) and UEFI firmware,
# like a LattePanda, but no Wi-Fi, camera, mic or speaker: provision.sh skips
# the hotspot and Ember waits for internet, which is expected.
#
#   bash deploy/provision/usb/test_in_vm.sh ember-unit-002.iso          # window
#   bash deploy/provision/usb/test_in_vm.sh ember-unit-002.iso --headless
#
# --headless runs without a window; drive it through the QEMU monitor socket
# printed below (screendump to see the screen, sendkey to type).
# Needs: sudo apt install qemu-system-x86 ovmf. The VM disk is kept in
# $VM_DIR between runs (the "power on again" step); delete it when done.
set -euo pipefail

ISO=${1:?usage: test_in_vm.sh STICK.iso [--headless]}
VM_DIR=${VM_DIR:-$HOME/.cache/ember-vm}
mkdir -p "$VM_DIR"
DISK=$VM_DIR/disk.qcow2
VARS=$VM_DIR/OVMF_VARS.fd
[ -f "$DISK" ] || qemu-img create -q -f qcow2 "$DISK" 32G
[ -f "$VARS" ] || cp /usr/share/OVMF/OVMF_VARS_4M.fd "$VARS"

DISPLAY_ARGS=(-display gtk)
if [ "${2:-}" = --headless ]; then
    DISPLAY_ARGS=(-display none -monitor "unix:$VM_DIR/monitor.sock,server,nowait")
    echo "QEMU monitor: $VM_DIR/monitor.sock"
fi

# Stick first in the boot order, then the disk, as when the boot menu picks
# the stick. Once the installer has powered off, run again without the stick:
#   NO_STICK=1 bash test_in_vm.sh STICK.iso
STICK=(-device qemu-xhci -drive if=none,id=stick,format=raw,readonly=on,file="$ISO"
       -device usb-storage,drive=stick,bootindex=1)
[ -n "${NO_STICK:-}" ] && STICK=()

exec qemu-system-x86_64 -enable-kvm -machine q35 -cpu host -smp 2 -m 4096 \
    -drive if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd \
    -drive if=pflash,format=raw,file="$VARS" \
    "${STICK[@]}" \
    -drive if=none,id=disk,format=qcow2,file="$DISK" -device virtio-blk-pci,drive=disk,bootindex=2 \
    -nic none -serial "file:$VM_DIR/serial.log" \
    "${DISPLAY_ARGS[@]}"
