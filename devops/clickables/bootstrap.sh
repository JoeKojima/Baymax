#!/bin/bash
set -eu
CLICKABLE_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$CLICKABLE_DIR/../.." && pwd)"
ACTION="${1:-setup}"
case "$ACTION" in setup|monitor|start|stop) ;; *) echo "Unknown Ember action"; exit 1;; esac
if [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" != "arm64" ]; then
  echo "This dependency set requires an Apple Silicon Mac running natively; Intel/Rosetta installs are not supported yet."
  exit 1
fi
UV_DIR="$PROJECT_DIR/.devtools"
UV="$UV_DIR/uv"
ENV_PYTHON="$PROJECT_DIR/Baymax-main/.venv/bin/python"
if [ "$ACTION" = "stop" ] && [ ! -x "$ENV_PYTHON" ]; then echo "Ember has not been installed here; nothing to stop."; exit 0; fi
if [ ! -x "$UV" ]; then
  echo "First-time setup: downloading uv from astral.sh. Please wait..."
  mkdir -p "$UV_DIR"
  curl --fail --location --silent --show-error https://astral.sh/uv/install.sh -o "$UV_DIR/uv-install.sh"
  UV_UNMANAGED_INSTALL="$UV_DIR" sh "$UV_DIR/uv-install.sh"
fi
PYTHON="$ENV_PYTHON"
if [ ! -x "$PYTHON" ] || ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3,12) else 1)'; then
  echo "Preparing private Python 3.12; your system Python is unchanged..."
  "$UV" python install 3.12
  PYTHON="$("$UV" python find --managed-python 3.12)"
fi
exec "$PYTHON" "$CLICKABLE_DIR/bootstrap.py" "$ACTION" --uv "$UV"
