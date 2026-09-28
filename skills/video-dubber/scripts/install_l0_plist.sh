#!/bin/sh
# Helper script to dynamically install or render L0 launchd plist with current repo path.
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$SCRIPT_DIR/l0_resident_guard.sh" --install-plist "$@"
