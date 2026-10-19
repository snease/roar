#!/usr/bin/env sh
# Compatibility entry point for existing scripts; setup no longer needs tcsh.
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$SCRIPT_DIR/setup_roar.sh" "$@"
