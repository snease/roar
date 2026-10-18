![ROAR Logo](images/png/ROAR_LOGO.png)

# ROAR Installation Instructions

## Requirements

- **Python 3.10** or newer, with virtual environment support
- **Bash** for the setup script
- A Linux desktop environment with Qt/OpenGL system libraries

**Compatibility requirement:** ROAR must remain compatible with Python 3.11.
The separate ALIGN environment can use a different Python version; its tested
setup currently uses Python 3.10.

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

Setup creates `.venv` in the checkout, or reuses the environment selected by
an active `VIRTUAL_ENV`. It installs ROAR and its GUI dependencies, then
verifies GUI startup. A failed verification stops setup and reports the
problem; resolve it and rerun the same command.

On Ubuntu 22.04, setup offers to install native prerequisites using `apt`
and asks for explicit confirmation in the terminal before proceeding.
Installing system packages may require `sudo`. On other distributions,
install the corresponding Qt/OpenGL system packages separately before
running setup. Run setup from your desktop session so the verification can
check display access.

The setup flow has not yet been verified end to end on a fresh OS installation.

### 3. Run ROAR

```sh
./bin/roar
```

Run the same command in later sessions. The launcher uses the active virtual
environment, or the checkout's `.venv` if none is active. Activation,
sourcing an environment file, and tcsh are not required. Launching does not
install packages.

The historical `./bin/setup_roar.csh` and `./bin/run_roar.csh` commands remain
compatibility wrappers for setup and launch.

## Setup Options

Choose an installed Python interpreter when creating a new environment:

```sh
./bin/setup_roar.sh --python python3.11
```

This option does not replace the interpreter of an existing environment.

For a CAD-managed runtime whose native libraries are supplied separately,
skip system package installation:

```sh
./bin/setup_roar.sh --skip-system-packages
```

Python dependency installation and GUI verification still run. Both options
can be combined when creating a new environment.

## Troubleshooting

### Missing Python Environment or Dependencies

Run `./bin/setup_roar.sh` again from the checkout. If an unrelated virtual
environment is active, leave it first so setup and launch use the checkout's
`.venv`. If a checkout has moved, recreate its virtual environment at the new
location before rerunning setup.

### GUI or Display Check Fails

Inspect the setup error and install any missing native libraries using your
distribution's package manager. Check that you are running in a graphical
desktop session with display access. To rerun only the GUI check:

```sh
./bin/roar --check-gui
```

ALIGN installation is separate from GUI setup. Use
`./bin/roar --check-setup` when you also want to check a configured ALIGN
installation, and see the [floorplanning guide](FLOORPLANNING.md) for details.

### Permission Denied

If an extracted archive has lost the scripts' executable permissions:

```sh
chmod +x bin/setup_roar.sh bin/roar
```

## Quick Start Summary

```sh
cd roar
./bin/setup_roar.sh
./bin/roar
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
