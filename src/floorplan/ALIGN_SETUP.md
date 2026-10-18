# Set up ALIGN

Editing, saving and exporting floorplans require no ALIGN installation. For
placement, ask CAD support to prepare a separate environment once, then choose
its paths in **ALIGN settings**. Keep it separate from ROAR's Python environment.

## Tested configuration

Linux x86-64 (or Linux under WSL), Python 3.10, ALIGN 0.9.8 and Dash 2.18.2.
The official ALIGN wheel supplies the native placer. Installing dependencies
such as gdspy may still require a C++ compiler and Python development headers.
With Python 3.10 and its venv module installed:

```sh
python3.10 -m venv "$HOME/roar-align"
"$HOME/roar-align/bin/python" -m pip install --upgrade pip
"$HOME/roar-align/bin/python" -m pip install --only-binary=align-analoglayout \
  'align-analoglayout==0.9.8' 'dash==2.18.2'
git clone https://github.com/vibeic/ALIGN-pdk-sky130.git "$HOME/roar-align-sky130"
git -C "$HOME/roar-align-sky130" checkout 427b3b94242fdcf8009e418f6bbe14286fc71334
```

The 0.9.8 wheel omits the viewer's styles and logo. Install those static assets
from the tested source revision (requires `curl`):

```sh
roar_align_assets=$("$HOME/roar-align/bin/python" -c 'import importlib.util; from pathlib import Path; print(Path(importlib.util.find_spec("align").origin).parent / "gui/assets")')
mkdir -p "$roar_align_assets"
for asset in 00_from_codepen.css 01_style.css 02_typography.css align.png; do
  curl -fL "https://raw.githubusercontent.com/ALIGN-analoglayout/ALIGN-public/8d3cc2e9aa5511315203eb5cbfd00d2c88fdbd18/align/gui/assets/$asset" -o "$roar_align_assets/$asset"
done
```

In **ALIGN settings**, select **SKY130A**, then:

- **ALIGN Python:** `roar-align/bin/python` in your home folder.
- **ALIGN backend folder:** `roar-align-sky130/SKY130_PDK` in your home folder.
- **Placement runs folder:** a writable folder for generated inputs, layouts and logs.

Click **Test installation** before generating placement. It checks the selected
versions, native imports, viewer assets, backend files and write access to the runs folder;
it creates that folder if needed. It does not install tools or prove that a
particular circuit can be placed.

## If setup fails

| Message | What to do |
| --- | --- |
| No module named `align` or `dash` | Select the separate environment's Python; install the packages there using the command above. |
| Native import fails / no matching wheel | Check Python 3.10, Linux x86-64 and system libraries. For other builds, follow the [ALIGN installation guide](https://align-analoglayout.github.io/ALIGN-public/notes/installation.html); those builds have not been validated here. |
| Backend does not match | Use the exact vibeic revision above and select its `SKY130_PDK` subfolder. The official ALIGN SKY130 backend and open_pdks `sky130A` are different installations. |
| ALIGN viewer assets are missing | Complete the static-assets step above in the selected ALIGN environment. This restores the viewer's layout and logo. |
| Cannot write to placement runs folder | Choose an existing writable location or a new folder beneath one. |
| Placement ready, but the browser does not open | Copy the URL from the run window into your browser. Under WSL, automatic opening uses `wslview` or Windows PowerShell and your Windows default browser. |
| Placement fails after setup passes | Open **View last run / log…** on Placement, then **Copy diagnostics** to share the error with CAD support. Fix the named device/constraint and regenerate. |

The initial backend supports SKY130 1.8 V MOS placement. It does not run routing
or DRC/LVS. Existing results can be reopened with **Open last placement**.
