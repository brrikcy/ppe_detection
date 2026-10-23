#!/usr/bin/env bash
# Starts the local Moondream server, waits until its model is loaded, then the web app.
# Both listen on localhost only; from your laptop:  ssh -L 8000:localhost:8000 ubuntu@<instance-ip>
# then open http://localhost:8000. Ctrl+C stops both.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs

.venv/bin/python -m moondream_server.serve "$@" > logs/moondream.log 2>&1 &
MOONDREAM_PID=$!
trap 'kill $MOONDREAM_PID 2>/dev/null' EXIT

echo "Loading Moondream (first start downloads the weights; see logs/moondream.log)..."
until curl -sf http://127.0.0.1:2020/health > /dev/null; do
    kill -0 $MOONDREAM_PID 2>/dev/null || { echo "Moondream server stopped:"; tail -20 logs/moondream.log; exit 1; }
    sleep 2
done
echo "Moondream is ready."

.venv/bin/python run.py --no-browser
