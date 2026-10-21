#!/bin/bash
CLICKABLE_DIR="$(cd "$(dirname "$0")" && pwd)"
bash "$CLICKABLE_DIR/../bootstrap.sh" stop
RESULT=$?
if [ "$RESULT" -ne 0 ]; then read -r -p "Setup/launch failed. Read the error above. Press Enter to close."; fi
exit "$RESULT"
