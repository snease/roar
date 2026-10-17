"""Qt smoke tests; run with QT_QPA_PLATFORM=offscreen and the GUI extra."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    from PyQt6.QtWidgets import QApplication, QFileDialog, QInputDialog, QMessageBox, QWidget
except ImportError:
    QApplication = None

from roar_api import Design
from roar_api.floorplan import FloorplanProject

if QApplication is not None:
    from roar_api.floorplan.dialog import FloorplanDialog


NETLIST = """.subckt pair d g s b
M1 d g s b nch W=2u L=0.15u nf=2
M2 d g s b nch W=2u L=0.15u nf=2
M3 d g s b nch W=2u L=0.15u nf=2
M4 d g s b nch W=2u L=0.15u nf=2
.ends pair
"""


@unittest.skipIf(QApplication is None, "PyQt6 GUI extra is not installed")
class FloorplanDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.parent = QWidget()
        self.design = Design().add_instance("M1_2", W=8e-6, L=0.2e-6, NF=4)
        self.dialog = FloorplanDialog(
            self.parent, lambda: Design.from_state(self.design.to_state()))
        self.dialog.set_project(FloorplanProject.from_text(NETLIST))

    def tearDown(self):
        self.dialog.close()
        self.parent.deleteLater()
        self.app.processEvents()
        self.directory.cleanup()

    def test_controls_create_shared_axis_rows_alignment_and_packing(self):
        self.dialog.constraint_instances.setText("M1, M2; M3, M4")
        self.dialog._add_intent()
        self.assertEqual(self.dialog.project.constraints[0]["pairs"], [["M1", "M2"], ["M3", "M4"]])
        for kind, names in (("Relative order", "M1,M2"), ("Align", "M1,M2"),
                            ("Row", "M1,M2"), ("Rows / regions", "M1,M2;M3,M4"),
                            ("Packing", "")):
            self.dialog.constraint_kind.setCurrentText(kind)
            self.dialog.constraint_instances.setText(names)
            self.dialog._add_intent()
        self.dialog._collect_project().validate()
        self.assertEqual(self.dialog.project.constraints[-1],
                         {"constraint": "CompactPlacement", "style": "left"})
        self.assertEqual(self.dialog.project.constraints[-2]["regions"], [["M1", "M2"], ["M3", "M4"]])

    def test_explicit_snapshot_mapping_and_total_width_conversion(self):
        original = self.design.to_state()
        self.dialog._read_roar()
        self.design.set_instance_attribute("M1_2", "W", 100e-6)
        self.dialog.import_targets.setText("M1, M2")
        self.dialog._apply_roar()
        self.assertEqual([d.width_um for d in self.dialog.project.devices], [2, 2, 2, 2])
        self.assertEqual([d.nf for d in self.dialog.project.devices], [4, 4, 2, 2])
        for device in self.dialog.project.devices[:2]:
            self.assertAlmostEqual(device.length_um, 0.2)
        self.assertEqual(self.design.get_instance_parameters()["M1_2"]["W"]["value"], 100e-6)
        self.assertEqual(original["design_editor"]["instance_table"][0]["_attributes"]["W"]["resolved"], 8e-6)

    def test_invalid_cell_and_unknown_mapping_do_not_mutate_project(self):
        original = self.dialog.project.to_dict()
        self.dialog.devices_table.item(0, 3).setText("7")
        self.dialog.devices_table.item(1, 5).setText("3")
        with self.assertRaisesRegex(ValueError, "even integer"):
            self.dialog._collect_project()
        self.assertEqual(self.dialog.project.to_dict(), original)
        self.dialog._render()
        self.dialog._read_roar()
        self.dialog.import_targets.setText("M1, NOT_A_DEVICE")
        with self.assertRaises(ValueError):
            self.dialog._apply_roar()
        self.assertEqual(self.dialog.project.to_dict(), original)

    def test_failed_refresh_invalidates_old_snapshot_and_errors_are_visible(self):
        self.dialog._read_roar()
        def fail():
            raise RuntimeError("ROAR snapshot is unavailable")
        self.dialog.snapshot_provider = fail
        with patch.object(QMessageBox, "warning") as warning:
            self.dialog._guard(self.dialog._read_roar)
        warning.assert_called_once()
        self.assertIn("snapshot is unavailable", self.dialog.status.text())
        self.assertIsNone(self.dialog.roar_design)
        self.assertEqual(self.dialog.source_instance.count(), 0)

    def test_netlist_import_save_open_and_align_export_paths(self):
        source = self.root / "input.sp"
        source.write_text(NETLIST.replace("W=2u", "W=8").replace("L=0.15u", "L=0.2"))
        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(source), "")), \
             patch.object(QInputDialog, "getText", return_value=("pair", True)), \
             patch.object(QInputDialog, "getItem", side_effect=[("Micrometres (µm)", True), ("Total width (divide by NF)", True)]):
            self.dialog._load_netlist()
        self.assertEqual(self.dialog.project.devices[0].width_um, 4)
        with patch.object(QFileDialog, "getSaveFileName", return_value=(str(source), "")):
            with self.assertRaisesRegex(ValueError, "cannot overwrite the source"):
                self.dialog._export_constraints()
        self.assertIn(".subckt", source.read_text())
        self.dialog.constraint_instances.setText("M1, M2")
        self.dialog._add_intent()
        project_path = self.root / "saved.floorplan.json"
        with patch.object(QFileDialog, "getSaveFileName", return_value=(str(project_path), "")):
            self.dialog._save_project()
        self.assertFalse(self.dialog._dirty)
        self.dialog.devices_table.item(0, 3).setText("99")
        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(project_path), "")), \
             patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Discard):
            self.dialog._open_project()
        self.assertEqual(self.dialog.devices_table.item(0, 3).text(), "4.0")
        with patch.object(QFileDialog, "getExistingDirectory", return_value=str(self.root)):
            self.dialog._export_align()
        self.assertTrue((self.root / "pair" / "pair.sp").exists())
        self.assertEqual(json.loads((self.root / "pair" / "pair.const.json").read_text()), self.dialog.project.constraints)
        with patch.object(QFileDialog, "getExistingDirectory", return_value=str(self.root)), \
             patch.object(QMessageBox, "warning") as warning:
            self.dialog._guard(self.dialog._export_align)
        warning.assert_called_once()
        self.assertIn("File exists", self.dialog.status.text())

    def test_constraints_import_can_replace_invalid_editor_and_export(self):
        native = [{"constraint": "Order", "instances": ["M2", "M1"], "direction": "left_to_right"}]
        source = self.root / "constraints.json"
        source.write_text(json.dumps(native))
        self.dialog.constraints_json.setPlainText("unfinished JSON")
        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(source), "")):
            self.dialog._import_constraints()
        target = self.root / "out.json"
        with patch.object(QFileDialog, "getSaveFileName", return_value=(str(target), "")):
            self.dialog._export_constraints()
        self.assertEqual(json.loads(target.read_text()), native)

    def test_unsupported_imported_constraint_survives_project_save(self):
        self.dialog.constraints_json.setPlainText('[{"constraint": "FutureConstraint", "value": 1}]')
        target = self.root / "future.floorplan.json"
        with patch.object(QFileDialog, "getSaveFileName", return_value=(str(target), "")):
            self.dialog._save_project()
        restored = FloorplanProject.load(target)
        self.assertEqual(restored.constraints, [{"constraint": "FutureConstraint", "value": 1}])
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            restored.validate()

    def test_close_reopen_preserves_project_and_parent(self):
        self.dialog.show()
        self.dialog.devices_table.item(0, 3).setText("6")
        self.dialog.close()
        self.dialog.show()
        self.app.processEvents()
        self.assertEqual(self.dialog.devices_table.item(0, 3).text(), "6")
        self.assertIs(self.dialog.parent(), self.parent)


if __name__ == "__main__":
    unittest.main()
