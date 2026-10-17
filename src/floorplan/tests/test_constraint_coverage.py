"""Designer controls must preserve native semantics and reject contradictions."""
import json
from pathlib import Path
import tempfile
import unittest

from roar_api.floorplan import FloorplanProject
from roar_api.floorplan.constraints import capability_note, net_pins

NETLIST = '''.subckt pair d1 d2 g s b
M1 d1 g s b sky130_fd_pr__nfet_01v8 W=1u L=0.15u NF=2
M2 d2 g s b sky130_fd_pr__nfet_01v8 W=1u L=0.15u NF=2
.ends pair
'''
CASES = [
    {'constraint': 'Boundary', 'max_width': 50, 'max_height': 40, 'halo_horizontal': 1.5, 'halo_vertical': 2},
    {'constraint': 'AspectRatio', 'ratio_low': 0.5, 'ratio_high': 2, 'weight': 2},
    {'constraint': 'Spread', 'instances': ['M1', 'M2'], 'direction': 'horizontal', 'distance': 420},
    {'constraint': 'BlockDistance', 'abs_distance': 420},
    {'constraint': 'HorizontalDistance', 'abs_distance': 420},
    {'constraint': 'VerticalDistance', 'abs_distance': 280},
    {'constraint': 'SameTemplate', 'instances': ['M1', 'M2']},
    {'constraint': 'AlignInOrder', 'instances': ['M1', 'M2'], 'line': 'center', 'direction': 'vertical', 'abut': False},
    {'constraint': 'PortLocation', 'ports': ['d1'], 'location': 'TC'},
    {'constraint': 'SymmetricNets', 'net1': 'd1', 'net2': 'd2', 'pins1': ['M1/D', 'd1'], 'pins2': ['M2/D', 'd2'], 'direction': 'V'},
    {'constraint': 'ClockPorts', 'ports': ['g'], 'propagate': False},
    {'constraint': 'DoNotIdentify', 'instances': ['M1']},
    {'constraint': 'ConfigureCompiler', 'merge_series_devices': False, 'merge_parallel_devices': False, 'auto_constraint': False},
]


class ConstraintCoverageTests(unittest.TestCase):
    def setUp(self):
        self.project = FloorplanProject.from_text(NETLIST)

    def test_new_controls_save_and_export_native_values_without_unit_changes(self):
        for rule in CASES:
            with self.subTest(kind=rule['constraint']), tempfile.TemporaryDirectory() as directory:
                self.project.set_constraints([rule])
                reopened = FloorplanProject.from_dict(self.project.to_dict())
                _, path = reopened.export_align(directory)
                expected = dict(rule, ports=[p.upper() for p in rule['ports']]) if rule['constraint'] == 'PortLocation' else rule
                self.assertEqual(json.loads(path.read_text()), [expected])
                self.assertEqual(reopened.constraints, [rule])

    def test_native_export_without_form_is_not_rejected_as_unknown(self):
        for rule in [
            {'constraint': 'NetPriority', 'nets': ['d1'], 'weight': 2},
            {'constraint': 'NetConst', 'nets': ['d1'], 'shield': 's', 'criticality': 3},
            {'constraint': 'MultiConnection', 'nets': ['d1'], 'multiplier': 2},
            {'constraint': 'DoNotRoute', 'nets': ['s']},
            {'constraint': 'DoNotUseLib', 'libraries': ['DP_NMOS'], 'propagate': True},
            {'constraint': 'Route', 'min_layer': 'M1', 'max_layer': 'M4', 'customize': [{'nets': ['d1'], 'match': True}]},
        ]:
            with self.subTest(kind=rule['constraint']):
                self.project.set_constraints([rule])
                self.project.validate()

    def test_export_normalizes_identifiers_missing_native_case_validators(self):
        rules = [
            {'constraint': 'PortLocation', 'ports': ['d1'], 'location': 'TC'},
            {'constraint': 'NetConst', 'nets': ['d1'], 'shield': 's'},
            {'constraint': 'DoNotUseLib', 'libraries': ['dp_nmos']},
            {'constraint': 'Route', 'min_layer': 'm1', 'customize': [{'nets': ['d2'], 'max_layer': 'm4'}]},
        ]
        self.project.set_constraints(rules)
        with tempfile.TemporaryDirectory() as directory:
            _, path = self.project.export_align(directory)
            exported = json.loads(path.read_text())
        self.assertEqual(exported[0]['ports'], ['D1'])
        self.assertEqual(exported[1]['nets'], ['D1'])
        self.assertEqual(exported[1]['shield'], 'S')
        self.assertEqual(exported[2]['libraries'], ['DP_NMOS'])
        self.assertEqual(exported[3]['min_layer'], 'M1')
        self.assertEqual(exported[3]['customize'], [{'nets': ['D2'], 'max_layer': 'M4'}])
        self.assertEqual(self.project.constraints, rules)

    def test_cycle_across_rows_and_order_identifies_both_rows(self):
        with self.assertRaisesRegex(ValueError, r'Constraints 1, 2:.*cycle'):
            self.project.set_constraints([
                {'constraint': 'Floorplan', 'regions': [['M1'], ['M2']]},
                {'constraint': 'Order', 'instances': ['M2', 'M1'], 'direction': 'top_to_bottom'},
            ])

    def test_clock_power_and_ground_roles_cannot_conflict(self):
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            self.project.set_constraints([
                {'constraint': 'PowerPorts', 'ports': ['s']},
                {'constraint': 'GroundPorts', 'ports': ['s']},
            ])

    def test_disabled_rows_do_not_shift_error_row_numbers(self):
        self.project.constraints = [
            {'constraint': 'UnknownImportedRule'},
            {'constraint': 'Order', 'instances': ['M1', 'M2'], 'direction': 'left_to_right'},
            {'constraint': 'Order', 'instances': ['M2', 'M1'], 'direction': 'left_to_right'},
        ]
        self.project.constraint_enabled = [False, True, True]
        with self.assertRaisesRegex(ValueError, 'Constraints 2, 3:'):
            self.project.validate()

    def test_invalid_region_reports_constraint_row_not_device_list(self):
        with self.assertRaisesRegex(ValueError, r'Constraint 1 \(Floorplan\): Unknown circuit instance'):
            self.project.set_constraints([{'constraint': 'Floorplan', 'regions': [['missing']]}])

    def test_net_symmetry_checks_actual_pin_connectivity(self):
        rule = dict(CASES[9], pins1=['M2/D', 'd1'])
        with self.assertRaisesRegex(ValueError, 'Pins on d1'):
            self.project.set_constraints([rule])
        self.assertEqual(net_pins(self.project)['D1'], ['d1', 'M1/D'])

    def test_native_pin_delimiter_ambiguity_is_rejected_locally(self):
        project = FloorplanProject.from_text(NETLIST.replace('M1', 'M/A').replace('M2', 'M/B'))
        rule = dict(CASES[9], pins1=['M/A/D', 'd1'], pins2=['M/B/D', 'd2'])
        with self.assertRaisesRegex(ValueError, 'names containing /'):
            project.set_constraints([rule])
        self.assertEqual(net_pins(project)['D1'], ['d1'])

    def test_invalid_bounds_spacing_and_compiler_types_are_actionable(self):
        for rule in [
            {'constraint': 'Boundary', 'max_width': 3, 'halo_horizontal': 2},
            {'constraint': 'AspectRatio', 'ratio_low': 2, 'ratio_high': 1},
            {'constraint': 'Spread', 'instances': ['M1', 'M2'], 'direction': 'horizontal', 'distance': 0.5},
            {'constraint': 'ConfigureCompiler', 'auto_constraint': 'false'},
            {'constraint': 'Route', 'customize': [{'nets': ['missing']}]},
        ]:
            with self.subTest(rule=rule), self.assertRaisesRegex(ValueError, 'Constraint 1'):
                self.project.set_constraints([rule])

    def test_dropped_placer_rule_is_explained(self):
        note = capability_note({'constraint': 'PlaceOnBoundary'})
        self.assertIn('C++', note)
        self.assertIn('drops', note)


try:
    from PyQt6.QtWidgets import QApplication, QDialog
    from roar_api.floorplan.constraint_editor import ConstraintEditorDialog, editable_constraint
except ImportError:
    QApplication = None


@unittest.skipIf(QApplication is None, 'GUI extra required')
class ConstraintFormTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_forms_round_trip_every_new_option(self):
        for rule in CASES:
            with self.subTest(kind=rule['constraint']):
                self.assertTrue(editable_constraint(rule))
                editor = ConstraintEditorDialog(FloorplanProject.from_text(NETLIST), rule)
                editor.accept()
                self.assertEqual(editor.result(), QDialog.DialogCode.Accepted, editor.error.text())
                # Defaults can be made explicit; every existing field is retained.
                self.assertEqual({key: editor.constraint[key] for key in rule}, rule)
                editor.deleteLater()
        self.app.processEvents()

    def test_missing_optional_boundary_fields_stay_omitted(self):
        editor = ConstraintEditorDialog(FloorplanProject.from_text(NETLIST), {'constraint': 'Boundary', 'max_width': 20})
        editor.accept()
        self.assertEqual(editor.constraint, {'constraint': 'Boundary', 'max_width': 20})
        editor.deleteLater()


if __name__ == '__main__':
    unittest.main()
