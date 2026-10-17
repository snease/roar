# OTA floorplan example

This bundle demonstrates a ten-transistor current-mirror OTA using the
constraint editor and the supported SKY130A ALIGN backend. The sizes are
synthetic layout examples, not an optimized or simulation-qualified design.

1. Open ROAR's floorplan editor with the layout button in the main header.
2. Choose **Open floorplan…** and select `cm_ota.roar.floorplan` here.
3. Inspect the **Devices**, **Parameters**, and **Constraints** tabs. The
   companion contains captured sizes and explicit bindings to `cm_ota_sizes.roar`.
   Use **Captured sizes (no updates)** to work with those captured values, or select
   **Saved ROAR file** to refresh from the adjacent sizing snapshot.
4. Use **Save as…** to keep your edited example in your own working directory.
5. To generate placement, open **Placement → ALIGN settings…**, choose the
   separate ALIGN Python and supported backend, and use **Test installation**.
   Then choose **Generate placement and open ALIGN**.

The browser shows placement candidates. This flow stops before routing and
does not produce a DRC/LVS-qualified final layout.

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
