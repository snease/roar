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

When upgrading a checkout that used the older PyPI Qt packages, setup rebuilds
the managed GUI environment once through Pixi to remove obsolete Qt plugin
paths. The separate ALIGN environment, backend assets, projects and desktop
preferences are preserved. If migration fails, resolve the reported error and
rerun the same setup command.

The managed launcher uses these paths by default. Existing saved external paths
do not silently replace them. **ALIGN settings** offers **Managed by ROAR** and
**Custom installation** choices; saved custom paths and your placement runs
folder remain available. Setup does not overwrite those local preferences.

Run this command from your desktop terminal so the GUI check can render a small
OpenGL test window. Opening a display connection alone does not verify graphics.
Setup installs into the checkout without changing system Python or
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

Ctrl+C in the launching terminal requests normal window closure, including any
unsaved-floorplan prompt and placement-process cleanup. Cancel keeps ROAR open.

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
WSL2, launch from the Ubuntu terminal with a working WSLg connection. Setup and
launch test an OpenGL widget through window composition and framebuffer readback.
If the default Wayland backend fails and `DISPLAY` is available, they test X11
(`xcb`); launch uses it only after that check succeeds. Explicit
`QT_QPA_PLATFORM` overrides are respected. To try X11 explicitly, run
`QT_QPA_PLATFORM=xcb ./bin/roar`. A missing display or graphics driver cannot be
supplied by a Python package manager.

A check reporting `offscreen` or `minimal` verified headless Qt initialization
only, without OpenGL rendering. Remove that `QT_QPA_PLATFORM` override and rerun
from the desktop terminal before treating visible startup as checked.

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
[Qt 6.11 platform dependencies](https://doc.qt.io/qt-6.11/linux-requirements.html).
Unlike the managed conda-forge runtime, the current PyPI Qt wheels require
glibc 2.34 or newer on Linux x86-64. With an installed Python 3.11 and its
`venv` support:

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

### Qt Runtime Selection

The managed GUI uses conda-forge's PyQt6 6.11.0, Qt 6.11.2 and QScintilla
2.14.1. This replaces the older bundled PyPI Qt libraries while retaining
Python 3.11, glibc 2.28 and kernel 4.18 as the managed runtime targets.

| Approach investigated | Result |
| --- | --- |
| **Conda-forge Qt 6.11.2 stack — selected** | A compatible Python 3.11 solve, SVG rendering, editor widget and real WSLg/OpenGL checks pass. No locally maintained Qt build is needed. |
| PyPI Qt 6.11.2 wheels | Suitable for expert manual installs, but require glibc 2.34 on Linux x86-64. |
| Qt 6.12 | Current published PyQt6 and QScintilla packages constrain Qt below 6.12. Revisit when compatible bindings are available; do not bypass the solver's constraints. |
| Custom patched Qt build | Adds a native build and security-maintenance burden; unnecessary for the identified SVG fixes. |

Pixi's inline `qt6-main` → `PyQt6-Qt6` mapping lets conda satisfy the GUI
extra's native Qt requirement, preventing a second Qt wheel from being
installed. The GUI extra also requires Qt >=6.11.2 for manual pip installs.
Keep the binding, editor and native library versions compatible when updating.
The conda packages expose more multimedia dependencies than the PyPI wheels;
the reviewed GUI artifacts total approximately 339 MiB of cold downloads,
about 90 MiB more than the previous lock. Users still run the same setup command.

### Security Review Scope

Qt 6.11.2 contains the fixes for the identified
[SVG recursion/use-after-free issues](https://www.qt.io/blog/security-advisory-uncontrolled-recursion-and-use-after-free-vulnerabilities-in-qt-svg-module-impact-qt)
(CVE-2025-10728 and CVE-2025-10729) and
[SVG marker issue](https://www.qt.io/blog/security-advisory-type-confusion-and-heap-buffer-overflow-vulnerability-in-qt-svg-marker-handling)
(CVE-2026-6210). The loaded native Qt version and library paths were checked,
alongside real SVG rendering and desktop tests; a clean Python-package audit
alone does not establish native-library security.

**Remaining upstream advisory:**
[CVE-2026-15037](https://www.qt.io/blog/security-advisory-cve-2026-15037-xml-injection)
affects QDom XML serialization before Qt 6.12. ROAR does not use QDom/QtXml
serialization; no affected application path was found in the October 2026
review. This is not a claim that Qt 6.11.2 is free of every advisory. Recheck
the [Qt vulnerability list](https://wiki.qt.io/List_of_known_vulnerabilities_in_Qt_products)
and move to a compatible patched stack when available. Any future QDom use
must account for the advisory's invalid-character policy.

The ALIGN environment uses Dash 3.4.0, Flask 3.1.3 and Werkzeug 3.1.9 after
the Python dependency review. Dash 4.4.1 requires Pydantic 2, which conflicts
with ALIGN 0.9.8's Pydantic 1 requirement; upgrade those together only after
native viewer validation.

### Runtime Manager

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
