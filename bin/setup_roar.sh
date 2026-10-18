#!/usr/bin/env bash
# Install the locked ROAR and ALIGN environments without changing the host OS.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: ./bin/setup_roar.sh

Install this checkout's pinned Pixi and locked environments: Python 3.11 for
ROAR and Python 3.10 for ALIGN. Then configure and check the managed runtime.
Requires Linux x86_64, a desktop display, curl or wget, tar, gzip, and sha256sum.
Downloads are stored in the checkout and Pixi's package cache. No sudo, system
Python, shell activation, or shell profile changes are needed.

  -h, --help  Show this help.

Launch after setup with ./bin/roar. Normal launch works offline and does not
install packages. Run setup again after changes to the committed lock file.
EOF
}

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

while (($#)); do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --python|--python=*) fail '--python is obsolete: Pixi manages Python 3.11 for ROAR and Python 3.10 for ALIGN. Run setup without this option.' ;;
        --skip-system-packages) fail '--skip-system-packages is obsolete: setup no longer installs system packages. Run setup without this option.' ;;
        *) fail "Unknown option: $1. Run ./bin/setup_roar.sh --help." ;;
    esac
done

[[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] ||
    fail 'The managed runtime currently supports Linux x86_64, including WSL2. See the installation guide for other platforms.'

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
manifest=$repo_dir/pyproject.toml
[[ -f "$manifest" && -f "$repo_dir/pixi.lock" ]] ||
    fail 'This checkout is missing pyproject.toml or pixi.lock. Restore the committed files before running setup.'

pixi_version=0.75.0
pixi_archive_sha256=bcd825d62905c29b3c754b71f9cdc9d6f119454398f58330f111a7b6a0de0a3f
pixi_binary_sha256=4383aed18b2d5569cf34a19638daf954aa4415cc87ad3a9da9f34059cc4a004c
pixi_url=https://github.com/prefix-dev/pixi/releases/download/v${pixi_version}/pixi-x86_64-unknown-linux-musl.tar.gz
pixi_dir=$repo_dir/.pixi/bin
pixi_bin=$pixi_dir/pixi

command -v sha256sum >/dev/null 2>&1 || fail 'sha256sum is required to verify the pinned Pixi download.'
verify_sha256() {
    local actual
    actual=$(sha256sum < "$1") || return 1
    [[ ${actual%% *} == "$2" ]]
}

[[ ! -L "$repo_dir/.pixi" && ! -L "$pixi_dir" ]] ||
    fail 'The managed .pixi or .pixi/bin directory is a symlink. Move it aside or use a separate checkout before setup.'
if [[ -e "$pixi_bin" || -L "$pixi_bin" ]]; then
    [[ -f "$pixi_bin" && ! -L "$pixi_bin" && -x "$pixi_bin" ]] &&
        verify_sha256 "$pixi_bin" "$pixi_binary_sha256" ||
        fail "The existing $pixi_bin is not the pinned Pixi $pixi_version binary. Move it aside and rerun setup; it has not been replaced."
else
    command -v tar >/dev/null 2>&1 || fail 'tar is required to unpack the pinned Pixi download.'
    command -v gzip >/dev/null 2>&1 || fail 'gzip is required to unpack the pinned Pixi download.'
    if command -v curl >/dev/null 2>&1; then
        downloader=curl
    elif command -v wget >/dev/null 2>&1; then
        downloader=wget
    else
        fail 'Install curl or wget, then rerun setup to download the pinned Pixi binary.'
    fi
    mkdir -p -- "$pixi_dir"
    download_dir=$(mktemp -d "$pixi_dir/.download.XXXXXXXX")
    trap 'rm -rf -- "$download_dir"' EXIT
    archive=$download_dir/pixi.tar.gz
    printf 'Downloading Pixi %s for this checkout...\n' "$pixi_version"
    if [[ $downloader == curl ]]; then
        curl -q --fail --location --proto '=https' --proto-redir '=https' --tlsv1.2 \
            --retry 3 --output "$archive" "$pixi_url" ||
            fail 'Could not download Pixi. Check network access to github.com, then rerun setup.'
    else
        wget --no-config --https-only --tries=3 --output-document="$archive" "$pixi_url" ||
            fail 'Could not download Pixi. Check network access to github.com, then rerun setup.'
    fi
    verify_sha256 "$archive" "$pixi_archive_sha256" ||
        fail 'Pixi archive checksum mismatch. Nothing from this download was executed; rerun setup to retry.'
    # Stream only the named executable; never extract archive paths to disk.
    tar -xOzf "$archive" pixi > "$download_dir/pixi" || fail 'Could not unpack the pinned Pixi executable.'
    verify_sha256 "$download_dir/pixi" "$pixi_binary_sha256" ||
        fail 'Pixi executable checksum mismatch. The download was not installed or executed.'
    chmod 755 "$download_dir/pixi"
    mv -T -- "$download_dir/pixi" "$pixi_bin"
fi

# Each managed environment supplies its own interpreter and native libraries.
unset VIRTUAL_ENV PYTHONHOME PYTHONPATH LD_LIBRARY_PATH LD_PRELOAD
unset QT_PLUGIN_PATH QT_QPA_PLATFORM_PLUGIN_PATH
unset ROAR_ALIGN_PYTHON ROAR_ALIGN_PDK_FIXED ROAR_MANAGED_RUNTIME
export ROAR_HOME="$repo_dir" PYTHONNOUSERSITE=1
# A failed install or migration must not leave an earlier readiness receipt.
rm -f -- "$repo_dir/.pixi/roar-runtime.json"
# Old PyPI QScintilla can leave data under PyQt6/Qt6 after uninstall. PyQt
# mistakes that directory for a bundled Qt and cannot find the conda plugins.
# Let Pixi rebuild only the GUI environment; backend assets and runs are separate.
if [[ -d "$repo_dir/.pixi/envs/default/lib/python3.11/site-packages/PyQt6/Qt6" ]]; then
    printf '\nMigrating the GUI environment from bundled PyPI Qt to conda-forge Qt...\n'
    "$pixi_bin" clean --no-config --manifest-path "$manifest" --environment default ||
        fail 'Could not reset the old GUI environment for the Qt migration. Resolve the Pixi error above, then rerun setup.'
fi
for environment in default align; do
    printf '\nInstalling locked %s environment...\n' "$environment"
    "$pixi_bin" install --no-config --manifest-path "$manifest" --locked --environment "$environment" ||
        fail "Could not install the locked $environment environment. Resolve the Pixi error above, then rerun setup."
done

printf '\nConfiguring and checking the managed runtime...\n'
"$pixi_bin" run --no-config --manifest-path "$manifest" --environment default \
    --offline --locked --no-install --executable python "$repo_dir/bin/configure_runtime.py" ||
    fail 'Runtime setup did not complete. Follow the diagnostic above, then rerun setup.'
printf '\nSetup passed. Launch ROAR with ./bin/roar from this checkout.\n'
