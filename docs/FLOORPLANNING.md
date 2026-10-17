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

## A companion to the ROAR design

The editable layout data lives in a separate `.roar.floorplan` file.
It contains the circuit template, chosen subcircuit, size values and bindings,
placement constraints, and an optional link to a saved ROAR design. ROAR's
existing `.roar` format is unchanged. Floorplan actions never write to that
design file or replace the running ROAR workspace. Existing `.floorplan.json`
files still open.

The usual workflow is **New floorplan…** or **Open floorplan…**, edit the sizes
and constraints, **Save**, then **Export ALIGN inputs…** when ready for ALIGN.

| Action | What it reads or writes |
| --- | --- |
| New floorplan… | Reads a circuit netlist to start a new floorplan. A ROAR design can optionally supply sizes. |
| Open floorplan… | Restores a complete saved floorplan: circuit, sizes, source bindings and placement constraints. |
| Save / Save as… | Writes that complete editable document as `.roar.floorplan`. |
| Export ALIGN inputs… | Writes the sized circuit netlist and placement constraints together in ALIGN's required formats. |
| Advanced: Load ALIGN constraints… | Reads only placement rules from an existing ALIGN constraint file. Choose whether to add them or replace the current rules; the circuit, sizes and ROAR link stay unchanged. |

Constraints belong to the floorplan and are saved with it. A separate constraints
file is only needed to exchange rules with ALIGN or another tool. There is one
export action, which includes both the sized netlist and the constraints.

**Open floorplan…** shows both file paths in one dialog. Choosing a floorplan
fills in its linked ROAR design; you can browse for a different design. An
explicit design choice is retained when changing the floorplan, with the
different saved link shown for comparison. Missing links can be repaired with
Browse, or the floorplan can open using its saved sizes alone.

Links are saved relative to the companion file. Moving the two files together
preserves their association, and **Save as…** rebases the link to the same design.
The original netlist text is embedded, so it remains available even if its
original file moves. Version 1 floorplan files still open; new saves use version 2.

Opening a floorplan restores its saved size snapshot. Loading or relinking a
ROAR design supplies a source for subsequent assignments; it does not silently
refresh the saved sizes. **Refresh bindings from this source** explicitly updates
values from the selected source. Failed refreshes keep the previous sizes and clear the
unavailable source from the editor.

## Try a linked, parameterized OTA

1. Click the header's layout button, then **Open floorplan…** and choose
   [`cm_ota.roar.floorplan`](../design/floorplan/ota/cm_ota.roar.floorplan).
   Its linked [`cm_ota_sizes.roar`](../design/floorplan/ota/cm_ota_sizes.roar)
   appears automatically. Open the companion.
2. Inspect the **Parameters** tab. Each shared symbol lists its affected device
   fields, captured value and ROAR source. For example, `w1_2` is bound to
   `M1_2.W`. W is 2.2 µm total, NF is 2, and the Devices tab therefore shows
   1.1 µm per finger. These are synthetic demo sizes, not optimized designs.
3. Select a parameter to enter a manual value or bind a different resolved ROAR
   instance attribute. Manual assignment replaces that parameter's binding.
   Clearing its value makes it unresolved again. Shared symbols update all
   their device references together.
4. Use **Save** or **Save as…**, then **Export ALIGN inputs…**. Export creates a
   fresh circuit directory with sized SPICE and native constraint JSON.

This example embeds the actual [`design/cm_ota.spice`](../design/cm_ota.spice)
template. To start from scratch instead, choose **New floorplan…**, select
that file, and choose micrometres and total width. Optionally select
`cm_ota_sizes.roar` as the sizing source in the same dialog. The template loads
with unresolved widths and finger counts; you can save that draft and author
constraints before assigning all values. Export waits for complete, valid sizes.

The source template makes `XM10` refer to `nf1_2`, while `XM9` refers to `nf9_10`.
The affected-device column exposes that relationship and preserves it exactly.

## Create an OTA floorplan from a numeric netlist

1. Click the layout button, then **New floorplan…**. Choose
   [`current_mirror_ota.sp`](../design/floorplan/ota/current_mirror_ota.sp).
   The import dialog selects the only subcircuit automatically. Choose a circuit
   from the dropdown for files containing several. Units and width convention
   appear together in the same dialog; keep micrometres and width per finger for
   this example. Its explicit SPICE suffixes take precedence over the unit
   selector.
2. Edit **W per finger (µm)**, **L (µm)** and **NF** on the Devices tab. Sizes
   are synthetic geometry examples, not optimized or simulation-qualified designs.
   NF must be an even integer of at least two for the SKY130 generator.
3. On the **Constraints** tab, add a constraint and select its devices and
   settings. Each constraint has its own readable row; select a row to edit
   or remove it. For this example, you can also expand **Advanced** and use
   **Load ALIGN constraints…** to read the sibling `.const.json` file.
4. Use **Save** to retain your work.
   The project includes the original netlist text, edited sizes and constraints;
   reopening does not depend on the original file still being present.
5. **Export ALIGN inputs…** asks for a parent folder and creates a fresh
   `<subcircuit>/` directory containing `<subcircuit>.sp` and
   `<subcircuit>.const.json`. Existing bundle directories are refused, so choose
   another parent folder for a new revision.

The OTA contains ten MOS devices. Its example constraints pair devices around a
shared vertical axis and align each pair's horizontal centers. Common-source
and other circuit examples are deferred to a later contribution.

The import unit selector applies only to numbers without a suffix: with
micrometres selected, `L=0.5` means 0.5 µm. Select metres for a netlist that writes
the same dimension as `L=0.5e-6`. `L=0.5u` means 0.5 µm with either selection.
The editing table always uses micrometres, and export converts to SI. The headless
`from_netlist` API retains its metre default; pass `dimension_unit="um"` for bare
micrometre inputs.

## Placement intent

Each constraint appears as a row with its type, affected devices or ports, and
settings. Add a constraint through its form, or select an existing row and edit
it (double-clicking also opens the form). Changes are validated before applying;
cancelling leaves the constraint unchanged. No JSON editing is needed.

See ALIGN's
[constraint reference](https://align-analoglayout.github.io/ALIGN-public/notes/const.html)
for the underlying semantics.

| Editor intent | Native ALIGN constraint | Meaning |
| --- | --- | --- |
| Symmetry | `SymmetricBlocks` | All pairs share one horizontal or vertical axis. An additional singleton can sit on that axis. |
| Relative order | `Order` | Left-to-right or top-to-bottom order; reverse the device list for the opposite order. Optional abutment requests touching edges. This does not also align centers. |
| Align centers / edges | `Align` | Align centers or edges along the selected horizontal or vertical line. |
| Rows / regions | `Floorplan` | Regions run from top to bottom. Optionally order devices left to right within each row, or mirror them around one shared vertical axis. Add `Align` separately when row centers must coincide. |
| Packing | `CompactPlacement` | Request left, right or centered packing for the entire circuit. |
| Place closer | `PlaceCloser` | Encourage selected devices to be close together. |
| Power ports / Ground ports | `PowerPorts` / `GroundPorts` | Identify supply ports, retaining their order and whether the assignment propagates into child circuits. |

Symmetric pairs must have matching models and parameters. Centered packing is a
placement preference, not a guarantee of exact centering in a fixed boundary.

Local validation checks names, supported fields and concrete dimensions. It does
not solve combined constraints, generate primitives, or prove physical
feasibility. These examples were also checked with ALIGN 0.9.8's parser,
constraint verification and native placement-constraint translation. A successful
export alone does not establish DRC, LVS, model-bin validity or generated geometry.

## Bring sizes over from ROAR

Use **Load / relink ROAR design…** for a saved design or **Read current ROAR sizes**
for the running design. Both supply detached snapshots. The Parameters tab binds
one netlist symbol to one explicitly chosen instance attribute, using ROAR's
existing assigned scalar values. It records whether that binding came from the
linked file or the running design, and preserves the captured value on save.
Refreshing updates bindings for the selected source only. Missing assignments,
disabled instances, nonfinite values and vectors are rejected; the editor does
not evaluate sweeps or select a design point on your behalf.

For a fully numeric netlist, the Devices tab also accepts explicit source-row
to target-device mappings. For example, map ROAR row `M1_2` to `M1, M2`; names
are never expanded automatically. Fields controlled by template symbols are
edited through those shared parameters instead. Numeric fields, such as a
literal L in an otherwise parameterized device, remain editable directly.

**Import numeric .param…** can populate matching symbols from a numeric SPICE parameter
file. Choose the file's units and width convention. The import is atomic:
invalid used values leave all existing sizes intact. Unused names are reported;
matched assignments replace their previous bindings. Expressions and includes
are outside this importer. The existing simulation parameter files with NF=1
therefore need explicit layout-appropriate finger counts before import.

The defaults are metres for ROAR W/L and total width. When total width is selected,
the editor divides W by NF once. Its table always shows per-finger W and L in
micrometres. Export always writes SI dimensions and per-finger W. Assigned ROAR
values must be concrete scalars; unresolved template symbols can remain in a
saved draft, but prevent export.

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
project.save("ota.roar.floorplan")
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

For a template and companion association:

```python
from roar_api import Design

project = FloorplanProject.from_netlist(
    "design/cm_ota.spice", dimension_unit="um", width_convention="total")
project.roar_project_path = "design/floorplan/ota/cm_ota_sizes.roar"
design = Design.load(project.roar_project_path)
project.bind_parameter(design, "w1_2", "M1_2", "W",
                       dimension_unit="m", width_convention="total", source="linked")
project.set_parameter("nf1_2", 2)
project.save("ota-draft.roar.floorplan")  # remaining symbols can be assigned later
```

`parameter_rows` lists values, units, provenance and affected fields.
`unresolved_parameters` lists symbols awaiting values. `refresh_bindings(design,
source="linked")` updates linked bindings atomically; `import_parameters(path,
dimension_unit="um", width_convention="total")` imports numeric `.param` values.

## Supported input boundary

This version accepts a selected flat `.subckt` with four-terminal `M` or `X`
MOS devices and numeric two-terminal resistors. W, L, NF and optional M can be
numbers or simple shared symbol names. NF must resolve to an even integer of at
least two; M must resolve to a positive integer. `MULT` is accepted as M, while
specifying both is rejected as ambiguous. Select the top explicitly for files
with multiple subcircuits. Xschem's commented wrappers and continuations work.

The original template remains intact. For SKY130 devices, the editor identifies
simulation parameters AD, AS, PD, PS, NRD, NRS, SA, SB and SD and lists their
omission from the generated layout input. Those expressions are not evaluated;
ALIGN generates its own device geometry. Other extra parameters, arithmetic
sizing expressions, hierarchy, includes and unsupported device kinds produce
actionable errors. This adapter is a bounded layout export, not a SPICE simulator.

The constraint forms support the subset listed above. Unsupported constraints
or settings already present in a saved floorplan remain visible and are
preserved when saving. They cannot be edited through a form that does not
understand them, and validation/export explains the unsupported setting.
The advanced **Load ALIGN constraints…** action checks supported rules before
changing the floorplan. Rules with unsupported types or settings are retained
as clearly marked, read-only rows; they must be removed or replaced before
export. A file in the wrong format, an invalid supported rule, or cancellation
leaves the existing constraints intact.

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

This checks the numeric OTA example, the saved OTA floorplan,
and ordinary/symmetric xschem rows through
ALIGN's parser, constraint verification and native placement translation. It
prints a receipt and writes bundles and logs to a temporary directory. It does
not run physical placement or routing.
