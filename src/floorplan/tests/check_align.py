"""Verify ROAR exports with an independently installed ALIGN (no placement run)."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

args = argparse.ArgumentParser(description=__doc__)
args.add_argument('--repo', type=Path, required=True)
args.add_argument('--pdk', type=Path, required=True)
args.add_argument('--output', type=Path)
args = args.parse_args()
args.repo = args.repo.resolve()
args.pdk = args.pdk.resolve()
output = args.output.resolve() if args.output else Path(tempfile.mkdtemp(prefix='roar-floorplan-native-'))
output.mkdir(parents=True, exist_ok=True)
# ALIGN configures a LOG directory on import. Keep it with the check artifacts.
os.chdir(output)
sys.path.insert(0, str(args.repo / 'src'))
from floorplan import FloorplanProject
import align
from align.schema.parser import SpiceParser
from align.compiler.user_const import ConstraintParser
from align.pnr.write_constraint import PnRConstraintWriter

results = {'align_version': align.__version__, 'align_module': align.__file__,
           'pdk': str(args.pdk.resolve()), 'repo': str(args.repo.resolve()), 'cases': []}

def native(project, parent):
    netlist, constraints = project.export_align(parent)
    parser = SpiceParser()
    parser.parse((args.pdk / 'models.sp').read_text())
    parser.parse(netlist.read_text())
    top = parser.library.find(project.top.upper())
    ConstraintParser(args.pdk, netlist.parent).annotate_user_constraints(top)
    top.verify()
    mapped = PnRConstraintWriter().map_valid_const(top.constraints,
        {'instances': [{'instance_name': item.name} for item in top.elements]})
    assert len(top.elements) == len(project.devices)
    for original in project.devices:
        parsed = top.get_element(original.name)
        assert list(parsed.pins.values()) == [net.upper() for net in original.terminals]
        if original.kind == 'mos':
            assert parsed.model == original.model.upper()
            assert abs(float(parsed.parameters['W']) - original.width_um * 1e-6) < 1e-18
            assert abs(float(parsed.parameters['L']) - original.length_um * 1e-6) < 1e-18
            assert int(parsed.parameters['NF']) == original.nf
    (netlist.parent / 'native.pnr.constraints.json').write_text(json.dumps(mapped, indent=2)+'\n')
    return {'top': top.name, 'devices': len(top.elements), 'status': 'PASS',
            'bundle': str(netlist.parent), 'native_constraints': len(mapped['constraints']),
            'native_kinds': [item['const_name'] for item in mapped['constraints']]}

for relative in ['ota/current_mirror_ota']:
    source = args.repo / 'design/floorplan' / (relative + '.sp')
    project = FloorplanProject.from_netlist(source)
    project.set_constraints(json.loads(source.with_suffix('.const.json').read_text()))
    result = native(project, output / 'fixtures')
    result['source_sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
    results['cases'].append(result)

companion = args.repo / 'design/floorplan/ota/cm_ota.roar.floorplan'
project = FloorplanProject.load(companion)
assert Path(project.roar_project_path) == companion.with_name('cm_ota_sizes.roar')
assert not project.unresolved_parameters
result = native(project, output / 'companion')
result['source_sha256'] = hashlib.sha256(companion.read_bytes()).hexdigest()
results['cases'].append(result)

base = '.subckt XSCHEM d g s b\nXM1 d g s b sky130_fd_pr__nfet_01v8 W=2.2 L=0.5 NF=2\nXM2 d g s b sky130_fd_pr__nfet_01v8 W=2.2 L=0.5 NF=2\n.ends XSCHEM\n'
for symmetrize in [False, True]:
    project = FloorplanProject.from_text(base, dimension_unit='um', width_convention='total')
    project.set_constraints([{'constraint': 'Floorplan', 'regions': [['XM1', 'XM2']],
                             'order': True, 'symmetrize': symmetrize}])
    results['cases'].append(native(project, output / ('rows-symmetric' if symmetrize else 'rows')))
    assert project.devices[0].width_um == 1.1

for bad in ['NF=1', 'NF=3', 'NF=2 M=0.5']:
    try:
        FloorplanProject.from_text(base.replace('NF=2', bad), dimension_unit='um')
    except ValueError:
        pass
    else:
        raise AssertionError('Invalid generator size accepted: ' + bad)
results['rejected_sizes'] = ['NF=1', 'NF=3', 'M=0.5']
(output / 'receipt.json').write_text(json.dumps(results, indent=2) + '\n')
print(json.dumps(results, indent=2))
