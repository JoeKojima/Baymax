#!/bin/bash
CLICKABLE_DIR="$(cd "$(dirname "$0")" && pwd)"
bash "$CLICKABLE_DIR/../debian-prerequisites.sh"
RESULT=$?
read -r -p "Press Enter to close."
exit "$RESULT"
