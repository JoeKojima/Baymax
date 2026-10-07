#!/bin/bash
BAYMAX_HOME=/home/meowmax/Baymax           # this script, the portal and the boot logs
APP_DIR=$BAYMAX_HOME/Baymax/Baymax-main    # the app folder of the git checkout
exec >> "$BAYMAX_HOME/startup_boot.log" 2>&1
echo "startup.sh began at $(date)"

# Per-robot settings from /etc/ember/device.toml (see deploy/device.example.toml).
# The second argument is used if the app folder can't be read, so a robot
# still boots with a different branch checked out.
cfg() {
    /usr/bin/python3 "$APP_DIR/device_config.py" "$1" || echo "$2"
}
CORE_USER=$(cfg system.user meowmax)                 # runs the AI core; owns PipeWire
CORE_UID=$(id -u "$CORE_USER")
CORE_HOME=$(getent passwd "$CORE_USER" | cut -d: -f6)
AP_IFACE=$(cfg network.ap_iface wlo1)
HOTSPOT=$(cfg network.hotspot_connection Baymax_Hotspot)

# Audio levels applied once per boot. WirePlumber restores whatever level was
# last used (which can be 0% and muted), so set and unmute explicitly.
SPEAKER_VOLUME=$(cfg audio.speaker_volume 75%)
MIC_VOLUME=$(cfg audio.mic_volume 85%)
SPEAKER_SINK_MATCH=$(cfg audio.speaker_match "UACDemo Jieli")   # space-separated; same speaker the AI core pins

# PipeWire is the core user's service, so pactl has to run as that user.
pactl_user() {
    sudo -u "$CORE_USER" env XDG_RUNTIME_DIR="/run/user/$CORE_UID" pactl "$@"
}

set_audio_levels() {
    local sink="" i
    # The USB speaker can take a few seconds to appear after boot.
    for i in {1..15}; do
        sink=$(pactl_user list sinks short 2>/dev/null | awk -v m="$SPEAKER_SINK_MATCH" '
            BEGIN { n = split(tolower(m), want, " ") }
            { for (j = 1; j <= n; j++) if (index(tolower($2), want[j])) { print $2; exit } }')
        [ -n "$sink" ] && break
        sleep 2
    done
    if [ -n "$sink" ]; then
        pactl_user set-sink-mute "$sink" 0
        pactl_user set-sink-volume "$sink" "$SPEAKER_VOLUME"
        echo "Speaker $sink set to $SPEAKER_VOLUME at $(date)"
    else
        echo "WARNING: USB speaker ($SPEAKER_SINK_MATCH) not found; speaker volume not set"
    fi

    # The AI core records from PipeWire's default source, so that is the one to set.
    if pactl_user set-source-mute @DEFAULT_SOURCE@ 0 && pactl_user set-source-volume @DEFAULT_SOURCE@ "$MIC_VOLUME"; then
        echo "Microphone $(pactl_user get-default-source) set to $MIC_VOLUME at $(date)"
    else
        echo "WARNING: could not set microphone volume"
    fi
}

start_webapp() {
    cd "$APP_DIR" || exit
    source venv/bin/activate
    echo "Starting Baymax web app at $(date)" >> "$BAYMAX_HOME/gemini_boot.log"
    python3 baymax_app.py >> "$BAYMAX_HOME/webapp_boot.log" 2>&1 &
    WEBAPP_PID=$!
    echo "Web app started (PID $WEBAPP_PID)" >> "$BAYMAX_HOME/gemini_boot.log"
    # Wait for Flask to be ready
    for i in {1..15}; do
        if wget -q --spider --timeout=2 http://localhost:5000/ 2>/dev/null; then
            echo "Web app is ready" >> "$BAYMAX_HOME/gemini_boot.log"
            break
        fi
        sleep 1
    done
}

run_gemini_with_retry() {
    # Added the while loop so it ACTUALLY retries!
    while true; do
        # Ensure ChromaDB store is writable by all users (root creates it, the core user uses it)
        chmod -R 777 "$APP_DIR/chroma_store/" 2>/dev/null

        echo "Starting Gemini script at $(date)" >> "$BAYMAX_HOME/gemini_boot.log"

        # Run as the core user so PipeWire audio devices are accessible (PipeWire is a user service)
        sudo -u "$CORE_USER" env \
            HOME="$CORE_HOME" \
            XDG_RUNTIME_DIR="/run/user/$CORE_UID" \
            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$CORE_UID/bus" \
            APP_DIR="$APP_DIR" \
            bash -c '
            # Route OpenCV V4L2 calls through PipeWire so camera is shareable
            export LD_PRELOAD=/usr/lib/x86_64-linux-gnu/pipewire-0.3/v4l2/libpw-v4l2.so
            cd "$APP_DIR"
            source venv/bin/activate
            # v10 only exists on the fleet-management branch until it merges.
            # If another branch is checked out, run v8 instead of crash-looping.
            core=realtime_gemini_10.py
            if [ ! -f "$core" ]; then
                echo "realtime_gemini_10.py not found (branch: $(git branch --show-current 2>/dev/null)); falling back to realtime_gemini_8.py"
                core=realtime_gemini_8.py
            fi
            python3 "$core"
        ' >> "$BAYMAX_HOME/gemini_boot.log" 2>&1

        echo "Gemini script stopped/crashed with exit code $?. Restarting in 5 seconds..." >> "$BAYMAX_HOME/gemini_boot.log"
        sleep 5
    done
}

# Keep the captive portal up for the WHOLE session, not just while offline.
# It is the only way to hand Baymax new Wi-Fi credentials, so it has to be
# reachable on the always-on hotspot even when internet is working fine.
start_portal_supervised() {
    pkill -f baymax_portal.py
    fuser -k 80/tcp 2>/dev/null
    sleep 2
    (
        cd "$BAYMAX_HOME" || exit
        while true; do
            echo "Starting captive portal at $(date)" >> "$BAYMAX_HOME/portal_boot.log"
            /usr/bin/python3 baymax_portal.py >> "$BAYMAX_HOME/portal_boot.log" 2>&1
            echo "Portal exited ($?). Restarting in 5s..." >> "$BAYMAX_HOME/portal_boot.log"
            sleep 5
        done
    ) &
    echo "Captive portal supervisor started (always-on)"
}

# Give Debian time to initialize the Wi-Fi hardware
sleep 30

# Before the network checks, so levels are right even on the offline path.
set_audio_levels

# Wait for DNS to be ready
for i in {1..10}; do
    if host google.com > /dev/null 2>&1; then
        break
    fi
    sleep 2
done

# The hotspot is always-on via NetworkManager (connection.autoconnect=yes).
# Nudge it in case NM has not finished, then log radio-level proof either way.
if nmcli -t -f NAME connection show --active | grep -qx "$HOTSPOT"; then
    echo "$HOTSPOT already active at $(date)"
elif nmcli connection up "$HOTSPOT"; then
    echo "$HOTSPOT brought up at $(date)"
else
    echo "ERROR: nmcli failed to bring up $HOTSPOT (exit $?)"
    nmcli device status
fi
/usr/sbin/iw dev "$AP_IFACE" info 2>&1 | grep -E "ssid|type|channel"

# Portal must be reachable for the entire session, online or not.
start_portal_supervised

# Ping a reliable server to check for an existing internet connection
if wget -q --spider --timeout=10 https://www.google.com; then
    echo "Internet verified. Booting main robot processes..."
    start_webapp
    run_gemini_with_retry
else
    echo "No internet detected. Hotspot + portal are up; waiting for credentials..."

    while true; do
        sleep 10
        if wget -q --spider --timeout=10 https://www.google.com; then
            echo "Internet detected. Launching realtime gemini script (portal stays up)"

            # Hotspot is now always-on via NetworkManager autoconnect; do NOT take it down --
            # it is the only way in when Wi-Fi drops.  (was: nmcli connection down Baymax_Hotspot)
            sleep 2

            for i in {1..10}; do
                if host google.com > /dev/null 2>&1; then
                    break
                fi
                sleep 2
            done

            start_webapp
            run_gemini_with_retry
            break
        fi
        echo "Still no internet, portal still running. Clients on hotspot: $(/usr/sbin/iw dev "$AP_IFACE" station dump 2>/dev/null | grep -c Station)"
    done
fi
