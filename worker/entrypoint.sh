#!/bin/sh

export PATH="/usr/local/bin:$PATH"

# Start the Blaxel sandbox API in the background.
/usr/local/bin/sandbox-api &

wait_for_port() {
    port=$1
    timeout=30
    count=0
    echo "Waiting for port $port..."
    while ! nc -z 127.0.0.1 "$port"; do
        sleep 1
        count=$((count + 1))
        if [ "$count" -gt "$timeout" ]; then
            echo "Timeout waiting for port $port"
            exit 1
        fi
    done
    echo "Port $port available"
}

wait_for_port 8080

# The orchestrator starts `ant beta:worker poll` via the sandbox process API
# once the sandbox is up, so we just keep sandbox-api alive here.
wait
