#!/bin/bash
# Serve the map on the LAN/tailnet (file:// also works — everything is inline).
cd "$(dirname "$0")"; PORT=${1:-8765}
echo "http://$(ipconfig getifaddr en0 2>/dev/null || hostname):$PORT/  (ctrl-c to stop)"
python3 -m http.server "$PORT" --bind 0.0.0.0
