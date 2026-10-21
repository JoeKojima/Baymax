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
exec systemctl --user start ember-mvp.service
