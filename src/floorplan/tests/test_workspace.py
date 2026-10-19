"""Document operations use fake editor callbacks and temporary files only."""
from copy import deepcopy
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from roar_api import Design
from roar_api.floorplan.workspace import DesignWorkspace, editor_signature, load_roar_design


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.design = Design().add_instance("M1", W=2.2e-6, L=0.5e-6, NF=2)
        self.graphs = {"keep": [1, 2, 3]}
        self.applies = []
        self.capture_state_calls = 0
        self.workspace = DesignWorkspace(capture_design=lambda: self.design,
                                         capture_state=self.capture_state, apply_design=self.apply)

    def capture_state(self):
        self.capture_state_calls += 1
        state = self.design.to_state()
        state["test_graph_marker"] = deepcopy(self.graphs)
        return state

    def apply(self, design):
        self.applies.append(design.to_state())
        self.design = Design.from_state(design.to_state())

    def target(self, name="target.roar"):
        path = self.root / name
        Design("Other graph").add_instance("M2", W=3e-6, L=0.5e-6, NF=4).save(path)
        return path

    def test_untracked_content_is_unsaved_and_snapshot_is_detached_and_cheap(self):
        self.assertIsNone(self.workspace.path)
        self.assertTrue(self.workspace.is_modified())
        snapshot = self.workspace.snapshot()
        snapshot.set_instance_attribute("M1", "W", 9e-6)
        self.assertEqual(self.design.get_instance_parameters()["M1"]["W"]["value"], 2.2e-6)
        self.assertEqual(self.capture_state_calls, 0)
        self.design = Design()
        self.assertFalse(self.workspace.is_modified())

    def test_companion_commit_failure_rolls_back_design_bytes_and_identity(self):
        previous = self.root / 'original.roar'
        self.workspace.record(previous)
        self.design.set_instance_attribute('M1', 'W', 4e-6)
        for exists in (False, True):
            with self.subTest(existing_destination=exists):
                target = self.root / f'target-{exists}.roar'
                if exists:
                    target.write_bytes(b'previous destination')
                @contextmanager
                def fail_after_design_commit():
                    yield True
                    self.assertEqual(self.workspace.path, str(target))
                    self.assertEqual(Design.load(target).get_instance_parameters()['M1']['W']['value'], 4e-6)
                    raise OSError('companion ownership commit failed')
                with self.assertRaisesRegex(OSError, 'companion ownership'):
                    self.workspace.save_document(target, self.design.to_state(), save_context=fail_after_design_commit())
                self.assertEqual(self.workspace.path, str(previous))
                self.assertTrue(self.workspace.is_modified())
                if exists:
                    self.assertEqual(target.read_bytes(), b'previous destination')
                else:
                    self.assertFalse(target.exists())

    def test_record_tracks_path_and_design_changes_without_graph_changes(self):
        path = self.root / "original.roar"
        self.workspace.record(path)
        self.assertEqual(self.workspace.path, str(path))
        self.assertFalse(self.workspace.is_modified())
        self.graphs["keep"].append(4)
        self.assertFalse(self.workspace.is_modified())
        self.design.set_instance_attribute("M1", "W", 3e-6)
        self.assertTrue(self.workspace.is_modified())

    def test_recording_unfinished_editor_tracks_identity_without_validating_sizes(self):
        self.workspace.record(self.root / "previous.roar")
        self.workspace._capture_design = lambda: {"instance_table": [{"Instance": ""}]}
        self.workspace.record(self.root / "unfinished.json")
        self.assertEqual(self.workspace.path, str(self.root / "unfinished.json"))
        self.assertFalse(self.workspace.is_modified())
        with self.assertRaises(ValueError):
            self.workspace.snapshot()
        self.workspace._capture_design = lambda: {"instance_table": "unreadable"}
        with self.assertRaises(ValueError):
            self.workspace.record(self.root / "unfinished.json")
        self.assertIsNone(self.workspace.path)

    def test_unfinished_editor_can_be_discarded_and_failed_switch_restores_raw_rows(self):
        state = self.design.to_state()['design_editor']
        state['instance_table'].append({'Instance': '', 'W': 'unfinished', 'disabled': False})
        holder = {'editor': deepcopy(state)}
        def apply(design):
            holder['editor'] = design.to_state()['design_editor']
        def restore(editor):
            holder['editor'] = deepcopy(editor)
        workspace = DesignWorkspace(
            capture_design=lambda: Design.from_state(holder['editor']),
            capture_editor=lambda: holder['editor'], capture_state=lambda: {},
            apply_design=apply, apply_editor=restore)
        self.assertTrue(workspace.is_modified())
        self.assertTrue(workspace.has_content())
        with self.assertRaises(ValueError):
            workspace.snapshot()
        target = self.target()
        def fail_after_apply(design):
            apply(design)
            raise RuntimeError('target editor failed')
        workspace._apply_design = fail_after_apply
        with self.assertRaisesRegex(RuntimeError, 'target editor failed'):
            workspace.switch(target)
        self.assertEqual(holder['editor'], state)
        self.assertIsNone(workspace.path)
        workspace._apply_design = apply
        workspace.switch(target)
        self.assertEqual(workspace.path, str(target))
        self.assertEqual(workspace.snapshot().instances[0]['Instance'], 'M2')

    def test_failed_raw_capture_cannot_be_treated_as_an_empty_editor(self):
        original = self.root / 'current.roar'
        self.workspace.record(original)
        target = self.target()
        for unreadable in ({}, {'design_editor': {}}, {'iterative_solver': {}}):
            self.workspace._capture_editor = lambda value=unreadable: value
            with self.subTest(value=unreadable):
                for action in (self.workspace.editor_snapshot, self.workspace.has_content,
                               self.workspace.is_modified, lambda: self.workspace.switch(target)):
                    with self.assertRaisesRegex(ValueError, 'could not be read'):
                        action()
                self.assertEqual(self.workspace.path, str(original))
                self.assertEqual(self.applies, [])
        # A recognized table is enough: an empty or unfinished table is valid
        # raw editor data even though the sizing snapshot may reject its rows.
        self.workspace._capture_editor = lambda: {'instance_table': []}
        self.assertFalse(self.workspace.has_content())
        self.workspace._capture_editor = lambda: {'instance_table': [{'Instance': ''}]}
        self.assertTrue(self.workspace.has_content())

    def test_saved_design_loader_reports_excessive_json_as_input_errors(self):
        path = self.root / 'malformed.roar'
        for content in ('[' * 1500 + '0' + ']' * 1500,
                        json.dumps({'instance_table': [{'Instance': 'M1', 'W': 10 ** 400}]}),
                        json.dumps({'design_editor': {'instance_table': []}, 'graph_tabs': None})):
            path.write_text(content)
            with self.subTest(content=content[:40]), self.assertRaises(ValueError):
                load_roar_design(path)

    def test_save_complete_document_is_atomic_and_sets_baseline(self):
        path = self.root / "new.roar"
        actual = self.workspace.save(path)
        state = json.loads(path.read_text())
        self.assertIn("design_editor", state)
        self.assertEqual(state["test_graph_marker"], self.graphs)
        self.assertIn("saved_at", state)
        self.assertEqual(editor_signature(load_roar_design(path)), editor_signature(actual))
        self.assertEqual(self.workspace.path, str(path))
        self.assertFalse(self.workspace.is_modified())
        self.assertEqual(self.capture_state_calls, 1)

    def test_overwrite_preserves_document_shape_regardless_of_filename_suffix(self):
        for name, complete in (("editor.roar", False), ("app.json", True)):
            path = self.root / name
            if complete:
                self.design.save(path)
            else:
                self.design.export_design(path)
            self.design.set_instance_attribute("M1", "W", 3e-6)
            self.workspace.save(path)
            self.assertEqual("design_editor" in json.loads(path.read_text()), complete)
            self.assertEqual(load_roar_design(path).get_instance_parameters()["M1"]["W"]["value"], 3e-6)

    def test_new_json_is_editor_only_and_does_not_capture_graphs(self):
        path = self.root / "new.json"
        self.workspace.save(path)
        self.assertIn("instance_table", json.loads(path.read_text()))
        self.assertEqual(self.capture_state_calls, 0)

    def test_failed_atomic_replace_preserves_original_file_path_and_dirty_state(self):
        path = self.target()
        before = path.read_bytes()
        self.workspace.record(self.root / "current.roar")
        self.design.set_instance_attribute("M1", "W", 4e-6)
        with patch("roar_api.floorplan.workspace.os.replace", side_effect=OSError("disk unavailable")):
            with self.assertRaisesRegex(OSError, "disk unavailable"):
                self.workspace.save(path)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.workspace.path, str(self.root / "current.roar"))
        self.assertTrue(self.workspace.is_modified())
        self.assertEqual(list(self.root.glob(".target.roar.*")), [])

    def test_switch_changes_only_editor_and_does_not_write_source_files(self):
        path = self.target()
        before = path.read_bytes()
        original_graphs = deepcopy(self.graphs)
        result = self.workspace.switch(path)
        self.assertEqual([row["Instance"] for row in result.instances], ["M2"])
        self.assertEqual(self.graphs, original_graphs)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.workspace.path, str(path))
        self.assertFalse(self.workspace.is_modified())
        self.assertEqual(self.capture_state_calls, 0)

    def test_invalid_target_is_rejected_before_applying_or_changing_baseline(self):
        for state in ({}, [], {"unrelated": 1}, {"version": 3, "source_netlist": "..."},
                      {"instance_table": "invalid"},
                      {"instance_table": [{"Instance": "M1", "_attributes": None}]},
                      {"instance_table": [], "expression_tabs": "invalid"},
                      {"instance_table": [{"Instance": "M1"}, {"Instance": "M1"}]}):
            path = self.root / "invalid.json"
            path.write_text(json.dumps(state))
            with self.assertRaises((ValueError, TypeError)):
                self.workspace.switch(path)
        self.assertEqual(self.applies, [])
        self.assertIsNone(self.workspace.path)
        self.assertTrue(self.workspace.is_modified())

    def test_failed_partial_apply_rolls_back_and_preserves_document_identity(self):
        self.workspace.record(self.root / "current.roar")
        before = self.design.to_state()
        original_apply = self.workspace._apply_design
        def apply_then_fail(design):
            original_apply(design)
            if design.instances[0]["Instance"] == "M2":
                raise RuntimeError("editor refused target")
        self.workspace._apply_design = apply_then_fail
        with self.assertRaisesRegex(RuntimeError, "editor refused target"):
            self.workspace.switch(self.target())
        self.assertEqual(self.design.to_state(), before)
        self.assertEqual(self.workspace.path, str(self.root / "current.roar"))
        self.assertFalse(self.workspace.is_modified())

    def test_silent_partial_apply_is_detected_and_rolled_back(self):
        before = self.design.to_state()
        def drop_target_width(design):
            self.apply(design)
            if design.instances[0]["Instance"] == "M2":
                self.design.set_instance_attribute("M2", "W", 1e-6)
        self.workspace._apply_design = drop_target_width
        with self.assertRaisesRegex(ValueError, "could not load all"):
            self.workspace.switch(self.target())
        self.assertEqual(self.design.to_state(), before)
        self.assertIsNone(self.workspace.path)

    def test_rollback_failure_is_explicit(self):
        self.workspace._apply_design = lambda _design: (_ for _ in ()).throw(RuntimeError("editor failure"))
        with self.assertRaisesRegex(RuntimeError, "restoring the previous design also failed"):
            self.workspace.switch(self.target())

    def test_redundant_display_defaults_do_not_hide_assigned_value_changes(self):
        original = self.design.to_state()
        rendered = deepcopy(original)
        row = rendered["design_editor"]["instance_table"][0]
        row["W"] = "2.2e-06"
        row["extra_empty_column"] = ""
        rendered["design_editor"]["expression_tabs"] = ["Expressions"]
        self.assertEqual(editor_signature(Design.from_state(original)),
                         editor_signature(Design.from_state(rendered)))
        row["_attributes"]["W"]["resolved"] = 4e-6
        self.assertNotEqual(editor_signature(Design.from_state(original)),
                            editor_signature(Design.from_state(rendered)))


if __name__ == "__main__":
    unittest.main()
