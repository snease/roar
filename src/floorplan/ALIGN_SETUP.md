# Set up ALIGN

Sketching, editing, saving and exporting floorplans require no ALIGN installation.
The normal checkout setup also prepares ALIGN for physical placement:

```sh
./bin/setup_roar.sh
./bin/roar
```

Setup installs the locked Pixi environments, with Python 3.11 for ROAR and a
separate Python 3.10 environment for ALIGN. It supplies the matching SKY130
backend and configures the paths automatically. Normal use needs no Python-path
entry, shell activation, Conda knowledge or `sudo`.
The managed launcher uses these paths with **Managed by ROAR** selected in
**ALIGN settings**. Choose **Custom installation** deliberately to use other
paths. Existing custom paths and the placement runs folder are preserved;
saved interpreter paths alone do not override managed mode.

The Linux x86-64 target requires glibc 2.28 or newer and kernel 4.18 or newer.
The runtime has been exercised on Ubuntu 22.04 under WSLg; other distributions
have not yet been validated. The host still supplies the display and graphics
driver. ALIGN remains a separate process with its own dependencies, not a
security sandbox.

## Check or repair the managed installation

Run `./bin/roar --check-setup` from the checkout. It checks desktop startup and
the configured ALIGN toolchain without generating a placement. If the managed
runtime is missing or incomplete, rerun `./bin/setup_roar.sh`; do not install
packages manually into its `.pixi` environments.

Inside the floorplanner, **ALIGN settings → Test installation** reports versions,
checks backend files and native imports, and verifies write access to the runs
folder. It creates that folder if needed. Optional browser-viewer problems are
warnings and do not block native previews. The check does not install tools or
prove that a particular circuit can be placed.

Setup also supplies the optional ALIGN browser viewer's four static assets from
a pinned source revision, with recorded checksums. Rerun setup to repair managed
backend files or viewer assets. Custom installations are left unchanged.

If generation finds missing or invalid configuration, it opens settings with
the specific issue. **Save and generate** resumes using the confirmed settings;
**Cancel** preserves the draft and current sizes. **Setup help** opens this guide.
Only choose an external executable when you intend to use that installation.

## Toolchain versions

The profile uses Python 3.10, the official ALIGN 0.9.8 wheel and Dash 2.18.2,
with the vibeic SKY130 backend at
`427b3b94242fdcf8009e418f6bbe14286fc71334`. The wheel supplies the native placer;
its release and artifact identify the installed code. The ALIGN source revision
`8d3cc2e9aa5511315203eb5cbfd00d2c88fdbd18` is a compatibility reference and the
source of viewer assets, not a claim about the wheel's source commit.

## Expert manual installation

For a CAD-managed installation outside Pixi, prepare a separate ALIGN environment
and select it explicitly in **ALIGN settings**. Keep it separate from ROAR's GUI
environment. Installing dependencies such as gdspy through pip may require a C++
compiler and Python development headers. On Ubuntu 22.04, an administrator can
prepare those prerequisites with:

```sh
sudo apt update
sudo apt install python3.10 python3.10-venv python3.10-dev build-essential git
```

Then create the separate manual ALIGN environment:

```sh
python3.10 -m venv "$HOME/roar-align"
"$HOME/roar-align/bin/python" -m pip install --upgrade pip
"$HOME/roar-align/bin/python" -m pip install --only-binary=align-analoglayout \
  'align-analoglayout==0.9.8' 'dash==2.18.2'
git clone https://github.com/vibeic/ALIGN-pdk-sky130.git "$HOME/roar-align-sky130"
git -C "$HOME/roar-align-sky130" checkout 427b3b94242fdcf8009e418f6bbe14286fc71334
```

Native physical previews run without a browser or local web server. The optional
**Open in ALIGN browser…** viewer needs additional static assets: the 0.9.8 wheel
omits its styles and logo. Install those from the tested source revision if you
want that viewer (requires `curl`):

```sh
roar_align_assets=$("$HOME/roar-align/bin/python" -c 'import importlib.util; from pathlib import Path; print(Path(importlib.util.find_spec("align").origin).parent / "gui/assets")')
mkdir -p "$roar_align_assets"
for asset in 00_from_codepen.css 01_style.css 02_typography.css align.png; do
  curl -fL "https://raw.githubusercontent.com/ALIGN-analoglayout/ALIGN-public/8d3cc2e9aa5511315203eb5cbfd00d2c88fdbd18/align/gui/assets/$asset" -o "$roar_align_assets/$asset"
done
```

For this manual installation, select **SKY130A** in **ALIGN settings**, then:

- **ALIGN Python:** `roar-align/bin/python` in your home folder.
- **ALIGN backend folder:** `roar-align-sky130/SKY130_PDK` in your home folder.
- **Placement runs folder:** a writable folder for generated inputs, layouts and logs.

Click **Test installation** before generating placement. These paths are machine
preferences; the floorplan stores the technology profile rather than executable
paths. Environment variables `ROAR_ALIGN_PYTHON` and `ROAR_ALIGN_PDK_FIXED` can
provide defaults for a manually launched ROAR environment.

From an activated ROAR environment, `python3 -m roar_api --check-setup` also
checks the desktop's native libraries and display, then the configured ALIGN
paths. It reports errors without opening ROAR or changing installations.
For manual installations, missing EGL/OpenGL or xcb libraries belong to the ROAR
desktop setup; installing ALIGN alone does not provide them. The normal managed
setup supplies those runtime libraries for the GUI.

## If setup fails

| Message | What to do |
| --- | --- |
| Managed runtime is missing or incomplete | Rerun `./bin/setup_roar.sh` from this checkout, then launch `./bin/roar`. |
| No module named `align` or `dash` | Repair the managed runtime with setup. For a manual installation, select its separate Python and install the packages there using the recipe above. |
| Native import fails / no matching wheel | Check Linux x86-64 and the reported runtime requirements. A manual installation requires Python 3.10 and its native libraries. Other builds from the [ALIGN installation guide](https://align-analoglayout.github.io/ALIGN-public/notes/installation.html) have not been validated here. |
| Backend does not match | Rerun managed setup, or use the exact vibeic revision above for a custom installation and select its `SKY130_PDK` subfolder. The official ALIGN SKY130 backend and open_pdks `sky130A` are different installations. |
| ALIGN viewer assets are missing | Native previews still work. Rerun setup for the managed viewer, or complete the manual static-assets step above for a custom installation. |
| Cannot write to placement runs folder | Choose an existing writable location or a new folder beneath one. |
| Placement ready, but the browser does not open | Copy the URL from the run window into your browser. Under WSL, automatic opening uses `wslview` or Windows PowerShell and your Windows default browser. |
| Placement fails after setup passes | Open **View last run / log…** on Placement, then **Copy diagnostics** to share the error with CAD support. Fix the named device/constraint and regenerate. |

The initial backend supports SKY130 1.8 V MOS placement. It does not run routing
or DRC/LVS. Existing physical previews reopen in ROAR without rerunning ALIGN;
**Open in ALIGN browser…** explicitly starts the optional browser viewer.
