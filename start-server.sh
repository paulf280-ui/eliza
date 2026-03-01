#!/bin/bash
# start-server.sh — Launch the elizaOS server from the project root.
#
# Handles:
#   1) engine.io / socket.io debug-module patch for Bun CJS interop
#   2) .env loading
#   3) Passing through any CLI args (e.g. --character trader.json)

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Fix engine.io/socket.io debug issue if needed
if [ -f "$SCRIPT_DIR/patch-socketio-debug.sh" ]; then
  bash "$SCRIPT_DIR/patch-socketio-debug.sh"
fi

# Load .env
if [ -f "$SCRIPT_DIR/.env" ]; then
  set -a
  source "$SCRIPT_DIR/.env"
  set +a
fi

exec elizaos start "$@"
