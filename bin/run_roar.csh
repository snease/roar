#!/usr/bin/env sh
# Compatibility entry point; bin/roar selects the environment automatically.

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$SCRIPT_DIR/roar" "$@"

