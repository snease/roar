#!/usr/bin/env sh
# Compatibility entry point: refresh the locked setup without deleting environments.
set -eu
build_appimage=0
for argument in "$@"; do
    case "$argument" in
        --appimage) build_appimage=1 ;;
        -h|--help)
            printf '%s\n' 'Usage: ./bin/rebuild_roar.csh [--appimage]' \
                'Refresh managed setup without deleting environments.' \
                '  --appimage  Also build the optional launcher; requires appimagetool.'
            exit 0 ;;
        *) printf 'Unknown option: %s. Use --help for usage.\n' "$argument" >&2; exit 1 ;;
    esac
done
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ "$build_appimage" = 0 ]; then
    exec "$script_dir/setup_roar.sh"
fi
"$script_dir/setup_roar.sh"
exec "$script_dir/build_appimage.sh"
