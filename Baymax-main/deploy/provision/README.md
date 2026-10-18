# Shipping a new Ember unit

New units run the robot software from the published image (`ghcr.io/joekojima/baymax`) and update themselves overnight. Unit 001 (the original LattePanda) still runs from its git checkout. See "Unit 001" below.

## One-time setup (GitHub)

Done on 2026-10-15:
- The workflows in `.github/workflows/` publish `:main` and `:sha-<commit>` on every push to `main`. Pull requests only check that the image builds.
- The package `ghcr.io/joekojima/baymax` is public, so robots pull it without logging in. The image holds no secrets: `.dockerignore` is an allow-list.
- `stable` points at `sha-6d23861`.

## Releasing an update

Run *Promote release* with the `sha-…` tag of a *Robot image* run on `main`. Every unit on that channel:
- updates at a random time between 2 and 3 AM, robot-local;
- restarts on the new version;
- goes back to the previous one if the AI core doesn't print "System ready" or the dashboard doesn't answer within 5 minutes.

A release that failed on a unit isn't retried there. Log: `/var/log/ember/update.log`.

To roll every unit back, promote the previous `sha-…` tag.

## Making an installer stick (the usual way)

The stick installs everything with **no network**: Debian 13 with the GNOME desktop, the extra packages, the Ember image and the unit's settings. It **erases the board's internal disk**, after one confirmation.

On an x86-64 Linux machine with Docker, `xorriso` (`sudo apt install xorriso`), ~15 GB free, this repo, and the shared secrets in `/etc/ember/secrets.env` (unit 001 has them):

```bash
sudo bash deploy/provision/make_usb.sh --serial 002 --usb /dev/sdX
```

`/dev/sdX` is the stick, 8 GB or larger. `lsblk` shows which one it is. The script refuses anything that isn't a USB device.

Takes about 20 minutes, plus a one-time ~50-minute download of the Debian DVD, which is cached in `/var/cache/ember-usb`. It:
- generates the unit's login and hotspot passwords;
- records them in `/etc/ember/units.csv` (root-only);
- prints the label.

Options:
- `--timezone` (default `America/New_York`);
- `--channel` (default `stable`);
- `--dvd FILE` to use a DVD image you already have;
- `--force` to reuse a serial that's already in the records;
- leave out `--usb` to get just the stick image.

**The stick holds the shared secrets.** Keep it safe, and re-flash it for the next unit.

## Installing a unit from the stick

The board needs the stick, a keyboard and a monitor, plus the USB speaker, mic, camera and Wi-Fi dongle. It doesn't need a network.

1. Power on and open the boot menu. On LattePanda boards that's usually **F7**; **Del** opens the BIOS setup. Pick the USB stick.
2. *Install Ember unit 002* starts by itself after 5 seconds.
3. The screen shows the disk it will erase. Type `ERASE` and press Enter. Anything else stops without touching the disk.
4. It installs Debian and the desktop on its own (~20 min), then **powers off**.
5. Remove the stick and power on. The first boot sets up Ember (~10 min; progress shows on screen), then reboots into Ember.

Log: `/var/log/ember/provision.log`. If the first boot fails, it tries again on the next boot.

## Board that already runs Debian

`prepare_unit.sh` makes just the unit bundle; the board installs packages and the image over the internet:

```bash
sudo bash deploy/provision/prepare_unit.sh --serial 002 --out /media/$USER/USBSTICK
```

The board needs:
- Debian 13 desktop with GNOME and a user account;
- internet over **Ethernet or a USB Wi-Fi dongle**, never the built-in `wlo1`, which becomes the hotspot;
- the USB speaker, mic and camera plugged in.

```bash
sudo bash /media/<user>/USBSTICK/ember-unit-002/provision.sh
```

What `provision.sh` does, here and on the stick's first boot:
1. sets the time zone;
2. installs Docker and the host packages, with Docker's firewall rewriting turned off, which the hotspot needs;
3. installs the settings and secrets, then deletes the secrets from the bundle;
4. copies the host files to `/opt/ember`;
5. turns on auto-login so PipeWire audio runs at boot;
6. pulls or loads the image;
7. creates the hotspot;
8. enables `ember.service` and the nightly `ember-update.timer`;
9. reboots.

The log is in `/var/log/ember/provision.log`. It's safe to run again.

## On the unit

| | |
|---|---|
| `/etc/ember/device.toml` | settings (`fall_detection.enabled`, `update.channel`, …) |
| `/etc/ember/secrets.env` | shared keys (root and the `docker` group only) |
| `/var/lib/ember` | memory, accounts, recordings, logs written by the app |
| `/opt/ember` | `startup.sh`, the Wi-Fi portal, `compose.yaml`, `ember-update.sh` |
| `/var/log/ember` | boot, portal, update and provisioning logs |

```bash
sudo systemctl restart ember.service        # restart everything (the core saves its session first)
sudo docker compose -f /opt/ember/compose.yaml logs -f core
sudo /opt/ember/ember-update.sh             # update now instead of waiting for the night
```

## Limits for now

- **No disk encryption yet.** Transcripts, recordings and memories on a stolen unit are readable. This is on the HIPAA list.
- **Shared secrets.** Every unit carries the same Gemini key and email login until the console issues per-robot tokens. If a unit is lost, rotate the key.
- **Host files don't update.** `startup.sh`, the portal and the update script only change if you re-run provisioning.
- **x86 only.** The Raspberry Pi 5 needs an `arm64` build (see `platforms` in `image.yml`).
- **No remote view yet.** The fleet agent and console come next.

## Unit 001

It keeps `system.runtime = "venv"` and `baymax.service`. Moving it to containers means:
1. run `deploy/docker/migrate_data.sh` again for the latest memories;
2. set `runtime = "docker"`;
3. install the `/opt/ember` files and `ember.service` in place of `baymax.service`.
