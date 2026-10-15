# Shipping a new Ember unit

New units run the robot software from the published image (`ghcr.io/joekojima/baymax`) and update themselves overnight. Unit 001 (the original LattePanda) still runs from its git checkout. See "Unit 001" below.

## One-time setup (GitHub)

1. **Merge the PR that adds `.github/workflows/`.** The *Robot image* workflow then builds and publishes `:main` and `:sha-<commit>` on every push to `main`. Pull requests only check that the image builds.
2. **Make the package public.** Only the repo owner can do this. After the first publish, go to github.com/JoeKojima → Packages → `baymax` → Package settings → Change visibility → Public. Robots pull without logging in, and the image holds no secrets (`.dockerignore` is an allow-list).
3. **Release it.** Actions → *Promote release* → Run workflow, with tag `sha-<commit>` and channel `stable`.

## Releasing an update

Run *Promote release* with the `sha-…` tag of a *Robot image* run on `main`. Every unit on that channel:
- updates at a random time between 2 and 3 AM, robot-local;
- restarts on the new version;
- goes back to the previous one if the AI core doesn't print "System ready" or the dashboard doesn't answer within 5 minutes.

A release that failed on a unit isn't retried there. Log: `/var/log/ember/update.log`.

To roll every unit back, promote the previous `sha-…` tag.

## Preparing a unit

On any machine with this repo checked out and a copy of the shared secrets (unit 001 has them in `/etc/ember/secrets.env`):

```bash
bash deploy/provision/prepare_unit.sh --serial 002 --out /media/$USER/USBSTICK
```

This creates `ember-unit-002/`, holding:
- the unit's `device.toml`: serial, `runtime = "docker"`, hotspot `Ember-0002`, fall detection off;
- the secrets;
- a freshly generated hotspot password;
- the host files.

It prints the hotspot name and password for the unit's label.

Options: `--timezone` (default `America/New_York`), `--user` (default `meowmax`), `--channel` (default `stable`), `--secrets`.

## Setting it up

The board needs:
- Debian 13 desktop with GNOME and a user account;
- internet over **Ethernet or a USB Wi-Fi dongle**, never the built-in `wlo1`, which becomes the hotspot;
- the USB speaker, mic and camera plugged in.

```bash
sudo bash /media/<user>/USBSTICK/ember-unit-002/provision.sh
```

It runs unattended. In order, it:
1. sets the time zone;
2. installs Docker and the host packages, with Docker's firewall rewriting turned off, which the hotspot needs;
3. installs the settings and secrets, then deletes the secrets from the stick;
4. copies the host files to `/opt/ember`;
5. turns on auto-login so PipeWire audio runs at boot;
6. pulls the image;
7. creates the hotspot;
8. enables `ember.service` and the nightly `ember-update.timer`;
9. reboots.

The log is in `/var/log/ember/provision.log`, with a copy on the stick. It's safe to run again.

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

- **Shared secrets.** Every unit carries the same Gemini key and email login until the console issues per-robot tokens. If a unit is lost, rotate the key.
- **Host files don't update.** `startup.sh`, the portal and the update script only change if you re-run provisioning.
- **x86 only.** The Raspberry Pi 5 needs an `arm64` build (see `platforms` in `image.yml`).
- **No remote view yet.** The fleet agent and console come next.

## Unit 001

It keeps `system.runtime = "venv"` and `baymax.service`. Moving it to containers means:
1. run `deploy/docker/migrate_data.sh` again for the latest memories;
2. set `runtime = "docker"`;
3. install the `/opt/ember` files and `ember.service` in place of `baymax.service`.
