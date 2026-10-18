"""Designer controls must preserve native semantics and reject contradictions."""
import json
from pathlib import Path
import tempfile
import unittest

from roar_api.floorplan import FloorplanProject
from roar_api.floorplan.constraints import net_pins

from roar_api.floorplan.tests.constraint_cases import CASES, EXPORT_ONLY, NETLIST


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
        for rule in EXPORT_ONLY:
            with self.subTest(kind=rule['constraint']):
                self.project.set_constraints([rule])
                self.project.validate()

    def test_zero_net_weight_uses_native_netpriority_instead_of_ignored_netconst(self):
        with self.assertRaisesRegex(ValueError, 'use NetPriority with weight 0'):
            self.project.set_constraints([{'constraint': 'NetConst', 'nets': ['d1'], 'criticality': 0}])
        rule = {'constraint': 'NetPriority', 'nets': ['d1'], 'weight': 0}
        self.project.set_constraints([rule])
        with tempfile.TemporaryDirectory() as directory:
            _, path = self.project.export_align(directory)
            self.assertEqual(json.loads(path.read_text()), [rule])

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

    def test_repeated_supply_rows_are_rejected_atomically_in_either_order(self):
        self.project.set_constraints([{'constraint': 'ClockPorts', 'ports': ['g']}])
        before = self.project.to_dict()
        for kind in ('PowerPorts', 'GroundPorts'):
            first = {'constraint': kind, 'ports': ['s']}
            for ports in (['b'], ['S']):
                for propagation in ({}, {'propagate': True}, {'propagate': False}):
                    second = dict(constraint=kind, ports=ports, **propagation)
                    for rules in ([first, second], [second, first]):
                        with self.subTest(rules=rules):
                            with self.assertRaisesRegex(ValueError, f'Constraints 1 and 2: overlapping {kind}.*one enabled row'):
                                self.project.set_constraints(rules)
                            self.assertEqual(self.project.to_dict(), before)

    def test_imported_supply_rows_remain_editable_but_cannot_export_until_repaired(self):
        for kind in ('PowerPorts', 'GroundPorts'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                self.project.constraints = [
                    {'constraint': kind, 'ports': ['s']},
                    {'constraint': 'FutureRule'},
                    {'constraint': kind, 'ports': ['b'], 'propagate': False},
                ]
                self.project.constraint_enabled = [True, False, True]
                saved = self.project.save(Path(directory) / 'draft.roar.floorplan')
                reopened = FloorplanProject.load(saved)
                self.assertEqual(reopened.to_dict(), self.project.to_dict())
                destination = Path(directory) / 'export'
                with self.assertRaisesRegex(ValueError, f'Constraints 1 and 3: overlapping {kind}'):
                    reopened.export_align(destination)
                self.assertFalse(destination.exists())
                reopened.set_constraint_enabled(0, False)
                _, path = reopened.export_align(destination)
                self.assertEqual(json.loads(path.read_text()), [self.project.constraints[2]])
                self.assertEqual(reopened.constraints, self.project.constraints)

    def test_supply_lists_preserve_port_order_and_propagation_on_export(self):
        for propagate in (True, False):
            with self.subTest(propagate=propagate), tempfile.TemporaryDirectory() as directory:
                rules = [
                    {'constraint': 'PowerPorts', 'ports': ['d2', 'd1'], 'propagate': propagate},
                    {'constraint': 'GroundPorts', 'ports': ['s', 'b'], 'propagate': propagate},
                    {'constraint': 'ClockPorts', 'ports': ['g']},
                ]
                self.project.set_constraints(rules)
                reopened = FloorplanProject.from_dict(self.project.to_dict())
                _, path = reopened.export_align(directory)
                self.assertEqual(json.loads(path.read_text()), rules)

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

    def test_native_overrides_are_rejected_in_both_orders_with_original_row_numbers(self):
        pairs = [
            ({'constraint': 'HorizontalDistance', 'abs_distance': 5000},
             {'constraint': 'BlockDistance', 'abs_distance': 420}),
            ({'constraint': 'VerticalDistance', 'abs_distance': 5000},
             {'constraint': 'BlockDistance', 'abs_distance': 420}),
            ({'constraint': 'NetPriority', 'nets': ['d1'], 'weight': 3},
             {'constraint': 'NetConst', 'nets': ['D1'], 'criticality': 1}),
        ]
        for kind in ('Boundary', 'AspectRatio', 'CompactPlacement', 'ConfigureCompiler',
                     'BlockDistance', 'HorizontalDistance', 'VerticalDistance', 'NetPriority'):
            rule = next((c for c in CASES if c['constraint'] == kind), {'constraint': kind})
            pairs.append((rule, rule))
        for first, second in pairs:
            for rules in ([first, second], [second, first]):
                with self.subTest(rules=rules), self.assertRaisesRegex(ValueError, 'Constraints 2 and 3: overlapping'):
                    self.project.constraints = [{'constraint': 'FutureRule'}] + rules
                    self.project.constraint_enabled = [False, True, True]
                    self.project.validate()
        self.project.set_constraints([
            {'constraint': 'HorizontalDistance', 'abs_distance': 5000},
            {'constraint': 'VerticalDistance', 'abs_distance': 420},
        ])
        self.project.validate()

    def test_netconst_only_reserves_placement_weight_for_nonnull_criticality(self):
        for fields in ({}, {'criticality': None}, {'criticality': 2}):
            for weight_rule in (
                {'constraint': 'NetPriority', 'nets': ['D1'], 'weight': 0},
                {'constraint': 'NetConst', 'nets': ['D1'], 'criticality': 3},
            ):
                shield_rule = {'constraint': 'NetConst', 'nets': ['d1'], 'shield': 's', **fields}
                for rules in ([shield_rule, weight_rule], [weight_rule, shield_rule]):
                    with self.subTest(fields=fields, rules=rules):
                        if fields.get('criticality') is not None:
                            with self.assertRaisesRegex(ValueError, 'overlapping placement weight'):
                                self.project.set_constraints(rules)
                        else:
                            self.project.set_constraints(rules)
                            self.project.validate()


try:
    from PyQt6.QtWidgets import QApplication, QDialog
    from roar_api.floorplan.constraint_editor import ConstraintEditorDialog, editable_constraint
except ModuleNotFoundError as exc:
    if exc.name != "PyQt6":
        raise
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

    def test_net_priority_selects_circuit_nets_and_rejects_invalid_weights(self):
        editor = ConstraintEditorDialog(FloorplanProject.from_text(NETLIST))
        editor.kind.setCurrentIndex(editor.kind.findData('NetPriority'))
        self.assertEqual({editor.names_editor.choice.itemText(i) for i in range(editor.names_editor.choice.count())}, {'D1', 'D2', 'G', 'S', 'B'})
        editor.names_editor.choice.setCurrentText('D1')
        editor.names_editor.add_name()
        for value in ('-1', '1.5'):
            editor.numbers['weight'][0].setText(value)
            editor.accept()
            self.assertIsNone(editor.constraint)
        editor.numbers['weight'][0].setText('4')
        editor.accept()
        self.assertEqual(editor.constraint, {'constraint': 'NetPriority', 'nets': ['D1'], 'weight': 4})
        editor.deleteLater()


if __name__ == '__main__':
    unittest.main()
