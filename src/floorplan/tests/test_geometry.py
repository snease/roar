"""Physical units, compiler identity, hierarchy, and untrusted artifact contract."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from roar_api.floorplan.geometry import PreviewError, build_preview, load_preview

FIXTURE = Path(__file__).parent / 'fixtures' / 'align098_ota'
RESULT = '3_pnr/Results/CURRENT_MIRROR_OTA_0.scaled_placement_verilog.json'


class GeometryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'run'
        shutil.copytree(FIXTURE, self.root)
        self.manifest = json.loads((self.root / 'manifest.json').read_text())

    def write_json(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def preview(self):
        return build_preview(self.root, self.manifest)

    def test_real_sky130_capture_units_identity_variant_and_selected_net(self):
        before = {p.relative_to(self.root): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in self.root.rglob('*') if p.is_file()}
        original_manifest = deepcopy(self.manifest)
        preview = self.preview()
        self.assertEqual(preview['units'], 'um')
        self.assertEqual(preview['coordinate_system'], 'y-up')
        candidate = preview['candidates'][0]
        self.assertEqual(candidate['bbox'], [0, 0, 25.8, 13.44])
        self.assertEqual(candidate['mapping'], {'status': 'complete', 'unmapped_source_instances': []})
        blocks = {b['id']: b for b in candidate['blocks']}
        self.assertEqual(len(blocks), 10)
        self.assertEqual({s for b in blocks.values() for s in b['source_instances']},
                         {f'M{i}' for i in range(1, 11)})
        self.assertEqual(blocks['X_M1']['source_instances'], ['M1'])
        self.assertEqual(blocks['X_M1']['concrete_name'], 'NMOS_4T_89063639_X1_Y2')
        self.assertEqual(blocks['X_M1']['bbox'], [0, 0, 2.58, 13.44])
        self.assertEqual(blocks['X_M1']['transform'], {'oX': 2.58, 'oY': 0, 'sX': -1, 'sY': 1})
        vinn = next(n for n in candidate['nets'] if n['name'] == 'VINN')['connections']
        self.assertEqual(len(vinn), 1)
        self.assertEqual(vinn[0]['block_id'], 'X_M1')
        self.assertEqual(vinn[0]['pin'], 'G')
        for actual, expected in zip(vinn[0]['bbox'], [1.15, 4.46, 1.43, 10.66]):
            self.assertAlmostEqual(actual, expected)
        self.assertEqual(vinn[0]['kind'], 'pin')
        self.assertEqual(self.manifest, original_manifest)
        self.assertEqual(before, {p.relative_to(self.root): hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in self.root.rglob('*') if p.is_file()})
        self.write_json('preview.json', preview)
        self.assertEqual(load_preview(self.root / 'preview.json'), preview)

    def test_selected_concrete_variant_is_not_replaced_by_abstract_template(self):
        native = json.loads((self.root / RESULT).read_text())
        other = deepcopy(native['leaves'][-1])
        other['concrete_name'] = 'NMOS_4T_89063639_X2_Y1'
        other['bbox'] = [0, 0, 5160, 6720]
        native['leaves'].append(other)
        instance = next(i for i in native['modules'][0]['instances'] if i['instance_name'] == 'X_M1')
        instance['concrete_template_name'] = other['concrete_name']
        instance['transformation']['oX'] = 5160
        self.write_json(RESULT, native)
        block = next(b for b in self.preview()['candidates'][0]['blocks'] if b['id'] == 'X_M1')
        self.assertEqual(block['concrete_name'], other['concrete_name'])
        self.assertEqual(block['bbox'], [0, 0, 5.16, 6.72])

    def test_slash_in_electrical_net_name_is_data_not_a_file_path(self):
        native = json.loads((self.root / RESULT).read_text())
        # Native SPICE accepts hierarchical names; the adapter must preserve them.
        native = json.loads(json.dumps(native).replace('VINN', 'STAGE/LEFT'))
        self.write_json(RESULT, native)
        preview = self.preview()
        self.assertTrue(any(net['name'] == 'STAGE/LEFT'
                            for net in preview['candidates'][0]['nets']))
        self.write_json('preview.json', preview)
        self.assertEqual(load_preview(self.root / 'preview.json'), preview)

    def nested_run(self):
        self.manifest['top'] = 'TOP'
        (self.root / self.manifest['netlist']).write_text(
            '.subckt TOP IN OUT VSS\nmLeft a b c d model\nX_Original a b c d model\n.ends TOP\n')
        for path in (self.root / '1_topology').glob('*.const.json'):
            path.unlink()
        self.write_json('1_topology/top.const.json', [
            {'constraint': 'GroupBlocks', 'instance_name': 'X_PAIR', 'instances': ['MLEFT', 'X_ORIGINAL']}])
        native = {'leaves': [{'abstract_name': 'FET', 'concrete_name': 'FET_X2_Y1',
                              'bbox': [10, 20, 210, 420],
                              'terminals': [{'name': 'G', 'rect': [20, 40, 60, 80]}]}],
                  'modules': [
                      {'abstract_name': 'PAIR', 'concrete_name': 'PAIR_0', 'bbox': [100, 200, 1100, 2200],
                       'instances': [{'instance_name': 'renamed_internal', 'concrete_template_name': 'FET_X2_Y1',
                                      'transformation': {'oX': 1000, 'oY': 1000, 'sX': -1, 'sY': 1},
                                      'fa_map': [{'formal': 'G', 'actual': 'INPUT'}]}]},
                      {'abstract_name': 'TOP', 'concrete_name': 'TOP_0', 'bbox': [-1000, -2000, 6000, 6000],
                       'instances': [{'instance_name': 'X_PAIR', 'concrete_template_name': 'PAIR_0',
                                      'transformation': {'oX': 3000, 'oY': 4000, 'sX': -1, 'sY': -1},
                                      'fa_map': [{'formal': 'INPUT', 'actual': 'IN'},
                                                 {'formal': 'OUTPUT_WITHOUT_GEOMETRY', 'actual': 'OUT'}]}]}]}
        self.write_json(RESULT, native)
        return native

    def test_group_membership_case_nonzero_origins_nested_mirrors_and_fallback(self):
        self.nested_run()
        candidate = self.preview()['candidates'][0]
        self.assertEqual(candidate['bbox'], [-1, -2, 6, 6])
        self.assertEqual(len(candidate['blocks']), 1)
        block = candidate['blocks'][0]
        self.assertEqual(block['source_instances'], ['mLeft', 'X_Original'])
        self.assertEqual(block['mapping_status'], 'complete')
        self.assertEqual(block['concrete_name'], 'PAIR_0')
        for actual, expected in zip(block['bbox'], [1.9, 1.8, 2.9, 3.8]):
            self.assertAlmostEqual(actual, expected)
        pin = block['pins'][0]
        self.assertEqual(pin['net'], 'IN')
        self.assertEqual(pin['bbox'], [2.02, 2.92, 2.06, 2.96])
        nets = {n['name']: n for n in candidate['nets']}
        self.assertEqual(nets['OUT']['connections'][0]['kind'], 'block')
        self.assertNotIn('bbox', nets['OUT']['connections'][0])
        # Canonically renamed internal primitive is never guessed to be mLeft.
        self.assertEqual(block['label'], 'mLeft, X_Original')

    def test_missing_merge_lineage_is_explicit_and_prefixes_are_not_identity(self):
        self.nested_run()
        self.write_json('1_topology/top.const.json', [
            {'constraint': 'GroupBlocks', 'instance_name': 'X_PAIR', 'instances': ['MLEFT']}])
        candidate = self.preview()['candidates'][0]
        self.assertEqual(candidate['mapping']['status'], 'incomplete')
        self.assertEqual(candidate['mapping']['unmapped_source_instances'], ['X_Original'])
        self.assertEqual(candidate['blocks'][0]['source_instances'], ['mLeft'])
        self.assertEqual(candidate['blocks'][0]['mapping_status'], 'incomplete')
        self.assertIn('merging or removal', candidate['blocks'][0]['mapping_reason'])
        self.write_json('1_topology/top.const.json', [])
        block = self.preview()['candidates'][0]['blocks'][0]
        self.assertEqual(block['source_instances'], [])
        self.assertEqual(block['mapping_status'], 'incomplete')

    def test_lineage_dag_memoization_depth_work_cycles_and_overlap(self):
        original = json.loads((self.root / '1_topology/current_mirror_ota.const.json').read_text())

        def lineage(extra, root='G26'):
            rows = deepcopy(original)
            next(r for r in rows if r.get('instance_name') == 'X_M10')['instances'] = [root]
            self.write_json('1_topology/current_mirror_ota.const.json', rows + extra)

        # Shared unresolved ancestors must be visited once, not expanded into
        # millions of repeated branches. Unknown sources remain visibly unknown.
        dag = [{'constraint': 'GroupBlocks', 'instance_name': f'G{i}',
                'instances': ['UNKNOWN'] if i < 2 else [f'G{i-1}', f'G{i-2}']} for i in range(27)]
        lineage(dag)
        with patch('roar_api.floorplan.geometry.MAX_LINEAGE_WORK', 100):
            candidate = self.preview()['candidates'][0]
        self.assertEqual(candidate['mapping']['status'], 'incomplete')
        self.assertIn('M10', candidate['mapping']['unmapped_source_instances'])
        with patch('roar_api.floorplan.geometry.MAX_LINEAGE_WORK', 10):
            with self.assertRaisesRegex(PreviewError, 'lineage.*work limit'):
                self.preview()
        chain = [{'constraint': 'GroupBlocks', 'instance_name': f'G{i}',
                  'instances': [f'G{i+1}' if i < 1100 else 'M10']} for i in range(1101)]
        lineage(chain, 'G0')
        with self.assertRaisesRegex(PreviewError, 'lineage.*depth limit'):
            self.preview()
        lineage([{'constraint': 'GroupBlocks', 'instance_name': 'G0', 'instances': ['G1']},
                 {'constraint': 'GroupBlocks', 'instance_name': 'G1', 'instances': ['G0']}], 'G0')
        with self.assertRaisesRegex(PreviewError, 'Cyclic'):
            self.preview()
        dag[0]['instances'] = dag[1]['instances'] = ['M10']
        lineage(dag)
        with self.assertRaisesRegex(PreviewError, 'overlapping source membership'):
            self.preview()
        lineage([], 'M1')
        with self.assertRaisesRegex(PreviewError, 'multiple placed blocks'):
            self.preview()

    def test_native_terminal_expansion_is_bounded_before_output_construction(self):
        native = json.loads((self.root / RESULT).read_text())
        pin = native['leaves'][0]['terminals'][0]
        native['leaves'][0]['terminals'] = [pin] * 10000
        self.write_json(RESULT, native)
        with self.assertRaisesRegex(PreviewError, 'geometry expansion.*output limit'):
            self.preview()

    def test_unit_scale_is_read_from_captured_layers_and_version_is_explicit(self):
        self.write_json('3_pnr/inputs/layers.json', {'ScaleFactor': 10})
        self.assertEqual(self.preview()['candidates'][0]['bbox'], [0, 0, 2.58, 1.344])
        self.manifest['generation_config']['profile']['align_version'] = 'future'
        with self.assertRaisesRegex(PreviewError, 'Unsupported ALIGN placement version'):
            self.preview()

    def test_native_geometry_and_artifact_paths_are_untrusted(self):
        original = json.loads((self.root / RESULT).read_text())
        for label, change, pattern in [
            ('nonfinite', lambda d: d['leaves'][0].update(bbox=[0, 0, float('inf'), 3]), 'nonfinite'),
            ('reversed', lambda d: d['leaves'][0].update(bbox=[1, 0, 0, 3]), 'positive'),
            ('rotation', lambda d: d['modules'][0]['instances'][0]['transformation'].update(sX=0), 'mirror'),
            ('missing template', lambda d: d['modules'][0]['instances'][0].update(concrete_template_name='missing'), 'missing concrete'),
            ('duplicate identity', lambda d: d['modules'][0]['instances'].append(deepcopy(d['modules'][0]['instances'][0])), 'duplicate'),
        ]:
            with self.subTest(label=label):
                data = deepcopy(original)
                change(data)
                self.write_json(RESULT, data)
                with self.assertRaisesRegex(PreviewError, pattern):
                    self.preview()
        self.write_json(RESULT, original)
        self.manifest['netlist'] = '../outside.sp'
        with self.assertRaisesRegex(PreviewError, 'escapes'):
            self.preview()
        self.manifest['netlist'] = 'input/link.sp'
        (self.root / 'input/link.sp').symlink_to(Path(self.temporary.name) / 'outside.sp')
        with self.assertRaisesRegex(PreviewError, 'escapes'):
            self.preview()

    def test_normalized_geometry_identity_and_security_validation(self):
        original = self.preview()
        for label, change, pattern in [
            ('schema', lambda d: d.update(schema_version=2), 'schema'),
            ('units', lambda d: d.update(units='nm'), 'micrometres'),
            ('nan', lambda d: d['candidates'][0]['bbox'].__setitem__(2, float('nan')), 'nonfinite'),
            ('unknown physical pin', lambda d: d['candidates'][0]['nets'][0]['connections'][0].update(pin='NO_SUCH_PIN'), 'recorded block pin'),
            ('wrong pin geometry', lambda d: d['candidates'][0]['nets'][0]['connections'][0].update(position=[100, 100]), 'recorded block pin'),
            ('complete with unmapped source', lambda d: d['candidates'][0]['mapping'].update(unmapped_source_instances=['M1']), 'cannot contain unmapped'),
            ('complete with incomplete block', lambda d: d['candidates'][0]['blocks'][0].update(mapping_status='incomplete'), 'incomplete block mapping'),
            ('overlapping sources', lambda d: d['candidates'][0]['blocks'][0].update(source_instances=['M1']), 'overlapping normalized source'),
            ('unknown block', lambda d: d['candidates'][0]['nets'][0]['connections'][0].update(block_id='unknown'), 'unknown compiled'),
            ('traversal', lambda d: d['provenance']['artifacts'].append('../escape.json'), 'relative'),
            ('absolute path', lambda d: d['provenance']['artifacts'].append('/tmp/escape.json'), 'relative'),
            ('candidate id', lambda d: d['candidates'][0].update(name='../bad'), 'identifier'),
        ]:
            with self.subTest(label=label):
                data = deepcopy(original)
                change(data)
                self.write_json('preview.json', data)
                with self.assertRaisesRegex(PreviewError, pattern):
                    load_preview(self.root / 'preview.json')
        (self.root / '3_pnr/Results/link.json').symlink_to(Path(self.temporary.name) / 'outside.json')
        original['provenance']['artifacts'].append('3_pnr/Results/link.json')
        self.write_json('preview.json', original)
        with self.assertRaisesRegex(PreviewError, 'escapes'):
            load_preview(self.root / 'preview.json')


if __name__ == '__main__':
    unittest.main()
