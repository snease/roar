# OTA floorplan example

This bundle demonstrates a ten-transistor current-mirror OTA using the
graphical floorplanner and the supported SKY130A ALIGN backend. The sizes are
synthetic layout examples, not an optimized or simulation-qualified design.

1. After running `./bin/setup_roar.sh`, launch `./bin/roar` from the checkout root. Open the
   floorplan editor with the layout button in the main header.
2. Choose **Open floorplan…** and select `cm_ota.roar.floorplan` here.
3. Use **Captured sizes (no updates)** to keep the companion's saved values.
   To use its linked `cm_ota_sizes.roar` live, choose **Open another ROAR design…**
   (**Saved ROAR file** in a standalone editor). Inspect **Devices** and
   **Parameters** for the captured sizes and explicit bindings.
4. On **Floorplan**, arrange the unscaled sketch. Try **Align**, **Order**,
   **Make row**, or **Make symmetric…** to create a native rule, then inspect
   it in **Constraints**. Dragging and snapping alone create no native rules;
   **Group for drawing…** only moves labeled members together. Undo, then
   **Save as…** and reopen to check your saved arrangement.
5. To generate physical geometry, open **Placement → ALIGN settings…** and use
   **Test installation**. Managed setup already selects the separate ALIGN Python
   and supported backend.
   Choose **Generate preview** on **Floorplan** to compare a physical candidate
   beside the sketch. No browser is opened. Inspect dimensions, select a source
   device, and choose a net under **Connections**.
6. Use **Export handoff…** to share captured sizes, native inputs and drawings
   in one ZIP. Including a displayed candidate also includes the inputs that
   produced it; an older result is clearly distinguished from current inputs.

The companion labels the input pair `diffpair_inn` / `diffpair_inp`, the PMOS
mirror reference/output devices `pmirror_inn_ref` / `pmirror_inn_out` and
`pmirror_inp_ref` / `pmirror_inp_out`, the NMOS mirror `nmirror_ref` /
`nmirror_out`, and the bias devices `tail` / `bias_ref`. Original instance IDs
remain visible. Select one box and choose **Role label…** to edit its role.
Hover over a box for its directly associated rules, or use **Rules for selection →
Inspect rule…**. Drawing groups are separate from enabled native constraints.

Changing sizes or enabled rules marks the captured preview **Earlier inputs**; generate again to apply them. Drawing-only edits keep the
preview current. **Open in ALIGN browser…** remains an explicit optional action.
This flow stops before routing and does not produce a DRC/LVS-qualified final layout.

| File | Purpose |
| --- | --- |
| `cm_ota.roar.floorplan` | Ready-to-open project with captured sizing, bindings and constraints. |
| `cm_ota_sizes.roar` | Small ROAR sizing snapshot; no characterization data installation is needed to inspect it. |
| `current_mirror_ota.sp` | Fully numeric OTA netlist for starting a new floorplan. Explicit SPICE suffixes set its dimension units. |
| `current_mirror_ota.const.json` | Native ALIGN rules for the numeric netlist; load these through the Constraints tab's Advanced controls. |

The parameterized companion embeds the existing `design/cm_ota.spice` template.
Its source is stored in the project, so reopening the companion does not depend
on that original file. Keep the companion and sizing snapshot together when
moving the bundle; their association uses a relative path.

See the [floorplanning guide](../../../docs/FLOORPLANNING.md) for setup,
constraint semantics, supported inputs, and the Python API.
For an existing custom GUI environment, launch with `python3 -m roar_api` instead
and configure its separate ALIGN Python and supported backend in ALIGN settings.
