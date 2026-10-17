"""Qt smoke tests; run with QT_QPA_PLATFORM=offscreen and the GUI extra."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QFileDialog, QMessageBox, QWidget
except ImportError:
    QApplication = None

from roar_api import Design
from roar_api.floorplan import FloorplanProject

if QApplication is not None:
    from roar_api.floorplan.dialog import FloorplanDialog, NetlistImportDialog


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
        examples = self.root / "design" / "floorplan"
        examples.mkdir(parents=True)
        def accept_import():
            options = QApplication.activeModalWidget()
            options.width_convention.setCurrentIndex(options.width_convention.findData("total"))
            options.buttons.button(QDialogButtonBox.StandardButton.Ok).click()
        QTimer.singleShot(0, accept_import)
        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(source), "")) as picker, \
             patch.dict(os.environ, {"ROAR_DESIGN": str(examples.parent)}):
            self.dialog._load_netlist()
        self.assertEqual(picker.call_args.args[2], str(examples))
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

    def test_unified_import_dialog_selects_sole_circuit_and_combines_options(self):
        source = self.root / "commented.spice"
        source.write_text(NETLIST.replace(".subckt", "**.subckt").replace(".ends", "**.ends"))
        options = NetlistImportDialog(source, self.parent)
        self.assertEqual(options.circuit_combo.count(), 1)
        self.assertEqual(options.circuit_combo.currentData(), "pair")
        self.assertEqual(options.dimension_unit.currentData(), "um")
        self.assertEqual(options.width_convention.currentData(), "per_finger")
        QTimer.singleShot(0, lambda: options.buttons.button(QDialogButtonBox.StandardButton.Ok).click())
        self.assertEqual(options.exec(), QDialog.DialogCode.Accepted)
        options.deleteLater()

    def test_multiple_circuits_require_selection_and_metres_import_correctly(self):
        source = self.root / "multiple.sp"
        si_circuit = NETLIST.replace("pair", "second").replace("W=2u", "W=8e-6").replace("L=0.15u", "L=0.2e-6")
        source.write_text(NETLIST + si_circuit)
        options = NetlistImportDialog(source, self.parent)
        self.assertEqual([options.circuit_combo.itemData(i) for i in range(options.circuit_combo.count())],
                         [None, "pair", "second"])
        self.assertFalse(options.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled())
        options.deleteLater()
        def accept_import():
            modal = QApplication.activeModalWidget()
            modal.circuit_combo.setCurrentIndex(modal.circuit_combo.findData("second"))
            modal.dimension_unit.setCurrentIndex(modal.dimension_unit.findData("m"))
            modal.width_convention.setCurrentIndex(modal.width_convention.findData("total"))
            modal.buttons.button(QDialogButtonBox.StandardButton.Ok).click()
        QTimer.singleShot(0, accept_import)
        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(source), "")):
            self.dialog._load_netlist()
        self.assertEqual(self.dialog.project.top, "second")
        self.assertEqual(self.dialog.project.devices[0].width_um, 4)
        self.assertAlmostEqual(self.dialog.project.devices[0].length_um, 0.2)

    def test_cancelling_unified_import_leaves_current_project_unchanged(self):
        source = self.root / "cancel.sp"
        source.write_text(NETLIST)
        original = self.dialog.project
        QTimer.singleShot(0, lambda: QApplication.activeModalWidget().buttons.button(QDialogButtonBox.StandardButton.Cancel).click())
        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(source), "")):
            self.dialog._load_netlist()
        self.assertIs(self.dialog.project, original)
        self.assertFalse(self.dialog._dirty)

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
