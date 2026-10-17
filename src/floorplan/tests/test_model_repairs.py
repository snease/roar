"""Regression cases for safe export and editable, portable sizing drafts."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from roar_api import Design
from roar_api.floorplan import FloorplanProject


CIRCUIT = '''.subckt amp IN OUT VDD VSS
M1 OUT IN VSS VSS sky130_fd_pr__nfet_01v8 W=1u L=0.5u NF=2 M=7
R1 VDD OUT .5k
.ends amp
'''


class ModelRepairTests(unittest.TestCase):
    def project(self):
        return FloorplanProject.from_text(CIRCUIT)

    def test_annotation_and_token_injection_rejected_in_every_identifier(self):
        # ALIGN evaluates annotation text. Reject it before constructing any
        # native input, including when hidden inside a subcircuit port token.
        payloads = [";@:__import__('os').getcwd()", '$@:int(1)', '//@:int(1)',
                    '*@:int(1)', "'OUT'", '.ends', 'OUT\\', 'OUT{1}', 'OUT\x00']
        for payload in payloads:
            sources = [
                CIRCUIT.replace('amp IN', f'amp {payload}', 1),
                CIRCUIT.replace('M1 OUT', f'M{payload} OUT', 1),
                CIRCUIT.replace('M1 OUT', f'M1 {payload}', 1),
                CIRCUIT.replace('sky130_fd_pr__nfet_01v8', payload),
                CIRCUIT.replace('R1 VDD', f'R1 {payload}', 1),
            ]
            if payload == '.ends':
                sources.pop(1)  # M.ends is an ordinary instance NAME, not a directive.
            for source in sources:
                with self.subTest(payload=payload, source=source), self.assertRaises(ValueError):
                    FloorplanProject.from_text(source)

    def test_export_rechecks_mutated_public_fields_before_creating_files(self):
        mutators = [
            lambda p: setattr(p, 'top', '../escape'),
            lambda p: p.ports.append(';@:int(1)'),
            lambda p: setattr(p.devices[0], 'name', 'M1\n;@:int(1)'),
            lambda p: setattr(p.devices[0], 'model', 'model;@:int(1)'),
            lambda p: setattr(p.devices[0], 'terminals', ('OUT', 'IN', 'VSS', 'VSS\n.ends amp')),
            lambda p: p.devices[0].parameters.update({'M\n;@:int(1)': 1}),
            lambda p: setattr(p.devices[1], 'value', '1k\n;@:int(1)'),
            lambda p: p.omitted_parameters.update({'M1': ['AD\n;@:int(1)']}),
        ]
        for mutate in mutators:
            project = self.project()
            mutate(project)
            with tempfile.TemporaryDirectory() as directory:
                export = Path(directory) / 'new'
                with self.assertRaises(ValueError):
                    project.export_align(export)
                self.assertFalse(export.exists())

    def test_ordinary_spice_net_names_and_numeric_resistor_export(self):
        project = FloorplanProject.from_text(CIRCUIT.replace('IN', 'IN[0]').replace('OUT', 'stage/out:1')
                                            .replace('VSS', '0').replace('VDD', 'VDD!'))
        with tempfile.TemporaryDirectory() as directory:
            netlist, _ = project.export_align(directory)
            text = netlist.read_text()
            self.assertIn('R1 VDD! stage/out:1 500\n', text)
            self.assertIn('IN[0]', text)
            restored = FloorplanProject.from_netlist(netlist)
            self.assertEqual(restored.devices[0].terminals, project.devices[0].terminals)
            self.assertEqual(restored.devices[1].value, '500')
        self.assertIn('.5k', project.source_netlist)

    def test_nf_one_and_odd_values_are_repairable_drafts(self):
        for nf in (1, 3):
            project = FloorplanProject.from_text(CIRCUIT.replace('NF=2', f'NF={nf}'))
            project.validate()  # Backend restrictions are checked by the runner.
            restored = FloorplanProject.from_dict(project.to_dict())
            self.assertEqual(restored.devices[0].nf, nf)
            restored.set_mos_parameters('M1', 1, .5, 4)
            self.assertEqual(restored.devices[0].nf, 4)
        project = FloorplanProject.from_text(CIRCUIT.replace('NF=2', 'NF=fingers'))
        project.set_parameter('fingers', 1)
        self.assertEqual(FloorplanProject.from_dict(project.to_dict()).devices[0].nf, 1)

    def test_edit_multiplicity_and_resistance_round_trips_without_source_rewrite(self):
        project = self.project()
        project.set_mos_multiplicity('m1', 3)
        project.set_resistance('r1', '.25meg')
        state = project.to_dict()
        restored = FloorplanProject.from_dict(state)
        self.assertEqual(restored.devices[0].parameters['M'], 3)
        self.assertEqual(restored.devices[1].value, '250000')
        self.assertEqual(restored.source_netlist, CIRCUIT)
        with tempfile.TemporaryDirectory() as directory:
            netlist, _ = restored.export_align(directory)
            self.assertIn('M=3\n', netlist.read_text())
            self.assertIn('R1 VDD OUT 250000\n', netlist.read_text())
        for value in (0, -1, 1.5, True, float('inf')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                restored.set_mos_multiplicity('M1', value)
            self.assertEqual(restored.to_dict(), state)
        for value in ('x', 0, -1, float('nan')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                restored.set_resistance('R1', value)
            self.assertEqual(restored.to_dict(), state)
        absent = FloorplanProject.from_text(CIRCUIT.replace(' M=7', ''))
        absent.set_mos_multiplicity('M1', 9)
        self.assertEqual(FloorplanProject.from_dict(absent.to_dict()).devices[0].parameters['M'], 9)

    def test_shared_multiplicity_cannot_be_overridden_per_device(self):
        project = FloorplanProject.from_text(CIRCUIT.replace('M=7', 'M=multiplicity'))
        project.set_parameter('multiplicity', 7)
        before = project.to_dict()
        with self.assertRaisesRegex(ValueError, 'controlled by shared parameter'):
            project.set_mos_multiplicity('M1', 2)
        self.assertEqual(project.to_dict(), before)

    def test_binding_can_be_repaired_and_unbound_without_old_source(self):
        project = FloorplanProject.from_text(CIRCUIT.replace('W=1u L=0.5u', 'W=width L=length'))
        old = Design().add_instance('old', W=4e-6, L=.5e-6)
        project.bind_parameter(old, 'width', 'old', 'W')
        project.bind_parameter(old, 'length', 'old', 'L')
        replacement = Design().add_instance('new', W=8e-6)
        with self.assertRaisesRegex(ValueError, 'enabled ROAR instance'):
            project.refresh_bindings(replacement)
        # The old length source is also missing: replacing width must still work.
        project.bind_parameter(replacement, 'width', 'new', 'W')
        self.assertEqual(project.devices[0].width_um, 4)
        project.unbind_parameter('length')
        project.refresh_bindings(replacement)
        self.assertEqual(project.devices[0].length_um, .5)
        self.assertIsNone(next(row for row in project.parameter_rows if row['symbol'] == 'length')['binding'])
        self.assertEqual(FloorplanProject.from_dict(project.to_dict()).devices, project.devices)

    def test_source_intent_and_run_options_round_trip_and_legacy_defaults(self):
        project = self.project()
        for mode in ('saved', 'live', 'linked'):
            project.source_mode = mode
            project.run_options = {'nvariants': 8, 'placer_sa_iterations': 10000, 'seed': 42, 'lambda_coeff': .5}
            self.assertEqual(FloorplanProject.from_dict(project.to_dict()).source_mode, mode)
            self.assertEqual(FloorplanProject.from_dict(project.to_dict()).run_options, project.run_options)
        for version in (1, 2, 3):
            state = project.to_dict()
            state['version'] = version
            for key in ('source_mode', 'run_options', 'constraint_enabled'):
                state.pop(key)
            if version < 3:
                state.pop('align_profile')
            if version == 1:
                for key in ('parameters', 'overrides', 'roar_project_path'):
                    state.pop(key)
            restored = FloorplanProject.from_dict(state)
            self.assertEqual(restored.source_mode, 'saved')
            self.assertEqual(restored.run_options, {})
        invalid_options = [{'nvariants': True}, {'nvariants': 101}, {'seed': -1},
                           {'seed': 2**31}, {'lambda_coeff': float('nan')}, {'lambda_coeff': -1},
                           {'placer_sa_iterations': 0}, {'other': 1}]
        for options in invalid_options:
            state = project.to_dict()
            state['run_options'] = options
            with self.subTest(options=options), self.assertRaises(ValueError):
                FloorplanProject.from_dict(state)

    def test_disabled_imported_constraints_are_preserved_but_not_exported(self):
        project = self.project()
        unsupported = {'constraint': 'FutureNativeConstraint', 'opaque': 'preserved'}
        project.constraints = [unsupported, {'constraint': 'PowerPorts', 'ports': ['VDD']}]
        project.set_constraint_enabled(0, False)
        clone = FloorplanProject.from_dict(project.to_dict())
        self.assertEqual(clone.constraint_enabled, [False, True])
        self.assertEqual(clone.constraints[0], unsupported)
        with tempfile.TemporaryDirectory() as directory:
            _, constraints = clone.export_align(directory)
            self.assertEqual(json.loads(constraints.read_text()), [{'constraint': 'PowerPorts', 'ports': ['VDD']}])
        clone.add_constraint({'constraint': 'GroundPorts', 'ports': ['VSS']})
        self.assertEqual(clone.constraint_enabled, [False, True, True])
        clone.remove_constraint(1)
        self.assertEqual(clone.constraint_enabled, [False, True])
        state = clone.to_dict()
        for flags in ([False], [False, 1], 'false'):
            malformed = deepcopy(state)
            malformed['constraint_enabled'] = flags
            with self.assertRaises(ValueError):
                FloorplanProject.from_dict(malformed)


if __name__ == '__main__':
    unittest.main()
