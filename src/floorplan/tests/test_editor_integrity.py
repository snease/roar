"""Regression tests for saving, repairing and editing floorplan documents."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from roar_api import Design
from roar_api.floorplan import FloorplanProject

try:
    from PyQt6.QtCore import QSettings, Qt
    from PyQt6.QtWidgets import QApplication, QDialog, QFileDialog, QMainWindow, QMessageBox, QPlainTextEdit, QWidget
    from qt_test_helpers import modal_interaction
    from roar_api.floorplan.align_runner import AlignConfig
    from roar_api.floorplan.dialog import FloorplanDialog, OpenFloorplanDialog
except ModuleNotFoundError as exc:
    if exc.name != "PyQt6":
        raise
    QApplication = None


CIRCUIT = """.subckt pair d g s b
M1 d g s b nch W=2u L=0.15u nf=2 M=7
M2 d g s b nch W=2u L=0.15u nf=2
R1 d s .5k
.ends pair
"""


@unittest.skipIf(QApplication is None, "PyQt6 GUI extra is not installed")
class EditorIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.parent = QWidget()
        self.source = Design().add_instance("input_pair", W=8e-6, L=0.2e-6, NF=4)
        self.dialog = FloorplanDialog(self.parent, lambda: Design.from_state(self.source.to_state()),
            placement_options={"settings": QSettings(str(self.root / "align.ini"), QSettings.Format.IniFormat),
                               "config": AlignConfig("", "", str(self.root / "runs"))})
        self.dialog.set_project(FloorplanProject.from_text(CIRCUIT))

    def tearDown(self):
        self.dialog._dirty = False
        self.dialog.close()
        self.parent.deleteLater()
        self.app.processEvents()
        self.temporary.cleanup()

    def test_literal_multiplicity_resistance_and_effective_width_are_visible_and_editable(self):
        table = self.dialog.devices_table
        self.assertEqual(float(table.item(0, 6).text()), 7)
        self.assertEqual(float(table.item(0, 8).text()), 28)
        self.assertEqual(table.item(2, 7).text(), ".5k")
        table.item(0, 6).setText("3")
        table.item(2, 7).setText("1.5k")
        candidate = self.dialog._collect_project()
        self.assertEqual(candidate.devices[0].parameters["M"], 3)
        self.assertEqual(candidate.devices[2].value, "1500")
        self.assertEqual(float(table.item(0, 8).text()), 12)
        self.assertFalse(table.item(0, 8).flags() & Qt.ItemFlag.ItemIsEditable)
        table.selectRow(0)
        self.assertIn("G → g [circuit port]", self.dialog.connectivity.toPlainText())
        self.assertIn("M2.G", self.dialog.connectivity.toPlainText())
        self.assertIn("R1.1", self.dialog.connectivity.toPlainText())

    def test_invalid_cell_edits_can_be_undone_and_redone_without_validation(self):
        original = self.dialog.devices_table.item(0, 3).text()
        self.dialog.devices_table.item(0, 3).setText("unfinished")
        self.assertTrue(self.dialog._dirty)
        self.assertTrue(self.dialog.windowTitle().startswith("* "))
        self.dialog._undo()
        self.assertEqual(self.dialog.devices_table.item(0, 3).text(), original)
        self.assertFalse(self.dialog._dirty)
        self.dialog._redo()
        self.assertEqual(self.dialog.devices_table.item(0, 3).text(), "unfinished")
        with self.assertRaises(ValueError):
            self.dialog._collect_project()

    def test_constraint_duplicate_disable_remove_and_undo_preserve_native_fields(self):
        self.dialog.project.set_constraints([{"constraint": "Order", "instances": ["M1", "M2"], "direction": "left_to_right"}])
        self.dialog._render_constraints()
        self.dialog._record_edit()
        self.dialog.constraints_table.selectRow(0)
        self.dialog._duplicate_constraint()
        self.assertEqual(len(self.dialog.project.constraints), 2)
        self.dialog._toggle_constraint()
        self.assertEqual(self.dialog.project.constraint_enabled, [True, False])
        self.assertEqual(len(self.dialog.project.active_constraints), 1)
        self.assertIn("Disabled", self.dialog.constraints_table.item(1, 0).text())
        self.dialog._remove_constraint()
        self.assertEqual(len(self.dialog.project.constraints), 1)
        self.dialog._undo()
        self.assertEqual(self.dialog.project.constraint_enabled, [True, False])
        self.dialog._undo()
        self.assertEqual(self.dialog.project.constraint_enabled, [True, True])

    def test_unknown_constraint_details_show_all_fields_and_can_be_disabled(self):
        self.dialog.project.constraints = [{"constraint": "FutureRule", "instances": ["M1"], "nested": {"field": 12}}]
        self.dialog._render_constraints()
        self.dialog.constraints_table.selectRow(0)
        def inspect():
            modal = QApplication.activeModalWidget()
            text = modal.findChild(QPlainTextEdit).toPlainText()
            self.assertIn('"field": 12', text)
            modal.reject()
        with modal_interaction(inspect):
            self.dialog._inspect_constraint()
        self.dialog._toggle_constraint()
        self.dialog._collect_project().validate()

    def test_frozen_source_intent_survives_save_and_reopen(self):
        linked = self.root / "sizes.roar"
        self.source.save(linked)
        project = FloorplanProject.from_text(CIRCUIT.replace("W=2u", "W=shared"))
        project.bind_parameter(self.source, "shared", "input_pair", "W", source="linked")
        project.roar_project_path = str(linked)
        project.source_mode = "linked"
        self.dialog.set_project(project, design=self.source, source_origin="linked")
        self.dialog._activate_source("saved")
        path = self.root / "frozen.roar.floorplan"
        with patch.object(QFileDialog, "getSaveFileName", return_value=(str(path), "")):
            self.dialog._save_project()
        before = self.dialog.project.devices[0].width_um
        self.source.set_instance_attribute("input_pair", "W", 80e-6).save(linked)
        options = OpenFloorplanDialog(self.parent, path)
        self.assertEqual(options.source_mode.currentData(), "saved")
        options.accept()
        self.assertEqual(options.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(options.project.devices[0].width_um, before)
        self.assertEqual(options.project.source_mode, "saved")
        self.assertIsNotNone(options.project.parameter_rows[0]["binding"])
        options.deleteLater()

    def test_broken_binding_can_be_replaced_then_unbound_without_refreshing_it(self):
        project = FloorplanProject.from_text(CIRCUIT.replace("W=2u", "W=shared"))
        project.bind_parameter(self.source, "shared", "input_pair", "W", source="live")
        self.dialog.set_project(project, design=self.source, source_origin="live")
        self.source = Design().add_instance("input_pair", L=0.25e-6, NF=4)
        with self.assertRaises(ValueError):
            self.dialog._ensure_current_source()
        self.dialog.parameter_symbol.setCurrentText("shared")
        self.dialog.binding_instance.setCurrentText("input_pair")
        self.dialog.binding_attribute.setCurrentIndex(self.dialog.binding_attribute.findData("L"))
        self.dialog.binding_width.setCurrentIndex(self.dialog.binding_width.findData("per_finger"))
        self.dialog._bind_parameter()
        self.assertEqual(self.dialog.project.devices[0].width_um, 0.25)
        self.dialog._unbind_parameter()
        self.assertIsNone(self.dialog.project.parameter_rows[0]["binding"])
        self.assertEqual(self.dialog.project.devices[0].width_um, 0.25)

    def test_unbinding_or_replacing_bad_binding_clears_cached_placement_error(self):
        for repair in ('unbind', 'manual'):
            with self.subTest(repair=repair):
                project = FloorplanProject.from_text(CIRCUIT.replace('W=2u', 'W=shared'))
                valid = Design().add_instance('input_pair', W=4e-6)
                project.bind_parameter(valid, 'shared', 'input_pair', 'W', source='live')
                self.dialog.set_project(project, design=valid, source_origin='live')
                self.source = Design().add_instance('input_pair', L=.5e-6)
                self.dialog._live_source_update()
                self.assertTrue(self.dialog._source_error)
                if repair == 'unbind':
                    self.dialog._unbind_parameter()
                else:
                    self.dialog.parameter_value.setText('1.1')
                    self.dialog.parameter_unit.setCurrentIndex(self.dialog.parameter_unit.findData('um'))
                    self.dialog.parameter_width.setCurrentIndex(self.dialog.parameter_width.findData('per_finger'))
                    self.dialog._set_parameter()
                self.assertFalse(self.dialog._source_error)
                self.dialog._collect_placement_project().validate()
                with patch.object(self.dialog.placement_panel, '_validator'):
                    self.dialog.placement_panel.refresh()
                self.assertTrue(self.dialog.placement_panel.generate_button.isEnabled())

    def test_corrected_device_cell_and_failed_new_source_do_not_keep_stale_errors(self):
        self.dialog._activate_source('live')
        self.dialog.devices_table.item(0, 3).setText('unfinished')
        self.dialog._live_source_update()
        self.assertTrue(self.dialog._source_error)
        self.dialog.devices_table.item(0, 3).setText('1.1')
        self.assertFalse(self.dialog._source_error)
        self.dialog._collect_placement_project().validate()
        self.dialog._activate_source('saved')
        before = self.dialog.project.to_dict()
        with self.assertRaises(OSError):
            self.dialog._activate_source('linked', str(self.root / 'missing.roar'))
        self.assertEqual(self.dialog.project.to_dict(), before)
        self.assertFalse(self.dialog._source_error)
        self.dialog._collect_placement_project().validate()

    def test_live_refresh_does_not_add_undo_commands(self):
        project = FloorplanProject.from_text(CIRCUIT.replace("W=2u", "W=shared"))
        project.bind_parameter(self.source, "shared", "input_pair", "W", source="live")
        self.dialog.set_project(project, design=self.source, source_origin="live")
        self.source.set_instance_attribute("input_pair", "W", 16e-6)
        self.dialog._ensure_current_source()
        self.assertTrue(self.dialog._dirty)
        self.assertFalse(self.dialog._undo_states)
        self.dialog.devices_table.item(0, 4).setText("0.3")
        self.assertEqual(len(self.dialog._undo_states), 1)
        self.source.set_instance_attribute("input_pair", "W", 32e-6)
        self.dialog._ensure_current_source()
        width = self.dialog.project.devices[0].width_um
        self.dialog._undo()
        self.assertEqual(self.dialog.project.devices[0].width_um, width)
        self.assertEqual(self.dialog.project.devices[0].length_um, 0.15)

    def test_local_checks_work_without_align_and_errors_use_visible_row_numbers(self):
        self.dialog._check_constraints()
        self.assertIn("checks passed", self.dialog.status.text())
        self.assertIn("Configure ALIGN", self.dialog.status.text())
        self.dialog.project.constraints = [
            {"constraint": "FutureRule"},
            {"constraint": "Order", "instances": ["M1", "M2"], "direction": "left_to_right"},
            {"constraint": "Order", "instances": ["M2", "M1"], "direction": "left_to_right"},
        ]
        self.dialog.project.constraint_enabled = [False, True, True]
        self.dialog._render_constraints()
        with self.assertRaisesRegex(ValueError, "Constraints 2, 3"):
            self.dialog._check_constraints()

    def test_close_cancel_or_failed_save_leaves_editor_and_worker_open(self):
        self.dialog.show()
        self.dialog.devices_table.item(0, 3).setText("unfinished")
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Cancel), \
             patch.object(self.dialog.placement_panel, "close_panel") as stop:
            self.dialog.close()
        stop.assert_not_called()
        self.assertTrue(self.dialog.isVisible())
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Save), \
             patch.object(QMessageBox, "warning") as warning, \
             patch.object(self.dialog.placement_panel, "close_panel") as stop:
            self.dialog.close()
        stop.assert_not_called()
        warning.assert_called_once()
        self.assertTrue(self.dialog.isVisible())

    def test_discard_on_close_restores_saved_values(self):
        path = self.root / "saved.roar.floorplan"
        with patch.object(QFileDialog, "getSaveFileName", return_value=(str(path), "")):
            self.dialog._save_project()
        self.dialog.devices_table.item(0, 3).setText("9")
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Discard):
            self.dialog.close()
        self.assertEqual(float(self.dialog.devices_table.item(0, 3).text()), 2)
        self.assertFalse(self.dialog._dirty)

    def test_save_as_conflict_does_not_write_destination_or_change_document(self):
        destination = self.root / "conflict.roar.floorplan"
        destination.write_text("existing destination")
        self.dialog.devices_table.item(0, 3).setText("9")
        @contextmanager
        def conflict(_path):
            raise ValueError("Another placement owns this destination")
            yield
        with patch.object(self.dialog.placement_panel, "save_as_transaction", conflict), \
             patch.object(QFileDialog, "getSaveFileName", return_value=(str(destination), "")):
            with self.assertRaisesRegex(ValueError, "owns this destination"):
                self.dialog._save_project(save_as=True)
        self.assertEqual(destination.read_text(), "existing destination")
        self.assertIsNone(self.dialog.project_path)
        self.assertTrue(self.dialog._dirty)

    def _main_window(self):
        from roar_api._backend import load_backend
        window_type = load_backend("roar_gui").ROARApp
        # Initialize the real Qt window and exercise its actual File/Close
        # methods without constructing unrelated OpenGL graph panes.
        window = window_type.__new__(window_type)
        QMainWindow.__init__(window)
        window._capture_default_layout = Mock()  # Graph panes are outside this fixture.
        window.header = SimpleNamespace(_floorplan_dialog=self.dialog)
        state = self.source.to_state()
        state["marker"] = "saved"
        window.capture_app_state = Mock(return_value=state)
        window._capture_design_editor_state = lambda: self.source.to_state()["design_editor"]
        window._capture_floorplan_design = lambda: Design.from_state(self.source.to_state())
        self.dialog.workspace = window.floorplan_workspace()
        self.dialog.snapshot_provider = self.dialog.workspace.snapshot
        return window

    def _close_main_window(self, window):
        window.header._floorplan_dialog = None
        window.close()
        window.deleteLater()

    def test_save_as_and_first_save_keep_live_companion_with_real_document_identity(self):
        for previous_name in ("old.roar", None):
            with self.subTest(previous=previous_name):
                window = self._main_window()
                self.addCleanup(self._close_main_window, window)
                previous = self.root / previous_name if previous_name else None
                if previous:
                    window._record_design_document(previous)
                companion = self.root / "pair.roar.floorplan"
                project = FloorplanProject.from_text(CIRCUIT)
                project.roar_project_path = str(previous) if previous else None
                self.dialog.set_project(project, companion, self.source, "live")
                self.dialog.devices_table.item(0, 3).setText("3")
                target = self.root / "new.roar"
                with patch.object(QFileDialog, "getSaveFileName", return_value=(str(target), "")), \
                     patch.object(QMessageBox, "information") as success:
                    window.save_app_state()
                success.assert_called_once()
                self.assertEqual(window.floorplan_workspace().path, str(target))
                self.assertFalse(window.floorplan_workspace().is_modified())
                self.assertEqual(self.dialog.project.roar_project_path, str(target))
                self.assertEqual(FloorplanProject.load(companion).roar_project_path, str(target))
                self.assertFalse(self.dialog._dirty)
                self.dialog._ensure_current_source()
                self.dialog._undo()
                self.assertEqual(self.dialog.project.roar_project_path, str(target))
                self.dialog._ensure_current_source()

    def test_design_json_and_workspace_saves_follow_current_live_association_only(self):
        from roar_api._backend import load_backend
        editor_type = load_backend("design_editor").ROAREditorWindow
        for kind in ("editor", "workspace"):
            for mode, different_source in (("live", False), ("saved", False), ("live", True), ("linked", True)):
                with self.subTest(kind=kind, mode=mode, different_source=different_source):
                    window = self._main_window()
                    self.addCleanup(self._close_main_window, window)
                    previous = self.root / "old.roar"
                    window._record_design_document(previous)
                    associated = str(self.root / "other.roar" if different_source else previous)
                    companion = self.root / "pair.roar.floorplan"
                    project = FloorplanProject.from_text(CIRCUIT)
                    project.roar_project_path = associated
                    self.dialog.set_project(project, companion, self.source, mode)
                    project.save(companion)
                    original_companion = companion.read_bytes()
                    target = self.root / ("editor.json" if kind == "editor" else "workspace.roar")
                    if kind == "editor":
                        data = self.source.to_state()["design_editor"]
                        editor = SimpleNamespace(top_level_app=window,
                            expression_editor=SimpleNamespace(get_table_data=lambda: data["expression_editor"],
                                                               get_tab_names=lambda: ["Expressions"]),
                            constraint_editor=SimpleNamespace(get_table_data=lambda: data["constraint_editor"]),
                            instance_table=SimpleNamespace(get_table_data=lambda: data["instance_table"]),
                            get_iterative_settings=lambda: {})
                        with patch.object(QFileDialog, "getSaveFileName", return_value=(str(target), "")), \
                             patch.object(QMessageBox, "information"):
                            editor_type.save_all_data(editor)
                        self.assertNotIn("design_editor", json.loads(target.read_text()))
                    else:
                        self.assertIsNotNone(window.floorplan_workspace().save(target))
                    expected = str(target) if mode == "live" and not different_source else associated
                    self.assertEqual(self.dialog.project.roar_project_path, expected)
                    self.assertEqual(FloorplanProject.load(companion).roar_project_path, expected)
                    self.assertEqual(window.floorplan_workspace().path, str(target))
                    if expected == associated:
                        self.assertEqual(companion.read_bytes(), original_companion)
                    else:
                        self.dialog._ensure_current_source()

    def test_save_failure_and_cancel_do_not_commit_document_identity_or_claim_success(self):
        window = self._main_window()
        self.addCleanup(self._close_main_window, window)
        previous, target = self.root / "old.roar", self.root / "new.roar"
        window._record_design_document(previous)
        project = FloorplanProject.from_text(CIRCUIT)
        project.roar_project_path = str(previous)
        self.dialog.set_project(project, design=self.source, source_origin="live")
        for failure in ("cancel", "companion"):
            with self.subTest(failure=failure), \
                 patch.object(QFileDialog, "getSaveFileName", side_effect=[(str(target), ""), ("", "")]), \
                 patch.object(QMessageBox, "warning"), \
                 patch.object(QMessageBox, "information") as success:
                if failure == "companion":
                    with patch.object(self.dialog, "_save_project", side_effect=OSError("unwritable companion")):
                        window.save_app_state()
                else:
                    window.save_app_state()
            success.assert_not_called()
            self.assertFalse(target.exists())
            self.assertEqual(window.floorplan_workspace().path, str(previous))
            self.assertEqual(self.dialog.project.roar_project_path, str(previous))
            self.dialog._ensure_current_source()

    def test_load_identity_does_not_reassociate_live_floorplan(self):
        window = self._main_window()
        self.addCleanup(self._close_main_window, window)
        previous = self.root / "old.roar"
        window._record_design_document(previous)
        project = FloorplanProject.from_text(CIRCUIT)
        project.roar_project_path = str(previous)
        self.dialog.set_project(project, design=self.source, source_origin="live")
        window._record_design_document(self.root / "loaded.roar")
        self.assertEqual(self.dialog.project.roar_project_path, str(previous))
        with self.assertRaisesRegex(ValueError, "active ROAR design changed"):
            self.dialog._ensure_current_source()

    def test_save_and_switch_saves_current_companion_with_the_new_design_path(self):
        from roar_api.floorplan.dialog import _choose_source
        window = self._main_window()
        self.addCleanup(self._close_main_window, window)
        previous, saved_as, linked = (self.root / name for name in ("old.roar", "saved-as.roar", "linked.roar"))
        window._record_design_document(previous)
        self.source.set_instance_attribute("input_pair", "W", 12e-6)
        companion = self.root / "pair.roar.floorplan"
        project = FloorplanProject.from_text(CIRCUIT)
        project.roar_project_path = str(previous)
        self.dialog.set_project(project, companion, self.source, "live")
        linked_design = Design().add_instance("input_pair", W=16e-6, L=0.2e-6, NF=4)
        linked_design.save(linked)
        candidate = FloorplanProject.from_text(CIRCUIT)
        candidate.roar_project_path = str(linked)
        workspace = window.floorplan_workspace()
        def apply(design):
            self.source = design
        workspace._apply_design = apply
        with patch("roar_api.floorplan.dialog._source_switch_choice", return_value="save"), \
             patch.object(QFileDialog, "getSaveFileName", return_value=(str(saved_as), "")):
            result = _choose_source(self.dialog, candidate, "linked", str(linked), workspace)
        self.assertIsNotNone(result)
        self.assertEqual(workspace.path, str(linked))
        self.assertEqual(result[0].roar_project_path, str(linked))
        self.assertEqual(FloorplanProject.load(companion).roar_project_path, str(saved_as))
        self.assertEqual(Design.load(saved_as).get_instance_parameters()["input_pair"]["W"]["value"], 12e-6)

    def test_failed_design_commit_preserves_destination_and_keeps_companion_retryable(self):
        window = self._main_window()
        self.addCleanup(self._close_main_window, window)
        previous, target = self.root / "old.roar", self.root / "new.roar"
        companion = self.root / "pair.roar.floorplan"
        window._record_design_document(previous)
        project = FloorplanProject.from_text(CIRCUIT)
        project.roar_project_path = str(previous)
        self.dialog.set_project(project, companion, self.source, "live")
        target.write_text("existing destination")
        replace = os.replace
        def fail_design_replace(source, destination):
            if Path(destination) == target:
                raise OSError("design destination unavailable")
            return replace(source, destination)
        with patch.object(QFileDialog, "getSaveFileName", return_value=(str(target), "")), \
             patch("roar_api.floorplan.workspace.os.replace", side_effect=fail_design_replace), \
             patch.object(QMessageBox, "critical") as error, \
             patch.object(QMessageBox, "information") as success:
            window.save_app_state()
        error.assert_called_once()
        success.assert_not_called()
        self.assertEqual(target.read_text(), "existing destination")
        self.assertEqual(window.floorplan_workspace().path, str(previous))
        self.assertEqual(self.dialog.project.roar_project_path, str(previous))
        self.assertTrue(self.dialog._dirty)
        self.dialog._ensure_current_source()
        self.assertEqual(list(self.root.glob(".new.roar.*")), [])
        self.assertTrue(self.dialog.save_for_application())
        self.assertEqual(FloorplanProject.load(companion).roar_project_path, str(previous))

    def test_checkout_opens_floorplan_without_editable_install(self):
        # A fresh process with the existing GUI dependencies but no editable
        # import mappings reproduces launching src/roar.py after a branch switch.
        script = """
import sys
from pathlib import Path
sys.meta_path[:] = [f for f in sys.meta_path if not f.__module__.startswith('__editable__')]
sys.path_hooks[:] = [h for h in sys.path_hooks if not h.__module__.startswith('__editable__')]
sys.path_importer_cache.clear()
root = Path(sys.argv[1])
sys.path.insert(0, str(root))
if sys.argv[2] == 'script':
    sys.path.insert(0, str(root / 'src'))
from roar_api.floorplan import FloorplanProject
example = root / 'examples/floorplan/ota/cm_ota.roar.floorplan'
project = FloorplanProject.load(example)
assert len(project.devices) == 10
from roar_api import Session
session = Session(home=root)
try:
    session.app.header.on_layout_button_clicked()
    editor = session.app.header._floorplan_dialog
    assert editor is not None and editor.isVisible()
    editor.set_project(project, example)
    assert editor.devices_table.rowCount() == 10
finally:
    session.close()
"""
        for launch in ("script", "module"):
            with self.subTest(launch=launch):
                result = subprocess.run(
                    [sys.executable, "-I", "-c", script, str(Path(__file__).resolve().parents[3]), launch],
                    env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_cli_and_python_open_saved_documents_with_the_same_identity(self):
        path = self.root / "cli.roar"
        self.source.save(path)
        script = r'''
import signal
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
from roar_api import Session, launch
from roar_api.__main__ import main
path = Path(sys.argv[2]).resolve()
entry = sys.argv[3]
previous_handler = signal.getsignal(signal.SIGINT)
def inspect(session):
    workspace = session.app.floorplan_workspace()
    assert workspace.path == str(path), workspace.path
    assert not workspace.is_modified()
    assert workspace.snapshot().instances[0]['Instance'] == 'input_pair'
    assert signal.getsignal(signal.SIGINT) == previous_handler
    session.close()
    return 0
if entry == 'cli':
    sys.argv = ['roar', str(path)]
    with patch('roar_api.floorplan.doctor.check_setup', return_value=0), patch.object(Session, 'run', inspect):
        assert main() == 0
elif entry == 'launch':
    inspect(launch(path))
else:
    session = Session.from_file(path)
    assert session._app is None
    inspect(session)
'''
        for entry in ('cli', 'launch', 'session'):
            with self.subTest(entry=entry):
                result = subprocess.run(
                    [sys.executable, "-I", "-c", script, str(Path(__file__).resolve().parents[3]), str(path), entry],
                    env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_main_window_cancel_does_not_shutdown_placement(self):
        window = self._main_window()
        window.show()
        self.dialog.devices_table.item(0, 3).setText("9")
        try:
            with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Cancel), \
                 patch.object(self.dialog.placement_panel, "shutdown") as shutdown:
                self.assertFalse(window.close())
            shutdown.assert_not_called()
            self.assertTrue(window.isVisible())
        finally:
            window.header._floorplan_dialog = None
            window.close()
            window.deleteLater()

    def test_save_app_state_saves_companion_and_invalid_floorplan_prevents_success(self):
        window = self._main_window()
        state_path, floorplan_path = self.root / "app.roar", self.root / "pair.roar.floorplan"
        self.dialog.devices_table.item(0, 3).setText("3")
        try:
            with patch.object(QFileDialog, "getSaveFileName", side_effect=[(str(state_path), ""), (str(floorplan_path), "")]), \
                 patch.object(QMessageBox, "information") as success:
                window.save_app_state()
            self.assertEqual(json.loads(state_path.read_text())["marker"], "saved")
            self.assertEqual(FloorplanProject.load(floorplan_path).devices[0].width_um, 3)
            self.assertFalse(self.dialog._dirty)
            self.assertIn(str(floorplan_path), success.call_args.args[2])
            self.dialog.devices_table.item(0, 3).setText("unfinished")
            failed_path = self.root / "must-not-exist.roar"
            with patch.object(QFileDialog, "getSaveFileName", return_value=(str(failed_path), "")), \
                 patch.object(QMessageBox, "warning") as warning, \
                 patch.object(QMessageBox, "information") as success:
                window.save_app_state()
            warning.assert_called_once()
            success.assert_not_called()
            self.assertFalse(failed_path.exists())
        finally:
            window.header._floorplan_dialog = None
            window.close()
            window.deleteLater()

    def test_direct_design_load_respects_floorplan_cancel(self):
        from roar_api._backend import load_backend
        editor_type = load_backend("design_editor").ROAREditorWindow
        path = self.root / "design.json"
        path.write_text("{}")
        confirm = Mock(return_value=False)
        editor = SimpleNamespace(top_level_app=SimpleNamespace(_confirm_floorplan_navigation=confirm))
        editor_type.load_all_data(editor, str(path), show_success_message=False)
        confirm.assert_called_once()

    def test_session_close_honors_window_cancellation(self):
        from roar_api import Session
        session = Session.__new__(Session)
        session._closed, session._owns_window = False, True
        session._app = Mock()
        session._app.close.return_value = False
        session.close()
        self.assertFalse(session._closed)
        session._app.deleteLater.assert_not_called()
        session._app.close.return_value = True
        session._app.editor_window._iterative_solver_dialog = None
        session.close()
        self.assertTrue(session._closed)
        session._app.deleteLater.assert_called_once()


if __name__ == "__main__":
    unittest.main()
