#!/usr/bin/env bash
# Set up the checkout with the standard Python and Ubuntu package managers.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: ./bin/setup_roar.sh [--python python3.11] [--skip-system-packages]

Create .venv in this checkout, or reuse the active VIRTUAL_ENV. Install ROAR's
GUI dependencies and check desktop startup. No shell activation is required
when using the checkout's .venv with ./bin/roar.

  --python COMMAND        Choose Python 3.10+ for a new environment.
                          Existing environments are preserved, never replaced.
  --skip-system-packages  Leave native libraries to your CAD/system administrator.
                          The final GUI check still runs.
  -h, --help              Show this help.

Ubuntu 22.04: offers to install missing native libraries using apt and sudo.
Other distributions: install the native prerequisites in docs/FLOORPLANNING.md.
Run from a desktop terminal (WSL2 needs WSLg). ALIGN is optional and separate.
EOF
}

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

python_choice=""
skip_system=0
while (($#)); do
    case "$1" in
        --python)
            (($# >= 2)) && [[ -n "$2" ]] || fail '--python needs an interpreter name or path.'
            python_choice=$2
            shift 2 ;;
        --skip-system-packages) skip_system=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) fail "Unknown option: $1. Run ./bin/setup_roar.sh --help." ;;
    esac
done

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
venv_dir=${VIRTUAL_ENV:-$repo_dir/.venv}
venv_python=$venv_dir/bin/python3
new_environment=0
if [[ -e "$venv_dir" || -L "$venv_dir" ]]; then
    [[ -f "$venv_dir/pyvenv.cfg" && -x "$venv_python" ]] ||
        fail "$venv_dir is not a usable virtual environment. Select a working environment or move this directory aside, then retry."
    python_cmd=$venv_python
    if [[ -n "$python_choice" ]]; then
        fail "An environment already exists at $venv_dir. Omit --python to reuse it; create a separate environment to change Python versions."
    fi
else
    [[ -z "${VIRTUAL_ENV:-}" ]] || fail "The active VIRTUAL_ENV does not exist: $venv_dir. Deactivate it and retry."
    python_cmd=${python_choice:-python3}
    command -v "$python_cmd" >/dev/null 2>&1 || fail "Cannot find $python_cmd. Install Python 3.10 or 3.11, or use --python /path/to/python3."
    new_environment=1
fi

"$python_cmd" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' ||
    fail 'ROAR requires Python 3.10 or newer. Choose it with --python, or activate a compatible environment.'
python_version=$("$python_cmd" -c 'import sys; print("{}.{}".format(*sys.version_info[:2]))')
printf 'Using Python %s; environment: %s\n' "$python_version" "$venv_dir"

# Use the same package list as the startup diagnostic, without importing Qt.
native_libraries=$("$python_cmd" -c 'import runpy, sys; print(runpy.run_path(sys.argv[1])["_UBUNTU_LIBRARIES"])' "$repo_dir/src/floorplan/doctor.py")
if (( ! skip_system )); then
    distro=""
    distro_version=""
    if [[ -r /etc/os-release ]]; then
        # This is an OS-owned configuration file, not input from a project.
        distro=$(. /etc/os-release; printf '%s' "${ID:-}")
        distro_version=$(. /etc/os-release; printf '%s' "${VERSION_ID:-}")
    fi
    if [[ "$distro" == ubuntu && "$distro_version" == 22.04 ]]; then
        read -r -a packages <<< "$native_libraries"
        if (( new_environment )) && ! "$python_cmd" -c 'import ensurepip' >/dev/null 2>&1; then
            packages+=("python${python_version}-venv")
        fi
        missing=()
        for package in "${packages[@]}"; do
            if [[ $(dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true) != 'install ok installed' ]]; then
                missing+=("$package")
            fi
        done
        if ((${#missing[@]})); then
            printf '\nUbuntu desktop packages are missing. This is separate from the Python environment.\n'
            printf '  sudo apt-get update\n  sudo apt-get install'
            printf ' %s' "${missing[@]}"
            printf '\n'
            [[ -t 0 ]] || fail 'Run setup interactively to approve system packages, or have your administrator run the commands above, then retry.'
            read -r -p 'Install these system packages now? [y/N] ' answer
            case "$answer" in
                y|Y|yes|YES) ;;
                *) fail 'System package installation declined. Nothing was installed; rerun after the prerequisites are available.' ;;
            esac
            elevate=()
            if (( EUID != 0 )); then
                command -v sudo >/dev/null 2>&1 || fail 'sudo is unavailable. Ask your administrator to run the displayed apt-get commands.'
                elevate=(sudo)
            fi
            "${elevate[@]}" apt-get update || fail 'apt-get update failed. Check network access and repository configuration, then rerun setup.'
            "${elevate[@]}" apt-get install -y --no-install-recommends "${missing[@]}" ||
                fail 'System package installation failed. Resolve the apt error above, then rerun setup.'
        fi
    else
        printf 'Native packages are managed separately on this OS; see docs/FLOORPLANNING.md.\n'
    fi
fi

if (( new_environment )); then
    "$python_cmd" -m venv "$venv_dir" ||
        fail "Could not create $venv_dir. On Ubuntu, install python${python_version}-venv. If creation left an incomplete directory, move it aside before retrying."
fi
# Never run pip through sudo or against the system interpreter.
"$venv_python" -c 'import sys; raise SystemExit(sys.prefix == sys.base_prefix)' ||
    fail "The interpreter at $venv_python is not running in a virtual environment."
"$venv_python" -m pip install --upgrade pip setuptools wheel ||
    fail 'Could not update Python installation tools. Check the pip error above and your network/package index, then rerun setup.'
"$venv_python" -m pip install -e "$repo_dir[gui]" ||
    fail 'Could not install ROAR GUI dependencies. Check the pip error above, then rerun setup.'

printf '\nChecking GUI startup in the selected environment...\n'
"$venv_python" "$repo_dir/src/roar.py" --check-gui ||
    fail 'Desktop startup is not ready. Follow the FIX above, then rerun setup. ALIGN is not required for this check.'
printf '\nSetup check passed. Launch ROAR with: %s/bin/roar\n' "$repo_dir"
if [[ -n "${VIRTUAL_ENV:-}" ]]; then
    printf 'This uses your active environment. Activate %s again in new terminals.\n' "$venv_dir"
fi
