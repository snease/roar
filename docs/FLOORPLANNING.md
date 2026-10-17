# Floorplan constraints for ALIGN

The layout button in ROAR's header opens a floorplan editor for device sizes and
high-level placement intent. It exports sized SPICE and native ALIGN constraint
JSON. ALIGN supplies the placement engine; this first integration covers input
authoring and export. Running ALIGN and displaying its placement results are
planned as separate follow-up contributions.

The editor uses ROAR's existing Qt interface and netlist parser. Install ROAR
with `pip install -e '.[gui]'`, then launch it normally. ALIGN, Xschem, Magic and
a PDK are not required to author or export a project. The Python API works with
the headless installation, `pip install -e .`.

## Try the OTA or common-source example

1. Click the layout button, then **Load netlist…**. Choose
   [`current_mirror_ota.sp`](../design/floorplan/ota/current_mirror_ota.sp) or
   [`common_source.sp`](../design/floorplan/common_source/common_source.sp).
   The import dialog selects the only subcircuit automatically. Choose a circuit
   from the dropdown for files containing several. Units and width convention
   appear together in the same dialog; keep micrometres and width per finger for
   these examples. Their explicit SPICE suffixes take precedence over the unit
   selector.
2. Edit **W per finger (µm)**, **L (µm)** and **NF** on the Devices tab. Sizes
   are synthetic geometry examples, not optimized or simulation-qualified designs.
   NF must be an even integer of at least two for the SKY130 generator.
3. On the Constraints tab, use **Import constraints…** to load the sibling
   `.const.json`, or select an intent and enter device names. Separate names
   with commas and symmetry pairs or rows with semicolons.
4. Use **Validate / format JSON**, then **Save project…** to retain your work.
   The project includes the original netlist text, edited sizes and constraints;
   reopening does not depend on the original file still being present.
5. **Export ALIGN inputs…** asks for a parent folder and creates a fresh
   `<subcircuit>/` directory containing `<subcircuit>.sp` and
   `<subcircuit>.const.json`. Existing bundle directories are refused, so choose
   another parent folder for a new revision.

The OTA contains ten MOS devices. Its example constraints pair devices around a
shared vertical axis and align each pair's horizontal centers. The common-source
example contains one MOS device and a resistor, with the resistor above the MOS.
Its generic resistor is useful for input/export testing; physical layout requires
a compatible resistor primitive in the selected ALIGN PDK. The tested SKY130
backend does not currently provide that complete path.

The older [`design/cm_ota.spice`](../design/cm_ota.spice) is a schematic template
with unresolved sizes such as `W=w1_2` and `nf=nf1_2`. Import requires concrete
sizes; the separate `design/floorplan/ota` example supplies synthetic ones for
trying the editor.

The import unit selector applies only to numbers without a suffix: with
micrometres selected, `L=0.5` means 0.5 µm. Select metres for a netlist that writes
the same dimension as `L=0.5e-6`. `L=0.5u` means 0.5 µm with either selection.
The editing table always uses micrometres, and export converts to SI. The headless
`from_netlist` API retains its metre default; pass `dimension_unit="um"` for bare
micrometre inputs.

## Placement intent

The controls write the same native dictionaries shown in the JSON editor. Edit
or remove entries directly in that list. See ALIGN's
[constraint reference](https://align-analoglayout.github.io/ALIGN-public/notes/const.html)
for the underlying semantics.

| Editor intent | Native ALIGN constraint | Meaning |
| --- | --- | --- |
| Symmetry | `SymmetricBlocks` | All semicolon-separated pairs share one horizontal or vertical axis. An additional singleton can sit on that axis. |
| Relative order | `Order` | Left-to-right or top-to-bottom order; reverse the names for the opposite order. |
| Align | `Align` | Align centers or edges along the selected horizontal or vertical line. |
| Row | `Order` + `Align` | Order a horizontal row or vertical column and align its centers. |
| Rows / regions | `Floorplan` | Describe regions as rows, optionally ordered and symmetric. Add `Align` separately when row centers must coincide. |
| Packing | `CompactPlacement` | Request left, right or centered packing for the entire circuit. |

`PlaceCloser`, `PowerPorts` and `GroundPorts` are also accepted in the JSON editor.
Symmetric pairs must have matching models and parameters. Centered packing is a
placement preference, not a guarantee of exact centering in a fixed boundary.

Local validation checks names, supported fields and concrete dimensions. It does
not solve combined constraints, generate primitives, or prove physical
feasibility. These examples were also checked with ALIGN 0.9.8's parser,
constraint verification and native placement-constraint translation. A successful
export alone does not establish DRC, LVS, model-bin validity or generated geometry.

## Bring sizes over from ROAR

**Read current ROAR sizes** captures the current design. Choose an enabled source
row, enter explicit target MOS names, and choose the source units and width
convention before applying. For a grouped ROAR row such as `M1_2`, explicitly map
to `M1, M2`; names are never expanded automatically. Read again to refresh after
changing the ROAR design. Applying sizes changes only the floorplan project.

The defaults are metres for ROAR W/L and total width. When total width is selected,
the editor divides W by NF once. Its table always shows per-finger W and L in
micrometres. Export always writes SI dimensions and per-finger W. W, L and NF must
already be concrete scalar values; unresolved expressions are rejected.

## Python API

Run from the checkout root after installing ROAR:

```python
import json
from pathlib import Path
from roar_api.floorplan import FloorplanProject

example = Path("design/floorplan/ota/current_mirror_ota.sp")
project = FloorplanProject.from_netlist(example)
project.set_constraints(json.loads(example.with_suffix(".const.json").read_text()))
project.set_mos_parameters("M1", width_um=1.05, length_um=0.15, nf=4)
project.save("ota.floorplan.json")
netlist, constraints = project.export_align("floorplan-output")
```

Use `FloorplanProject.load(path)` to reopen a project. To import a resolved
`roar_api.Design`, call:

```python
project.import_roar_parameters(
    design, "M1_2", ["M1", "M2"],
    dimension_unit="m", width_convention="total",
)
```

## Supported input boundary

This version accepts a selected flat `.subckt` with four-terminal `M` or `X`
MOS devices and numeric two-terminal resistors. MOS parameters are concrete W,
L, NF and optional positive integer M. Select the top explicitly for a file with
multiple subcircuits. Xschem's commented subcircuit wrappers and continuation
lines are supported. Hierarchy, includes, parameter expressions, extra MOS
parameters and other selected device kinds are rejected with an explanation;
prepare a self-contained flat sizing netlist for this editor.

The JSON controls support the subset listed above. Unsupported constraint
dictionaries already present in project JSON can be saved and reopened for
inspection; validation and export refuse them. This keeps project data intact
while making the current compatibility boundary explicit.
The **Import constraints…** action validates immediately and accepts supported
types only.

## Checks

After installing the GUI extra, run:

```sh
QT_QPA_PLATFORM=offscreen python -m unittest discover -s src/floorplan/tests -v
```

The tests cover topology, unit conversion, explicit ROAR mappings, persistence,
export collisions, invalid input and Qt editing flows. GUI tests skip when PyQt6
is absent. The core package does not import Qt or ALIGN.

For an independent compatibility check, use a separately installed ALIGN and a
SKY130 ALIGN PDK directory containing `models.sp`:

```sh
/path/to/align-venv/bin/python /path/to/roar/src/floorplan/tests/check_align.py \
    --repo /path/to/roar --pdk /path/to/ALIGN-pdk-sky130/SKY130_PDK
```

This checks both exported examples and ordinary/symmetric xschem rows through
ALIGN's parser, constraint verification and native placement translation. It
prints a receipt and writes bundles and logs to a temporary directory. It does
not run physical placement or routing.
