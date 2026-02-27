#!/bin/bash
# Ensure meridian_bridge.py is running. Idempotent — safe to call repeatedly.
# Starts the bridge if not running and executor is reachable.

BRIDGE_CMD="python3 -m claudehopper.meridian_bridge"
BRIDGE_DIR="$HOME/dev/claudehopper"
BRIDGE_LOG="/tmp/bridge.log"
EXECUTOR_URL="${EXECUTOR_URL:-https://localhost:18111}"

# Already running?
if pgrep -f "claudehopper.meridian_bridge" > /dev/null 2>&1; then
    echo "Bridge already running (PID $(pgrep -f 'claudehopper.meridian_bridge'))"
    exit 0
fi

# Check executor is reachable
if ! curl -sk --max-time 3 "$EXECUTOR_URL/" > /dev/null 2>&1; then
    echo "Executor not reachable at $EXECUTOR_URL — bridge not started"
    exit 1
fi

# Start bridge
cd "$BRIDGE_DIR" || exit 1
nohup $BRIDGE_CMD > "$BRIDGE_LOG" 2>&1 &
BRIDGE_PID=$!
sleep 2

if kill -0 $BRIDGE_PID 2>/dev/null; then
    echo "Bridge started (PID $BRIDGE_PID, log: $BRIDGE_LOG)"
    exit 0
else
    echo "Bridge failed to start — check $BRIDGE_LOG"
    exit 1
fi
