"""Qt smoke tests; run with QT_QPA_PLATFORM=offscreen and the GUI extra."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    from PyQt6.QtCore import QTimer, Qt
    from PyQt6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QFileDialog, QMessageBox, QPushButton, QWidget
except ImportError:
    QApplication = None

from roar_api import Design
from roar_api.floorplan import FloorplanProject

if QApplication is not None:
    from roar_api.floorplan.dialog import FloorplanDialog, NetlistImportDialog, OpenFloorplanDialog, load_roar_design
    from roar_api.floorplan.constraint_editor import ConstraintEditorDialog, editable_constraint, constraint_summary


NETLIST = """.subckt pair d g s b
M1 d g s b nch W=2u L=0.15u nf=2
M2 d g s b nch W=2u L=0.15u nf=2
M3 d g s b nch W=2u L=0.15u nf=2
M4 d g s b nch W=2u L=0.15u nf=2
.ends pair
"""

SYMBOLIC = """.subckt shared d g s b
M1 d g s b nch W=ws L=0.15u nf=nfs
M2 d g s b nch W=ws L=0.15u nf=nfs
.ends shared
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

    def test_constraints_have_readable_rows_and_typed_add_for_every_kind(self):
        for kind in ("SymmetricBlocks", "Order", "Align", "Floorplan", "CompactPlacement", "PlaceCloser", "PowerPorts", "GroundPorts"):
            def accept_constraint(kind=kind):
                editor = QApplication.activeModalWidget()
                editor.kind.setCurrentIndex(editor.kind.findData(kind))
                if kind == "SymmetricBlocks":
                    for column, name in enumerate(("M1", "M2")):
                        choice = editor.pairs_editor.table.cellWidget(0, column)
                        choice.setCurrentIndex(choice.findData(name))
                elif kind == "Floorplan":
                    editor.regions_editor.devices.set_names(["M1", "M2"])
                    editor.regions_editor.add_row(["M3", "M4"])
                    editor.flags["order"][0].setChecked(True)
                    editor.flags["symmetrize"][0].setChecked(True)
                elif kind == "CompactPlacement":
                    editor.option.setCurrentIndex(editor.option.findData("center"))
                else:
                    editor.names_editor.set_names(["d", "s"] if kind in ("PowerPorts", "GroundPorts") else ["M2", "M1"])
                editor.buttons.button(QDialogButtonBox.StandardButton.Ok).click()
            QTimer.singleShot(0, accept_constraint)
            self.dialog._add_constraint()
        self.assertEqual(self.dialog.constraints_table.rowCount(), 8)
        self.assertEqual(self.dialog.project.constraints[0]["pairs"], [["M1", "M2"]])
        self.assertEqual(self.dialog.project.constraints[3]["regions"], [["M1", "M2"], ["M3", "M4"]])
        self.assertEqual(self.dialog.constraints_table.item(6, 0).text(), "Power ports")
        self.assertIn("first grid port: d", self.dialog.constraints_table.item(6, 2).text())
        self.assertFalse(hasattr(self.dialog, "constraints_json"))
        self.assertFalse(hasattr(self.dialog, "_export_constraints"))
        self.dialog._collect_project().validate()

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
        self.dialog.project.set_constraints([{
            "constraint": "SymmetricBlocks", "pairs": [["M1", "M2"]], "direction": "V"}])
        self.dialog._render_constraints()
        project_path = self.root / "saved.floorplan.json"
        with patch.object(QFileDialog, "getSaveFileName", return_value=(str(project_path), "")):
            self.dialog._save_project()
        self.assertFalse(self.dialog._dirty)
        self.dialog.devices_table.item(0, 3).setText("99")
        def open_saved():
            options = QApplication.activeModalWidget()
            options.floorplan_path.setText(str(project_path))
            options.floorplan_path.editingFinished.emit()
            options.buttons.button(QDialogButtonBox.StandardButton.Open).click()
        QTimer.singleShot(0, open_saved)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Discard):
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

    def test_load_align_constraints_add_replace_cancel_and_wrong_file_are_safe(self):
        initial = {"constraint": "Order", "instances": ["M1", "M2"], "direction": "left_to_right", "abut": True}
        loaded = [{"constraint": "PowerPorts", "ports": ["d", "s"], "propagate": False},
                  {"constraint": "FutureRule", "opaque": {"preserve": [1, 2]}}]
        self.dialog.project.constraints = [initial]
        self.dialog._render_constraints()
        source = self.root / "rules.const.json"
        source.write_text(json.dumps(loaded))
        for action, expected in (("Add to existing", [initial] + loaded), ("Replace existing", loaded)):
            previous = self.dialog.project.to_dict()
            def choose_action(action=action):
                question = QApplication.activeModalWidget()
                next(button for button in question.buttons() if button.text() == action).click()
            QTimer.singleShot(0, choose_action)
            with patch.object(QFileDialog, "getOpenFileName", return_value=(str(source), "")):
                self.dialog._import_constraints()
            self.assertEqual(self.dialog.project.constraints, expected)
            current = self.dialog.project.to_dict()
            self.assertEqual({key: value for key, value in current.items() if key != "constraints"},
                             {key: value for key, value in previous.items() if key != "constraints"})
        original = self.dialog.project
        QTimer.singleShot(0, lambda: QApplication.activeModalWidget().button(QMessageBox.StandardButton.Cancel).click())
        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(source), "")):
            self.dialog._import_constraints()
        self.assertIs(self.dialog.project, original)
        sidecar = self.root / "whole.roar.floorplan"
        self.dialog.project.save(sidecar)
        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(sidecar), "")):
            with self.assertRaisesRegex(ValueError, "Use Open floorplan"):
                self.dialog._import_constraints()
        design = self.root / "source.roar"
        self.design.save(design)
        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(design), "")):
            with self.assertRaisesRegex(ValueError, "placement rules"):
                self.dialog._import_constraints()
        self.assertIs(self.dialog.project, original)

    def test_opaque_rows_survive_supported_edit_cancel_remove_and_save(self):
        opaque = {"constraint": "FutureConstraint", "value": {"nested": [1, "keep"]}}
        extra = {"constraint": "Order", "instances": ["M1", "M2"], "direction": "left_to_right", "new_flag": 3}
        supported = {"constraint": "Order", "instances": ["M2", "M1"], "direction": "left_to_right", "abut": True}
        self.dialog.project.constraints = [opaque, extra, supported]
        self.dialog._render_constraints()
        self.dialog.constraints_table.selectRow(0)
        self.assertFalse(self.dialog.edit_constraint_button.isEnabled())
        self.dialog.constraints_table.selectRow(1)
        self.assertFalse(self.dialog.edit_constraint_button.isEnabled())
        self.dialog.constraints_table.selectRow(2)
        original = self.dialog.project.to_dict()
        def cancel_edit():
            editor = QApplication.activeModalWidget()
            editor.option.setCurrentIndex(editor.option.findData("top_to_bottom"))
            editor.reject()
        QTimer.singleShot(0, cancel_edit)
        self.dialog._edit_constraint()
        self.assertEqual(self.dialog.project.to_dict(), original)
        def accept_edit():
            editor = QApplication.activeModalWidget()
            editor.option.setCurrentIndex(editor.option.findData("top_to_bottom"))
            editor.accept()
        QTimer.singleShot(0, accept_edit)
        self.dialog.constraints_table.cellDoubleClicked.emit(2, 0)
        self.assertEqual(self.dialog.project.constraints[:2], [opaque, extra])
        self.assertEqual(self.dialog.project.constraints[2], dict(supported, direction="top_to_bottom"))
        self.dialog._remove_constraint()
        self.assertEqual(self.dialog.project.constraints, [opaque, extra])
        target = self.root / "preserved.roar.floorplan"
        with patch.object(QFileDialog, "getSaveFileName", return_value=(str(target), "")):
            self.dialog._save_project()
        self.assertEqual(FloorplanProject.load(target).constraints, [opaque, extra])
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            self.dialog._export_align()

    def test_typed_editor_roundtrips_native_defaults_optional_flags_and_order(self):
        native = [
            {"constraint": "SymmetricBlocks", "pairs": [["M2", "M1"], ["M3"]], "direction": "H"},
            {"constraint": "Order", "instances": ["M2", "M1"], "direction": "top_to_bottom"},
            {"constraint": "Order", "instances": ["M2", "M1"], "direction": "left_to_right", "abut": False},
            {"constraint": "Align", "instances": ["M1", "M2"], "line": "v_right"},
            {"constraint": "Floorplan", "regions": [["M2", "M1"], ["M3", "M4"]], "order": False, "symmetrize": True},
            {"constraint": "CompactPlacement"},
            {"constraint": "CompactPlacement", "style": "right"},
            {"constraint": "PlaceCloser", "instances": ["M3", "M1"]},
            {"constraint": "PowerPorts", "ports": ["s", "d"]},
            {"constraint": "GroundPorts", "ports": ["b", "s"], "propagate": False},
        ]
        for constraint in native:
            with self.subTest(constraint=constraint):
                editor = ConstraintEditorDialog(self.dialog.project, constraint, parent=self.parent)
                self.assertEqual(editor.value(), constraint)
                editor.accept()
                self.assertEqual(editor.result(), QDialog.DialogCode.Accepted)
                self.assertEqual(editor.constraint, constraint)
                editor.deleteLater()
        for invalid in ({"constraint": "Order", "instances": ["M1", "M2"], "direction": "right_to_left"},
                        {"constraint": "SymmetricBlocks", "pairs": [["M1", "M2"]]},
                        {"constraint": ["opaque"], "keep": 1}):
            self.assertFalse(editable_constraint(invalid))
            self.assertIn("preserved", constraint_summary(invalid)[2])

    def test_region_device_changes_and_order_are_explicit_without_json(self):
        editor = ConstraintEditorDialog(self.dialog.project, parent=self.parent)
        editor.kind.setCurrentIndex(editor.kind.findData("Floorplan"))
        regions = editor.regions_editor
        self.assertEqual(regions.regions(), [[]])
        regions.devices.set_names(["M1", "M2"])
        regions.add_row(["M3", "M4"])
        regions.rows.setCurrentRow(0)
        regions.devices.items.setCurrentRow(1)
        regions.devices.move(-1)
        self.assertEqual(regions.regions(), [["M2", "M1"], ["M3", "M4"]])
        regions.move(1)
        self.assertEqual(regions.regions(), [["M3", "M4"], ["M2", "M1"]])
        editor.accept()
        self.assertEqual(editor.result(), QDialog.DialogCode.Accepted)
        editor.deleteLater()

    def test_floorplan_names_filters_default_extension_and_source_protection(self):
        labels = [button.text() for button in self.dialog.findChildren(QPushButton)]
        self.assertIn("New floorplan…", labels)
        self.assertIn("Open floorplan…", labels)
        target = self.root / "no_extension"
        with patch.object(QFileDialog, "getSaveFileName", return_value=(str(target), "")) as picker:
            self.dialog._save_project()
        self.assertEqual(picker.call_args.args[2], "pair.roar.floorplan")
        self.assertTrue(Path(str(target) + ".roar.floorplan").exists())
        legacy = self.root / "legacy.floorplan.json"
        self.dialog.project.save(legacy)
        options = OpenFloorplanDialog(self.parent)
        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(legacy), "")) as picker:
            options._browse_floorplan()
        self.assertEqual(picker.call_args.args[1], "Choose Floorplan")
        self.assertIn("*.roar.floorplan", picker.call_args.args[3])
        self.assertIn("*.floorplan.json", picker.call_args.args[3])
        options.accept()
        self.assertEqual(options.result(), QDialog.DialogCode.Accepted)
        options.deleteLater()
        source = self.root / "source.roar"
        self.design.save(source)
        original = source.read_bytes()
        with patch.object(QFileDialog, "getSaveFileName", return_value=(str(source), "")):
            with self.assertRaisesRegex(ValueError, "cannot overwrite a ROAR project"):
                self.dialog._save_project(save_as=True)
        self.assertEqual(source.read_bytes(), original)

    def test_close_reopen_preserves_project_and_parent(self):
        self.dialog.show()
        self.dialog.devices_table.item(0, 3).setText("6")
        self.dialog.close()
        self.dialog.show()
        self.app.processEvents()
        self.assertEqual(self.dialog.devices_table.item(0, 3).text(), "6")
        self.assertIs(self.dialog.parent(), self.parent)

    def test_open_dialog_follows_suggestions_but_preserves_explicit_override(self):
        first_design = self.root / "first.roar"
        second_design = self.root / "second.roar"
        self.design.save(first_design)
        self.design.save(second_design)
        project = FloorplanProject.from_text(NETLIST)
        first = self.root / "first.floorplan.json"
        second = self.root / "second.floorplan.json"
        project.roar_project_path = first_design
        project.save(first)
        project.roar_project_path = second_design
        project.save(second)
        options = OpenFloorplanDialog(self.parent, first)
        self.assertEqual(options.design_path.text(), str(first_design))
        with patch.object(FloorplanProject, "load", wraps=FloorplanProject.load) as loader:
            options.floorplan_path.setText(str(second))
            loader.assert_not_called()
            self.assertFalse(options.buttons.button(QDialogButtonBox.StandardButton.Open).isEnabled())
            options.floorplan_path.editingFinished.emit()
            loader.assert_called_once()
        self.assertEqual(options.design_path.text(), str(second_design))
        options.design_path.setText(str(first_design))
        options.design_path.textEdited.emit(str(first_design))
        options.floorplan_path.setText(str(first))
        options.floorplan_path.editingFinished.emit()
        options.floorplan_path.setText(str(second))
        options.floorplan_path.editingFinished.emit()
        self.assertEqual(options.design_path.text(), str(first_design))
        self.assertIn("Using your ROAR choice", options.link_status.text())
        QTimer.singleShot(0, lambda: options.buttons.button(QDialogButtonBox.StandardButton.Open).click())
        self.assertEqual(options.exec(), QDialog.DialogCode.Accepted)
        self.assertEqual(options.project.roar_project_path, str(first_design))
        options.deleteLater()

    def test_missing_link_opens_saved_sizes_without_losing_association(self):
        project = FloorplanProject.from_text(NETLIST)
        missing = self.root / "missing.roar"
        project.roar_project_path = missing
        sidecar = self.root / "missing.floorplan.json"
        project.save(sidecar)
        options = OpenFloorplanDialog(self.parent, sidecar)
        self.assertTrue(options.without_design.isChecked())
        self.assertIn("missing", options.link_status.text())
        self.assertTrue(options.buttons.button(QDialogButtonBox.StandardButton.Open).isEnabled())
        QTimer.singleShot(0, lambda: options.buttons.button(QDialogButtonBox.StandardButton.Open).click())
        self.assertEqual(options.exec(), QDialog.DialogCode.Accepted)
        self.assertIsNone(options.design)
        self.assertEqual(options.project.roar_project_path, str(missing))
        self.dialog.set_project(options.project, sidecar, options.design)
        self.assertFalse(self.dialog.refresh_bindings_button.isEnabled())
        self.assertEqual(self.dialog.project.devices[0].width_um, 2)
        options.deleteLater()

    def test_wrong_roar_file_is_rejected_inline_and_open_cancel_is_transactional(self):
        sidecar = self.root / "existing.floorplan.json"
        self.dialog.project.save(sidecar)
        options = OpenFloorplanDialog(self.parent, sidecar)
        options.design_path.setText(str(sidecar))
        options.design_path.textEdited.emit(str(sidecar))
        options.accept()
        self.assertEqual(options.result(), QDialog.DialogCode.Rejected)
        self.assertIn("Choose a ROAR design", options.link_status.text())
        self.assertIsNone(options.project)
        with self.assertRaisesRegex(ValueError, "Choose a ROAR design"):
            load_roar_design(sidecar)
        options.deleteLater()
        self.dialog._read_roar()
        original_project, original_source = self.dialog.project, self.dialog.roar_design
        def cancel_open():
            modal = QApplication.activeModalWidget()
            modal.floorplan_path.setText(str(sidecar))
            modal.floorplan_path.editingFinished.emit()
            modal.buttons.button(QDialogButtonBox.StandardButton.Cancel).click()
        QTimer.singleShot(0, cancel_open)
        self.dialog._open_project()
        self.assertIs(self.dialog.project, original_project)
        self.assertIs(self.dialog.roar_design, original_source)

    def test_parameter_fields_are_shared_constant_fields_remain_editable_and_draft_saves(self):
        self.dialog.set_project(FloorplanProject.from_text(SYMBOLIC, dimension_unit="um"))
        self.assertEqual(self.dialog.project.unresolved_parameters, ["ws", "nfs"])
        self.assertFalse(self.dialog.devices_table.item(0, 3).flags() & Qt.ItemFlag.ItemIsEditable)
        self.assertFalse(self.dialog.devices_table.item(0, 5).flags() & Qt.ItemFlag.ItemIsEditable)
        self.assertTrue(self.dialog.devices_table.item(0, 4).flags() & Qt.ItemFlag.ItemIsEditable)
        draft = self.root / "draft.floorplan.json"
        with patch.object(QFileDialog, "getSaveFileName", return_value=(str(draft), "")):
            self.dialog._save_project()
        self.assertEqual(FloorplanProject.load(draft).unresolved_parameters, ["ws", "nfs"])
        with self.assertRaisesRegex(ValueError, "[Uu]nresolved"):
            self.dialog._export_align()
        self.dialog.devices_table.item(0, 4).setText("0.25")
        self.dialog.parameter_symbol.setCurrentText("nfs")
        self.dialog.parameter_value.setText("4")
        self.dialog._set_parameter()
        self.dialog.parameter_symbol.setCurrentText("ws")
        self.dialog.parameter_value.setText("8u")
        self.dialog.parameter_unit.setCurrentIndex(self.dialog.parameter_unit.findData("um"))
        self.dialog.parameter_width.setCurrentIndex(self.dialog.parameter_width.findData("total"))
        self.dialog._set_parameter()
        self.assertEqual([d.width_um for d in self.dialog.project.devices], [2, 2])
        self.assertEqual([d.length_um for d in self.dialog.project.devices], [0.25, 0.15])
        with patch.object(QFileDialog, "getSaveFileName") as picker:
            self.dialog._save_project()
        picker.assert_not_called()

    def test_saved_source_open_keeps_captured_values_until_explicit_refresh(self):
        saved_design = self.root / "sizes.roar"
        self.design.save(saved_design)
        original_running = self.design.to_state()
        project = FloorplanProject.from_text(SYMBOLIC)
        project.roar_project_path = saved_design
        project.set_parameter("nfs", 4)
        project.bind_parameter(self.design, "ws", "M1_2", "W", source="linked")
        sidecar = self.root / "bound.floorplan.json"
        project.save(sidecar)
        changed = Design.from_state(self.design.to_state()).set_instance_attribute("M1_2", "W", 20e-6)
        changed.save(saved_design)
        source_bytes = saved_design.read_bytes()
        def open_saved():
            modal = QApplication.activeModalWidget()
            modal.floorplan_path.setText(str(sidecar))
            modal.floorplan_path.editingFinished.emit()
            modal.buttons.button(QDialogButtonBox.StandardButton.Open).click()
        QTimer.singleShot(0, open_saved)
        self.dialog._open_project()
        self.assertEqual(self.dialog._source_origin, "linked")
        self.assertEqual(self.dialog.project.devices[0].width_um, 2)
        self.dialog._refresh_bindings()
        self.assertEqual(self.dialog.project.devices[0].width_um, 5)
        self.assertEqual(self.design.to_state(), original_running)
        self.assertEqual(saved_design.read_bytes(), source_bytes)
        before = self.dialog.project.to_dict()
        saved_design.unlink()
        with self.assertRaises(OSError):
            self.dialog._refresh_bindings()
        self.assertIsNone(self.dialog.roar_design)
        self.assertFalse(self.dialog.refresh_bindings_button.isEnabled())
        self.assertEqual(self.dialog.project.to_dict(), before)

    def test_parameter_binding_uses_explicit_attribute_and_preserves_captured_source(self):
        self.dialog.set_project(FloorplanProject.from_text(SYMBOLIC))
        self.dialog._read_roar()
        self.dialog.parameter_symbol.setCurrentText("nfs")
        self.dialog.binding_attribute.setCurrentIndex(self.dialog.binding_attribute.findData("NF"))
        self.dialog._bind_parameter()
        self.dialog.parameter_symbol.setCurrentText("ws")
        self.dialog.binding_attribute.setCurrentIndex(self.dialog.binding_attribute.findData("W"))
        self.dialog._bind_parameter()
        self.assertEqual(self.dialog.project.devices[0].width_um, 2)
        self.assertEqual(self.dialog.project.parameter_rows[0]["binding"],
                         {"instance": "M1_2", "attribute": "W", "source": "live"})
        self.dialog.parameter_value.setText("12e-6")
        self.dialog._set_parameter()
        self.assertIsNone(self.dialog.project.parameter_rows[0]["binding"])
        self.assertEqual(self.dialog.project.devices[0].width_um, 3)

    def test_numeric_parameter_file_uses_own_options_even_when_count_selected(self):
        self.dialog.set_project(FloorplanProject.from_text(SYMBOLIC))
        self.dialog.parameter_symbol.setCurrentText("nfs")
        self.assertFalse(self.dialog.parameter_unit.isEnabled())
        parameters = self.root / "sizes.param"
        parameters.write_text(".param ws=8 nfs=4 unused=1\n")
        def accept_parameters():
            modal = QApplication.activeModalWidget()
            modal.dimension_unit.setCurrentIndex(modal.dimension_unit.findData("um"))
            modal.width_convention.setCurrentIndex(modal.width_convention.findData("total"))
            modal.buttons.button(QDialogButtonBox.StandardButton.Ok).click()
        QTimer.singleShot(0, accept_parameters)
        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(parameters), "")):
            self.dialog._import_parameters()
        self.assertEqual([d.width_um for d in self.dialog.project.devices], [2, 2])
        self.assertIn("Unused names: unused", self.dialog.status.text())

    def test_creation_can_associate_saved_design_without_changing_current_roar(self):
        source = self.root / "shared.sp"
        source.write_text(SYMBOLIC)
        saved_design = self.root / "source.roar"
        self.design.save(saved_design)
        original = self.design.to_state()
        def accept_import():
            modal = QApplication.activeModalWidget()
            modal.source_mode.setCurrentIndex(modal.source_mode.findData("linked"))
            modal.design_path.setText(str(saved_design))
            modal.buttons.button(QDialogButtonBox.StandardButton.Ok).click()
        QTimer.singleShot(0, accept_import)
        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(source), "")):
            self.dialog._load_netlist()
        self.assertEqual(self.dialog.project.roar_project_path, str(saved_design))
        self.assertEqual(self.dialog._source_origin, "linked")
        self.assertEqual(self.dialog.project.unresolved_parameters, ["ws", "nfs"])
        self.assertEqual(self.design.to_state(), original)


if __name__ == "__main__":
    unittest.main()
