#!/bin/bash
set -eu
# Run in the logged-in user's desktop session so PipeWire and the browser work.
systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS
exec systemctl --user start ember-mvp.service
