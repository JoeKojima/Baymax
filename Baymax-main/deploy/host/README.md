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

## Known issues

- Fixed 2026-10-06: root's crontab also launched `startup.sh` at boot (`@reboot`), so two AI cores ran. The line was removed; after the next reboot only one copy started. `baymax.service` should be the only launcher on every robot.
- `startup.sh` sets `LD_PRELOAD=.../libpw-v4l2.so`, but the `pipewire-v4l2` package is not installed, so the preload is ignored and OpenCV opens the camera directly.
- `startup.sh` still hardcodes `BAYMAX_HOME=/home/meowmax/Baymax` (where it, the portal and the logs live), and `test_hotspot.sh` hardcodes this unit's USB dongle (`wlx3c3300008209`).
