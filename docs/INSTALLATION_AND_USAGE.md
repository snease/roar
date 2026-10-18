![ROAR Logo](images/png/ROAR_LOGO.png)

# ROAR Installation Instructions

## Requirements

- Linux x86-64, targeting glibc 2.28 or newer and kernel 4.18 or newer
- Bash, `curl` or `wget`, `tar`, `gzip` and `sha256sum`; Git if cloning the repository
- Network access during setup and several GB of disk space for both environments and package caches
- A working desktop display and graphics driver; WSL2 users need WSLg

The managed runtime has been exercised on Ubuntu 22.04 under WSLg. Other
Linux distributions meeting these requirements have not yet been validated.
Offscreen checks alone do not establish that a visible desktop works.

You do not need a preinstalled Python, `sudo`, Conda shell commands or virtual
environment activation for the normal setup. Pixi supplies Python 3.11 for
ROAR and Python 3.10 for the separate ALIGN process. **Python 3.11 compatibility
remains a requirement** for ROAR code and dependency changes.

## Installation Steps

### 1. Download ROAR

```sh
git clone https://github.com/alecadair/roar.git
cd roar
```

If you received an archive, extract it and change into its top-level `roar`
directory instead.

### 2. Set Up ROAR

```sh
./bin/setup_roar.sh
```

Setup downloads Pixi 0.75.0 into `.pixi/bin`, verifies the archive against the
checksum recorded in ROAR, and installs the committed `pixi.lock` environments.
It prepares the GUI, native libraries, separate ALIGN installation and matching
SKY130 backend, then checks the configuration. ALIGN's Python and backend paths
are configured automatically; ordinary use needs no path entry in the GUI.
A failed step stops setup and reports the problem.

The managed launcher uses these paths by default. Existing saved external paths
do not silently replace them. **ALIGN settings** offers **Managed by ROAR** and
**Custom installation** choices; saved custom paths and your placement runs
folder remain available. Setup does not overwrite those local preferences.

Run this command from your desktop terminal so the GUI check can test display
access. Setup installs into the checkout without changing system Python or
installing OS packages. The host still supplies its display server and graphics
driver. Shell startup files do not need modification.

### 3. Run ROAR

```sh
./bin/roar
```

The launcher uses the checkout's managed runtime, including when an unrelated
virtual environment is active. It runs offline without installing packages or
changing the dependency lock. After updating or moving the checkout, rerun
setup before launching. Do not copy a managed environment between locations.

The historical `./bin/setup_roar.csh` and `./bin/run_roar.csh` commands remain
compatibility wrappers. The old setup options `--python` and
`--skip-system-packages` no longer apply: Python versions are managed and setup
has no system-package installation step.

## Troubleshooting

### Setup Download or Runtime Check Fails

Read the reported error, resolve it, and rerun `./bin/setup_roar.sh`. Check
network access and available disk space for download failures. A checksum
mismatch stops setup; do not bypass verification or replace the expected hash
with one taken from the failed download. Use a reviewed checkout and the
pinned release, or ask CAD support to investigate.

If launch reports a missing or outdated runtime, rerun setup from this checkout.
Installing packages into system Python or an unrelated `.venv` will not repair
the managed runtime. Avoid manual `pip install` inside `.pixi` environments;
maintainers update the dependency declaration and lock together.

### GUI or Display Check Fails

To rerun only the GUI check:

```sh
./bin/roar --check-gui
```

Confirm that the terminal belongs to your graphical desktop session. Under
WSL2, launch from the Ubuntu terminal with a working WSLg connection. If the
Wayland/OpenGL path fails, try `QT_QPA_PLATFORM=xcb ./bin/roar`. A missing display
or graphics driver cannot be supplied by a Python package manager.

A check reporting `offscreen` or `minimal` verified headless Qt initialization
only. Remove that `QT_QPA_PLATFORM` override and rerun from the desktop terminal
before treating visible startup as checked.

### ALIGN Preview Is Unavailable

```sh
./bin/roar --check-setup
```

This also checks the configured ALIGN environment and backend. Rerun setup to
repair the managed installation. **ALIGN settings → Test installation** provides
the same tool-specific checks inside the floorplanner. If a custom installation
is selected, repair those paths or choose **Managed by ROAR** again in settings.

When generation encounters missing installation settings, it opens the settings
dialog directly. **Save and generate** resumes the request; **Cancel** preserves
the draft. Sketching, editing and exporting remain available without ALIGN.
See the [floorplanning guide](FLOORPLANNING.md) for placement and recovery details.

### Permission Denied

If an extracted archive has lost the scripts' executable permissions:

```sh
chmod +x bin/setup_roar.sh bin/roar
```

The checkout must be writable for setup. No administrator privileges are needed
for the managed runtime.

## Expert Manual Installation

A CAD-managed Python environment remains supported. This bypasses the managed
launcher and requires your administrator to supply Qt/OpenGL/X11 libraries and
a compatible desktop connection. See the
[Qt 6.8 platform dependencies](https://doc.qt.io/qt-6.8/linux-requirements.html).
With an installed Python 3.11 and its `venv` support:

```sh
python3.11 -m venv "$HOME/roar-gui"
"$HOME/roar-gui/bin/python" -m pip install --upgrade pip setuptools wheel
"$HOME/roar-gui/bin/python" -m pip install -e '.[gui]'
"$HOME/roar-gui/bin/python" -m roar_api --check-gui
"$HOME/roar-gui/bin/python" -m roar_api
```

For a headless API installation, omit `[gui]`. Manual floorplan generation also
needs a separately installed ALIGN toolchain and explicit settings; see
[Set up ALIGN](../src/floorplan/ALIGN_SETUP.md#expert-manual-installation).
Do not install ALIGN into the ROAR GUI environment.

## Runtime Maintenance

The existing `pyproject.toml` is the source of Python package requirements.
Pixi adds the Python interpreters and native dependencies, and `pixi.lock`
records the resolved artifacts. Keep the lock in version control and review
its changes with dependency updates. Users rerun setup to install that reviewed
runtime; launch never resolves a new one.

Maintainers should update the pinned Pixi version/checksum deliberately and
verify Python 3.11 compatibility, desktop startup, floorplan tests and a real
ALIGN preview after runtime changes. Bundled native libraries need dependency
updates even when the workstation's OS packages are current. Package locks and
checksums make changes reviewable; they do not establish that third-party code
is safe. Local editable ROAR code and backend code remain trusted inputs.

Pixi was chosen because this desktop needs Python packages and native graphics
and placement libraries together. `venv` or `uv` would still leave the native
installation to each workstation. Docker would add display and filesystem
integration to normal desktop use; Nix would introduce a broader packaging
workflow for users and maintainers. Those tools can suit other deployments,
but the checkout setup here uses one runtime manager and retains a manual
Python fallback. See Pixi's [Python project support](https://pixi.prefix.dev/latest/python/pyproject_toml/)
and [lock-file documentation](https://pixi.prefix.dev/latest/workspace/lock_file/).

## Quick Start Summary

```sh
cd roar
./bin/setup_roar.sh && ./bin/roar
```

## Usage

### Operating Modes

ROAR operates in two primary modes:

- **Design Eqs Mode**: Work with design equations and circuit parameters
- **Device Params Mode**: Configure and adjust device-specific parameters

Toggle between modes using the interface controls or keyboard shortcuts.

### Hot Keys

#### Plot Controls

| Key | Action |
|-----|--------|
| `x` | Toggle X-axis log scale |
| `y` | Toggle Y-axis log scale |
| `l` | Toggle log scale (both axes) |
| `v` | Add/toggle vertical marker |
| `h` | Add/toggle horizontal marker |

#### General

| Key | Action |
|-----|--------|
| `Ctrl+S` | Save current state |
| `Ctrl+O` | Open/load saved state |
| `Ctrl+Z` | Undo last action |
| `Ctrl+Y` | Redo action |
| `Ctrl+Q` | Quit ROAR |

### Saving and Loading States

**To save your current session:**
- Use `Ctrl+S` or File → Save State
- States are saved to `$ROAR_HOME/states/` by default

**To load a previously saved state:**
- Use `Ctrl+O` or File → Load State
- Navigate to your saved `.roar` state file

### Tips

- Save states frequently when working on complex designs
- Use descriptive filenames for saved states (e.g., `amplifier_v2.roar`)
- Switch to Device Params mode to fine-tune component values after initial design


### Known Issues

- 3-D Plotting functionality is currently limited and buggy; use 2-D plots for best results.
