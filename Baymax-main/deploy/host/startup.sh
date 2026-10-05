#!/bin/bash
exec >> /home/meowmax/Baymax/startup_boot.log 2>&1
echo "startup.sh began at $(date)"

# Audio levels applied once per boot. WirePlumber restores whatever level was
# last used (which can be 0% and muted), so set and unmute explicitly.
SPEAKER_VOLUME=75%
MIC_VOLUME=85%
SPEAKER_SINK_MATCH="UACDemo"   # the USB speaker realtime_gemini_8.py pins as default sink

# PipeWire is meowmax's user service, so pactl has to run as meowmax.
pactl_user() {
    sudo -u meowmax env XDG_RUNTIME_DIR=/run/user/1000 pactl "$@"
}

set_audio_levels() {
    local sink="" i
    # The USB speaker can take a few seconds to appear after boot.
    for i in {1..15}; do
        sink=$(pactl_user list sinks short 2>/dev/null | awk -v m="$SPEAKER_SINK_MATCH" 'index($2, m) {print $2; exit}')
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
    cd /home/meowmax/Baymax/Baymax/Baymax-main/ || exit
    source venv/bin/activate
    echo "Starting Baymax web app at $(date)" >> /home/meowmax/Baymax/gemini_boot.log
    python3 baymax_app.py >> /home/meowmax/Baymax/webapp_boot.log 2>&1 &
    WEBAPP_PID=$!
    echo "Web app started (PID $WEBAPP_PID)" >> /home/meowmax/Baymax/gemini_boot.log
    # Wait for Flask to be ready
    for i in {1..15}; do
        if wget -q --spider --timeout=2 http://localhost:5000/ 2>/dev/null; then
            echo "Web app is ready" >> /home/meowmax/Baymax/gemini_boot.log
            break
        fi
        sleep 1
    done
}

run_gemini_with_retry() {
    # Added the while loop so it ACTUALLY retries!
    while true; do
        # Ensure ChromaDB store is writable by all users (root creates it, meowmax uses it)
        chmod -R 777 /home/meowmax/Baymax/Baymax/Baymax-main/chroma_store/ 2>/dev/null

        echo "Starting Gemini script at $(date)" >> /home/meowmax/Baymax/gemini_boot.log

        # Run as meowmax so PipeWire audio devices are accessible (PipeWire is a user service)
        sudo -u meowmax bash -c '
            export HOME=/home/meowmax
            export XDG_RUNTIME_DIR=/run/user/1000
            export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
            # Route OpenCV V4L2 calls through PipeWire so camera is shareable
            export LD_PRELOAD=/usr/lib/x86_64-linux-gnu/pipewire-0.3/v4l2/libpw-v4l2.so
            cd /home/meowmax/Baymax/Baymax/Baymax-main
            source venv/bin/activate
            python3 realtime_gemini_8.py
        ' >> /home/meowmax/Baymax/gemini_boot.log 2>&1

        echo "Gemini script stopped/crashed with exit code $?. Restarting in 5 seconds..." >> /home/meowmax/Baymax/gemini_boot.log
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
        cd /home/meowmax/Baymax/ || exit
        while true; do
            echo "Starting captive portal at $(date)" >> /home/meowmax/Baymax/portal_boot.log
            /usr/bin/python3 baymax_portal.py >> /home/meowmax/Baymax/portal_boot.log 2>&1
            echo "Portal exited ($?). Restarting in 5s..." >> /home/meowmax/Baymax/portal_boot.log
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
if nmcli -t -f NAME connection show --active | grep -qx "Baymax_Hotspot"; then
    echo "Baymax_Hotspot already active at $(date)"
elif nmcli connection up Baymax_Hotspot; then
    echo "Baymax_Hotspot brought up at $(date)"
else
    echo "ERROR: nmcli failed to bring up Baymax_Hotspot (exit $?)"
    nmcli device status
fi
/usr/sbin/iw dev wlo1 info 2>&1 | grep -E "ssid|type|channel"

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
        echo "Still no internet, portal still running. Clients on hotspot: $(/usr/sbin/iw dev wlo1 station dump 2>/dev/null | grep -c Station)"
    done
fi