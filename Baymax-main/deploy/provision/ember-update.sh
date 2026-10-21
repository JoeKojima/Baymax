#!/bin/bash
# Updates the Ember image on units with the docker runtime. Run nightly by
# ember-update.timer (2-4 AM robot-local time) and by the fleet agent for the
# console's "Update now" and "Roll back". If the AI core doesn't reach "System
# ready" or the dashboard doesn't answer within HEALTH_TIMEOUT, it goes back
# to the previous image and the previous host files.
#
# Which release: units connected to the fleet console follow the release the
# console picked for their channel (ember-agent.py writes it to
# $AGENT_STATE/target). Units without the agent follow the GHCR tag named
# by update.image:update.channel in device.toml. Only RELEASE_IMAGE is ever
# installed, and from the console only sha-<commit> tags.
#
# Host files (this script, startup.sh, the fleet agent, compose.yaml, the
# systemd units) ride in the image. They're installed before the restart, so
# the health check covers them (Ember restarts through ember.service, which
# runs the new startup.sh and compose.yaml), and the ones they replaced are
# kept in $EMBER_HOME/host-previous: a failed update or a rollback puts them
# back along with the previous image.
#
# Local tags: ember:current is what compose runs, ember:previous the one
# before it (the rollback target).
#
#   sudo /opt/ember/ember-update.sh             # same as the timer
#   sudo /opt/ember/ember-update.sh --now       # also retries a release that failed here
#   sudo /opt/ember/ember-update.sh --rollback  # back to ember:previous
#
# The last line logged says what happened (also in $AGENT_STATE/last-run);
# the agent reports it to the console.
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
FLEET_ENV=${EMBER_FLEET_ENV:-/etc/ember/fleet.env}
UNIT_DIR=${EMBER_UNIT_DIR:-/etc/systemd/system}
RELEASE_IMAGE=${EMBER_RELEASE_IMAGE:-ghcr.io/joekojima/baymax}
HEALTH_TIMEOUT=${HEALTH_TIMEOUT:-300}     # seconds for the new version to come up
READY_PATTERN="System ready"              # printed by the AI core once Gemini is connected
DASHBOARD_URL=${DASHBOARD_URL:-http://127.0.0.1:5000/}
FAILED_FILE=$LOG_DIR/failed-image   # id of a release that failed here; not retried
HOST_PREVIOUS=$EMBER_HOME/host-previous   # host files that go with ember:previous

mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_DIR/update.log") 2>&1
# Each line also goes straight to $AGENT_STATE/last-run: tee can still be
# writing update.log when this script exits, and the agent reports the last
# line as the command's result.
log() {
    local line
    line="$(date '+%F %T') $*"
    echo "$line"
    [ -d "$AGENT_STATE" ] && echo "$*" > "$AGENT_STATE/last-run"
    return 0
}

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
revision_of() { docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$1" 2>/dev/null; }
# Release name of an image (sha-1a2b3c4, from its revision label), else its short id.
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
    docker images --no-trunc --format '{{.Repository}}:{{.Tag}} {{.ID}}' "$RELEASE_IMAGE" | while read -r ref id; do
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
    # Not "docker logs | grep -q": grep stops at the first match, docker logs
    # then fails writing, and pipefail turns a healthy core into a failure.
    grep -q "$READY_PATTERN" < <(docker logs --since "$START" "$core" 2>&1) || return 1
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

# Restart Ember on ember:current and wait for it. Through ember.service when
# it runs, so the installed startup.sh and compose.yaml are what's tested.
restart_ember() {
    START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    if systemctl is-active --quiet ember.service; then
        # Stops the containers with SIGINT (the core saves its session first).
        systemctl restart ember.service
    else
        compose up -d --no-build --pull never
    fi
    wait_healthy
}

# ── Host files ─────────────────────────────────────────────────────────
# path in the image : mode, installed flat into $EMBER_HOME; units into $UNIT_DIR.
HOST_FILES="deploy/host/startup.sh:755 deploy/provision/ember-update.sh:755 deploy/provision/ember-agent.py:755
deploy/host/baymax_portal.py:644 device_config.py:644 deploy/docker/compose.yaml:644"
UNIT_FILES="ember.service ember-update.service ember-update.timer ember-agent.service"

# Copies the installed host files (and REVISION) into DIR.
save_host_files() {
    local dir=$1 entry name
    rm -rf "$dir" && mkdir -p "$dir/units"
    for entry in $HOST_FILES REVISION:644; do
        name=$(basename "${entry%:*}")
        [ -f "$EMBER_HOME/$name" ] && cp -p "$EMBER_HOME/$name" "$dir/"
    done
    for name in $UNIT_FILES; do
        [ -f "$UNIT_DIR/$name" ] && cp -p "$UNIT_DIR/$name" "$dir/units/"
    done
    return 0
}

# Installs host files from DIR (the layout save_host_files writes). Replaces
# by rename: startup.sh and this script are read by running bash.
put_host_files() {
    local dir=$1 entry name mode units=""
    for entry in $HOST_FILES REVISION:644; do
        name=$(basename "${entry%:*}") mode=${entry#*:}
        [ -f "$dir/$name" ] || continue
        cmp -s "$dir/$name" "$EMBER_HOME/$name" && continue
        install -m "$mode" "$dir/$name" "$EMBER_HOME/.$name.new" && mv -f "$EMBER_HOME/.$name.new" "$EMBER_HOME/$name"
    done
    for name in $UNIT_FILES; do
        [ -f "$dir/units/$name" ] || continue
        cmp -s "$dir/units/$name" "$UNIT_DIR/$name" && continue
        install -m 644 "$dir/units/$name" "$UNIT_DIR/$name"
        units=1
    done
    if [ -n "$units" ]; then
        systemctl daemon-reload
        systemctl enable --quiet ember-agent.service 2>/dev/null || true
    fi
    return 0
}

# Extracts and checks the host files in ember:current into DIR (layout as
# above). Returns 1 if the image has none, or they fail a check: then the
# installed ones stay.
extract_host_files() {
    local dir=$1 tmp c entry src name rev ok=0
    tmp=$(mktemp -d)
    c=$(docker create ember:current) || { rm -rf "$tmp"; return 1; }
    docker cp "$c:/app/deploy" "$tmp/deploy" 2>/dev/null && docker cp "$c:/app/device_config.py" "$tmp/" 2>/dev/null || ok=1
    docker rm -f "$c" >/dev/null
    if [ $ok != 0 ]; then
        log "this release carries no host files; keeping the installed ones"
        rm -rf "$tmp"; return 1
    fi
    rm -rf "$dir" && mkdir -p "$dir/units"
    for entry in $HOST_FILES; do
        src=$tmp/${entry%:*}
        if [ ! -f "$src" ]; then log "host files incomplete (${entry%:*} missing); keeping the installed ones"; ok=1; break; fi
        case $src in
            *.sh) bash -n "$src" ;;
            *.py) python3 -c 'import ast, sys; ast.parse(open(sys.argv[1]).read())' "$src" ;;
            *) true ;;
        esac || { log "host file ${entry%:*} is broken; keeping the installed ones"; ok=1; break; }
        cp "$src" "$dir/"
    done
    for name in $UNIT_FILES; do
        [ $ok = 0 ] || break
        if [ ! -f "$tmp/deploy/provision/$name" ]; then log "host files incomplete ($name missing); keeping the installed ones"; ok=1; break; fi
        cp "$tmp/deploy/provision/$name" "$dir/units/"
    done
    rm -rf "$tmp"
    # The new agent must at least start and build a report, with its own
    # device_config.py, before it replaces a working one.
    if [ $ok = 0 ] && ! EMBER_HOME=$dir EMBER_AGENT_STATE=$dir/selftest \
            timeout 60 python3 "$dir/ember-agent.py" --self-test >/dev/null 2>&1; then
        log "the new fleet agent fails its self-test; keeping the installed host files"
        ok=1
    fi
    rm -rf "$dir/selftest" "$dir/__pycache__"
    if [ $ok != 0 ]; then rm -rf "$dir"; return 1; fi
    rev=$(revision_of ember:current)
    [ -n "$rev" ] && echo "${rev:0:7}" > "$dir/REVISION"
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
    # Swap images and host files, so "Update now" can go forward again.
    save_host_files "$EMBER_HOME/.host-swap"
    [ -d "$HOST_PREVIOUS" ] && put_host_files "$HOST_PREVIOUS"
    rm -rf "$HOST_PREVIOUS" && mv "$EMBER_HOME/.host-swap" "$HOST_PREVIOUS"
    docker tag ember:previous ember:current
    docker tag "$CUR" ember:previous
    if restart_ember; then
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
    cloud=$(sed -n 's/^EMBER_CLOUD_URL=//p' "$FLEET_ENV" 2>/dev/null | tail -n1 | tr -d "\"'")
    curl -s -o /dev/null --max-time 10 "${cloud:-https://tadashirobotics.com}/" && echo "$fallback"   # any HTTP answer
}
REF=$(pick_ref)
if [ -z "$REF" ]; then
    log "no release chosen for this unit (console unreachable or nothing on channel $CHANNEL); keeping the current version"
    exit 0
fi
TAG=${REF##*:}
# Only our image, and only release tags (or the channel tags of the fallback).
if [ "${REF%:*}" != "$RELEASE_IMAGE" ] || ! [[ $TAG =~ ^(sha-[0-9a-f]{7,40}|stable|beta|dev|main)$ ]]; then
    log "refusing $REF: only $RELEASE_IMAGE release tags are installed"
    exit 1
fi

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

# The release's host files go in first, so the health check covers them.
save_host_files "$EMBER_HOME/.host-before"
if extract_host_files "$EMBER_HOME/.host-new"; then
    put_host_files "$EMBER_HOME/.host-new"
    log "installed this release's host files"
fi
rm -rf "$EMBER_HOME/.host-new"

if restart_ember; then
    rm -rf "$HOST_PREVIOUS" && mv "$EMBER_HOME/.host-before" "$HOST_PREVIOUS"
    prune_images
    report updated "$TAG" "Updated from $FROM"
    log "update ok: running $TAG"
    exit 0
fi

log "new version not healthy after ${HEALTH_TIMEOUT}s"
echo "$NEW" > "$FAILED_FILE"
compose logs --tail 40 core
# Back to the host files from before this attempt.
put_host_files "$EMBER_HOME/.host-before"
rm -rf "$EMBER_HOME/.host-before"
if [ -z "$CUR" ]; then
    report failed "$TAG" "$TAG didn't start within ${HEALTH_TIMEOUT}s and there was no previous version to go back to"
    log "$TAG not healthy and no previous image to roll back to"
    exit 1
fi
docker tag ember:previous ember:current
# Keep the rollback target as it was before this attempt.
[ -n "$OLD_PREV" ] && docker tag "$OLD_PREV" ember:previous
if restart_ember; then
    prune_images
    report rolled_back "$TAG" "$TAG didn't start within ${HEALTH_TIMEOUT}s; back on $FROM"
    log "$TAG not healthy; rolled back to $FROM"
else
    report rolled_back "$TAG" "$TAG didn't start within ${HEALTH_TIMEOUT}s; went back to $FROM, but that isn't healthy either"
    log "$TAG not healthy; rolled back to $FROM, but it is not healthy either"
fi
exit 1
