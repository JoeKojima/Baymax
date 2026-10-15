# Running Ember in Docker

One image (`Baymax-main/Dockerfile`) holds the AI core and the local dashboard, built from `requirements.lock` with the ONNX embedder, pose model and NLTK data baked in. `compose.yaml` runs it as two containers, `core` and `webapp`.

The portal, the hotspot and PipeWire stay on the host. The containers use the host's network and its PipeWire session.

Status: **experimental**. The robot still boots the non-container setup from `startup.sh`. Switching boot over to these containers is a separate step.

Tested on the LattePanda (2026-10-15): conversation through the USB mic and speaker, camera video to Gemini, fall detection and alert email, the dashboard's login gate, and the session summary and voice analysis on `docker compose stop` (38 s).

## What lives where

| | Location on the robot | In the container |
|---|---|---|
| Code, models | the image | `/app` |
| Per-robot settings | `/etc/ember/device.toml` | same path, read-only |
| Secrets (`GOOGLE_API_KEY`, email login, cloud key) | `/etc/ember/secrets.env` (mode 640, group `docker`) | environment variables |
| Logs, memory, accounts, recordings | `/var/lib/ember` (mode 700) | `/data` (`BAYMAX_DATA_DIR`) |

The image never contains secrets or data. `.dockerignore` is an allow-list, so a new file only gets in if it's named there.

## One-time setup on a robot

```bash
sudo mkdir -p /etc/docker /etc/ember /var/lib/ember
echo '{"iptables": false, "ip6tables": false}' | sudo tee /etc/docker/daemon.json
sudo apt install -y docker.io docker-compose
sudo usermod -aG docker meowmax                       # then log in again
sudo chown meowmax:meowmax /var/lib/ember
sudo install -m 640 -g docker .env /etc/ember/secrets.env
bash deploy/docker/migrate_data.sh                   # copy existing data, if any
```

The `daemon.json` setting stops Docker from rewriting the firewall. By default Docker blocks forwarding between interfaces, which breaks the hotspot's "shared" connection. Because the containers use host networking, they don't need Docker's networking.

## Build and run (from `Baymax-main`)

```bash
docker compose -f deploy/docker/compose.yaml build
docker compose -f deploy/docker/compose.yaml up -d
docker compose -f deploy/docker/compose.yaml logs -f core
docker compose -f deploy/docker/compose.yaml down     # SIGINT, so the session summary runs
```

Stop the non-container Ember first (`sudo systemctl stop baymax.service`). Both need the camera, mic, speaker and port 5000.

## How the hardware gets in

- **Audio.** The host's PipeWire socket and Pulse socket are mounted in. `pipewire-alsa` in the image provides the ALSA `pipewire` device, which the core already uses, and `pactl` pins the USB speaker.
- **Camera.** `/dev` is mounted, and only video devices (char major 81) are allowed, because the camera's `/dev/videoN` number changes between boots. The container joins the host's `video` group (gid 44).
- **User.** The containers run as uid `EMBER_UID` (default 1000), the host user that owns the PipeWire session and `/var/lib/ember`.
