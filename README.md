![ROAR Logo](images/png/ROAR_LOGO.png)

# ROAR

The Robust and Optimal Analog Reuse (ROAR) flow/tool is developed to enable a GUI based approach to the C/ID and gm/ID (I like to say Inverse ID) analog circuit design methodologies. This software enables the the ability to design, optimize, and generate process/technology agnostic design scripts in a graphical yet automated fashion.

## Requirements

- Linux x86-64, targeting glibc 2.28 or newer and kernel 4.18 or newer
- Bash, `curl` or `wget`, `tar`, `gzip` and `sha256sum`; network access during setup
- A working desktop display and graphics driver, including WSLg when using WSL2

The managed runtime has been exercised on Ubuntu 22.04 under WSLg. Other Linux
distributions meeting these requirements are targets, not yet validated platforms.

**Compatibility requirement:** ROAR must remain compatible with Python 3.11.
Preserve that support when changing code, dependencies or installation scripts.
Setup supplies Python 3.11 for ROAR and a separate Python 3.11 environment for
ALIGN. You do not need to install Python or choose interpreter paths yourself.

## Python API

See the [Python API guide](docs/PYTHON_API.md) for headless design editing,
evaluation/exports, and live GUI sessions. In an activated Python environment,
install the headless API with `python3 -m pip install -e .`. For the desktop,
use the setup command below, which also supplies native runtime libraries.

The header's layout button opens the [graphical floorplanner](docs/FLOORPLANNING.md)
for sketching placement intent and inspecting generated ALIGN placements side by side.
It also exports sized SPICE and native placement constraints. A runnable
[OTA example](examples/floorplan/ota/README.md) includes the netlist, sizing snapshot,
and saved floorplan project.

## Install

```sh
git clone https://github.com/alecadair/roar.git
cd roar
./bin/setup_roar.sh
```

Setup downloads the pinned Pixi 0.75.0 executable, verifies its checksum, and
installs the versions recorded in `pixi.lock`. It prepares ROAR's GUI and native
libraries, a separate ALIGN environment, and the SKY130 ALIGN backend. ALIGN
paths are configured automatically. The runtime lives under the checkout's
`.pixi` directory; setup needs no `sudo`, shell activation or Conda knowledge.

Run setup from your desktop terminal so its final checks can verify display
access. A failed check stops with repair guidance. The host still supplies the
display server and graphics driver; this is not a complete operating-system image.

## Run

```sh
./bin/roar
```

The launcher uses this checkout's managed environment and runs offline. It
does not install packages or use an unrelated active virtual environment.
After updating the checkout, rerun setup before launching. The older
`setup_roar.csh` and `run_roar.csh` commands remain compatibility wrappers.

Pixi lets ROAR keep Python and native dependencies in one reviewed lock file.
A plain virtual environment leaves the native libraries to each workstation;
the managed setup removes that extra step. See
[Installation and Usage](docs/INSTALLATION_AND_USAGE.md) for troubleshooting,
runtime maintenance and the expert manual-installation fallback.

## Disclaimer

Some parts of this codebase were generated with assistance from an LLM. All generated code and resulting behavior are reviewed, verified, and tested by a human before release.

