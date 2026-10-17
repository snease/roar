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
            self.assertIn('RLOAD VDD VOUT 10k', text)
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
            with self.assertRaisesRegex(ValueError, 'Unsupported native'):
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
            CIRCUIT.replace(' nf=2', ' nf=2 ad=1p'),
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

    def test_invalid_dimensions_and_fingers_rejected_atomically(self):
        project = self.project()
        before = project.devices[0].width_um
        for value in (0, -1, float('nan'), float('inf'), True, 'width/2'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    project.set_mos_parameters('M1', value, 0.5, 2)
                self.assertEqual(project.devices[0].width_um, before)
        for nf in (1, 3, 2.5, True, float('inf')):
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
