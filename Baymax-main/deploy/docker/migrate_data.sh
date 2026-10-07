#!/bin/bash
# Copies a pre-container robot's runtime data from the app folder into
# /var/lib/ember, where the containers keep it. Originals are left in place as
# a backup, and files already in /var/lib/ember are never overwritten, so it is
# safe to run more than once.
#
#   bash deploy/docker/migrate_data.sh            # from Baymax-main
#
# /var/lib/ember must exist and belong to the user the containers run as.
set -euo pipefail

SRC=$(cd "$(dirname "$0")/../.." && pwd)
DEST=${BAYMAX_DATA_DEST:-/var/lib/ember}

[ -d "$DEST" ] && [ -w "$DEST" ] || { echo "$DEST must exist and be writable"; exit 1; }

for item in chroma_store day_utterance cloud_queue \
            users.json .flask_secret fall_log.json transcript_log.json \
            voice_analysis_results.json voice_alerts.json; do
    if [ -e "$SRC/$item" ]; then
        cp -a --no-preserve=ownership --update=none "$SRC/$item" "$DEST/"
        echo "copied   $item"
    else
        echo "absent   $item"
    fi
done

# Transcripts, recordings and password hashes: readable by the owner only.
chmod 700 "$DEST"
chmod -f 600 "$DEST/users.json" "$DEST/.flask_secret" || true
