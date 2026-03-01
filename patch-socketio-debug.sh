#!/bin/bash
# patch-socketio-debug.sh — Fix debug-module CJS interop in engine.io / socket.io for Bun.
#
# Bun's CJS interop can fail to resolve `(0, debug_1.default)(...)` in some
# contexts.  This script rewrites those calls to a safe fallback pattern:
#   (typeof debug_1.default === "function" ? debug_1.default : debug_1)(...)
#
# Safe to run multiple times (idempotent).

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

PATCHED=0
for pkg in engine.io socket.io socket.io-parser socket.io-adapter; do
  PKG_DIR="$SCRIPT_DIR/node_modules/$pkg"
  [ -d "$PKG_DIR" ] || continue

  while IFS= read -r -d '' file; do
    if grep -q '(0, debug_1.default)' "$file" 2>/dev/null; then
      sed -i 's/(0, debug_1\.default)/(typeof debug_1.default === "function" ? debug_1.default : debug_1)/g' "$file"
      PATCHED=$((PATCHED + 1))
    fi
  done < <(find "$PKG_DIR" -name '*.js' -print0 2>/dev/null)
done

if [ "$PATCHED" -gt 0 ]; then
  echo "[patch] Fixed debug-module interop in $PATCHED file(s)"
else
  echo "[patch] No debug-module patches needed"
fi
