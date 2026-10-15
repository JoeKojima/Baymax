#!/bin/bash
# Nightly update for units on the docker runtime (run by ember-update.timer,
# 2-4 AM robot-local time). Pulls update.image:update.channel from device.toml
# and, if it changed, restarts the robot on it. If the AI core doesn't reach
# "System ready" or the dashboard doesn't answer within HEALTH_TIMEOUT, it
# goes back to the previous image.
#
# Local tags: ember:current is what compose runs, ember:previous the one
# before it (the rollback target).
#
#   sudo /opt/ember/ember-update.sh          # same as the timer
set -uo pipefail

EMBER_HOME=${EMBER_HOME:-/opt/ember}
LOG_DIR=${LOG_DIR:-/var/log/ember}
EMBER_COMPOSE=${EMBER_COMPOSE:-$EMBER_HOME/compose.yaml}
HEALTH_TIMEOUT=${HEALTH_TIMEOUT:-300}     # seconds for the new version to come up
READY_PATTERN="System ready"              # printed by the AI core once Gemini is connected
DASHBOARD_URL=${DASHBOARD_URL:-http://127.0.0.1:5000/}
FAILED_FILE=$LOG_DIR/failed-image   # id of a release that failed here; not retried

mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_DIR/update.log") 2>&1
log() { echo "$(date '+%F %T') $*"; }

exec 9>"${LOCK_FILE:-/run/ember-update.lock}"
flock -n 9 || { log "another update is running"; exit 0; }

cfg() { /usr/bin/python3 "$EMBER_HOME/device_config.py" "$1"; }
IMAGE=$(cfg update.image) && CHANNEL=$(cfg update.channel) || { log "cannot read device.toml"; exit 1; }
CORE_UID=$(id -u "$(cfg system.user)")
REF="$IMAGE:$CHANNEL"

compose() {
    EMBER_UID=$CORE_UID EMBER_IMAGE=ember:current \
        docker compose -f "$EMBER_COMPOSE" "$@"
}
image_id() { docker image inspect -f '{{.Id}}' "$1" 2>/dev/null; }

# True once the core has printed READY_PATTERN since START and is still up,
# and the dashboard answers.
healthy() {
    local core
    core=$(compose ps -q core)
    [ -n "$core" ] || return 1
    [ "$(docker inspect -f '{{.State.Running}}' "$core")" = true ] || return 1
    docker logs --since "$START" "$core" 2>&1 | grep -q "$READY_PATTERN" || return 1
    curl -fs -o /dev/null --max-time 5 "$DASHBOARD_URL"
}

wait_healthy() {
    local waited=0
    while [ $waited -lt "$HEALTH_TIMEOUT" ]; do
        healthy && return 0
        sleep 10
        waited=$((waited + 10))
    done
    return 1
}

# Any HTTP answer (ghcr.io gives 401 here) means the registry is reachable.
curl -s -o /dev/null --max-time 10 https://ghcr.io/v2/ || { log "offline; skipping update"; exit 0; }

log "checking $REF"
if ! docker pull -q "$REF" >/dev/null; then
    log "pull of $REF failed; skipping"
    exit 1
fi

NEW=$(image_id "$REF")
CUR=$(image_id ember:current)
if [ "$NEW" = "$CUR" ]; then
    log "up to date (${CUR:7:12})"
    exit 0
fi
if [ "$NEW" = "$(cat "$FAILED_FILE" 2>/dev/null)" ]; then
    log "${NEW:7:12} failed here before; waiting for a newer release"
    exit 0
fi

log "updating ${CUR:7:12} -> ${NEW:7:12}"
[ -n "$CUR" ] && docker tag ember:current ember:previous
docker tag "$REF" ember:current

START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
# Recreates the containers; the core gets SIGINT and saves its session first.
compose up -d --no-build --pull never

if wait_healthy; then
    log "update ok: running ${NEW:7:12}"
    docker image prune -f >/dev/null
    exit 0
fi

log "new version not healthy after ${HEALTH_TIMEOUT}s"
echo "$NEW" > "$FAILED_FILE"
compose logs --tail 40 core
if [ -z "$CUR" ]; then
    log "no previous image to roll back to"
    exit 1
fi
docker tag ember:previous ember:current
START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
compose up -d --no-build --pull never
if wait_healthy; then
    log "rolled back to ${CUR:7:12}"
else
    log "rolled back to ${CUR:7:12}, but it is not healthy either"
fi
exit 1
