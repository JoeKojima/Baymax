# Host boot tooling

Tracked copies of the files that boot a robot. They are **copies, not the live files yet**: the robot still runs them from `/home/meowmax/Baymax/` and `/etc/systemd/system/`. Running the robot directly from these copies is a later fleet-management step that needs root.

`startup.sh` launches `realtime_gemini_10.py`. Until this branch merges, v10 only exists on `fleet-management`, so if another branch is checked out, `startup.sh` falls back to `realtime_gemini_8.py` rather than crash-looping.

| File | Live location | Role |
|---|---|---|
| `startup.sh` | `/home/meowmax/Baymax/startup.sh` | Boot orchestrator: audio levels, hotspot, portal, web app, AI core restart loop |
| `baymax_portal.py` | `/home/meowmax/Baymax/baymax_portal.py` | Always-on captive portal (port 80) for entering Wi-Fi credentials over the hotspot |
| `test_hotspot.sh` | `/home/meowmax/Baymax/test_hotspot.sh` | Headless test of the no-internet boot path |
| `baymax.service` | `/etc/systemd/system/baymax.service` | systemd unit that runs `startup.sh` as root |
| `ember_wifi_setup.py` | `/usr/local/lib/ember/ember_wifi_setup.py` | Orange Pi 4 Pro only: setup hotspot and phone page when no saved Wi-Fi works |
| `ember-wifi-setup.service` | `/etc/systemd/system/ember-wifi-setup.service` | Orange Pi 4 Pro only: runs `ember_wifi_setup.py` as root |

If you change a live file, copy it back here so the two don't drift.

## Per-robot settings

The user, AP radio, hotspot connection, speaker, camera indices, audio levels and fall detection on/off come from `/etc/ember/device.toml` (see `../device.example.toml`). Without that file, everything defaults to this LattePanda's values. To install it on a robot:

```bash
sudo mkdir -p /etc/ember
sudo cp deploy/device.example.toml /etc/ember/device.toml   # then edit for this unit
```

`startup.sh` reads values with `python3 device_config.py section.key`, falling back to built-in defaults if the app folder can't be read. The portal reads the file directly, since it runs outside the app folder.

## Orange Pi 4 Pro Wi-Fi setup hotspot

The x86 robot above keeps its NetworkManager hotspot on a second radio. The
Orange Pi 4 Pro (DietPi, ifupdown + `wpa_supplicant`, one aic8800 radio on
`wlan0`) cannot do both at once, so `ember_wifi_setup.py` takes turns instead:

- While any default route exists (a saved Wi-Fi network or Ethernet), it does nothing.
- After 90 s with no route it stops the Wi-Fi client and starts a WPA2 hotspot
  `Ember-Setup-XXXX` (last four characters of the hostname) on `192.168.4.1`,
  with DHCP and DNS pointing every name at the board, so phones open the page as
  a sign-in page. The page lists nearby networks and takes a name and password.
- A submitted network is added to `/etc/wpa_supplicant/wpa_supplicant.conf`
  (priority 200, derived PSK only, same-name entry replaced, other networks kept),
  the hotspot closes and the board tries for 60 s. If that fails, the hotspot
  returns with an error message. With no phone connected it also closes every
  five minutes to retry saved networks, for example after a router restart.

It uses only the system `python3` standard library. Install on the board:

```bash
sudo apt-get install -y --no-install-recommends hostapd dnsmasq-base iw
sudo systemctl mask hostapd   # the script runs hostapd itself, only when needed
sudo install -D -m 644 deploy/host/ember_wifi_setup.py /usr/local/lib/ember/ember_wifi_setup.py
sudo install -m 644 deploy/host/ember-wifi-setup.service /etc/systemd/system/
printf '{"password": "%s"}\n' 'CHOOSE-8-TO-63-CHARS' | sudo install -D -m 600 /dev/stdin /etc/ember/wifi-setup.json
sudo systemctl enable --now ember-wifi-setup
```

The hotspot never runs open: without `/etc/ember/wifi-setup.json` the service
exits. Give each robot its own password and put it on the robot's label. The
script is installed outside the user's release folder because it runs as root.
The aic8800 driver registers cfg80211 AP support (`start_ap`/`stop_ap`), but no
board has run the hotspot yet. Hardware test pending: boot with the saved network
off and confirm the hotspot appears and needs the password; join from an iPhone
and an Android phone and confirm the page opens and lists networks; a correct
password joins and the hotspot closes; a wrong one brings it back with the error;
a saved network coming back is picked up within about six minutes; no hotspot
runs while a saved network or Ethernet works; the build-time network still works
after adding one. Log: `journalctl -u ember-wifi-setup`.

## Known issues

- Fixed 2026-10-06: root's crontab also launched `startup.sh` at boot (`@reboot`), so two AI cores ran. The line was removed; after the next reboot only one copy started. `baymax.service` should be the only launcher on every robot.
- `startup.sh` sets `LD_PRELOAD=.../libpw-v4l2.so`, but the `pipewire-v4l2` package is not installed, so the preload is ignored and OpenCV opens the camera directly.
- `startup.sh` still hardcodes `BAYMAX_HOME=/home/meowmax/Baymax` (where it, the portal and the logs live), and `test_hotspot.sh` hardcodes this unit's USB dongle (`wlx3c3300008209`).
