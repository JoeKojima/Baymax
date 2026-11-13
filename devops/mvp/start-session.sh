#!/bin/bash
set -eu
# Run in the logged-in user's desktop session so PipeWire and the browser work.
SESSION_VARIABLES=()
for NAME in DISPLAY WAYLAND_DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS; do
  if [ -n "${!NAME:-}" ]; then SESSION_VARIABLES+=("$NAME"); fi
done
if [ "${#SESSION_VARIABLES[@]}" -gt 0 ]; then
  systemctl --user import-environment "${SESSION_VARIABLES[@]}"
fi
PROJECT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
if [ "${1:-}" = "--monitor" ]; then
  systemctl --user start ember-monitor.service
  exec "$PROJECT/Baymax-main/.venv/bin/python" "$PROJECT/devops/mvp/monitor.py" --open
fi
systemctl --user start ember-mvp.service ember-monitor.service
exec "$PROJECT/Baymax-main/.venv/bin/python" "$PROJECT/devops/mvp/monitor.py" --auto-open
