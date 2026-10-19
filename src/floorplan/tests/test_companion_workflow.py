"""Headless integration checks for a template and its independent ROAR source.

Run against an editable installation with python3 -m unittest discover
-s src/floorplan/tests. Native ALIGN compatibility is checked separately.
"""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from roar_api.floorplan import FloorplanProject
from roar_api.floorplan.model import parse_spice_netlist
from roar_api import Design


ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = ROOT / "design/cm_ota.spice"
GROUPS = ("1_2", "3_4", "5_6", "7_8", "9_10")


class CompanionWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="roar-companion-test-")
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)

    def project(self):
        return FloorplanProject.from_netlist(
            TEMPLATE, dimension_unit="um", width_convention="total")

    def design(self):
        design = Design("Synthetic chosen sizing point")
        for index, group in enumerate(GROUPS):
            design.add_instance("M" + group, W=(4.2 + index * .4) * 1e-6,
                                L=.5e-6, NF=4)
        return design

    def bind(self, project, design, source="linked"):
        for group in GROUPS:
            project.bind_parameter(design, "w" + group, "M" + group, "W",
                                   dimension_unit="m", width_convention="total", source=source)
            project.set_parameter("nf" + group, 4)

    def test_original_template_saved_source_to_layout_bundle(self):
        project, caller = self.project(), self.design()
        before = caller.to_state()
        source = caller.save(self.folder / "sizes.roar")
        source_bytes = source.read_bytes()
        project.roar_project_path = source
        self.bind(project, Design.load(source))
        project.set_constraints([{"constraint": "SymmetricBlocks", "direction": "V",
            "pairs": [["XM1", "XM2"], ["XM3", "XM4"], ["XM5", "XM6"],
                      ["XM7", "XM8"], ["XM9", "XM10"]]}])
        sidecar = project.save(self.folder / "layout.floorplan.json")
        reopened = FloorplanProject.load(sidecar)
        netlist, constraints = reopened.export_align(self.folder / "inputs")
        parsed = parse_spice_netlist(str(netlist)).subcircuits[0]
        self.assertEqual(parsed.ports, project.ports)
        self.assertEqual(len(parsed.instances), 10)
        for original, exported in zip(project.devices, parsed.instances):
            self.assertEqual(exported.name, original.name)
            self.assertEqual(exported.model, original.model)
            self.assertEqual((exported.drain, exported.gate, exported.source, exported.body), original.terminals)
            self.assertAlmostEqual(float(exported.parameters["W"]), original.width_um * 1e-6, places=17)
            self.assertAlmostEqual(float(exported.parameters["L"]), .5e-6, places=17)
            self.assertEqual(int(exported.parameters["nf"]), 4)
            self.assertEqual(float(exported.parameters["M"]), 1)
        self.assertEqual(json.loads(constraints.read_text()), project.constraints)
        self.assertEqual(reopened.source_netlist, TEMPLATE.read_text())
        self.assertTrue(reopened.omitted_parameters)
        self.assertNotIn("ad=", netlist.read_text().lower())
        self.assertNotIn("nrd=", netlist.read_text().lower())
        self.assertEqual(source.read_bytes(), source_bytes)
        self.assertEqual(caller.to_state(), before)

    def test_incomplete_sidecar_saves_shared_symbols_and_gates_export(self):
        project = self.project()
        project.bind_parameter(self.design(), "w1_2", "M1_2", "W", source="linked")
        self.assertIsNone(project._device("XM1").width_um)
        sidecar = project.save(self.folder / "unfinished.floorplan.json")
        reopened = FloorplanProject.load(sidecar)
        self.assertEqual(reopened.unresolved_parameters, project.unresolved_parameters)
        target = self.folder / "should-not-exist"
        with self.assertRaisesRegex(ValueError, "Unresolved"):
            reopened.export_align(target)
        self.assertFalse(target.exists())
        reopened.set_parameter("nf1_2", 4)
        self.assertAlmostEqual(reopened._device("XM1").width_um, 1.05)
        self.assertAlmostEqual(reopened._device("XM2").width_um, 1.05)
        self.assertEqual(reopened._device("XM10").nf, 4)
        reopened.set_parameter("nf1_2", 2)
        self.assertAlmostEqual(reopened._device("XM1").width_um, 2.1)
        self.assertEqual(reopened._device("XM10").nf, 2)

    def test_relative_association_move_save_as_and_missing_source(self):
        original = self.folder / "original"
        original.mkdir()
        source = self.design().save(original / "sizes.roar")
        source_bytes = source.read_bytes()
        project = self.project()
        project.roar_project_path = source
        self.bind(project, Design.load(source))
        sidecar = project.save(original / "layout.floorplan.json")
        self.assertEqual(json.loads(sidecar.read_text())["roar_project_path"], "sizes.roar")
        moved = self.folder / "moved"
        shutil.move(str(original), moved)
        reopened = FloorplanProject.load(moved / sidecar.name)
        self.assertEqual(Path(reopened.roar_project_path), moved / "sizes.roar")
        clone = FloorplanProject.from_dict(reopened.to_dict())
        saved_as = clone.save(moved / "variants/other.floorplan.json")
        self.assertEqual(json.loads(saved_as.read_text())["roar_project_path"], "../sizes.roar")
        self.assertEqual((moved / "sizes.roar").read_bytes(), source_bytes)
        (moved / "sizes.roar").unlink()
        without_source = FloorplanProject.load(saved_as)
        without_source.validate()  # the captured, resolved sizing point is preserved
        self.assertEqual(without_source.to_dict()["devices"], reopened.to_dict()["devices"])

    def test_refresh_is_explicit_and_atomic_and_relink_keeps_snapshot(self):
        project = self.project()
        first = self.design()
        self.bind(project, first)
        captured = deepcopy(project.to_dict())
        second = self.design().set_instance_attribute("M1_2", "W", 8.4e-6)
        project.roar_project_path = self.folder / "replacement.roar"
        self.assertEqual(project.to_dict()["devices"], captured["devices"])
        project.refresh_bindings(second, source="linked")
        self.assertAlmostEqual(project._device("XM1").width_um, 2.1)
        self.assertAlmostEqual(project._device("XM2").width_um, 2.1)
        before = project.to_dict()
        invalid = self.design().set_instance_attribute("M1_2", "W", 12e-6)
        invalid.set_instance_attribute("M9_10", "W", [2e-6, 3e-6])
        with self.assertRaises(ValueError):
            project.refresh_bindings(invalid, source="linked")
        self.assertEqual(project.to_dict(), before)
        project.refresh_bindings(first, source="live")
        self.assertEqual(project.to_dict(), before)  # linked bindings do not read a live source

    def test_sidecar_cannot_overwrite_roar_and_v1_remains_readable(self):
        source = self.design().save(self.folder / "sizes.roar")
        source_bytes = source.read_bytes()
        project = self.project()
        project.roar_project_path = source
        with self.assertRaises(ValueError):
            project.save(source)
        self.assertEqual(source.read_bytes(), source_bytes)
        numeric = FloorplanProject.from_text(
            ".subckt OLD d g s b\nM1 d g s b sky130_fd_pr__nfet_01v8 W=1.1u L=.5u NF=2\n.ends OLD\n")
        legacy = numeric.to_dict()
        for key in ("parameters", "overrides", "roar_project_path", "align_profile",
                    "source_mode", "run_options", "constraint_enabled", "constraint_ids", "sketch", "device_labels"):
            del legacy[key]
        legacy["version"] = 1
        self.assertEqual(FloorplanProject.from_dict(legacy).devices, numeric.devices)

if __name__ == "__main__":
    unittest.main()
