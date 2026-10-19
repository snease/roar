"""Qt smoke tests; run with QT_QPA_PLATFORM=offscreen and the GUI extra."""

import json
import os
from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

try:
    from PyQt6.QtCore import QSettings, Qt, QObject, pyqtSignal
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication, QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QMessageBox, QPushButton, QWidget
except ModuleNotFoundError as exc:
    if exc.name != "PyQt6":
        raise
    QApplication = None

from roar_api import Design
from roar_api.floorplan import FloorplanProject
from roar_api.floorplan.workspace import DesignWorkspace

if QApplication is not None:
    from qt_test_helpers import modal_interaction
    from roar_api.floorplan.dialog import FloorplanDialog, NetlistImportDialog, OpenFloorplanDialog, load_roar_design
    from roar_api.floorplan.constraint_editor import ConstraintEditorDialog, editable_constraint, constraint_summary
    from roar_api.floorplan.align_runner import AlignConfig


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
            self.parent, lambda: Design.from_state(self.design.to_state()),
            placement_options={
                "settings": QSettings(str(self.root / "align.ini"), QSettings.Format.IniFormat),
                "config": AlignConfig("", "", str(self.root / "align-runs"))})
        self.dialog.set_project(FloorplanProject.from_text(NETLIST))

    def tearDown(self):
        self.dialog._dirty = False  # Tests explicitly cover navigation prompts.
        self.dialog.close()
        self.parent.deleteLater()
        self.app.processEvents()
        self.directory.cleanup()

    def select_source(self, mode, dialog=None):
        dialog = self.dialog if dialog is None else dialog
        index = dialog.source_mode.findData(mode)
        dialog.source_mode.setCurrentIndex(index)
        dialog.source_mode.activated.emit(index)

    def test_preview_actions_follow_invalid_inputs_and_active_run_state(self):
        from roar_api.floorplan.align_runner import RunSnapshot, RunState
        placement, preview = self.dialog.placement_panel, self.dialog.preview_panel
        runner = Mock()
        runner.snapshot.return_value = RunSnapshot()
        with patch.object(placement, '_ensure_runner', return_value=runner), \
             patch.object(placement, '_validator', return_value=None):
            placement.refresh()
            self.assertTrue(preview.generate_button.isEnabled())
            self.dialog.devices_table.item(0, 3).setText('unfinished')
            placement.refresh()
            self.assertFalse(preview.generate_button.isEnabled())
            self.assertTrue(preview.generate_button.toolTip())
            runner.snapshot.return_value = RunSnapshot(current=RunState(
                str(self.root / 'run'), 'starting', None, None, 'align.log', 'fingerprint'))
            placement.refresh()
            self.assertTrue(preview.generate_button.isEnabled())
            self.assertEqual(preview.generate_button.text(), 'Show active ALIGN run…')
            self.assertFalse(preview.settings_button.isEnabled())
            runner.snapshot.return_value = RunSnapshot()
            placement.refresh()
            self.assertFalse(preview.generate_button.isEnabled())
            self.assertTrue(preview.settings_button.isEnabled())
            for name in ('generate_button', 'settings_button'):
                source, target = getattr(placement, name), getattr(preview, name)
                self.assertEqual((source.isEnabled(), source.text(), source.toolTip()),
                                 (target.isEnabled(), target.text(), target.toolTip()))

    def workspace(self, design, path=None):
        editor = SimpleNamespace(design=design)
        editor.capture = Mock(wraps=lambda: editor.design)
        workspace = DesignWorkspace(
            capture_design=editor.capture, capture_state=lambda: editor.design.to_state(),
            apply_design=lambda design: setattr(editor, 'design', design))
        if path is not None:
            workspace.record(path)
        workspace.save = Mock(wraps=workspace.save)
        workspace.switch = Mock(wraps=workspace.switch)
        return workspace, editor

    def test_modal_callback_assertion_reports_failure_and_cancels_pending_interactions(self):
        calls = []

        class FailedInteraction(unittest.TestCase):
            def runTest(self):
                modal = QDialog()
                self.addCleanup(modal.deleteLater)

                def inspect():
                    calls.append('inspected')
                    self.fail('scheduled assertion reaches unittest')

                with modal_interaction(inspect):
                    self.assertEqual(modal.exec(), QDialog.DialogCode.Accepted)

        result = unittest.TestResult()
        FailedInteraction().run(result)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.errors, [])
        self.assertIn('scheduled assertion reaches unittest', result.failures[0][1])
        self.assertIsNone(QApplication.activeModalWidget())
        # A disabled opener must fail here and cancel its pending callback,
        # rather than letting it act on the next test's modal.
        with self.assertRaisesRegex(AssertionError, 'interaction did not run'):
            with modal_interaction(lambda: calls.append('leaked')):
                pass
        self.app.processEvents()
        self.assertEqual(calls, ['inspected'])

    def test_sketch_history_keeps_incomplete_cells_and_relationship_ids(self):
        dialog = self.dialog
        self.assertIs(dialog.tabs.currentWidget(), dialog.floorplan_views)
        original = dialog.project.sketch
        dialog.devices_table.item(0, 3).setText('unfinished')
        moved = dialog.project.sketch
        moved['positions']['M1'][0] += 40
        dialog._sketch_edited(moved)
        dialog._undo()
        self.assertEqual(dialog.project.sketch, original)
        self.assertEqual(dialog.devices_table.item(0, 3).text(), 'unfinished')
        dialog._redo()
        self.assertEqual(dialog.project.sketch, moved)
        self.assertEqual(dialog.devices_table.item(0, 3).text(), 'unfinished')
        dialog.devices_table.item(0, 3).setText('2')
        dialog._sketch_relationship({'constraint': 'Align', 'instances': ['M1', 'M2'], 'line': 'h_center'})
        identifier = dialog.project.constraint_ids[-1]
        aligned = dialog.project.sketch
        dialog._undo()
        self.assertFalse(dialog.project.constraints)
        dialog._redo()
        self.assertEqual(dialog.project.constraint_ids[-1], identifier)
        self.assertEqual(dialog.project.sketch, aligned)
        dialog.devices_table.item(0, 3).setText('3')
        dialog.devices_table.item(0, 3).setText('4')
        dialog._undo()
        self.assertIn('W 3 /', dialog.sketch_panel.items_by_name['M1'].detail)
        dialog._redo()
        self.assertIn('W 4 /', dialog.sketch_panel.items_by_name['M1'].detail)

    def test_failed_live_sizing_marks_physical_preview_stale_until_repaired(self):
        from roar_api.floorplan.align_runner import RunSnapshot, RunState, project_fingerprint
        from roar_api.floorplan.geometry import build_preview
        project = FloorplanProject.from_text(SYMBOLIC)
        project.set_parameter('nfs', 4)
        project.bind_parameter(self.design, 'ws', 'M1_2', 'W', source='live')
        self.dialog.set_project(project, design=self.design, source_origin='live')
        fixture = Path(__file__).parent / 'fixtures' / 'align098_ota'
        data = build_preview(fixture, json.loads((fixture / 'manifest.json').read_text()))
        (self.root / 'preview.json').write_text(json.dumps(data))
        state = RunState(str(self.root), 'completed', None, None, 'align.log',
                         project_fingerprint(project), has_preview=True)
        panel = self.dialog.placement_panel
        runner = panel._ensure_runner()
        with patch.object(runner, 'snapshot', return_value=RunSnapshot(preview=state)):
            panel.refresh()
            self.assertFalse(self.dialog.preview_panel.state.stale)
            self.design.set_instance_attribute('M1_2', 'W', -1)
            self.dialog._live_source_update()
            panel.refresh()
            self.assertTrue(self.dialog.preview_panel.state.stale)
            self.assertIn('Sizing source error', self.dialog.snapshot_label.text())
            self.design.set_instance_attribute('M1_2', 'W', 8e-6)
            self.dialog._live_source_update()
            panel.refresh()
            self.assertFalse(self.dialog.preview_panel.state.stale)

    def test_physical_selection_links_sources_without_editing_geometry(self):
        from roar_api.floorplan.geometry import build_preview
        fixture = Path(__file__).parent / 'fixtures' / 'align098_ota'
        project = FloorplanProject.from_text((fixture / 'input' / 'current_mirror_ota.sp').read_text())
        self.dialog.set_project(project)
        data = build_preview(fixture, json.loads((fixture / 'manifest.json').read_text()))
        self.dialog.placement_panel.previewReady.emit(data, None)
        panel = self.dialog.preview_panel
        group = next(iter(panel.items_by_id.values()))
        group.setSelected(True)
        names = set(group.block['source_instances'])
        self.assertEqual(set(self.dialog._selected_device_names()), names)
        self.assertEqual(set(self.dialog.sketch_panel.selected_names()), names)
        self.assertIn(next(iter(names)), self.dialog.connectivity.toPlainText())
        self.assertFalse(self.dialog._dirty)
        self.dialog.sketch_panel.set_selection([project.devices[0].name])
        self.assertEqual(self.dialog._selected_device_names(), [project.devices[0].name])
        self.assertIn(project.devices[0].name, panel.selected_names())
        self.assertEqual(panel.data, data)
        self.assertFalse(self.dialog._dirty)

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
                    editor.names_editor.set_names(["d"] if kind == "PowerPorts" else ["s"] if kind == "GroundPorts" else ["M1", "M2"])
                editor.buttons.button(QDialogButtonBox.StandardButton.Ok).click()
            with modal_interaction(accept_constraint):
                self.dialog._add_constraint()
        self.assertEqual(self.dialog.constraints_table.rowCount(), 8)
        self.assertEqual(self.dialog.project.constraints[0]["pairs"], [["M1", "M2"]])
        self.assertEqual(self.dialog.project.constraints[3]["regions"], [["M1", "M2"], ["M3", "M4"]])
        self.assertEqual(self.dialog.constraints_table.item(6, 0).text(), "Power ports")
        self.assertIn("first grid port: d", self.dialog.constraints_table.item(6, 2).text())
        self.dialog._collect_project().validate()

    def test_explicit_snapshot_mapping_and_total_width_conversion(self):
        original = self.design.to_state()
        self.select_source("live")
        self.design.set_instance_attribute("M1_2", "W", 100e-6)
        self.dialog.import_targets.setText("M1, M2")
        self.dialog._apply_roar()
        self.assertEqual([d.width_um for d in self.dialog.project.devices], [25, 25, 2, 2])
        self.assertEqual([d.nf for d in self.dialog.project.devices], [4, 4, 2, 2])
        for device in self.dialog.project.devices[:2]:
            self.assertAlmostEqual(device.length_um, 0.2)
        self.assertEqual(self.design.get_instance_parameters()["M1_2"]["W"]["value"], 100e-6)
        self.assertEqual(original["design_editor"]["instance_table"][0]["_attributes"]["W"]["resolved"], 8e-6)

    def test_invalid_cell_and_unknown_mapping_do_not_mutate_project(self):
        original = self.dialog.project.to_dict()
        self.dialog.devices_table.item(0, 3).setText("7")
        self.dialog.devices_table.item(1, 5).setText("three")
        with self.assertRaisesRegex(ValueError, "integer NF"):
            self.dialog._collect_project()
        self.assertEqual(self.dialog.project.to_dict(), original)
        self.dialog._render()
        self.select_source("live")
        original = self.dialog.project.to_dict()
        self.dialog.import_targets.setText("M1, NOT_A_DEVICE")
        with self.assertRaises(ValueError):
            self.dialog._apply_roar()
        self.assertEqual(self.dialog.project.to_dict(), original)

    def test_failed_refresh_preserves_snapshot_and_errors_are_visible(self):
        self.select_source("live")
        def fail():
            raise RuntimeError("ROAR snapshot is unavailable")
        original = self.dialog.roar_design
        self.dialog.snapshot_provider = fail
        with patch.object(QMessageBox, "warning") as warning:
            self.select_source("live")
        warning.assert_called_once()
        self.assertIn("snapshot is unavailable", self.dialog.status.text())
        self.assertIs(self.dialog.roar_design, original)
        self.assertIn("snapshot is unavailable", self.dialog.snapshot_label.text())

    def test_netlist_import_save_open_and_align_export_paths(self):
        source = self.root / "input.sp"
        source.write_text(NETLIST.replace("W=2u", "W=8").replace("L=0.15u", "L=0.2"))
        examples = self.root / "design" / "floorplan"
        examples.mkdir(parents=True)
        def accept_import():
            options = QApplication.activeModalWidget()
            options.width_convention.setCurrentIndex(options.width_convention.findData("total"))
            options.buttons.button(QDialogButtonBox.StandardButton.Ok).click()
        with modal_interaction(accept_import):
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
        with modal_interaction(open_saved):
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

    def test_handoff_uses_displayed_sketch_and_optional_selected_candidate(self):
        from roar_api.floorplan.geometry import build_preview
        fixture = Path(__file__).parent / 'fixtures' / 'align098_ota'
        preview = build_preview(fixture, json.loads((fixture / 'manifest.json').read_text()))
        second = deepcopy(preview['candidates'][0])
        second['name'] = 'SECOND_CANDIDATE'
        preview['candidates'].append(second)
        state = SimpleNamespace(run_dir=str(self.root), stale=True)
        self.dialog.preview_panel.set_preview(preview, state)
        self.dialog.preview_panel.candidate_combo.setCurrentIndex(1)
        self.dialog.project.set_device_labels({'M1': 'Input pair'})
        before = self.dialog.project.to_dict()
        self.dialog.sketch_panel.items_by_name['M1'].setPos(375, 215)
        displayed = self.dialog.sketch_panel.display_positions()
        self.assertNotEqual(displayed, before['sketch']['positions'])
        for include_preview in (True, False):
            def choose(options):
                self.assertEqual(options.windowTitle(), 'Export handoff')
                checkbox = options.findChild(QCheckBox)
                self.assertTrue(checkbox.isEnabled())
                checkbox.setChecked(include_preview)
                return QDialog.DialogCode.Accepted
            with self.subTest(include_preview=include_preview), \
                 patch.object(QDialog, 'exec', new=choose), \
                 patch.object(QFileDialog, 'getSaveFileName', return_value=(str(self.root / 'review'), '')), \
                 patch('roar_api.floorplan.handoff.export_handoff') as export:
                self.dialog._export_handoff()
            export.assert_called_once()
            packaged, destination = export.call_args.args
            self.assertEqual(destination, str(self.root / 'review.zip'))
            self.assertEqual(packaged.sketch['positions'], displayed)
            self.assertEqual(packaged.device_labels, {'M1': 'Input pair'})
            self.assertEqual(self.dialog.project.to_dict(), before)
            self.assertIs(export.call_args.kwargs['preview'], self.dialog.preview_panel.data if include_preview else None)
            self.assertIs(export.call_args.kwargs['state'], state if include_preview else None)
            self.assertEqual(export.call_args.kwargs['candidate_index'], 1 if include_preview else 0)

    def test_handoff_cancellation_and_wrong_suffix_leave_destinations_untouched(self):
        destination = self.root / 'existing.roar'
        destination.write_bytes(b'existing design')
        for accept_options, path in ((False, str(destination)), (True, ''), (True, str(destination))):
            def choose(options):
                self.assertFalse(options.findChild(QCheckBox).isEnabled())
                return QDialog.DialogCode.Accepted if accept_options else QDialog.DialogCode.Rejected
            with self.subTest(accept_options=accept_options, path=path), \
                 patch.object(QDialog, 'exec', new=choose), \
                 patch.object(QFileDialog, 'getSaveFileName', return_value=(path, '')) as picker, \
                 patch('roar_api.floorplan.handoff.export_handoff') as export:
                if accept_options and path:
                    with self.assertRaisesRegex(ValueError, r'\.zip filename'):
                        self.dialog._export_handoff()
                else:
                    self.dialog._export_handoff()
                export.assert_not_called()
                if not accept_options:
                    picker.assert_not_called()
            self.assertEqual(destination.read_bytes(), b'existing design')

    def test_labeled_demo_reopens_captured_and_preserves_roles_when_linked(self):
        path = Path(__file__).resolve().parents[3] / 'examples/floorplan/ota/cm_ota.roar.floorplan'
        original = FloorplanProject.load(path)
        self.assertTrue(original.device_labels)
        options = OpenFloorplanDialog(self.parent, path)
        self.assertEqual(options.source_mode.currentData(), 'saved')
        options.accept()
        self.assertIsNone(options.design)
        self.assertEqual(options.project.device_labels, original.device_labels)
        self.assertEqual(options.project.devices, original.devices)
        options.deleteLater()
        options = OpenFloorplanDialog(self.parent, path)
        options.source_mode.setCurrentIndex(options.source_mode.findData('linked'))
        options.accept()
        self.assertEqual(options.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(options.source_origin, 'linked')
        self.assertEqual(options.project.device_labels, original.device_labels)
        self.assertEqual(options.project.devices, original.devices)
        options.deleteLater()

    def test_unified_import_dialog_selects_sole_circuit_and_combines_options(self):
        source = self.root / "commented.spice"
        source.write_text(NETLIST.replace(".subckt", "**.subckt").replace(".ends", "**.ends"))
        options = NetlistImportDialog(source, self.parent)
        self.assertEqual(options.circuit_combo.count(), 1)
        self.assertEqual(options.circuit_combo.currentData(), "pair")
        self.assertEqual(options.dimension_unit.currentData(), "um")
        self.assertEqual(options.width_convention.currentData(), "per_finger")
        with modal_interaction(lambda: options.buttons.button(QDialogButtonBox.StandardButton.Ok).click()):
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
        with modal_interaction(accept_import):
            with patch.object(QFileDialog, "getOpenFileName", return_value=(str(source), "")):
                self.dialog._load_netlist()
        self.assertEqual(self.dialog.project.top, "second")
        self.assertEqual(self.dialog.project.devices[0].width_um, 4)
        self.assertAlmostEqual(self.dialog.project.devices[0].length_um, 0.2)

    def test_cancelling_unified_import_leaves_current_project_unchanged(self):
        source = self.root / "cancel.sp"
        source.write_text(NETLIST)
        original = self.dialog.project
        with modal_interaction(lambda: QApplication.activeModalWidget().buttons.button(QDialogButtonBox.StandardButton.Cancel).click()):
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
            with modal_interaction(choose_action):
                with patch.object(QFileDialog, "getOpenFileName", return_value=(str(source), "")):
                    self.dialog._import_constraints()
            self.assertEqual(self.dialog.project.constraints, expected)
            current = self.dialog.project.to_dict()
            self.assertEqual({key: value for key, value in current.items() if key not in ("constraints", "constraint_enabled", "constraint_ids")},
                             {key: value for key, value in previous.items() if key not in ("constraints", "constraint_enabled", "constraint_ids")})
        original = self.dialog.project
        with modal_interaction(lambda: QApplication.activeModalWidget().button(QMessageBox.StandardButton.Cancel).click()):
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

    def test_supported_native_imports_are_validated_without_a_form(self):
        source = self.root / 'invalid.const.json'
        before = self.dialog.project.to_dict()
        for rule in ({'constraint': 'NetConst', 'nets': ['missing'], 'criticality': 3},
                     {'constraint': 'DoNotUseLib', 'libraries': []},
                     {'constraint': 'NetPriority', 'nets': ['d'], 'weight': -2}):
            source.write_text(json.dumps([{'constraint': 'FutureRule'}, rule]))
            with patch.object(QFileDialog, 'getOpenFileName', return_value=(str(source), '')):
                with self.assertRaisesRegex(ValueError, 'Constraint 2'):
                    self.dialog._import_constraints()
            self.assertEqual(self.dialog.project.to_dict(), before)

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
        with modal_interaction(cancel_edit):
            self.dialog._edit_constraint()
        self.assertEqual(self.dialog.project.to_dict(), original)
        def accept_edit():
            editor = QApplication.activeModalWidget()
            editor.option.setCurrentIndex(editor.option.findData("top_to_bottom"))
            editor.accept()
        with modal_interaction(accept_edit):
            self.dialog.constraints_table.cellDoubleClicked.emit(2, 0)
        self.assertEqual(self.dialog.project.constraints[:2], [opaque, extra])
        self.assertEqual(self.dialog.project.constraints[2], dict(supported, direction="top_to_bottom"))
        self.dialog._remove_constraint()
        self.assertEqual(self.dialog.project.constraints, [opaque, extra])
        target = self.root / "preserved.roar.floorplan"
        with patch.object(QFileDialog, "getSaveFileName", return_value=(str(target), "")):
            self.dialog._save_project()
        self.assertEqual(FloorplanProject.load(target).constraints, [opaque, extra])
        with self.assertRaisesRegex(ValueError, "[Uu]nsupported"):
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
        self.assertIn("pair (unsaved)", self.dialog.windowTitle())
        self.assertFalse(hasattr(self.dialog, "project_label"))
        labels = [button.text() for button in self.dialog.findChildren(QPushButton)]
        self.assertIn("New floorplan…", labels)
        self.assertIn("Open floorplan…", labels)
        target = self.root / "no_extension"
        with patch.object(QFileDialog, "getSaveFileName", return_value=(str(target), "")) as picker:
            self.dialog._save_project()
        self.assertEqual(picker.call_args.args[2], "pair.roar.floorplan")
        self.assertTrue(Path(str(target) + ".roar.floorplan").exists())
        self.assertIn("no_extension.roar.floorplan", self.dialog.windowTitle())
        self.assertEqual(self.dialog.windowFilePath(), str(target) + ".roar.floorplan")
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
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Cancel):
            self.dialog.close()
        self.assertTrue(self.dialog.isVisible())
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Save), \
             patch.object(QFileDialog, "getSaveFileName", return_value=(str(self.root / "close.roar.floorplan"), "")):
            self.dialog.close()
        self.dialog.show()
        self.app.processEvents()
        self.assertEqual(self.dialog.devices_table.item(0, 3).text(), "6")
        self.assertIs(self.dialog.parent(), self.parent)

    def test_escape_uses_the_floorplan_placement_cleanup_policy(self):
        self.dialog.show()
        with patch.object(self.dialog.placement_panel, "close_panel") as cleanup:
            QTest.keyClick(self.dialog, Qt.Key.Key_Escape)
        cleanup.assert_called_once()
        self.assertFalse(self.dialog.isVisible())

    def test_open_dialog_follows_suggestions_but_preserves_explicit_override(self):
        first_design = self.root / "first.roar"
        second_design = self.root / "second.roar"
        self.design.save(first_design)
        self.design.save(second_design)
        project = FloorplanProject.from_text(NETLIST)
        project.source_mode = "linked"
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
        self.assertIn("Association will change", options.link_status.text())
        with modal_interaction(lambda: options.buttons.button(QDialogButtonBox.StandardButton.Open).click()):
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
        self.assertEqual(options.source_mode.currentData(), "saved")
        self.assertIn("missing", options.link_status.text())
        self.assertTrue(options.buttons.button(QDialogButtonBox.StandardButton.Open).isEnabled())
        with modal_interaction(lambda: options.buttons.button(QDialogButtonBox.StandardButton.Open).click()):
            self.assertEqual(options.exec(), QDialog.DialogCode.Accepted)
        self.assertIsNone(options.design)
        self.assertEqual(options.project.roar_project_path, str(missing))
        self.dialog.set_project(options.project, sidecar, options.design)
        self.assertFalse(self.dialog.source_reload_button.isEnabled())
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
        self.select_source("live")
        original_project, original_source = self.dialog.project, self.dialog.roar_design
        def cancel_open():
            modal = QApplication.activeModalWidget()
            modal.floorplan_path.setText(str(sidecar))
            modal.floorplan_path.editingFinished.emit()
            modal.buttons.button(QDialogButtonBox.StandardButton.Cancel).click()
        with modal_interaction(cancel_open):
            with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Discard):
                self.dialog._open_project()
        self.assertIs(self.dialog.project, original_project)
        self.assertIs(self.dialog.roar_design, original_source)

    def test_malformed_documents_report_inline_without_escaping_qt_slots(self):
        original = self.dialog.project.to_dict()
        huge = self.dialog.project.to_dict()
        huge['run_options'] = {'lambda_coeff': 10 ** 400}
        path = self.root / 'malformed.roar.floorplan'
        options = OpenFloorplanDialog(self.parent)
        for content in ('[' * 1500 + '0' + ']' * 1500, json.dumps(huge)):
            path.write_text(content)
            options.floorplan_path.setText(str(path))
            options.floorplan_path.editingFinished.emit()
            self.assertTrue(options.link_status.text())
            self.assertFalse(options.buttons.button(QDialogButtonBox.StandardButton.Open).isEnabled())
            options.accept()
            self.assertEqual(options.result(), QDialog.DialogCode.Rejected)
            self.assertIsNone(options.project)
        options.deleteLater()
        constraints = self.root / 'malformed.const.json'
        constraints.write_text('[' * 1500 + '0' + ']' * 1500)
        with patch.object(QFileDialog, 'getOpenFileName', return_value=(str(constraints), '')), \
             patch.object(QMessageBox, 'warning') as warning:
            self.dialog._guard(self.dialog._import_constraints)
        warning.assert_called_once()
        self.assertEqual(self.dialog.project.to_dict(), original)

    def test_unnamed_live_source_reopens_captured_until_explicitly_selected(self):
        source = Design().add_instance('M1_2', W=2.2e-6, L=.5e-6, NF=2)
        workspace, editor = self.workspace(source)
        self.dialog.workspace = workspace
        self.dialog.snapshot_provider = workspace.snapshot
        project = FloorplanProject.from_text(SYMBOLIC)
        project.set_parameter('nfs', 2)
        self.dialog.set_project(project)
        self.dialog._activate_source('live')
        self.dialog.binding_instance.setCurrentText('M1_2')
        self.dialog.binding_attribute.setCurrentIndex(self.dialog.binding_attribute.findData('W'))
        self.dialog._bind_parameter()
        path = self.root / 'unnamed.roar.floorplan'
        with patch.object(QFileDialog, 'getSaveFileName', return_value=(str(path), '')):
            self.dialog._save_project()
        self.assertEqual(self.dialog._source_mode, 'live')
        self.assertEqual(FloorplanProject.load(path).source_mode, 'saved')
        unrelated = self.root / 'unrelated.roar'
        Design().add_instance('M1_2', W=3e-6, L=.5e-6, NF=2).save(unrelated)
        workspace.switch(unrelated)
        # Cover both new captured drafts and older files that persisted live
        # intent without recording any source identity.
        for legacy in (False, True):
            if legacy:
                state = json.loads(path.read_text())
                state['source_mode'] = 'live'
                path.write_text(json.dumps(state))
            options = OpenFloorplanDialog(self.parent, path, workspace=workspace)
            self.assertEqual(options.source_mode.currentData(), 'saved')
            options.accept()
            self.assertAlmostEqual(options.project.devices[0].width_um, 1.1)
            self.assertIsNone(options.project.roar_project_path)
            options.deleteLater()
        options = OpenFloorplanDialog(self.parent, path, workspace=workspace)
        options.source_mode.setCurrentIndex(options.source_mode.findData('live'))
        options.source_mode.activated.emit(options.source_mode.currentIndex())
        options.accept()
        self.assertAlmostEqual(options.project.devices[0].width_um, 1.5)
        self.assertEqual(options.project.roar_project_path, str(unrelated))
        options.deleteLater()

    def test_unfinished_current_design_can_reach_discard_and_switch(self):
        raw = {'instance_table': [{'Instance': '', 'W': 'unfinished'}]}
        holder = {'editor': raw}
        def apply(design):
            holder['editor'] = design.to_state()['design_editor']
        workspace = DesignWorkspace(
            capture_design=lambda: Design.from_state(holder['editor']),
            capture_editor=lambda: holder['editor'], capture_state=lambda: {},
            apply_design=apply, apply_editor=lambda state: holder.update(editor=state))
        target = self.design.save(self.root / 'linked.roar')
        project = self._bound_project()
        project.roar_project_path = target
        path = project.save(self.root / 'linked.roar.floorplan')
        options = OpenFloorplanDialog(self.parent, path, workspace=workspace)
        with patch('roar_api.floorplan.dialog._source_switch_choice', return_value='cancel') as choice:
            options.accept()
        choice.assert_called_once()
        self.assertEqual(holder['editor'], raw)
        with patch('roar_api.floorplan.dialog._source_switch_choice', return_value='switch'):
            options.accept()
        self.assertEqual(options.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(workspace.path, str(target))
        self.assertEqual(options.source_origin, 'live')
        options.deleteLater()

    def test_role_label_editing_is_undoable_and_constraint_links_select_rows(self):
        before = self.dialog.project.to_dict()
        with patch('roar_api.floorplan.dialog.QInputDialog.getText', return_value=('Input pair', True)):
            self.dialog.sketch_panel.labelRequested.emit('M1')
        self.assertEqual(self.dialog.project.device_labels, {'M1': 'Input pair'})
        self.dialog._undo()
        self.assertEqual(self.dialog.project.to_dict(), before)
        self.dialog._redo()
        self.assertEqual(self.dialog.project.device_labels, {'M1': 'Input pair'})
        with patch('roar_api.floorplan.dialog.QInputDialog.getText', return_value=('', True)):
            self.dialog._edit_device_label('M1')
        self.assertFalse(self.dialog.project.device_labels)
        self.dialog.project.constraints = [{'constraint': 'Order', 'instances': ['M1', 'M2'], 'direction': 'left_to_right'}]
        self.dialog._render_constraints()
        self.dialog.sketch_panel.constraintRequested.emit(0)
        self.assertEqual(self.dialog.constraints_table.currentRow(), 0)
        self.assertIs(self.dialog.tabs.currentWidget(), self.dialog.constraints_table.parentWidget())

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

    def test_saved_source_open_refreshes_mappings_and_reloads_before_actions(self):
        saved_design = self.root / "sizes.roar"
        self.design.save(saved_design)
        original_running = self.design.to_state()
        project = FloorplanProject.from_text(SYMBOLIC)
        project.roar_project_path = saved_design
        project.source_mode = "linked"
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
        with modal_interaction(open_saved):
            self.dialog._open_project()
        self.assertEqual(self.dialog._source_origin, "linked")
        self.assertEqual(self.dialog.project.devices[0].width_um, 5)
        self.dialog._refresh_bindings()
        self.assertEqual(self.dialog.project.devices[0].width_um, 5)
        self.assertEqual(self.design.to_state(), original_running)
        self.assertEqual(saved_design.read_bytes(), source_bytes)
        before = self.dialog.project.to_dict()
        saved_design.unlink()
        with self.assertRaises(OSError):
            self.dialog._refresh_bindings()
        self.assertIsNotNone(self.dialog.roar_design)
        self.assertIn("Sizing source error", self.dialog.snapshot_label.text())
        self.assertEqual(self.dialog.project.to_dict(), before)

    def test_parameter_binding_uses_explicit_attribute_and_preserves_captured_source(self):
        self.dialog.set_project(FloorplanProject.from_text(SYMBOLIC))
        self.select_source("live")
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

    def test_saved_bindings_restore_source_and_units_before_rebinding(self):
        source = (Design().add_instance("First", W=100, L=100, NF=2)
                  .add_instance("M1_2", W=1.1, L=0.5, NF=2))
        project = FloorplanProject.from_text(SYMBOLIC.replace("L=0.15u", "L=ls"))
        project.set_parameter("nfs", 2)
        project.bind_parameter(source, "ws", "M1_2", "W", dimension_unit="um", width_convention="per_finger")
        project.bind_parameter(source, "ls", "M1_2", "L", dimension_unit="um", width_convention="per_finger")
        sidecar = self.root / "binding-units.roar.floorplan"
        project.save(sidecar)
        self.dialog.snapshot_provider = lambda: source
        self.dialog.set_project(FloorplanProject.load(sidecar), sidecar, source, "live")
        for symbol, attribute in (("ls", "L"), ("ws", "W"), ("ls", "L"), ("ws", "W")):
            # Simulate the previous symbol/control state differing from the saved binding.
            self.dialog.binding_unit.setCurrentIndex(self.dialog.binding_unit.findData("m"))
            self.dialog.binding_width.setCurrentIndex(self.dialog.binding_width.findData("total"))
            self.dialog.binding_instance.setCurrentText("First")
            self.dialog.parameter_symbol.setCurrentText(symbol)
            self.dialog._parameter_selected()
            self.assertEqual(self.dialog.binding_instance.currentText(), "M1_2")
            self.assertEqual(self.dialog.binding_attribute.currentData(), attribute)
            self.assertEqual(self.dialog.binding_unit.currentData(), "um")
            self.assertEqual(self.dialog.binding_width.currentData(), "per_finger")
            self.dialog._bind_parameter()
            self.assertEqual(self.dialog.project.devices[0].width_um, 1.1)
            self.assertEqual(self.dialog.project.devices[0].length_um, 0.5)
        # A manual symbol leaves the user's existing source interpretation alone.
        self.dialog.parameter_symbol.setCurrentText("nfs")
        self.assertEqual(self.dialog.binding_unit.currentData(), "um")
        self.assertEqual(self.dialog.binding_width.currentData(), "per_finger")
        # A fresh source render must also preserve an intentional new binding choice.
        self.dialog.parameter_symbol.setCurrentText("ws")
        self.dialog.binding_instance.setCurrentText("First")
        self.dialog.binding_attribute.setCurrentIndex(self.dialog.binding_attribute.findData("W"))
        self.dialog.binding_unit.setCurrentIndex(self.dialog.binding_unit.findData("m"))
        self.dialog.binding_width.setCurrentIndex(self.dialog.binding_width.findData("total"))
        source.set_instance_attribute("First", "W", 12e-6)
        source.set_instance_attribute("M1_2", "W", 2.2)
        self.dialog._bind_parameter()
        self.assertEqual(self.dialog.project.devices[0].width_um, 6)
        self.assertEqual(self.dialog.project.parameter_rows[0]["binding"]["instance"], "First")

    def test_spice_suffix_source_attributes_bind_reopen_and_rebind(self):
        source = Design().add_instance("M1_2", W="4u", L="0.15u", NF=2, expression="2*W")
        source_path = self.root / "suffix-sizes.roar"
        source.save(source_path)
        project = FloorplanProject.from_text(SYMBOLIC.replace("L=0.15u", "L=ls"))
        project.set_parameter("nfs", 2)
        self.dialog.set_project(project)
        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(source_path), "")):
            self.dialog._load_linked_roar()
        attributes = [self.dialog.binding_attribute.itemData(index) for index in range(self.dialog.binding_attribute.count())]
        self.assertIn("W", attributes)
        self.assertIn("L", attributes)
        self.assertNotIn("expression", attributes)
        for symbol, attribute in (("ws", "W"), ("ls", "L")):
            self.dialog.parameter_symbol.setCurrentText(symbol)
            self.dialog.binding_attribute.setCurrentIndex(self.dialog.binding_attribute.findData(attribute))
            self.dialog._bind_parameter()
        sidecar = self.root / "suffix.roar.floorplan"
        with patch.object(QFileDialog, "getSaveFileName", return_value=(str(sidecar), "")):
            self.dialog._save_project()
        def open_saved():
            options = QApplication.activeModalWidget()
            options.floorplan_path.setText(str(sidecar))
            options.floorplan_path.editingFinished.emit()
            options.buttons.button(QDialogButtonBox.StandardButton.Open).click()
        with modal_interaction(open_saved):
            self.dialog._open_project()
        for symbol, attribute in (("ls", "L"), ("ws", "W")):
            self.dialog.parameter_symbol.setCurrentText(symbol)
            self.assertEqual(self.dialog.binding_attribute.currentData(), attribute)
            self.dialog._bind_parameter()
            self.assertEqual(self.dialog.project.devices[0].width_um, 2)
            self.assertAlmostEqual(self.dialog.project.devices[0].length_um, 0.15)

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
        with modal_interaction(accept_parameters):
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
        with modal_interaction(accept_import):
            with patch.object(QFileDialog, "getOpenFileName", return_value=(str(source), "")):
                self.dialog._load_netlist()
        self.assertEqual(self.dialog.project.roar_project_path, str(saved_design))
        self.assertEqual(self.dialog._source_origin, "linked")
        self.assertEqual(self.dialog.project.unresolved_parameters, ["ws", "nfs"])
        self.assertEqual(self.design.to_state(), original)

    def _bound_project(self):
        project = FloorplanProject.from_text(SYMBOLIC)
        project.source_mode = "linked"
        project.set_parameter("nfs", 4)
        project.bind_parameter(self.design, "ws", "M1_2", "W", source="linked")
        return project

    def test_current_source_migrates_only_bindings_and_debounces_updates(self):
        class Changes(QObject):
            changed = pyqtSignal()
        changes = Changes()
        current = self.design.save(self.root / 'current.roar')
        workspace, editor = self.workspace(self.design, current)
        dialog = FloorplanDialog(self.parent, workspace=workspace, source_changed_signals=(changes.changed,),
                                 placement_options={"settings": QSettings(str(self.root / "live.ini"), QSettings.Format.IniFormat), "config": AlignConfig("", "", str(self.root / "runs"))})
        project = self._bound_project()
        project.roar_project_path = str(self.root / "obsolete.roar")
        dialog.set_project(project)
        with patch('roar_api.floorplan.dialog._source_switch_choice', return_value='current'):
            self.select_source("live", dialog)
        self.assertEqual(dialog.project.roar_project_path, str(current))
        self.assertEqual(dialog.project.parameter_rows[0]["binding"]["source"], "live")
        self.assertIsNone(dialog.project.parameter_rows[1]["binding"])
        dialog.show()
        editor.design.set_instance_attribute("M1_2", "W", 24e-6)
        for _ in range(5):
            changes.changed.emit()
        QTest.qWait(380)
        self.assertEqual(dialog.project.devices[0].width_um, 6)
        self.assertEqual(dialog.project.parameter_rows[1]["value"], 4)
        before = dialog.project.to_dict()
        cached = dialog.roar_design
        editor.capture.side_effect = RuntimeError("source temporarily unavailable")
        changes.changed.emit()
        with patch.object(QMessageBox, "warning") as popup:
            QTest.qWait(380)
        popup.assert_not_called()
        self.assertEqual(dialog.project.to_dict(), before)
        self.assertIs(dialog.roar_design, cached)
        with self.assertRaisesRegex(RuntimeError, "temporarily unavailable"):
            dialog._export_align()
        editor.capture.side_effect = None
        editor.design.set_instance_attribute("M1_2", "W", 32e-6)
        # Generate performs the same freshness guard, without waiting for debounce.
        dialog.placement_panel._before_generate()
        self.assertEqual(dialog.project.devices[0].width_um, 8)
        dialog._activate_source("saved")
        editor.design.set_instance_attribute("M1_2", "W", 40e-6)
        dialog._ensure_current_source()
        self.assertEqual(dialog.project.devices[0].width_um, 8)
        self.assertIsNotNone(dialog.project.parameter_rows[0]["binding"])
        dialog._dirty = False
        dialog.close()
        dialog.deleteLater()

    def test_reload_action_only_appears_for_saved_file_sources(self):
        self.assertTrue(self.dialog.source_reload_button.isHidden())
        self.select_source("live")
        self.assertTrue(self.dialog.source_reload_button.isHidden())
        self.assertFalse(self.dialog.source_reload_button.isEnabled())
        saved = self.root / "reload.roar"
        self.design.save(saved)
        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(saved), "")):
            self.dialog._load_linked_roar()
        self.assertFalse(self.dialog.source_reload_button.isHidden())
        self.assertTrue(self.dialog.source_reload_button.isEnabled())
        self.assertEqual(len([button for button in self.dialog.findChildren(QPushButton) if button.text() == "Reload source"]), 1)
        self.dialog._activate_source("saved")
        self.assertTrue(self.dialog.source_reload_button.isHidden())
        self.assertFalse(self.dialog.source_reload_button.isEnabled())

    def test_open_same_association_uses_unsaved_live_design_without_switch(self):
        design_path = self.root / "current.roar"
        self.design.save(design_path)
        workspace, editor = self.workspace(self.design, design_path)
        project = self._bound_project()
        project.roar_project_path = design_path
        sidecar = self.root / "same.roar.floorplan"
        project.save(sidecar)
        editor.design.set_instance_attribute("M1_2", "W", 28e-6)
        options = OpenFloorplanDialog(self.parent, sidecar, workspace=workspace)
        self.assertEqual(options.source_mode.currentData(), "linked")
        with patch("roar_api.floorplan.dialog._source_switch_choice") as warning:
            options.accept()
        warning.assert_not_called()
        self.assertEqual(options.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(options.project.devices[0].width_um, 7)
        self.assertEqual(options.project.roar_project_path, str(design_path))
        self.assertEqual(options.source_origin, "live")
        workspace.switch.assert_not_called()
        options.deleteLater()

    def test_saved_live_source_requires_choice_before_rebinding_to_another_design(self):
        linked = self.design.save(self.root / 'linked.roar')
        current = self.design.save(self.root / 'current.roar')
        project = self._bound_project()
        project.roar_project_path = linked
        project.source_mode = 'live'
        sidecar = project.save(self.root / 'live.roar.floorplan')
        workspace, editor = self.workspace(self.design, current)
        # Identical sizes still do not make two design files interchangeable.
        for changed_sizes in (False, True):
            if changed_sizes:
                editor.design.set_instance_attribute('M1_2', 'W', 28e-6)
                linked.unlink()  # Captured sizes remain available with a missing link.
            options = OpenFloorplanDialog(self.parent, sidecar, workspace=workspace)
            self.assertEqual(options.source_mode.currentData(), 'live')
            with patch('roar_api.floorplan.dialog._source_switch_choice', return_value='cancel') as choose:
                options.accept()
            choose.assert_called_once()
            self.assertIsNone(options.project)
            with patch('roar_api.floorplan.dialog._source_switch_choice', return_value='saved'):
                options.accept()
            self.assertEqual(options.project.roar_project_path, str(linked))
            self.assertEqual(options.project.devices[0].width_um, 2)
            workspace.switch.assert_not_called()
            options.deleteLater()

    def test_open_conflict_cancel_save_cancel_discard_and_change_association(self):
        linked = self.root / "linked.roar"
        Design().add_instance("M1_2", W=20e-6, L=0.2e-6, NF=4).save(linked)
        current = self.root / "current.roar"
        self.design.save(current)
        workspace, editor = self.workspace(self.design, current)
        editor.design.set_instance_attribute("M1_2", "L", .25e-6)
        project = self._bound_project()
        project.roar_project_path = linked
        sidecar = self.root / "conflict.roar.floorplan"
        project.save(sidecar)
        original = editor.design.to_state()
        for choice in ("cancel", "save"):
            options = OpenFloorplanDialog(self.parent, sidecar, workspace=workspace)
            with patch("roar_api.floorplan.dialog._source_switch_choice", return_value=choice), patch.object(QFileDialog, "getSaveFileName", return_value=("", "")):
                options.accept()
            self.assertEqual(options.result(), QDialog.DialogCode.Rejected)
            self.assertIsNone(options.project)
            self.assertEqual(editor.design.to_state(), original)
            workspace.switch.assert_not_called()
            options.deleteLater()
        before_target = linked.read_bytes()
        options = OpenFloorplanDialog(self.parent, sidecar, workspace=workspace)
        with patch("roar_api.floorplan.dialog._source_switch_choice", return_value="save"), patch.object(QFileDialog, "getSaveFileName", return_value=(str(linked), "")):
            options.accept()
        self.assertIn("different file", options.link_status.text())
        self.assertEqual(linked.read_bytes(), before_target)
        self.assertEqual(editor.design.to_state(), original)
        workspace.save.assert_not_called()
        options.deleteLater()
        options = OpenFloorplanDialog(self.parent, sidecar, workspace=workspace)
        with patch("roar_api.floorplan.dialog._source_switch_choice", return_value="current"):
            options.accept()
        self.assertEqual(options.project.roar_project_path, str(current))
        self.assertEqual(options.project.devices[0].width_um, 2)
        self.assertTrue(workspace.is_modified())
        options.deleteLater()
        options = OpenFloorplanDialog(self.parent, sidecar, workspace=workspace)
        with patch("roar_api.floorplan.dialog._source_switch_choice", return_value="switch"):
            options.accept()
        workspace.switch.assert_called_once_with(str(linked))
        self.assertEqual(options.project.devices[0].width_um, 5)
        self.assertEqual(options.project.roar_project_path, str(linked))
        self.assertEqual(options.source_origin, "live")
        options.deleteLater()

    def test_open_real_conflict_buttons_and_successful_save_switch(self):
        linked = self.root / "target.roar"
        Design().add_instance("M1_2", W=16e-6, L=0.2e-6, NF=4).save(linked)
        current = self.root / "old.roar"
        self.design.save(current)
        workspace, editor = self.workspace(self.design, current)
        editor.design.set_instance_attribute("M1_2", "L", .25e-6)
        project = self._bound_project()
        project.roar_project_path = linked
        sidecar = self.root / "modal.roar.floorplan"
        project.save(sidecar)
        options = OpenFloorplanDialog(self.parent, sidecar, workspace=workspace)
        saved = self.root / "save-before-switch.roar"
        def save_and_switch():
            warning = QApplication.activeModalWidget()
            labels = [button.text() for button in warning.buttons()]
            self.assertIn("Discard and Switch", labels)
            self.assertIn("Change saved association", labels)
            next(button for button in warning.buttons() if button.text() == "Save and Switch…").click()
        with modal_interaction(save_and_switch):
            with patch.object(QFileDialog, "getSaveFileName", return_value=(str(saved), "")):
                options.accept()
        self.assertEqual(options.result(), QDialog.DialogCode.Accepted)
        workspace.save.assert_called_once_with(str(saved))
        workspace.switch.assert_called_once_with(str(linked))
        self.assertEqual(load_roar_design(saved).get_instance_parameters()["M1_2"]["W"]["value"], 8e-6)
        self.assertEqual(options.project.devices[0].width_um, 4)
        options.deleteLater()

    def test_open_binding_preflight_failure_does_not_switch_and_frozen_can_open(self):
        other = self.root / "other.roar"
        Design().add_instance("unrelated", W=8e-6).save(other)
        project = self._bound_project()
        project.roar_project_path = other
        sidecar = self.root / "missing-mapping.roar.floorplan"
        project.save(sidecar)
        workspace, editor = self.workspace(self.design)
        options = OpenFloorplanDialog(self.parent, sidecar, workspace=workspace)
        with patch("roar_api.floorplan.dialog._source_switch_choice", return_value="switch"):
            options.accept()
        self.assertEqual(options.result(), QDialog.DialogCode.Rejected)
        self.assertIn("M1_2", options.link_status.text())
        workspace.switch.assert_not_called()
        options.source_mode.setCurrentIndex(options.source_mode.findData("saved"))
        options.accept()
        self.assertEqual(options.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(options.project.devices[0].width_um, 2)
        self.assertEqual(options.project.roar_project_path, str(other))
        options.deleteLater()


if __name__ == "__main__":
    unittest.main()
