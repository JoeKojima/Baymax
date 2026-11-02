# OPI4-Flash

Build a personalized bootable microSD image for the **Orange Pi 4 Pro (A733)**.
The image installs Ember, a headless XFCE desktop, AnyDesk and a progress page.
No running Ember device is required to prepare it.

This is based on a DietPi Debian 13 image and the SD provisioning work tested on
one Orange Pi 4 Pro. It is not an image for the Orange Pi 4, 4 LTS, 5 or Raspberry
Pi. The reusable builder has offline checks; a fresh-card boot of this published
version and eye-matrix hardware testing remain pending.

## Three choices

Run the private configurator or copy `settings.example.json` to
`settings.private.json` and edit it locally:

```json
{
  "pr": 20,
  "wifi": {"ssid": "YOUR WIFI NAME", "password": ""},
  "gemini_api_key": ""
}
```

- **PR:** a positive PR number in `JoeKojima/Baymax`. Startup fetches
  `refs/pull/NUMBER/head`, installs and checks the release, then promotes it.
  The same PR is bundled as the first-install fallback if Git is temporarily
  unavailable. Choosing an incompatible PR can fail installation; it does not
  convert arbitrary PRs into runnable Ember versions. PR #20 is the initial target.
- **Wi-Fi:** one SSID and password, installed before first boot. A blank password
  means open Wi-Fi. WPA passwords must be 8–63 UTF-8 bytes or a 64-digit hex PSK.
  Captive-portal sign-in is not automated. Use a reachable private network for
  SSH; public apartment/guest networks may isolate clients.
- **Gemini key:** preload a key, or leave it blank to wait for Ember Setup.
  Password/key entry is hidden in the configurator. With a key and usable devices,
  Ember starts automatically after installation, sends microphone/camera context
  to Gemini, and may record session audio locally. Keep the key blank if capture
  should wait for manual setup.

Every build generates a distinct hostname and separate random Linux/AnyDesk
passwords. No person's passwords, SSH identity or Wi-Fi defaults are included.
The outputs contain secrets: keep the settings, generated access file and image
private. They are ignored by Git. Generate a separate image for each board.

## Build on Linux

Use a Linux host/VM with Python 3.12 or newer, root access and loop-device support.
An x86-64 Linux host can build this ARM image; the builder does not execute ARM
binaries. A Windows/macOS user can use a Linux VM with a writable shared folder.
WSL/container environments require working loop devices and mounts and have not
been verified. Allow at least **6 GiB free disk space** plus space for downloaded
assets. Internet is required during preparation and first boot.

From the repository root, on Debian/Ubuntu:

```bash
sudo apt-get update
sudo apt-get install python3 git xz-utils util-linux e2fsprogs openssl ca-certificates
cd devops/opi4-flash
python3 configure_image.py
sudo python3 build.py --settings settings.private.json --output output
# Make the three private outputs available to the invoking desktop account:
sudo chown "$USER:$(id -gn)" output output/ember-opi4.img \
  output/device-access.private.json output/ready-image.json
```

Confirm `python3 --version` is at least 3.12. The interactive configurator defaults
to PR #20. Enter `-` at the Wi-Fi password prompt for open Wi-Fi, or at the key
prompt to leave manual Setup. Enter preserves an existing secret. To rebuild,
change the settings and choose a new `--output` directory; existing builds are
never overwritten. `--cache` can select an existing verified asset cache.

The builder verifies all downloads against `assets.json`, bundles the selected
PR, injects the three choices and generated credentials, validates firmware and
private-file permissions, runs the offline provisioning tests and checks the
filesystem. It only mounts a **regular image file**. It has no physical-disk
writer. Output:

- `output/ember-opi4.img` — the private boot image.
- `output/device-access.private.json` — hostname, user `ember`, and generated
  Linux/sudo and AnyDesk passwords. These passwords are different.
- `output/ready-image.json` — image SHA256/size, selected PR/ref/commit and
  non-secret build metadata.

Upstream DietPi's URL can be updated in place. A checksum mismatch stops the build;
review the new release and its board layout before updating `assets.json` and the
layout assertions. Do not bypass verification. Third-party assets are downloaded
from [DietPi](https://dietpi.com/downloads/images/),
[AnyDesk](https://deb.anydesk.com/pool/main/a/anydesk/) and
[Astral's Python builds](https://github.com/astral-sh/python-build-standalone).
They are not redistributed in this repository.

## Flash and first boot

1. Use an image writer such as [balenaEtcher](https://etcher.balena.io/) to write
   `ember-opi4.img` to the intended microSD card. Select the card carefully; writing
   erases that card. Use the writer's verification step. An 8 GiB or larger card is
   recommended; larger cards leave room for packages, updates and recordings.
2. Power the Orange Pi off, insert the card, connect the usual USB microphone,
   speaker and optional camera, make the chosen Wi-Fi available, and power it on
   with a suitable board power supply. Eye hardware is optional and unverified.
3. From a device on the same reachable network, open
   `http://HOSTNAME.local:8765/`, using the generated hostname in the private access
   file. If mDNS does not resolve, use the board's IP from the router/hotspot list.
   The page shows numbered setup stages, recent package activity and AnyDesk ID.
   ARM voice-analysis builds may be slow; keep power and internet available.
4. Connect to that ID in AnyDesk with the generated **AnyDesk** password. The
   virtual 1280×720 Xorg/XFCE desktop needs no HDMI monitor. For terminal/sudo use
   the generated **Linux** password, not the AnyDesk password.
5. Open **Ember Setup** to inspect devices/key or **Ember Device Settings** for
   Wi-Fi, Git source, update, start/stop and logs. A single unambiguous USB input,
   USB output and camera can be selected automatically. Saved choices are resolved
   across Linux device renumbering. With several matching devices, choose in Setup
   and restart; the launcher stops rather than guesses. No camera permits audio-only
   operation. A saved disabled camera stays disabled.

First boot installs Debian prerequisites and runs the selected PR's complete
offline release gate. It also installs pinned v10 native audio/eye/audiobook
dependencies omitted by the current Orange Pi profile, using GCC for ARM builds.
No extra microphone/camera streams are opened by hardware enumeration or tests.

## Updates, recovery and data flows

Automatic updates run on boot. Packages and tests are installed in a separate
release directory before switching `~/ember-deployment/current`. The existing
`.env` survives a release update; the original key preset is used only for the
first release. Installer failures keep an existing working release. A runtime
failure after promotion is not an automatic rollback; select the previous release
and restart if needed. Retain the previous release and back up private settings.

Changing Wi-Fi can disconnect AnyDesk/SSH. Keep a known network available and
verify the new connection before turning it off. To inspect the board in its own
terminal: `sudo wpa_cli -i wlan0 status` shows `ssid` and `ip_address`.

Logs: `/var/lib/ember/provision.log`,
`/home/ember/ember-deployment/logs/update.log`, and `journalctl -b`.
Retry system setup with `sudo systemctl restart ember-provision`; retry the
updater with `sudo systemctl restart ember-update`. A first boot still requires
internet for apt/Python packages even when its source bundle is available.
No entry in the router list can indicate a power, boot or Wi-Fi problem; a status
page or ping alone does not establish a working live conversation.

Preparation contacts the listed asset hosts and GitHub. First boot contacts Debian,
Python package/model hosts and AnyDesk; remote sessions use AnyDesk's service.
Deliberately starting Ember sends audio/video to Google and the runtime may store
recordings locally. The chosen PR's toolkit documentation describes its additional
provider calls. These scripts do not configure caregiver alerts.

## Validation

```bash
python3 -m unittest discover -s devops/opi4-flash -p 'test_*.py'
cd Baymax-main
.venv/bin/python check_release.py --report .local-review/opi4-flash.md
```

Run the provisioning suite on Linux for its Bash updater integration tests.
Windows can run the pure-Python cases; Linux-only updater cases skip there.
The builder also runs the provisioning suite against its assembled payload and
checks the mounted image. Tests use fake devices, synthetic data and mocked
providers; they do not start capture or certify a fresh boot, live conversation,
AnyDesk session, speaker quality or eye hardware. Record those checks and obtain
lead review separately. See the repository testing protocol.
