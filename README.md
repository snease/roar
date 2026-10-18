![ROAR Logo](images/png/ROAR_LOGO.png)

# ROAR

The Robust and Optimal Analog Reuse (ROAR) flow/tool is developed to enable a GUI based approach to the C/ID and gm/ID (I like to say Inverse ID) analog circuit design methodologies. This software enables the the ability to design, optimize, and generate process/technology agnostic design scripts in a graphical yet automated fashion.

## Requirements

- **Python 3.10** or newer
- **Bash** for the setup script
- A Linux desktop environment with Qt/OpenGL system libraries

**Compatibility requirement:** ROAR must remain compatible with Python 3.11.
Preserve that support when changing code, dependencies or installation scripts.
The separate ALIGN environment currently uses a tested Python 3.10 setup;
ROAR and ALIGN do not need to use the same Python version.

## Python API

See the [Python API guide](docs/PYTHON_API.md) for headless design editing,
evaluation/exports, and live GUI sessions. In an activated Python environment,
install the headless API with `python3 -m pip install -e .`. For the desktop,
use the setup command below, which also checks native prerequisites.

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

Setup creates the checkout's `.venv`, installs ROAR and its GUI dependencies,
and checks that the GUI can start. If a virtual environment is already active,
setup uses that environment instead. A failed check stops setup and reports
what needs attention.

On Ubuntu 22.04, setup offers to install the required native packages with
`apt` after explicit confirmation in the terminal. On other distributions,
install the corresponding Qt/OpenGL system packages separately. Run setup
from a desktop session so it can check display access.

The setup flow has not yet been verified end to end on a fresh OS installation.

## Run

```sh
./bin/roar
```

The launcher uses the active virtual environment, or the checkout's `.venv`
when none is active. Activation, sourcing `roar_env.csh`, and tcsh are not
required. The older `setup_roar.csh` and `run_roar.csh` entry points remain
compatibility wrappers.

To choose Python 3.11 when creating a new environment, run
`./bin/setup_roar.sh --python python3.11`. For a CAD-managed runtime whose
native libraries are provided separately, use
`./bin/setup_roar.sh --skip-system-packages`; GUI verification still runs.

See [Installation and Usage](docs/INSTALLATION_AND_USAGE.md) for setup options
and troubleshooting.

## Disclaimer

Some parts of this codebase were generated with assistance from an LLM. All generated code and resulting behavior are reviewed, verified, and tested by a human before release.

