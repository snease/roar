# Floorplan constraints for ALIGN

The layout button in ROAR's header opens a floorplan editor for device sizes and
high-level placement intent. It exports sized SPICE and native ALIGN constraint
JSON. The optional **Placement** tab runs the circuit through ALIGN and opens ALIGN's
own placement explorer in a browser. ALIGN supplies both the placement engine
and the explorer; ROAR manages the inputs, settings and run lifecycle.

The editor uses ROAR's existing Qt interface and netlist parser. Install ROAR
with `pip install -e '.[gui]'`, then launch it normally. ALIGN, Xschem, Magic and
a PDK are not required to author or export a project. Generating placement needs
a separate ALIGN installation and the tested SKY130 ALIGN backend on Linux or
WSL. The Python API works with the headless installation, `pip install -e .`.

If your environment already has ROAR's GUI dependencies, pull the branch and
restart ROAR; no reinstall is needed to find the floorplan editor. Both
`python src/roar.py` and `python -m roar_api` work from the checkout root.
Activate that environment before launching; `python3 --version` must report
Python 3.10 or newer. If an older system Python is selected, reactivate the
existing ROAR environment rather than reinstalling ROAR.

## A companion to the ROAR design

The editable layout data lives in a separate `.roar.floorplan` file.
It contains the circuit template, chosen subcircuit, size values and bindings,
placement constraints, and an optional link to a saved ROAR design. ROAR's
existing `.roar` format is unchanged. Switching to an associated design requires
an explicit choice when it conflicts with the current design. Existing
`.floorplan.json` files still open.

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

**Open floorplan…** offers a sizing source: **Current ROAR design**, **Open another
ROAR design…**, or **Captured sizes (no updates)**. Opening another design changes
ROAR's active design and keeps bound sizes live; selecting its already active
file uses the current unsaved sizes. In a standalone editor, **Saved ROAR file**
reads the file without changing a ROAR workspace. The selected mode is saved with the project;
older projects reopen their captured sizes without silently refreshing a link. Missing links can be
repaired with Browse, or the floorplan can open using its saved sizes alone.

If the associated design differs from the active design, a conflict dialog
offers switching to the associated design, associating the floorplan with the
current design, using captured sizes, or cancelling. This also applies when a
saved floorplan reopens in live mode, even if the two designs have identical
sizes. Unsaved design changes add **Save and Switch**
and **Discard and Switch** choices. Cancelling or failing a save cancels the
switch. An unnamed current design must be saved before making a persistent
association. Switching changes the design editor's data; graph tabs and window
layout stay in place.

Links are saved relative to the companion file. Moving the two files together
preserves their association, and **Save as…** rebases the link to the same design.
The original netlist text is embedded, so it remains available even if its
original file moves. Versions 1–3 still open; new saves use version 4, which
records sizing-source intent, enabled constraints, and placement run options. Machine paths and run
history are stored separately from the floorplan and the `.roar` design.

An asterisk in the title marks unsaved floorplan edits. Closing the editor,
switching documents, or exiting ROAR offers Save, Discard, and Cancel. Saving
the application state also saves a modified floorplan companion; it does not
embed the floorplan into the `.roar` file. Undo and Redo cover manual device,
parameter and constraint edits, including temporary invalid cell text.

**Captured sizes (no updates)** restores the captured sizes without a live source.
**Current ROAR design** keeps explicitly bound parameters up to date with the
design editor. Manual values are preserved. A live source is refreshed again
before generating placement or exporting, so a missing or invalid binding
blocks those actions rather than exporting its old value.

## Try a linked, parameterized OTA

1. Click the header's layout button, then **Open floorplan…** and choose
   [`cm_ota.roar.floorplan`](../examples/floorplan/ota/cm_ota.roar.floorplan).
   Its linked [`cm_ota_sizes.roar`](../examples/floorplan/ota/cm_ota_sizes.roar)
   is retained as its association. Open with **Captured sizes (no updates)**, or choose
   **Open another ROAR design…** to open its linked design as the live sizing
   source (**Saved ROAR file** in a standalone editor).
2. Inspect the **Parameters** tab. Each shared symbol lists its affected device
   fields, captured value and ROAR source. For example, `w1_2` is bound to
   `M1_2.W`. W is 2.2 µm total, NF is 2, and the Devices tab therefore shows
   1.1 µm per finger. These are synthetic demo sizes, not optimized designs.
3. Select a parameter to enter a manual value or bind a different resolved ROAR
   instance attribute. Manual assignment replaces that parameter's binding.
   Clearing its value makes it unresolved again. Shared symbols update all
   their device references together.
4. Use **Save** or **Save as…**, then the **Placement** tab to generate and
   inspect an OTA placement. To run ALIGN manually elsewhere, **Export ALIGN
   inputs…** creates a fresh circuit directory with sized SPICE and native
   constraint JSON.

This example embeds the actual [`design/cm_ota.spice`](../design/cm_ota.spice)
template. To start from scratch instead, choose **New floorplan…**, select
that file, and choose micrometres and total width. Optionally select
`cm_ota_sizes.roar` as the sizing source in the same dialog. The template loads
with unresolved widths and finger counts; you can save that draft and author
constraints before assigning all values. Export waits for complete, valid sizes.

The source template makes `XM10` refer to `nf1_2`, while `XM9` refers to `nf9_10`.
The affected-device column exposes that relationship and preserves it exactly.

## Generate and inspect the OTA placement

### Set up ALIGN once

An administrator or CAD support engineer can prepare the separate tool
environment; subsequent editing and placement runs use the GUI. Keep ALIGN's
dependencies separate from ROAR's Qt environment.

Open **ALIGN settings… → Setup help…** for the
[installation recipe and common errors](../src/floorplan/ALIGN_SETUP.md). That
guide uses the official ALIGN 0.9.8 Python 3.10 wheel, Dash 2.18.2 and the pinned
vibeic SKY130 backend. The source reference used for compatibility checks is
[revision `8d3cc2e9`](https://github.com/ALIGN-analoglayout/ALIGN-public/tree/8d3cc2e9aa5511315203eb5cbfd00d2c88fdbd18);
the installed wheel is identified by its release, not assumed to equal that checkout.

**Test installation** reports versions, checks the backend and viewer assets,
imports native components and verifies write access to the placement runs folder. It creates
the runs folder if needed, but does not install or modify the toolchain.

### Run placement

1. Open the linked OTA example above and select **Placement**. Its saved
   backend profile selects **SKY130A** and supplies the repository's run defaults.
2. Open **ALIGN settings…**, select a **Layout technology**, and choose the Python
   executable from the separate ALIGN environment, the backend's `SKY130_PDK` directory, and a run
   directory, then use **Test installation** to check the versions and backend.
   These paths are local preferences; they are not written into
   the floorplan or the linked ROAR design. Settings can be populated from
   `ROAR_ALIGN_PYTHON` and `ROAR_ALIGN_PDK_FIXED` when ROAR starts.
3. Choose **Generate placement and open ALIGN**. ROAR captures the current
   sizes and constraints, exports them into a new run directory, and runs ALIGN
   in a separate process. Progress and logs remain available while ROAR stays
   responsive. The browser opens after the local explorer responds.
4. Explore the generated candidates, hierarchy, pins and nets in ALIGN's
   existing interface. The run stops before routing; this is placement
   exploration, not a DRC/LVS-qualified layout flow.
5. After changing sizes or constraints in ROAR, generate a new placement to
   apply them. **Open last placement** reopens captured results without
   recompiling the current floorplan. A stale indicator identifies results
   that differ from the current document.

**View last run / log…** reopens progress or failure details without generating
again. **Copy diagnostics** includes the run folder and displayed log for CAD
support, with viewer-link credentials removed. The full log remains in the run folder.

![ALIGN's browser viewer showing a generated OTA placement and candidate tradeoffs.](images/floorplan/placement.png)

Compare placement candidates and inspect devices, pins and nets in ALIGN's viewer.

The repository profile is
[`align_profile.json`](../src/floorplan/align_profile.json): ALIGN 0.9.8, Dash 2,
the tested `vibeic/ALIGN-pdk-sky130` backend revision, four requested candidates,
100 placement iterations and seed zero. **ALIGN settings…** exposes candidate
count, placement effort, random seed, and wirelength weight. These choices are
saved in the floorplan and captured for each run. The initial 100 iterations
are a quick preview; ALIGN's usual default is 10,000. These are demonstration defaults;
ALIGN may return fewer distinct candidates. The profile checks the backend
files that determine MOS geometry. The generic `ROAR_ALIGN_PDK` variable is not
used because it can refer to a backend with different geometry behavior.

The explorer is served on the local machine with its debugger disabled. Each
run uses an authenticated browser link; open it from ROAR. Foreign hosts and
cross-origin requests are rejected, including requests to Dash callbacks.
Closing a browser tab does not stop ALIGN; use the run controls or close the
floorplan editor. Captured inputs and generated artifacts remain available
after stopping, including across ROAR restarts. Runs are associated with the
saved floorplan path. Saving an unsaved floorplan before running gives it a
stable identity for reopening results. **Save as…** moves the current run's
association to the new floorplan path without restarting ALIGN.

Technology choices come from ROAR's sizing library. A characterization library
alone does not provide an ALIGN layout generator: technologies without a
configured backend remain visible but unavailable for generation. The included
backend supports four-terminal SKY130 1.8 V NMOS and PMOS models. Unsupported
devices are reported by name; device count is not restricted to the OTA example.
The OTA remains the included demonstration and native acceptance circuit.

## Create an OTA floorplan from a numeric netlist

1. Click the layout button, then **New floorplan…**. Choose
   [`current_mirror_ota.sp`](../examples/floorplan/ota/current_mirror_ota.sp).
   The import dialog selects the only subcircuit automatically. Choose a circuit
   from the dropdown for files containing several. Units and width convention
   appear together in the same dialog; keep micrometres and width per finger for
   this example. Its explicit SPICE suffixes take precedence over the unit
   selector.
2. Edit **W per finger (µm)**, **L (µm)**, **NF**, and **M** on the Devices tab. Sizes
   are synthetic geometry examples, not optimized or simulation-qualified designs.
   Positive integer NF values can be imported and saved as drafts; this SKY130
   generator requires even NF of at least two before placement. Effective width
   includes both NF and multiplicity. The terminal/net inspector shows the
   selected device's connectivity. Numeric resistor values are editable too,
   although the included placement backend supports MOS devices only.
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
shared vertical axis and align each pair's horizontal centers.

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

![Device symmetry form, with the shared axis marked 1 and device pairs marked 2.](images/floorplan/constraints.png)

**1** Choose the shared axis. **2** Pair devices, or place a singleton on that axis.

See ALIGN's
[constraint reference](https://align-analoglayout.github.io/ALIGN-public/notes/const.html)
for the underlying semantics.

| Editor intent | Native ALIGN constraint | Meaning |
| --- | --- | --- |
| Device symmetry | `SymmetricBlocks` | All pairs share one horizontal or vertical axis. An additional singleton can sit on that axis. |
| Relative order | `Order` | Left-to-right or top-to-bottom order; reverse the device list for the opposite order. Optional abutment requests touching edges. This does not also align centers. |
| Align centers / edges | `Align` | Align centers or edges along the selected horizontal or vertical line. |
| Rows / regions | `Floorplan` | Regions run from top to bottom. Optionally order devices left to right within each row, or mirror them around one shared vertical axis. Add `Align` separately when row centers must coincide. |
| Packing | `CompactPlacement` | Request left, right or centered packing for the entire circuit. |
| Place closer | `PlaceCloser` | Encourage selected devices to be close together. |
| Power ports / Ground ports | `PowerPorts` / `GroundPorts` | Identify supply ports, retaining their order and whether the assignment propagates into child circuits. |
| Boundary and margins | `Boundary` | Maximum width/height and margins, in µm. |
| Aspect ratio | `AspectRatio` | Bounds on circuit width/height and the native weight. |
| Minimum device spacing | `Spread` | A gap in nm between selected devices when they overlap in the other direction. |
| Circuit spacing | `BlockDistance`, `HorizontalDistance`, `VerticalDistance` | Circuit-wide gaps in nm; the placer rounds up to its routing grid. |
| Same device layout template | `SameTemplate` | Matching devices use the same physical template. This does not create a common-centroid arrangement. |
| Align and order | `AlignInOrder` | Edge/center alignment together with left-to-right or top-to-bottom ordering. |
| Port location | `PortLocation` | A boundary region for signal ports. |
| Net symmetry | `SymmetricNets` | Two nets and ordered corresponding pins, with an explicit symmetry axis. |
| Net placement priority | `NetPriority` | Select signal nets and assign a nonnegative integer wirelength weight. Larger values favor shorter connections during placement; unspecified nets have weight 1. This is not a matching or length guarantee. |
| Clock ports | `ClockPorts` | Exclude clock nets from ordinary signal symmetry inference. |
| Keep devices out of pattern recognition | `DoNotIdentify` | Prevent selected devices from being combined into recognized circuit patterns. |
| Compiler transformations | `ConfigureCompiler` | Control inference, merging, dummy removal, source/drain correction, and propagation. Defaults match ALIGN. |

Symmetric pairs must have matching models and parameters. Centered packing is a
placement preference, not a guarantee of exact centering in a fixed boundary.

Local validation checks names, fields, concrete dimensions, contradictory
ordering cycles and conflicting port roles. Use one enabled row for each
circuit-wide boundary, aspect ratio, packing or compiler setting. For circuit
spacing, use either one general row or separate horizontal and vertical rows;
overlaps are rejected because ALIGN can overwrite earlier settings. A net's
placement weight must likewise be set in only one enabled row.
**Check constraints** also runs
native combined checks when ALIGN is configured. Generation repeats native
preflight before making primitives. These checks do not prove physical
feasibility for generated geometry. These examples are also checked with ALIGN 0.9.8's parser,
constraint verification and native placement-constraint translation. A successful
export alone does not establish DRC, LVS, model-bin validity or generated geometry.
Native-check results stay visible on **Placement**, separately from run status.
Editing the inputs marks that check out of date; run **Check constraints** again.

## Bring sizes over from ROAR

![Parameters tab, with shared symbols marked 1 and ROAR binding controls marked 2.](images/floorplan/parameters.png)

**1** See which devices share each size. **2** Bind a ROAR value and specify its
units and width convention; total W is divided by NF for the device's per-finger width.

Use the **Sizing source** selector for the current design, a saved design, or
the floorplan's captured sizes. The Parameters tab binds
one netlist symbol to one explicitly chosen instance attribute, using ROAR's
existing assigned scalar values. It records whether that binding came from the
linked file or the running design, and preserves the captured value on save.
Selecting the current design updates existing explicit bindings to that source;
it does not infer device-name mappings. Missing assignments,
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
are outside this importer. Simulation parameter files with NF=1 can be imported;
choose layout-appropriate finger counts before running the SKY130 generator.

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

example = Path("examples/floorplan/ota/current_mirror_ota.sp")
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
project.roar_project_path = "examples/floorplan/ota/cm_ota_sizes.roar"
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
numbers or simple shared symbol names. NF and M must resolve to positive
integers. The SKY130 placement preflight additionally requires even NF ≥ 2,
per-finger W from 0.42 to 2.52 µm on a 0.01 µm grid, and L ≥ 0.15 µm on a
0.005 µm grid. These are generator restrictions, not foundry signoff checks.
Boundary margins also follow the backend routing grid: left/right margins
must be multiples of 0.43 µm and top/bottom margins multiples of 0.42 µm.
Preflight reports off-grid values instead of silently changing them.
`MULT` is accepted as M, while
specifying both is rejected as ambiguous. Select the top explicitly for files
with multiple subcircuits. Xschem's commented wrappers and continuations work.

The original template remains intact. For SKY130 devices, the editor identifies
simulation parameters AD, AS, PD, PS, NRD, NRS, SA, SB and SD and lists their
omission from the generated layout input. Those expressions are not evaluated;
ALIGN generates its own device geometry. Other extra parameters, arithmetic
sizing expressions, hierarchy, includes and unsupported device kinds produce
actionable errors. This adapter is a bounded layout export, not a SPICE simulator.

The constraint forms support the subset listed above. Imported constraints
or settings already present in a saved floorplan remain visible and are
preserved when saving. They cannot be edited through a form that does not
understand them; **Details** shows all fields and explains support. **Duplicate**,
**Enable / Disable**, Undo, and Redo support trying alternatives. Disabled rows
stay in the project and are omitted from native exports.

`NetConst`, `MultiConnection`, `DoNotRoute`, `DoNotUseLib`, and `Route` have native
export support without graphical forms. Their fields are checked on import and
export. `NetConst.criticality` weights placement wirelength; its shielding and
the other routing settings take effect when routing is run outside this flow.
`PlaceOnBoundary` is recognized by ALIGN but dropped by the C++ placer used here;
it is not offered as an effective placement control. Generator configuration,
grouping, guard rings, capacitor arrays, placement grids, and charge-flow data
require additional backend/workflow support and remain preserved but unavailable
for export here. Internal `AssignBboxVariables` is not a designer-facing option.
Graphical library exclusion needs meaningful library choices and guidance;
replacing a netlist while retaining rules/bindings and comparing historical
runs are also deferred.

The advanced **Load ALIGN constraints…** action checks supported rules before
changing the floorplan. Rules with unsupported types or settings are retained
as clearly marked, read-only rows; disable, remove or replace them before
export. A file in the wrong format, an invalid supported rule, or cancellation
leaves the existing constraints intact.

## Checks

After installing the GUI extra, run:

```sh
QT_QPA_PLATFORM=offscreen python -m unittest discover -s src/floorplan/tests -v
```

The tests cover topology, unit conversion, explicit ROAR mappings, persistence,
export collisions, invalid input and Qt editing flows. Placement tests cover
process ownership, cancellation, saved-result reopening, stale inputs, settings
and browser handoff using isolated subprocesses and mocked browser opening.
GUI tests skip when PyQt6 is absent. The core package does not import Qt or ALIGN.

For an independent compatibility check, use a separately installed ALIGN and a
SKY130 ALIGN PDK directory containing `models.sp`:

```sh
/path/to/align-venv/bin/python /path/to/roar/src/floorplan/tests/check_align.py \
    --repo /path/to/roar --pdk /path/to/ALIGN-pdk-sky130/SKY130_PDK
```

This checks the numeric OTA example, the saved OTA floorplan,
ordinary/symmetric xschem rows, and the additional constraint forms through
ALIGN's parser, constraint verification and native placement translation. It
prints a receipt and writes bundles and logs to a temporary directory. It does
not run physical placement unless `--placement` is supplied. That optional check
uses small real placement cases to inspect dimensions, spacing, matching,
port locations, net symmetry and compiler transformations. It never runs routing.
