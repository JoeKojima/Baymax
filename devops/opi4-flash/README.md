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
passwords. To use one password you can remember on every card instead, add
`"access_password": "..."` (8-64 characters, no spaces) to `settings.private.json`;
it then becomes both the Linux (SSH/sudo) and AnyDesk password. AnyDesk is
reachable by its ID from any network, so a short shared password is weaker. No person's passwords, SSH identity or Wi-Fi defaults are included.
The outputs contain secrets: keep the settings, generated access file and image
private. They are ignored by Git. Generate a separate image for each board.

## Build on Linux

Use a Linux host/VM with Python 3.12 or newer, root access and loop-device support.
An x86-64 Linux host can build this ARM image. It preinstalls the Debian packages,
AnyDesk and Ember's Python environment by running the image's ARM64 apt/Python in a
chroot through `qemu-user-static`, so first boot downloads almost nothing. An ARM64
Linux host runs them natively. A Windows/macOS user can use a Linux VM with a
writable shared folder. WSL/container environments require working loop devices,
mounts and binfmt emulation and have not been verified. Allow at least **20 GiB
free disk space** (a 7 GiB cached base image plus each 7 GiB card image).
Internet is required during preparation; first boot only checks Git for updates.

From the repository root, on Debian/Ubuntu:

```bash
sudo apt-get update
sudo apt-get install curl python3 git xz-utils util-linux e2fsprogs openssl ca-certificates \
  qemu-user-static binfmt-support
cd devops/opi4-flash
python3 configure_image.py
sudo python3 build.py --settings settings.private.json --output output
# Make the three private outputs available to the invoking desktop account:
sudo chown "$USER:$(id -gn)" output output/ember-opi4.img \
  output/device-access.private.json output/ready-image.json
```

On Ubuntu 26.04 and other newer releases, install `qemu-user-binfmt` instead of
`qemu-user-static binfmt-support`. `/proc/sys/fs/binfmt_misc/qemu-aarch64` must exist.

Confirm `python3 --version` is at least 3.12. The interactive configurator defaults
to PR #20. Enter `-` at the Wi-Fi password prompt for open Wi-Fi, or at the key
prompt to leave manual Setup. Enter preserves an existing secret. To rebuild,
change the settings and choose a new `--output` directory; existing builds are
never overwritten. `--cache` can select an existing verified asset cache.

The first build is slow (often an hour or more under emulation): it installs every
system package into a cached base image, `.cache/packages-<key>.img`. Later cards
reuse it and only add their settings and the Ember release; Python wheels built
under emulation are cached in `.cache/uv`. Packages PyPI only ships as source
(praat-parselmouth, miniaudio) download prebuilt from the `opi4-wheels-1` release,
so a new PC skips that compile; a version that no longer matches still compiles.
Changing the non-wheel `assets.json` entries,
the package stage of `build_linux.sh` or `payload/system-packages.txt` builds a new base.

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

The builder has already installed the system packages and run the selected PR's
complete offline release gate, including the pinned v10 native audio/eye/audiobook
dependencies omitted by the current Orange Pi profile. First boot enables services,
sets the AnyDesk password and checks Git. Only if the selected PR has new commits
does it install that release, downloading any changed Python packages.
No extra microphone/camera streams are opened by hardware enumeration or tests.

The conversation service starts **`run_mvp.py`**, which wraps the same v10
conversation in `run_pc.py` with saved-device readiness, single-instance handling,
the eye lifecycle and the selected PR's MVP defaults. PR #20 currently defaults
to 400 ms turn-end silence, interruptions off, the wait cue off and voice analysis
off. Existing `.env` overrides still win; for example
`BAYMAX_TURN_END_SILENCE_MS=600` is a device-local timing choice, not a different PR.
No connected eye board is required for conversation; physical eyes and their
serial port must be configured/verified separately. v10's built-in pose/fall
settings come from `/etc/ember/device.toml`; its optional CPU fall toolkit is a
separate setting and should not be mistaken for that built-in detector.

## Wipe, reflash and test a fresh card

For a clean Ember install, reflash the boot microSD. The image writer replaces
that card's partition table and filesystem; a separate Windows format is not
needed. This resets the SD installation, not any onboard eMMC/SPI storage, and is
not a secure erase of old secrets. Back up anything needed from the card first.

1. Shut down the board (`sudo poweroff`), wait for shutdown, unplug power and
   remove the microSD. Insert it into the PC through a USB card reader. A USB
   reader is the flashing adapter; this guide boots the board from microSD.
2. To change PR, Wi-Fi or key, run the configurator and builder above with a new
   output directory. Otherwise reuse the existing private image. Keep its private
   access file; reflashing the same image restores that image's credentials.
3. In the image writer, select `ember-opi4.img` (or your existing personalized
   `.img`), select the microSD by capacity/device identity, flash and verify.
   Everything on the selected card is erased. Cancel Windows prompts to format
   Linux partitions, then safely eject the reader.
4. Follow **Flash and first boot** above with internet and the configured Wi-Fi
   available. Record the setup page completing, the selected Git PR/commit,
   AnyDesk access and an audible multi-turn conversation. Confirm the intended
   400 ms timing and enabled built-in pose/fall settings in Setup/settings;
   restored device-local overrides can change those defaults.
5. With PR #21 integrated into PR #20, test "Ember disengage", then "Ember
   engage". Disengage withholds Gemini-bound microphone audio while an offline
   wake listener runs; camera frames and existing local recordings continue.
   Test fall check-ins with safe recorded/synthetic input separately.
6. Reboot once more with internet available. Confirm the updater finishes and
   conversation starts again. Record the installed commit; unchanged source can
   report "already installed". Verify eyes separately when connected.

An existing image can contain an older bundled PR revision. Its boot updater
fetches the latest selected PR when GitHub is reachable; the image file itself
does not change when that PR is updated. Without Git access, a fresh card can
install its older bundle, so check the installed commit before judging a test.

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
updater with `sudo systemctl restart ember-update`. A preinstalled card boots
without internet; an image without `/var/lib/ember/packages-baked` falls back to
downloading `system-packages.txt` on first boot.
No entry in the router list can indicate a power, boot or Wi-Fi problem; a status
page or ping alone does not establish a working live conversation.

Preparation contacts the listed asset hosts, GitHub, Debian and Python package
hosts. First boot contacts GitHub, plus Python package/model hosts for a newer
release, and AnyDesk; remote sessions use AnyDesk's service.
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
