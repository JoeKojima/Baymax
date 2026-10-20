#!/bin/bash
# Updates the Ember image on units with the docker runtime. Run nightly by
# ember-update.timer (2-4 AM robot-local time) and by the fleet agent for the
# console's "Update now" and "Roll back". If the AI core doesn't reach "System
# ready" or the dashboard doesn't answer within HEALTH_TIMEOUT, it goes back
# to the previous image.
#
# Which release: units connected to the fleet console follow the release the
# console picked for their channel (ember-agent.py writes it to
# $AGENT_STATE/target). Units without the agent follow the GHCR tag named
# by update.image:update.channel in device.toml.
#
# Local tags: ember:current is what compose runs, ember:previous the one
# before it (the rollback target).
#
#   sudo /opt/ember/ember-update.sh             # same as the timer
#   sudo /opt/ember/ember-update.sh --now       # also retries a release that failed here
#   sudo /opt/ember/ember-update.sh --rollback  # back to ember:previous
#
# A healthy switch also installs the host files (this script, startup.sh, the
# fleet agent, the systemd units) that came in the image, so they ship with
# releases. The last line written to update.log says what happened; the
# agent reports it to the console.
set -uo pipefail

MODE=nightly
case ${1:-} in
    "") ;;
    --now) MODE=now ;;
    --rollback) MODE=rollback ;;
    *) echo "usage: $0 [--now | --rollback]"; exit 2 ;;
esac

EMBER_HOME=${EMBER_HOME:-/opt/ember}
LOG_DIR=${LOG_DIR:-/var/log/ember}
EMBER_COMPOSE=${EMBER_COMPOSE:-$EMBER_HOME/compose.yaml}
AGENT_STATE=${EMBER_AGENT_STATE:-/var/lib/ember-agent}
SECRETS=${EMBER_SECRETS:-/etc/ember/secrets.env}
UNIT_DIR=${EMBER_UNIT_DIR:-/etc/systemd/system}
HEALTH_TIMEOUT=${HEALTH_TIMEOUT:-300}     # seconds for the new version to come up
READY_PATTERN="System ready"              # printed by the AI core once Gemini is connected
DASHBOARD_URL=${DASHBOARD_URL:-http://127.0.0.1:5000/}
FAILED_FILE=$LOG_DIR/failed-image   # id of a release that failed here; not retried

mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_DIR/update.log") 2>&1
log() { echo "$(date '+%F %T') $*"; }

exec 9>"${LOCK_FILE:-/run/ember-update.lock}"
if ! flock -n 9; then
    log "another update is running"
    [ "$MODE" = nightly ] && exit 0
    exit 1
fi

cfg() { /usr/bin/python3 "$EMBER_HOME/device_config.py" "$1"; }
IMAGE=$(cfg update.image) && CHANNEL=$(cfg update.channel) || { log "cannot read device.toml"; exit 1; }
CORE_UID=$(id -u "$(cfg system.user)")

compose() {
    EMBER_UID=$CORE_UID EMBER_IMAGE=ember:current \
        docker compose -f "$EMBER_COMPOSE" "$@"
}
image_id() { docker image inspect -f '{{.Id}}' "$1" 2>/dev/null; }
# Release name of an image (sha-1a2b3c4, from its revision label), else its short id.
revision_of() { docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$1" 2>/dev/null; }
release_of() {
    local rev id
    rev=$(revision_of "$1")
    if [[ $rev =~ ^[0-9a-f]{7} ]]; then echo "sha-${rev:0:7}"; else id=$(image_id "$1"); echo "${id:7:12}"; fi
}
# Each release is pulled under its own tag; untag all but current and
# previous so old ones don't fill the disk.
prune_images() {
    local keep ref id
    keep=" $(image_id ember:current) $(image_id ember:previous) "
    docker images --no-trunc --format '{{.Repository}}:{{.Tag}} {{.ID}}' "$IMAGE" | while read -r ref id; do
        [[ $keep == *" $id "* ]] || docker rmi "$ref" >/dev/null 2>&1
    done
    docker image prune -f >/dev/null
}

# For the console: the outcome of an update attempt, sent at the agent's next
# check-in (it emails the admins about failures). Only on units with the agent.
report() {    # result tag message
    [ -d "$AGENT_STATE" ] || return 0
    python3 - "$AGENT_STATE/last-update.json" "$@" <<'EOF'
import json, os, sys
from datetime import datetime, timezone
path, result, tag, message = sys.argv[1:]
at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
with open(path + ".tmp", "w") as f:
    json.dump({"at": at, "result": result, "tag": tag, "message": message}, f)
os.replace(path + ".tmp", path)
EOF
}

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

restart_on() {    # recreate the containers on ember:current and wait for them
    START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    # Recreates the containers; the core gets SIGINT and saves its session first.
    compose up -d --no-build --pull never
    wait_healthy
}

# Host files in the image (/app/deploy, /app/device_config.py) -> where
# provision.sh put them. Images built before 2026-10-20 have none; then the
# installed ones stay.
HOST_FILES="deploy/host/startup.sh:755 deploy/provision/ember-update.sh:755 deploy/provision/ember-agent.py:755
deploy/host/baymax_portal.py:644 device_config.py:644 deploy/docker/compose.yaml:644"
UNIT_FILES="ember.service ember-update.service ember-update.timer ember-agent.service"

install_host_files() {
    local tmp c entry src mode name changed="" units=""
    tmp=$(mktemp -d)
    c=$(docker create ember:current) || { rm -rf "$tmp"; return 0; }
    docker cp "$c:/app/deploy" "$tmp/deploy" 2>/dev/null && docker cp "$c:/app/device_config.py" "$tmp/" 2>/dev/null
    local copied=$?
    docker rm -f "$c" >/dev/null
    if [ $copied != 0 ]; then
        log "this release carries no host files; keeping the installed ones"
        rm -rf "$tmp"
        return 0
    fi
    # All there and readable before anything is replaced.
    for entry in $HOST_FILES; do
        src=$tmp/${entry%:*}
        [ -f "$src" ] || { log "host files incomplete (${entry%:*} missing); keeping the installed ones"; rm -rf "$tmp"; return 0; }
        case $src in
            *.sh) bash -n "$src" ;;
            *.py) python3 -c 'import ast, sys; ast.parse(open(sys.argv[1]).read())' "$src" ;;
            *) true ;;
        esac || { log "host file ${entry%:*} is broken; keeping the installed ones"; rm -rf "$tmp"; return 0; }
    done
    for name in $UNIT_FILES; do
        [ -f "$tmp/deploy/provision/$name" ] || { log "host files incomplete ($name missing); keeping the installed ones"; rm -rf "$tmp"; return 0; }
    done

    # Replace by rename: startup.sh and this script are read by running bash.
    for entry in $HOST_FILES; do
        src=$tmp/${entry%:*} mode=${entry#*:} name=$(basename "${entry%:*}")
        cmp -s "$src" "$EMBER_HOME/$name" && continue
        install -m "$mode" "$src" "$EMBER_HOME/.$name.new" && mv -f "$EMBER_HOME/.$name.new" "$EMBER_HOME/$name"
        changed="$changed $name"
    done
    for name in $UNIT_FILES; do
        cmp -s "$tmp/deploy/provision/$name" "$UNIT_DIR/$name" && continue
        install -m 644 "$tmp/deploy/provision/$name" "$UNIT_DIR/$name"
        units="$units $name"
    done
    if [ -n "$units" ]; then
        systemctl daemon-reload
        systemctl enable --quiet ember-agent.service 2>/dev/null || true
        systemctl start ember-agent.service 2>/dev/null || true
    fi
    local rev
    rev=$(revision_of ember:current)
    [ -n "$rev" ] && echo "${rev:0:7}" > "$EMBER_HOME/REVISION"
    rm -rf "$tmp"
    [ -n "$changed$units" ] && log "host files updated:$changed$units (startup.sh and units apply at Ember's next restart)"
    return 0
}

# ── Roll back (console command) ────────────────────────────────────────
if [ "$MODE" = rollback ]; then
    CUR=$(image_id ember:current)
    PREV=$(image_id ember:previous)
    [ -n "$PREV" ] || { log "no previous version on this unit to roll back to"; exit 1; }
    [ "$PREV" != "$CUR" ] || { log "already on the previous version"; exit 1; }
    FROM=$(release_of ember:current) TO=$(release_of ember:previous)
    log "rolling back $FROM -> $TO (requested from the console)"
    # Don't let the nightly update bring this release back; a newer one still installs.
    echo "$CUR" > "$FAILED_FILE"
    docker tag ember:previous ember:current
    docker tag "$CUR" ember:previous     # so "Update now" can go forward again
    if restart_on; then
        install_host_files
        prune_images
        log "rolled back to $TO; $FROM won't be installed again here unless you press Update now"
        exit 0
    fi
    compose logs --tail 40 core
    log "rolled back to $TO, but it is not healthy either"
    exit 1
fi

# ── Which release ──────────────────────────────────────────────────────
pick_ref() {
    local fallback="$IMAGE:$CHANNEL" cloud
    [ -d "$AGENT_STATE" ] || { echo "$fallback"; return; }      # no fleet agent here
    if [ -n "$(find "$AGENT_STATE/last-checkin" -mmin -1440 2>/dev/null)" ]; then
        cat "$AGENT_STATE/target" 2>/dev/null                  # empty: no release for the channel
        return
    fi
    # No check-in for a day (or never). If the site answers, the agent is the
    # problem: follow the channel's GHCR tag so a fixed release can still
    # arrive. If it doesn't, keep the current version.
    cloud=$(sed -n 's/^BAYMAX_CLOUD_URL=//p' "$SECRETS" 2>/dev/null | tail -n1 | tr -d "\"'")
    curl -s -o /dev/null --max-time 10 "${cloud:-https://tadashirobotics.com}/" && echo "$fallback"   # any HTTP answer
}
REF=$(pick_ref)
if [ -z "$REF" ]; then
    log "no release chosen for this unit (console unreachable or nothing on channel $CHANNEL); keeping the current version"
    exit 0
fi
TAG=${REF##*:}

# Any HTTP answer (ghcr.io gives 401 here) means the registry is reachable.
if ! curl -s -o /dev/null --max-time 10 https://ghcr.io/v2/; then
    log "offline; skipping update"
    [ "$MODE" = now ] && { report failed "$TAG" "The unit couldn't reach the image registry"; exit 1; }
    exit 0
fi

log "checking $REF"
if ! docker pull -q "$REF" >/dev/null; then
    log "download of $REF failed"
    [ "$MODE" = now ] && report failed "$TAG" "Download of $REF failed"
    exit 1
fi

NEW=$(image_id "$REF")
CUR=$(image_id ember:current)
if [ "$NEW" = "$CUR" ]; then
    log "up to date ($TAG)"
    exit 0
fi
if [ "$NEW" = "$(cat "$FAILED_FILE" 2>/dev/null)" ]; then
    if [ "$MODE" = now ]; then
        log "$TAG failed here before; trying again (Update now)"
        rm -f "$FAILED_FILE"
    else
        log "$TAG failed here before; waiting for a newer release"
        exit 0
    fi
fi

FROM=$(release_of ember:current)
OLD_PREV=$(image_id ember:previous)
log "updating $FROM -> $TAG"
[ -n "$CUR" ] && docker tag ember:current ember:previous
docker tag "$REF" ember:current

if restart_on; then
    install_host_files
    prune_images
    report updated "$TAG" "Updated from $FROM"
    log "update ok: running $TAG"
    exit 0
fi

log "new version not healthy after ${HEALTH_TIMEOUT}s"
echo "$NEW" > "$FAILED_FILE"
compose logs --tail 40 core
if [ -z "$CUR" ]; then
    report failed "$TAG" "$TAG didn't start within ${HEALTH_TIMEOUT}s and there was no previous version to go back to"
    log "$TAG not healthy and no previous image to roll back to"
    exit 1
fi
docker tag ember:previous ember:current
# Keep the rollback target as it was before this attempt.
[ -n "$OLD_PREV" ] && docker tag "$OLD_PREV" ember:previous
if restart_on; then
    prune_images
    report rolled_back "$TAG" "$TAG didn't start within ${HEALTH_TIMEOUT}s; back on $FROM"
    log "$TAG not healthy; rolled back to $FROM"
else
    report rolled_back "$TAG" "$TAG didn't start within ${HEALTH_TIMEOUT}s; went back to $FROM, but that isn't healthy either"
    log "$TAG not healthy; rolled back to $FROM, but it is not healthy either"
fi
exit 1
