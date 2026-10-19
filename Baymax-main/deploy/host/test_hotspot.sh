#!/bin/bash
# Headless test of the no-internet boot path, with automatic recovery.
# Run as root:  sudo bash /home/meowmax/Baymax/test_hotspot.sh
set -u
DONGLE=wlx3c3300008209
UPLINK="AirPennNet [9348be90]"

if [ "$(id -u)" -ne 0 ]; then echo "run with sudo"; exit 1; fi

# Clear any leftovers from a previous run
systemctl stop baymax-restore-net.timer baymax-failsafe-reboot.timer 2>/dev/null
systemctl reset-failed baymax-restore-net.service baymax-failsafe-reboot.service 2>/dev/null

# Safety net 1 (5 min): reconnect the dongle. startup.sh's loop then sees internet,
# tears down the hotspot and starts Baymax normally.
systemd-run --on-active=5min --unit=baymax-restore-net \
  /bin/bash -c "nmcli device connect $DONGLE || nmcli connection up '$UPLINK'"

# Safety net 2 (9 min): if there is STILL no internet, reboot. A normal boot
# autoconnects the dongle (verified today), which brings AnyDesk back.
systemd-run --on-active=9min --unit=baymax-failsafe-reboot \
  /bin/bash -c "wget -q --spider --timeout=10 https://www.google.com || systemctl reboot"

echo "Timers armed:"; systemctl list-timers baymax-* --no-pager

# Now drop internet (device-level disconnect blocks autoconnect on the dongle only)
nmcli device disconnect "$DONGLE"

# Re-run startup.sh from the top; it should take the 'No internet' branch
systemctl restart baymax.service
echo "Service restarted at $(date). Hotspot should appear within ~90s; dongle restores in 5 min."
