"""Topology, unit conversion and failure boundaries of the standalone adapter."""
import json
from pathlib import Path
import tempfile
import unittest

from roar_api import Design
from roar_api.floorplan import FloorplanProject


CIRCUIT = '''* flat common source with matched pair
.subckt amp VIN VOUT VDD VSS
M1 VOUT VIN VSS VSS sky130_fd_pr__nfet_01v8 W=2.2u L=0.5u nf=2
X2 VOUT VIN VSS VSS sky130_fd_pr__nfet_01v8 W=2.2u L=0.5u nf=2
RLOAD VDD VOUT 10k
.ends amp
'''


class ProjectTests(unittest.TestCase):
    def project(self, **kwargs):
        return FloorplanProject.from_text(CIRCUIT, **kwargs)

    def test_export_preserves_topology_and_per_finger_si(self):
        project = self.project(width_convention='total')
        self.assertAlmostEqual(project.devices[0].width_um, 1.1)
        project.set_mos_parameters('m1', 1.5, 0.15, 4)
        with tempfile.TemporaryDirectory() as directory:
            netlist, constraints = project.export_align(directory)
            text = netlist.read_text()
            self.assertIn('M1 VOUT VIN VSS VSS sky130_fd_pr__nfet_01v8 W=1.5e-06 L=1.5e-07 nf=4', text)
            self.assertIn('X2 VOUT VIN VSS VSS sky130_fd_pr__nfet_01v8', text)
            self.assertIn('RLOAD VDD VOUT 10000', text)
            self.assertEqual(json.loads(constraints.read_text()), [])
            restored = FloorplanProject.from_netlist(netlist)
            self.assertAlmostEqual(restored.devices[0].width_um, 1.5)
            self.assertEqual(restored.devices[0].nf, 4)
            with self.assertRaises(FileExistsError):
                project.export_align(directory)

    def test_xschem_wrappers_and_indented_continuations(self):
        source = CIRCUIT.replace('.subckt amp', '**.SUBCKT amp').replace('.ends amp', '**.ENDS amp')
        source = source.replace(' W=2.2u L=0.5u nf=2', ' W=2.2u\n   + L=0.5u nf=2')
        project = FloorplanProject.from_text(source)
        self.assertEqual(project.top, 'amp')
        self.assertEqual(len(project.devices), 3)
        self.assertEqual(project.source_netlist, source)

    def test_discovery_shares_wrappers_without_resolving_sizing(self):
        source = CIRCUIT.replace('.subckt amp', '**.SUBCKT amp').replace('.ends amp', '**.ENDS amp')
        source = source.replace('W=2.2u', 'W=unresolved')
        source += '.subckt Other VDD VSS\n.ends Other\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'discovery.sp'
            path.write_text(source)
            self.assertEqual(FloorplanProject.subcircuit_names(path), ['amp', 'Other'])

    def test_original_xschem_ota_preserves_unresolved_parameters_and_declares_omissions(self):
        path = Path(__file__).resolve().parents[3] / 'design/cm_ota.spice'
        self.assertEqual(FloorplanProject.subcircuit_names(path), ['cm_ota'])
        project = FloorplanProject.from_netlist(path, top='cm_ota', dimension_unit='um', width_convention='total')
        self.assertEqual(project.source_netlist, path.read_text())
        self.assertEqual(project.devices[0].length_um, 0.5)
        self.assertIsNone(project.devices[0].width_um)
        self.assertIn('w1_2', project.unresolved_parameters)
        self.assertEqual(project.devices[0].parameters, {'M': 1})
        self.assertIn('AD', project.omitted_parameters['XM1'])
        project.set_constraints([{'constraint': 'SymmetricBlocks', 'pairs': [['XM1', 'XM2']], 'direction': 'V'}])
        clone = FloorplanProject.from_dict(project.to_dict())
        self.assertEqual(clone.to_dict(), project.to_dict())
        with self.assertRaisesRegex(ValueError, 'Unresolved sizing parameters'):
            clone.validate()

    def test_quoted_numeric_parameters_and_assignment_whitespace(self):
        source = CIRCUIT.replace('W=2.2u L=0.5u nf=2', "W = ' 2.2u ' L = '0.5u' nf = '2'")
        project = FloorplanProject.from_text(source)
        self.assertAlmostEqual(project.devices[0].width_um, 2.2)
        self.assertAlmostEqual(project.devices[0].length_um, 0.5)
        self.assertEqual(project.devices[0].nf, 2)
        for malformed in ("W='1u'L='0.5u' nf=2", "W='1u L='0.5u' nf=2", "W=1u L=0.5u nf='2"):
            with self.subTest(malformed=malformed):
                with self.assertRaisesRegex(ValueError, 'malformed MOS parameter assignments'):
                    FloorplanProject.from_text(CIRCUIT.replace('W=2.2u L=0.5u nf=2', malformed))
        unsupported = source.replace("nf = '2'", "nf = '2' ad = 'int((nf+1)/2) * W/nf * 0.29'")
        project = FloorplanProject.from_text(unsupported)
        self.assertEqual(project.omitted_parameters['M1'], ['AD'])
        with tempfile.TemporaryDirectory() as directory:
            netlist, _ = project.export_align(directory)
            self.assertIn('Layout export omits SKY130 simulation parameters for M1: AD', netlist.read_text())
            self.assertNotIn('ad=', netlist.read_text())

    def test_bare_units_and_suffix_override(self):
        bare = CIRCUIT.replace('2.2u', '2.2').replace('0.5u', '0.5')
        um = FloorplanProject.from_text(bare, dimension_unit='um', width_convention='total')
        self.assertAlmostEqual(um.devices[0].width_um, 1.1)
        si = FloorplanProject.from_text(bare.replace('2.2', '2.2e-6').replace('0.5', '0.5e-6'), width_convention='total')
        self.assertAlmostEqual(si.devices[0].width_um, um.devices[0].width_um)
        explicit = self.project(dimension_unit='um')
        self.assertAlmostEqual(explicit.devices[0].length_um, 0.5)

    def test_save_contains_source_sizes_and_native_constraints(self):
        project = self.project()
        project.set_mos_parameters('M1', 3, 0.2, 6)
        project.constraints = [{'constraint': 'FutureNativeConstraint', 'opaque': ['M1']}]
        with tempfile.TemporaryDirectory() as directory:
            path = project.save(Path(directory) / 'project.json')
            restored = FloorplanProject.load(path)
            self.assertEqual(restored.to_dict(), project.to_dict())
            self.assertEqual(restored.source_netlist, CIRCUIT)
            self.assertEqual(restored.devices[0].width_um, 3)
            with self.assertRaisesRegex(ValueError, 'unsupported native'):
                restored.export_align(Path(directory) / 'export')
            self.assertFalse((Path(directory) / 'export').exists())

    def test_source_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'original.sp'
            path.write_text(CIRCUIT)
            project = FloorplanProject.from_netlist(path)
            with self.assertRaisesRegex(ValueError, 'source netlist'):
                project.save(path)
            self.assertEqual(path.read_text(), CIRCUIT)

    def test_constraints_validate_ids_shapes_native_mapping_and_symmetry(self):
        project = self.project()
        constraints = [
            {'constraint': 'SymmetricBlocks', 'pairs': [['M1', 'X2']], 'direction': 'V'},
            {'constraint': 'Order', 'instances': ['M1', 'X2'], 'direction': 'left_to_right'},
            {'constraint': 'Align', 'instances': ['M1', 'X2'], 'line': 'h_center'},
            {'constraint': 'Floorplan', 'regions': [['M1', 'X2'], ['RLOAD']], 'order': True},
            {'constraint': 'CompactPlacement', 'style': 'center'},
            {'constraint': 'PlaceCloser', 'instances': ['M1', 'X2']},
            {'constraint': 'PowerPorts', 'ports': ['VDD']},
            {'constraint': 'GroundPorts', 'ports': ['VSS']}]
        project.set_constraints(constraints)
        project.validate()
        for invalid in [
            {'constraint': 'Order', 'instances': ['M1', 'X2'], 'direction': 'bottom_to_top'},
            {'constraint': 'Align', 'instances': ['M1', 'X2'], 'line': 'h_any'},
            {'constraint': 'PlaceCloser', 'instances': ['M1', 'missing']},
            {'constraint': 'Floorplan', 'regions': [['M1'], ['M1']]},
            {'constraint': 'SymmetricBlocks', 'pairs': [['M1']], 'direction': 'V'},
            {'constraint': 'PowerPorts', 'ports': ['missing']},
            {'constraint': 'Order', 'instances': ['M1', 'X2'], 'direction': 'left_to_right', 'abut': 'false'},
        ]:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    project.set_constraints([invalid])
        project.set_constraints(constraints[:1])
        project.set_mos_parameters('M1', 3, 0.5, 2)
        with self.assertRaisesRegex(ValueError, 'matching models and parameters'):
            project.validate()

    def test_symmetry_compares_equivalent_concrete_parameters(self):
        source = CIRCUIT.replace('RLOAD VDD VOUT 10k', 'RLOAD VDD VOUT 10k\nR2 VDD VOUT 10000')
        source = source.replace('X2 VOUT VIN VSS VSS sky130_fd_pr__nfet_01v8 W=2.2u L=0.5u nf=2',
                                'X2 VOUT VIN VSS VSS sky130_fd_pr__nfet_01v8 W=2.2u L=0.5u nf=2 m=1')
        project = FloorplanProject.from_text(source)
        project.set_constraints([{'constraint': 'SymmetricBlocks', 'pairs': [['M1', 'X2'], ['RLOAD', 'R2']], 'direction': 'V'}])
        project.validate()

    def test_symmetry_compares_exact_exported_dimensions_after_roar_import(self):
        project = FloorplanProject.from_text(CIRCUIT.replace('W=2.2u', 'W=1.05').replace('nf=2', 'nf=4'), dimension_unit='um')
        design = Design().add_instance('M1_2', W=4.2e-6, L=0.5e-6, NF=4)
        project.import_roar_parameters(design, 'M1_2', ['M1'])
        self.assertNotEqual(project.devices[0].width_um, project.devices[1].width_um)
        project.set_constraints([{'constraint': 'SymmetricBlocks', 'pairs': [['M1', 'X2']], 'direction': 'V'}])
        project.validate()
        with tempfile.TemporaryDirectory() as directory:
            netlist, _ = project.export_align(directory)
            restored = FloorplanProject.from_netlist(netlist)
            self.assertEqual(restored.devices[0].width_um, restored.devices[1].width_um)
        project.set_mos_parameters('M1', 1.050000001, 0.5, 4)
        with self.assertRaisesRegex(ValueError, 'matching models and parameters'):
            project.validate()

    def test_unsupported_circuit_semantics_are_never_silently_dropped(self):
        invalid = [
            CIRCUIT.replace('RLOAD VDD VOUT 10k', 'CLOAD VDD VOUT 1p'),
            CIRCUIT.replace('RLOAD VDD VOUT 10k', '.param bias=1'),
            CIRCUIT.replace('VIN VOUT VDD VSS\n', 'VIN VOUT VDD VSS params: gain=2\n'),
            '.include models.sp\n' + CIRCUIT,
            CIRCUIT.replace(' W=2.2u', " W='total/nf'"),
            CIRCUIT.replace(' nf=2', ' nf=2 unknown_field=1p'),
            CIRCUIT.replace(' nf=2', ' nf=2 m=0.5'),
            CIRCUIT.replace(' nf=2', ' nf=2 trailing'),
            CIRCUIT.replace(' nf=2', ' nf=2 NF=4'),
            CIRCUIT.replace('.ends amp', ''),
            CIRCUIT + '.subckt child D G S B\nM3 D G S B nmos W=1u L=.1u nf=2\n.ends child\n',
        ]
        for source in invalid:
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    FloorplanProject.from_text(source)
        hierarchy = CIRCUIT.replace('sky130_fd_pr__nfet_01v8', 'child') + '.subckt child D G S B\nM3 D G S B nmos W=1u L=.1u nf=2\n.ends child\n'
        with self.assertRaisesRegex(ValueError, 'hierarchy'):
            FloorplanProject.from_text(hierarchy, top='amp')

    def test_shared_parameters_rebuild_atomically_and_keep_numeric_fields_editable(self):
        source = CIRCUIT.replace('W=2.2u L=0.5u nf=2', 'W=width L=0.5u nf=fingers')
        project = FloorplanProject.from_text(source, width_convention='total')
        project.set_parameter('width', 4.2e-6)
        self.assertIsNone(project.devices[0].width_um)
        project.set_parameter('fingers', 4)
        self.assertAlmostEqual(project.devices[0].width_um, 1.05)
        self.assertEqual(project.devices[0].width_um, project.devices[1].width_um)
        project.set_mos_parameters('M1', project.devices[0].width_um, 0.7, 4)
        clone = FloorplanProject.from_dict(project.to_dict())
        self.assertEqual(clone.devices[0].length_um, 0.7)
        clone.set_parameter('fingers', 2)
        self.assertAlmostEqual(clone.devices[0].width_um, 2.1)
        self.assertEqual(clone.devices[0].length_um, 0.7)
        before = clone.to_dict()
        with self.assertRaisesRegex(ValueError, 'controlled by shared parameter'):
            clone.set_mos_parameters('M1', 123, 0.8, 2)
        self.assertEqual(clone.to_dict(), before)
        for invalid in (2.5, float('inf'), 0):
            with self.assertRaises(ValueError):
                clone.set_parameter('fingers', invalid)
            self.assertEqual(clone.to_dict(), before)
        clone.set_parameter('width', None)
        self.assertIn('width', clone.unresolved_parameters)
        self.assertEqual(clone.devices[0].length_um, 0.7)

    def test_bindings_copy_scalars_track_sources_and_refresh_atomically(self):
        source = CIRCUIT.replace('W=2.2u L=0.5u nf=2', 'W=width L=length nf=fingers')
        project = FloorplanProject.from_text(source)
        design = Design().add_instance('M1_2', W=8e-6, L=0.5e-6, NF=4)
        original = design.to_state()
        project.bind_parameter(design, 'width', 'M1_2', 'W', source='linked')
        project.bind_parameter(design, 'fingers', 'M1_2', 'NF', source='linked')
        project.bind_parameter(design, 'length', 'M1_2', 'L', source='live')
        self.assertEqual(project.devices[0].width_um, 2)
        self.assertEqual(design.to_state(), original)
        self.assertEqual(project.parameter_rows[0]['binding']['source'], 'linked')
        refreshed = Design().add_instance('M1_2', W=12e-6, L=0.7e-6, NF=6)
        project.refresh_bindings(refreshed, source='live')
        self.assertAlmostEqual(project.devices[0].length_um, 0.7)
        self.assertEqual(project.devices[0].nf, 4)
        before = project.to_dict()
        invalid = Design().add_instance('M1_2', W=16e-6, L=0.5e-6, NF=2.5)
        with self.assertRaises(ValueError):
            project.refresh_bindings(invalid, source='linked')
        self.assertEqual(project.to_dict(), before)
        arrays = Design().add_instance('M1_2', W=[1e-6, 2e-6], NF=4)
        with self.assertRaises(ValueError):
            project.bind_parameter(arrays, 'width', 'M1_2', 'W')
        self.assertEqual(project.to_dict(), before)
        project.set_parameter('width', 6, dimension_unit='um', width_convention='per_finger')
        self.assertEqual(project.devices[0].width_um, 6)
        self.assertIsNone(project.parameter_rows[0]['binding'])

    def test_numeric_parameter_import_is_atomic_reports_unused_and_clears_bindings(self):
        source = CIRCUIT.replace('W=2.2u L=0.5u nf=2', 'W=width L=0.5u nf=fingers')
        project = FloorplanProject.from_text(source)
        design = Design().add_instance('group', W=4e-6)
        project.bind_parameter(design, 'width', 'group', 'W')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sizes.sp'
            path.write_text("* sizing\n.param WIDTH = '8u'\n+ fingers=4 unused=10k\n")
            self.assertEqual(project.import_parameters(path, dimension_unit='um'), ['unused'])
            self.assertAlmostEqual(project.devices[0].width_um, 2)
            self.assertIsNone(project.parameter_rows[0]['binding'])
            self.assertIn('sizes.sp', project.parameter_rows[0]['source'])
            before = project.to_dict()
            for invalid in ('.param width=16u fingers=1.5', '.param width=16u\n.param WIDTH=8u',
                            ".param width='total / 2'", '.include others.sp'):
                path.write_text(invalid)
                with self.assertRaises(ValueError):
                    project.import_parameters(path)
                self.assertEqual(project.to_dict(), before)
        original = FloorplanProject.from_netlist(Path(__file__).resolve().parents[3] / 'design/cm_ota.spice',
                                                 dimension_unit='um', width_convention='total')
        params = Path(__file__).resolve().parents[3] / 'design/cm_ota/simulation/spice/cm_ota_params.sp'
        original.import_parameters(params, dimension_unit='um')
        self.assertEqual(original.devices[0].nf, 1)
        self.assertEqual(FloorplanProject.from_dict(original.to_dict()).devices[0].nf, 1)

    def test_shared_total_width_tracks_manual_numeric_nf_without_competing_override(self):
        project = FloorplanProject.from_text(CIRCUIT.replace('W=2.2u', 'W=width'))
        project.set_parameter('width', 4e-6)
        project.set_mos_parameters('M1', project.devices[0].width_um, 0.5, 4)
        self.assertEqual(project.devices[0].width_um, 1)
        self.assertEqual(FloorplanProject.from_dict(project.to_dict()).devices[0].width_um, 1)
        numeric = self.project(width_convention='total')
        width = numeric.devices[0].width_um
        numeric.set_mos_parameters('M1', width, 0.5, 4)
        self.assertEqual(numeric.devices[0].width_um, width)
        self.assertEqual(FloorplanProject.from_dict(numeric.to_dict()).devices[0].width_um, width)

    def test_numeric_total_width_waits_for_shared_nf_and_survives_clone(self):
        project = FloorplanProject.from_text(CIRCUIT.replace('nf=2', 'nf=fingers'), width_convention='total')
        project.set_mos_parameters('M1', None, 0.7, None)
        clone = FloorplanProject.from_dict(project.to_dict())
        clone.set_parameter('fingers', 2)
        self.assertAlmostEqual(clone.devices[0].width_um, 1.1)
        self.assertEqual(clone.devices[0].length_um, 0.7)
        clone.set_parameter('fingers', 4)
        self.assertAlmostEqual(clone.devices[0].width_um, 0.55)

    def test_multiplicity_symbols_and_ambiguous_alias_rejection(self):
        project = FloorplanProject.from_text(CIRCUIT.replace(' nf=2', ' nf=2 mult=multiplicity'))
        self.assertIn('multiplicity', project.unresolved_parameters)
        project.set_parameter('multiplicity', 2)
        self.assertEqual(project.devices[0].parameters, {'M': 2})
        with self.assertRaises(ValueError):
            project.set_parameter('multiplicity', 0.5)
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            FloorplanProject.from_text(CIRCUIT.replace(' nf=2', ' nf=2 M=1 mult=1'))
        with self.assertRaisesRegex(ValueError, 'resolve arithmetic expressions'):
            FloorplanProject.from_text(CIRCUIT.replace('W=2.2u', "W='width / 2'"))

    def test_v3_profile_is_portable_and_v2_still_opens_without_one(self):
        project = self.project()
        legacy = project.to_dict()
        legacy['version'] = 2
        for key in ('align_profile', 'source_mode', 'run_options', 'constraint_enabled'):
            legacy.pop(key)
        restored = FloorplanProject.from_dict(legacy)
        self.assertIsNone(restored.align_profile)
        self.assertEqual(restored.devices, project.devices)
        project.align_profile = 'sky130-ota-vibeic-v1'
        with tempfile.TemporaryDirectory() as directory:
            path = project.save(Path(directory) / 'ota.roar.floorplan')
            state = json.loads(path.read_text())
            self.assertEqual(state['version'], 4)
            self.assertEqual(FloorplanProject.load(path).align_profile, project.align_profile)
            self.assertEqual(FloorplanProject.load(path).devices, project.devices)
            for invalid in ('', '  ', 12, {}):
                state['align_profile'] = invalid
                with self.subTest(value=invalid), self.assertRaisesRegex(ValueError, 'ALIGN profile'):
                    FloorplanProject.from_dict(state)

    def test_invalid_dimensions_and_fingers_rejected_atomically(self):
        project = self.project()
        before = project.devices[0].width_um
        for value in (0, -1, float('nan'), float('inf'), True, 'width/2'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    project.set_mos_parameters('M1', value, 0.5, 2)
                self.assertEqual(project.devices[0].width_um, before)
        for nf in (0, -1, 2.5, True, float('inf')):
            with self.assertRaises(ValueError):
                project.set_mos_parameters('M1', 2, 0.5, nf)

    def test_roar_grouped_row_requires_explicit_mapping_and_concrete_values(self):
        design = Design().add_instance('M1_2', W=8e-6, L=0.5e-6, NF=4)
        project = self.project()
        project.import_roar_parameters(design, 'M1_2', ['M1', 'X2'])
        self.assertEqual([(d.width_um, d.length_um, d.nf) for d in project.devices[:2]], [(2, 0.5, 4), (2, 0.5, 4)])
        microns = Design().add_instance('M1_2', W=8, L=0.5, NF=4)
        project.import_roar_parameters(microns, 'M1_2', ['M1'], dimension_unit='um')
        self.assertEqual((project.devices[0].width_um, project.devices[0].length_um), (2, 0.5))
        with self.assertRaises(ValueError):
            project.import_roar_parameters(design, 'M1_2', ['M1', 'RLOAD'])
        unresolved = Design().add_instance('M1_2', W='total/2', L=0.5e-6, NF=4)
        with self.assertRaisesRegex(ValueError, 'resolve expressions'):
            project.import_roar_parameters(unresolved, 'M1_2', ['M1'])


if __name__ == '__main__':
    unittest.main()
