#!/bin/bash
cd "$(dirname "$0")"
while true; do
    echo "[$(date)] Starting app.py..."
    python3 app.py
    echo "[$(date)] App crashed or stopped. Restarting in 3s..."
    sleep 3
done
