# Shipping a new Ember unit

New units run the robot software from the published image (`ghcr.io/joekojima/baymax`) and update themselves overnight. Unit 001 (the original LattePanda) still runs from its git checkout. See "Unit 001" below.

## One-time setup (GitHub)

Done on 2026-10-15:
- The workflows in `.github/workflows/` publish `:main` and `:sha-<commit>` on every push to `main`. Pull requests only check that the image builds.
- The package `ghcr.io/joekojima/baymax` is public, so robots pull it without logging in. The image holds no secrets: `.dockerignore` is an allow-list.
- `stable` points at `sha-6d23861`.

## Fleet console

Units report to the fleet console at **tadashirobotics.com/admin** through the fleet agent (`ember-agent.py`, run by `ember-agent.service`). The agent checks in every minute. It sends the unit's health, and the console sends back:
- **the release for the unit's channel**, which the unit installs in its nightly window;
- **commands**: restart, update now, roll back;
- **new settings**. The agent applies these at once and restarts Ember. If the unit then can't reach the console for 5 minutes, the agent puts the old `device.toml` back.

The console's channel wins over `update.channel` in `device.toml`; the agent copies it into the file. The console can't change `serial`, `system.runtime`, `system.user` or `update.image`; a settings file that changes them, or leaves them out, is rejected. Units only ever install `ghcr.io/joekojima/baymax:sha-…`, whatever the console sends.

**Keys and patient data:**
- **The console key:** each unit has its own, in `/etc/ember/fleet.env` (root-only). The containers never see that file, so connecting a unit to the console doesn't change where its data goes.
- **Patient data stays on the robot:** transcripts, falls and voice results go to its own dashboard and its fall emails.
- **Sending patient data to the site is a separate decision for each unit:** setting `BAYMAX_DEVICE_KEY` in `secrets.env`. Before anyone does that, note what it changes:
  - the robot's dashboard and its local fall emails stop getting the data;
  - the site emails falls only to accounts paired with the unit;
  - pairing only needs the serial number, which is visible in the hotspot name.

**One-time setup on the prep machine:** put the site's `ADMIN_TOKEN` in a root-only file. `make_usb.sh` uses it to register each new unit and put its key on the stick.

```bash
sudo install -m 600 /dev/null /etc/ember/admin-token
sudo nano /etc/ember/admin-token          # paste the token, save
```

**Units installed before the console** (or whose key was lost) connect with a small folder, made on the prep machine:

```bash
sudo bash deploy/provision/make_connect.sh --serial 002 --out /media/$USER/STICK
# at unit 002, stick plugged in, in a terminal:
sudo bash /media/<user>/<stick>/ember-connect-002/connect.sh
```

`connect.sh`:
- installs the agent, the key and the current host files;
- deletes the key from the stick;
- waits for the first check-in.

Registering gives the unit a new key at once (any old key stops working), so run `connect.sh` soon after. Unit 001 connects in place: `sudo bash deploy/provision/make_connect.sh --serial 001 --here`. That installs only the agent.

```bash
sudo systemctl status ember-agent
sudo journalctl -u ember-agent -n 50        # what it did (it logs changes, not every check-in)
```

## Releasing an update

With the console, promote a build on its *Releases* page: dev, then beta, then stable. Units without a device key follow the GHCR channel tags instead. For those, run *Promote release* on GitHub with the `sha-…` tag of a *Robot image* run on `main`. Either way, every unit on that channel:
- updates at a random time between 2 and 3 AM, robot-local;
- restarts on the new version;
- goes back to the previous one if the AI core doesn't print "System ready" or the dashboard doesn't answer within 5 minutes.

A release that failed on a unit isn't retried there unless an admin presses *Update now*. The console emails the admins about the failure. Log: `/var/log/ember/update.log`.

To roll every unit back, promote the previous `sha-…` tag.

**Host files come with releases.** The image carries `startup.sh`, the portal, `compose.yaml`, `ember-update.sh`, the agent and the systemd units.

How `ember-update.sh` handles them:
- **Checks before installing:** syntax for all of them, plus a self-test run of the new agent.
- **Installs them before restarting:** Ember restarts through `ember.service`, so the health check covers the new `startup.sh` and `compose.yaml`.
- **Keeps the ones they replaced** in `/opt/ember/host-previous`.
- **Puts the old ones back** when a release fails its health check, or on *Roll back*, along with the previous image.

If the agent stops checking in for a day while the site is up, the nightly update follows the channel's GHCR tag. That way, a release with a fixed agent can still arrive.

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
- `--channel` (`dev`, `beta` or `stable`; default `stable`);
- `--no-console` to skip registering the unit in the fleet console (it then follows the GHCR tag; the default needs `/etc/ember/admin-token`, see above);
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
8. enables `ember.service`, the nightly `ember-update.timer` and the fleet agent;
9. reboots.

The log is in `/var/log/ember/provision.log`. It's safe to run again.

## On the unit

| | |
|---|---|
| `/etc/ember/device.toml` | settings (`fall_detection.enabled`, `update.channel`, …) |
| `/etc/ember/secrets.env` | shared keys (root and the `docker` group only) |
| `/etc/ember/fleet.env` | the unit's fleet console key (root only) |
| `/var/lib/ember` | memory, accounts, recordings, logs written by the app |
| `/opt/ember` | `startup.sh`, the Wi-Fi portal, `compose.yaml`, `ember-update.sh`, `ember-agent.py` |
| `/var/lib/ember-agent` | fleet agent state: the console's release (`target`), last check-in, unconfirmed settings |
| `/var/log/ember` | boot, portal, update and provisioning logs |

```bash
sudo systemctl restart ember.service        # restart everything (the core saves its session first)
sudo docker compose -f /opt/ember/compose.yaml logs -f core
sudo /opt/ember/ember-update.sh             # update now instead of waiting for the night
sudo /opt/ember/ember-update.sh --rollback  # back to the version before
```

## Limits for now

- **No disk encryption yet.** Transcripts, recordings and memories on a stolen unit are readable. This is on the HIPAA list.
- **Shared secrets.** Every unit carries the same Gemini key and email login until the console issues per-robot tokens. If a unit is lost, rotate the key.
- **x86 only.** The Raspberry Pi 5 needs an `arm64` build (see `platforms` in `image.yml`).
- **No remote shell.** The console has status, settings, restart, update and rollback; Tailscale/SSH comes later.

## Unit 001

It keeps `system.runtime = "venv"` and `baymax.service`. Moving it to containers means:
1. run `deploy/docker/migrate_data.sh` again for the latest memories;
2. set `runtime = "docker"`;
3. install the `/opt/ember` files and `ember.service` in place of `baymax.service`.
