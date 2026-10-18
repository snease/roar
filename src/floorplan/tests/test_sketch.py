"""Persisted intent and live native relationships, independent of Qt/ALIGN."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from roar_api.floorplan.model import FloorplanProject
from roar_api.floorplan.sketch import (
    BLOCK_HEIGHT, BLOCK_WIDTH, COORDINATE_LIMIT, SketchConflict,
    sketch_capability, solve_positions,
)


CIRCUIT = '.subckt pair d g s b\n' + ''.join(
    f'{name} d g s b nmos W=2u L=.5u nf=2\n' for name in ('Mfirst', 'M2', 'M3', 'M4', 'M5')) + '.ends pair\n'


class SketchTests(unittest.TestCase):
    def project(self):
        return FloorplanProject.from_text(CIRCUIT)

    def test_versions_one_through_four_migrate_without_native_loss(self):
        project = self.project()
        project.constraints = [{'constraint': 'FutureNativeRule', 'private': {'anything': [1, 2]}},
                               {'constraint': 'Align', 'instances': ['Mfirst', 'M2'], 'line': 'h_center'}]
        project.constraint_enabled = [False, True]
        for version in (1, 2, 3, 4):
            state = project.to_dict()
            state['version'] = version
            for key in ('sketch', 'constraint_ids', 'device_labels'):
                state.pop(key)
            if version < 4:
                for key in ('source_mode', 'run_options', 'constraint_enabled'):
                    state.pop(key)
            if version < 3:
                state.pop('align_profile')
            if version < 2:
                for key in ('parameters', 'overrides', 'roar_project_path'):
                    state.pop(key)
            with self.subTest(version=version):
                restored = FloorplanProject.from_dict(state)
                self.assertEqual(restored.constraints, project.constraints)
                self.assertEqual(len(set(map(tuple, restored.sketch['positions'].values()))), 5)
                self.assertEqual(restored.sketch, FloorplanProject.from_dict(state).sketch)
                if version == 4:
                    self.assertEqual(restored._constraint_flags(), [False, True])
                self.assertEqual(restored.to_dict()['version'], 6)

    def test_save_groups_case_identity_and_constraint_ids(self):
        project = self.project()
        project.add_constraint({'constraint': 'Order', 'instances': ['mfirst', 'm2'], 'direction': 'left_to_right'})
        identity = project.constraint_ids[0]
        project.set_sketch({'positions': {'mFIRST': [-90, 33]},
                            'groups': [{'id': 'input-pair', 'label': 'Input pair', 'instances': ['mfirst', 'M2']}]})
        project.set_constraints([{'constraint': 'Order', 'instances': ['M2', 'Mfirst'], 'direction': 'left_to_right'}])
        self.assertEqual(project.constraint_ids, [identity])
        project.add_constraint({'constraint': 'Align', 'instances': ['M3', 'M4'], 'line': 'h_center'})
        second = project.constraint_ids[1]
        project.remove_constraint(0)
        self.assertEqual(project.constraint_ids, [second])
        self.assertNotIn('id', project.constraints[0])
        with tempfile.TemporaryDirectory() as directory:
            path = project.save(Path(directory) / 'pair.floorplan')
            restored = FloorplanProject.load(path)
            self.assertEqual(restored.to_dict(), project.to_dict())
            _, native = restored.export_align(Path(directory) / 'export')
            self.assertEqual(json.loads(native.read_text()), project.constraints)
        detached = project.sketch
        detached['positions']['Mfirst'][0] = 999
        self.assertEqual(project.sketch['positions']['Mfirst'], [-90, 33])
        self.assertEqual(project.sketch['groups'][0]['instances'], ['Mfirst', 'M2'])

    def test_malformed_sketch_and_ids_are_atomic(self):
        project = self.project()
        before = project.to_dict()
        for value in (float('nan'), float('inf'), True, COORDINATE_LIMIT + 1, '5'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                project.set_sketch({'positions': {'M2': [value, 0]}, 'groups': []})
            self.assertEqual(project.to_dict(), before)
        for groups in ([{'id': 'g', 'label': 'bad', 'instances': ['nope']}],
                       [{'id': 'g', 'label': 'one', 'instances': ['M2']},
                        {'id': 'h', 'label': 'two', 'instances': ['m2']}],
                       [{'id': 'g', 'label': '', 'instances': ['M2']}]):
            with self.assertRaises(ValueError):
                project.set_sketch({'positions': {}, 'groups': groups})
        project.add_constraint({'constraint': 'Align', 'instances': ['M3', 'M4'], 'line': 'h_center'})
        state = project.to_dict()
        state['constraint_ids'] = []
        with self.assertRaisesRegex(ValueError, 'Constraint IDs'):
            FloorplanProject.from_dict(state)

    def test_saved_link_resolves_relative_to_companion(self):
        project = self.project()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            linked = root / 'source.roar'
            linked.write_text('{}')
            project.roar_project_path = linked
            project.source_mode = 'linked'
            saved = project.save(root / 'companions' / 'pair.floorplan')
            state = json.loads(saved.read_text())
            self.assertEqual(state['version'], 6)
            self.assertEqual(state['roar_project_path'], '../source.roar')
            restored = FloorplanProject.load(saved)
            self.assertEqual(restored.roar_project_path, str(linked))
            self.assertEqual(restored.source_mode, 'linked')
            self.assertEqual(restored.to_dict(), project.to_dict())

    def test_order_row_and_screen_y_semantics(self):
        project = self.project()
        project.set_constraints([{'constraint': 'Order', 'instances': ['Mfirst', 'M2'], 'direction': 'top_to_bottom'},
                                 {'constraint': 'AlignInOrder', 'instances': ['M3', 'M4', 'M5'], 'line': 'center', 'direction': 'horizontal'}])
        solved = solve_positions(project)
        self.assertGreaterEqual(solved['M2'][1] - solved['Mfirst'][1], BLOCK_HEIGHT - 1e-6)
        self.assertNotEqual(solved['M2'][0], solved['Mfirst'][0])  # Order does not align.
        for first, second in (('M3', 'M4'), ('M4', 'M5')):
            self.assertAlmostEqual(solved[first][1], solved[second][1])
            self.assertGreaterEqual(solved[second][0] - solved[first][0], BLOCK_WIDTH - 1e-6)
        project.set_constraints([{'constraint': 'AlignInOrder', 'instances': ['Mfirst', 'M2'], 'line': 'left', 'direction': 'vertical', 'abut': True}])
        solved = solve_positions(project)
        self.assertAlmostEqual(solved['Mfirst'][0], solved['M2'][0])
        self.assertAlmostEqual(solved['M2'][1] - solved['Mfirst'][1], BLOCK_HEIGHT)

    def test_symmetry_shared_axis_and_drag_yields_to_required_rules(self):
        project = self.project()
        project.set_constraints([{'constraint': 'SymmetricBlocks', 'pairs': [['Mfirst', 'M2'], ['M3', 'M4'], ['M5']], 'direction': 'V'},
                                 {'constraint': 'Align', 'instances': ['Mfirst', 'M2'], 'line': 'h_center'}])
        original = project.to_dict()
        solved = solve_positions(project, suggestions={'mfirst': [0, 77], 'M2': [400, 98], 'M5': [300, 12]})
        axis = solved['M5'][0]
        self.assertAlmostEqual((solved['Mfirst'][0] + solved['M2'][0]) / 2, axis)
        self.assertAlmostEqual((solved['M3'][0] + solved['M4'][0]) / 2, axis)
        self.assertAlmostEqual(solved['Mfirst'][1], solved['M2'][1])
        self.assertLessEqual(abs(solved['M3'][1] - solved['M4'][1]), BLOCK_HEIGHT / 4 + 1e-6)
        self.assertEqual(project.to_dict(), original)

    def test_conflicts_identify_rules_and_preserve_prior_state(self):
        project = self.project()
        # Imported contradictions must remain inspectable, even though native
        # set_constraints correctly rejects this combination earlier.
        project.constraints = [{'constraint': 'Order', 'instances': ['Mfirst', 'M2'], 'direction': 'left_to_right'},
                               {'constraint': 'Align', 'instances': ['Mfirst', 'M2'], 'line': 'v_center'}]
        before = deepcopy(project.to_dict())
        with self.assertRaises(SketchConflict) as caught:
            solve_positions(project)
        self.assertEqual(set(caught.exception.rule_ids), set(project.constraint_ids))
        self.assertIn('Mfirst', str(caught.exception))
        self.assertEqual(project.to_dict(), before)
        project.set_constraint_enabled(1, False)
        solved = solve_positions(project, suggestions={'Mfirst': [500, 0], 'M2': [0, 0]})
        self.assertGreaterEqual(solved['M2'][0] - solved['Mfirst'][0], BLOCK_WIDTH - 1e-6)

    def test_symmetry_drawing_survives_unequal_resolved_sizing(self):
        project = self.project()
        project.set_constraints([{'constraint': 'SymmetricBlocks', 'pairs': [['Mfirst', 'M2']], 'direction': 'V'}])
        project.set_mos_parameters('M2', 4, .5, 2)
        before = project.to_dict()
        solved = solve_positions(project, suggestions={'M5': [1234, 567]})
        self.assertEqual(solved['M5'], [1234, 567])
        self.assertEqual(project.to_dict(), before)
        # Physical native checks still reject mismatched sizes for generation.
        with self.assertRaisesRegex(ValueError, 'matching models and parameters'):
            project.validate()
        project.constraints = [{'constraint': 'SymmetricBlocks', 'pairs': [['Mfirst', 'Missing']], 'direction': 'V'}]
        with self.assertRaises(SketchConflict) as caught:
            solve_positions(project)
        self.assertIn('Mfirst', caught.exception.instances)
        self.assertIn('Missing', caught.exception.instances)

    def test_feasible_saved_symmetry_positions_do_not_drift_on_reopen(self):
        project = self.project()
        project.set_constraints([{'constraint': 'SymmetricBlocks', 'pairs': [['Mfirst', 'M2']], 'direction': 'V'}])
        state = project.sketch
        state['positions'].update(Mfirst=[0, 0], M2=[200, 10])
        project.set_sketch(state)
        reopened = FloorplanProject.from_dict(project.to_dict())
        self.assertEqual(solve_positions(reopened), state['positions'])

    def test_capability_does_not_claim_unvalidated_native_export(self):
        project = self.project()
        unsupported = [{'constraint': 'Order', 'instances': ['Mfirst', 'M2'], 'direction': 'right_to_left'},
                       {'constraint': 'Align', 'instances': ['Mfirst', 'M2'], 'line': 'h_any'}]
        for rule in unsupported:
            self.assertEqual(sketch_capability(rule), 'unsupported')
            with self.assertRaises(ValueError):
                project.set_constraints([rule])

    def test_unsupported_and_physical_rules_do_not_move_drawing(self):
        project = self.project()
        project.constraints = [{'constraint': 'HorizontalDistance', 'abs_distance': 500000},
                               {'constraint': 'Floorplan', 'regions': [['M2'], ['Mfirst']], 'order': True},
                               {'constraint': 'FutureRule', 'opaque': True}]
        self.assertEqual([sketch_capability(row) for row in project.constraints], ['exported', 'exported', 'unsupported'])
        self.assertEqual(solve_positions(project), project.sketch['positions'])


if __name__ == '__main__':
    unittest.main()
