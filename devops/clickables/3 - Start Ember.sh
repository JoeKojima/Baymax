#!/bin/bash
CLICKABLE_DIR="$(cd "$(dirname "$0")" && pwd)"
EMBER_PYTHON="$CLICKABLE_DIR/../../Baymax-main/.venv/bin/python"
if [ ! -x "$EMBER_PYTHON" ]; then EMBER_PYTHON="$CLICKABLE_DIR/../../.venv/bin/python"; fi
if [ ! -x "$EMBER_PYTHON" ]; then
  echo "Install the environment using the README before opening Ember."
  read -r -p "Press Enter to close."
  exit 1
fi
exec "$EMBER_PYTHON" "$CLICKABLE_DIR/launch.py" start
