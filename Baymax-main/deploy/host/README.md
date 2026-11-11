# Host boot tooling

Tracked copies of the files that boot a robot. They are **copies, not the live files yet**: the robot still runs them from `/home/meowmax/Baymax/` and `/etc/systemd/system/`. Running the robot directly from these copies is a later fleet-management step that needs root.

`startup.sh` launches `realtime_gemini_10.py`. Until this branch merges, v10 only exists on `fleet-management`, so if another branch is checked out, `startup.sh` falls back to `realtime_gemini_8.py` rather than crash-looping.

| File | Live location | Role |
|---|---|---|
| `startup.sh` | `/home/meowmax/Baymax/startup.sh` | Boot orchestrator: audio levels, hotspot, portal, web app, AI core restart loop |
| `baymax_portal.py` | `/home/meowmax/Baymax/baymax_portal.py` | Always-on captive portal (port 80) for entering Wi-Fi credentials over the hotspot |
| `test_hotspot.sh` | `/home/meowmax/Baymax/test_hotspot.sh` | Headless test of the no-internet boot path |
| `baymax.service` | `/etc/systemd/system/baymax.service` | systemd unit that runs `startup.sh` as root |

If you change a live file, copy it back here so the two don't drift.

## Per-robot settings

The user, AP radio, hotspot connection, speaker, camera indices, audio levels and fall detection on/off come from `/etc/ember/device.toml` (see `../device.example.toml`). Without that file, everything defaults to this LattePanda's values. To install it on a robot:

```bash
sudo mkdir -p /etc/ember
sudo cp deploy/device.example.toml /etc/ember/device.toml   # then edit for this unit
```

`startup.sh` reads values with `python3 device_config.py section.key`, falling back to built-in defaults if the app folder can't be read. The portal reads the file directly, since it runs outside the app folder.

## x86 robot hotspot and portal

The hotspot is a NetworkManager connection on the built-in radio (`network.ap_iface`,
`wlo1`), up the whole time; the robot joins the user's Wi-Fi on a USB dongle.
`startup.sh` starts, before anything that waits:

- `baymax_portal.py` on port 80, restarted whenever it exits;
- a watchdog that checks the hotspot every 30 s and brings it back up when it is down
  and the radio is free (NetworkManager stops retrying after a few failed tries); it
  also sets the connection's `autoconnect-retries` to 0 (retry forever) on every boot;
- `/etc/NetworkManager/dnsmasq-shared.d/ember-captive.conf`, which points the names
  phones use to check for internet (`captive.apple.com`,
  `connectivitycheck.gstatic.com` and others; no real sites) at the hotspot's address.
  While the robot has internet the portal answers those checks the way each phone
  expects, so phones keep using the hotspot normally; while it is offline it sends
  them to the setup page, which iOS and Android then open as a sign-in page. The page
  is also at `http://10.42.0.1` (the hotspot's address; `provision.sh` fixes it).

The page suggests the networks the dongle can see, does not let the phone keyboard
capitalize or autocorrect the name, keeps the password exactly as typed, rescans for
the network before joining (`nmcli` only joins networks in its last scan), moves a
saved profile of the same name onto the dongle, and reports failures and timeouts on
`/status` without the password. Logs: `portal_boot.log` and `startup_boot.log` in the
log folder.

Hardware test pending (unit 001): reboot with the dongle's network off and confirm
the hotspot and page appear within a minute and an iPhone and an Android phone open
the page by themselves; with internet, confirm a phone on the hotspot shows no
sign-in page and browses; take the hotspot down (`nmcli connection down
Baymax_Hotspot`) and confirm it is back within a minute; join a network whose name
starts with a lowercase letter; a password with a space at the end.

## Known issues

- Fixed 2026-10-06: root's crontab also launched `startup.sh` at boot (`@reboot`), so two AI cores ran. The line was removed; after the next reboot only one copy started. `baymax.service` should be the only launcher on every robot.
- `startup.sh` sets `LD_PRELOAD=.../libpw-v4l2.so`, but the `pipewire-v4l2` package is not installed, so the preload is ignored and OpenCV opens the camera directly.
- `startup.sh` still hardcodes `BAYMAX_HOME=/home/meowmax/Baymax` (where it, the portal and the logs live), and `test_hotspot.sh` hardcodes this unit's USB dongle (`wlx3c3300008209`).
