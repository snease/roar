"""Portable handoffs use only stdlib code and detached floorplan data."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

from roar_api.floorplan import FloorplanProject
from roar_api.floorplan.align_runner import project_fingerprint
from roar_api.floorplan.geometry import load_preview
from roar_api.floorplan.handoff import export_handoff


SOURCE = '''* private source /private/workstation/source.sp
* viewer http://127.0.0.1:1234/roar-align/private-secret/
.subckt pair IN OUT VSS
M1 OUT IN VSS VSS sky130_fd_pr__nfet_01v8 W=width L=.15u NF=2
M2 OUT IN VSS VSS sky130_fd_pr__nfet_01v8 W=width L=.15u NF=2
.ends pair
'''


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = FloorplanProject.from_text(SOURCE)
        self.project.set_parameter('width', 1, dimension_unit='um', width_convention='per_finger')
        self.project.source_path = '/private/workstation/source.sp'
        self.project.roar_project_path = '/private/workstation/sizing.roar'
        self.project.source_mode = 'linked'
        self.project._parameters['width']['source'] = '/private/workstation/parameters.sp'
        self.project._parameters['width']['binding'] = {'instance': 'sizing', 'attribute': 'W', 'source': 'linked'}

    def captured_preview(self):
        root = self.root / 'capture'
        root.mkdir()
        (root / 'floorplan.json').write_text(json.dumps(self.project.to_dict()))
        candidate = {'name': 'PAIR_0', 'bbox': [0, 0, 4, 2],
                     'mapping': {'status': 'complete', 'unmapped_source_instances': []}, 'nets': [], 'blocks': []}
        for index, name in enumerate(('M1', 'M2')):
            candidate['blocks'].append({'id': name, 'path': name, 'label': name,
                'concrete_name': 'MOS_0', 'abstract_name': 'MOS', 'bbox': [index*2, 0, index*2+2, 2],
                'transform': {'oX': index*2, 'oY': 0, 'sX': 1, 'sY': 1},
                'source_instances': [name], 'mapping_status': 'complete', 'mapping_reason': '', 'pins': [],
                'private_debug': '/private/workstation/align.log'})
        other = deepcopy(candidate)
        other['name'] = 'PAIR_1'
        preview = {'schema_version': 1, 'units': 'um', 'coordinate_system': 'y-up', 'top': 'pair',
            'run_id': root.name, 'generation_fingerprint': project_fingerprint(self.project),
            'provenance': {'artifacts': [], 'align_version': '0.9.8', 'technology': 'SKY130A',
                'created_at': 123, 'native_to_um': .001, 'private_path': '/private/workstation/native',
                'url': 'http://127.0.0.1:1234/roar-align/private-secret/'},
            'candidates': [candidate, other]}
        state = SimpleNamespace(run_dir=str(root), stale=False, url='private-secret', log_path='/private/workstation/align.log')
        return preview, state

    def test_current_snapshot_roundtrips_without_bindings_paths_or_raw_comments(self):
        before = self.project.to_dict()
        path = export_handoff(self.project, self.root / 'handoff.zip')
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            self.assertEqual(set(manifest['files']), set(archive.namelist()))
            for name in archive.namelist():
                self.assertFalse(name.startswith('/'))
                self.assertNotIn('..', Path(name).parts)
                self.assertNotIn(b'/private/workstation', archive.read(name))
                self.assertNotIn(b'private-secret', archive.read(name))
            snapshot = json.loads(archive.read('current/floorplan.roar.floorplan'))
            reopened = FloorplanProject.from_dict(snapshot)
            self.assertEqual(reopened.source_mode, 'saved')
            self.assertIsNone(reopened.source_path)
            self.assertIsNone(reopened.roar_project_path)
            self.assertFalse(reopened.parameter_rows)
            self.assertEqual(reopened.devices[0].width_um, 1)
            self.assertIn(b'W=1e-06', archive.read('current/input/pair.sp'))
            self.assertEqual(json.loads(archive.read('current/input/pair.const.json')), [])
            self.assertIn(b'No physical placement is included', archive.read('index.html'))
        self.assertEqual(self.project.to_dict(), before)
        other = export_handoff(self.project, self.root / 'again.zip')
        self.assertEqual(path.read_bytes(), other.read_bytes())

    def test_earlier_candidate_keeps_its_original_inputs_and_selected_variant(self):
        self.project.set_device_labels({'M1': '<b>Captured role</b>'})
        self.project.set_constraints([{'constraint': 'Order', 'instances': ['M1', 'M2'], 'direction': 'left_to_right'}])
        preview, state = self.captured_preview()
        self.project.set_parameter('width', 2, dimension_unit='um', width_convention='per_finger')
        self.project.set_device_labels({'M1': 'Current role'})
        self.project.set_constraints([{'constraint': 'Order', 'instances': ['M2', 'M1'], 'direction': 'left_to_right'}])
        before = deepcopy(preview)
        path = export_handoff(self.project, self.root / 'earlier.zip', preview=preview, state=state, candidate_index=1)
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            self.assertTrue(manifest['placement']['earlier_inputs'])
            self.assertTrue(manifest['placement']['stale'])
            self.assertEqual(manifest['placement']['candidate'], 'PAIR_1')
            for folder, width in (('current', 2), ('placement', 1)):
                reopened = FloorplanProject.from_dict(json.loads(archive.read(f'{folder}/floorplan.roar.floorplan')))
                self.assertEqual(reopened.devices[0].width_um, width)
                self.assertIn(f'W={width}e-06'.encode(), archive.read(f'{folder}/input/pair.sp'))
            archive.extract('placement/preview.json', self.root)
            selected = load_preview(self.root / 'placement/preview.json')
            self.assertEqual([c['name'] for c in selected['candidates']], ['PAIR_1'])
            self.assertEqual(selected['provenance']['artifacts'], [])
            for name in archive.namelist():
                self.assertNotIn(b'/private/workstation', archive.read(name))
                self.assertNotIn(b'private-secret', archive.read(name))
            self.assertIn(b'EARLIER INPUTS', archive.read('README.txt'))
            self.assertIn(b'EARLIER INPUTS', archive.read('index.html'))
            self.assertIn('Width × height: 4 × 2 µm'.encode(), archive.read('index.html'))
            self.assertIn(b'Current role', archive.read('index.html'))
            current, captured = archive.read('index.html').split(b'<h2>Captured devices</h2>')
            self.assertIn(b'<td>Current role</td><td>sky130_fd_pr__nfet_01v8</td><td>2</td>', current)
            self.assertIn(b'<td>&lt;b&gt;Captured role&lt;/b&gt;</td><td>sky130_fd_pr__nfet_01v8</td><td>1</td>', captured)
            self.assertIn(b'<h2>Captured placement constraints</h2>', captured)
            self.assertIn(b'[&quot;M1&quot;, &quot;M2&quot;]', captured)
            self.assertIn(b'[&quot;M2&quot;, &quot;M1&quot;]', current)
            self.assertIn(b'&lt;b&gt;Captured role&lt;/b&gt;', archive.read('placement/placement.svg'))
            self.assertIn('PAIR_1 — 4 × 2 µm'.encode(), archive.read('placement/placement.svg'))
        self.assertEqual(preview, before)

    def test_svg_escapes_author_labels_and_uses_current_positions(self):
        label = '<script>&"Ω'
        self.project.set_device_labels({'M1': label})
        self.project.constraints = [{'constraint': '<rule>', 'note': '<img src=x onerror=alert(1)>'}]
        self.project.constraint_enabled = [False]
        sketch = self.project.sketch
        sketch['positions']['M1'] = [1000, 2000]
        sketch['groups'] = [{'id': 'group', 'label': label + '\x00', 'instances': ['M1']}]
        self.project.set_sketch(sketch)
        path = export_handoff(self.project, self.root / 'labels.zip')
        with zipfile.ZipFile(path) as archive:
            svg = archive.read('current/sketch.svg')
            root = ET.fromstring(svg)
            self.assertNotIn(b'<script>', svg)
            self.assertIn('&lt;script&gt;&amp;&quot;Ω'.encode(), svg)
            self.assertIn('M1: ' + label, ''.join(root.itertext()))
            self.assertIn(b'x="940" y="1964"', svg)
            index = archive.read('index.html')
            self.assertIn(b'<table>', index)
            self.assertNotIn(b'<script>', index)
            self.assertNotIn(b'<img src=x', index)
            self.assertIn('&lt;script&gt;&amp;&quot;Ω'.encode(), index)
            self.assertIn(b'&lt;rule&gt;', index)
            self.assertIn(b'Disabled', index)
            self.assertIn(b'D: OUT; G: IN; S: VSS; B: VSS', index)

    def test_tool_staleness_and_incomplete_mapping_remain_visible(self):
        preview, state = self.captured_preview()
        state.stale = True
        candidate = preview['candidates'][0]
        candidate['mapping']['status'] = 'incomplete'
        for block in candidate['blocks']:
            block['mapping_status'] = 'incomplete'
            block['mapping_reason'] = 'Compiler lineage may be partial.'
        path = export_handoff(self.project, self.root / 'partial.zip', preview=preview, state=state)
        with zipfile.ZipFile(path) as archive:
            placement = json.loads(archive.read('manifest.json'))['placement']
            self.assertFalse(placement['earlier_inputs'])
            self.assertTrue(placement['stale'])
            self.assertIn(b'INCOMPLETE SOURCE MAPPING', archive.read('index.html'))
            self.assertIn(b'EARLIER INPUTS / SETTINGS', archive.read('index.html'))

    def test_bad_candidate_or_captured_path_preserves_existing_destination(self):
        preview, state = self.captured_preview()
        target = self.root / 'preserved.zip'
        target.write_bytes(b'original')
        bad = deepcopy(preview)
        bad['generation_fingerprint'] = 'mismatched'
        with self.assertRaisesRegex(ValueError, 'does not match'):
            export_handoff(self.project, target, preview=bad, state=state)
        with self.assertRaisesRegex(ValueError, 'existing physical'):
            export_handoff(self.project, target, preview=preview, state=state, candidate_index=-1)
        outside = self.root / 'outside.json'
        (Path(state.run_dir) / 'floorplan.json').replace(outside)
        (Path(state.run_dir) / 'floorplan.json').symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'escapes'):
            export_handoff(self.project, target, preview=preview, state=state)
        self.assertEqual(target.read_bytes(), b'original')

    def test_archive_write_failure_is_atomic_and_removes_temporary_file(self):
        target = self.root / 'preserved.zip'
        target.write_bytes(b'original')
        with patch('roar_api.floorplan.handoff.zipfile.ZipFile.writestr', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                export_handoff(self.project, target)
        self.assertEqual(target.read_bytes(), b'original')
        self.assertEqual(list(self.root.glob('.preserved.zip.*.tmp')), [])


if __name__ == '__main__':
    unittest.main()
