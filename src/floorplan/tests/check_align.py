"""Verify native ALIGN exports; optionally check representative placed geometry."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

args = argparse.ArgumentParser(description=__doc__)
args.add_argument('--repo', type=Path, required=True)
args.add_argument('--pdk', type=Path, required=True)
args.add_argument('--output', type=Path)
args.add_argument('--placement', action='store_true', help='Generate representative native placements and verify geometry/compiled effects')
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

from floorplan.tests.constraint_cases import CASES as CAPABILITIES, EXPORT_ONLY, NETLIST as PAIR

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
    assert top.pins == [port.upper() for port in project.ports]
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
            'native_kinds': [item['const_name'] for item in mapped['constraints']],
            'native_mapping': mapped['constraints'],
            'annotated_constraints': [item.dict() for item in top.constraints]}

for relative in ['ota/current_mirror_ota']:
    source = args.repo / 'examples/floorplan' / (relative + '.sp')
    project = FloorplanProject.from_netlist(source)
    project.set_constraints(json.loads(source.with_suffix('.const.json').read_text()))
    result = native(project, output / 'fixtures')
    result['source_sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
    results['cases'].append(result)

companion = args.repo / 'examples/floorplan/ota/cm_ota.roar.floorplan'
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

for nf in [1, 3]:
    draft = FloorplanProject.from_text(base.replace('NF=2', f'NF={nf}'), dimension_unit='um')
    assert FloorplanProject.from_dict(draft.to_dict()).devices[0].nf == nf
results['repairable_draft_fingers'] = [1, 3]
for bad in ['NF=0', 'NF=2.5', 'NF=2 M=0.5']:
    try:
        FloorplanProject.from_text(base.replace('NF=2', bad), dimension_unit='um')
    except ValueError:
        pass
    else:
        raise AssertionError('Invalid generator size accepted: ' + bad)
results['rejected_sizes'] = ['NF=0', 'NF=2.5', 'M=0.5']

# Pin spelling is topology: the native NUMBER lexer casts through float/int,
# while NAME tokens such as 01 must remain distinct from 1.
for index, (name, other) in enumerate([
    ('0', '01'), ('1', '01'), ('1.', '1'), ('1meg', '1'), ('1.5', '1'), ('0.0001', '1'),
    ('9007199254740992', '1'), ('100000000000000000000', '1'),
]):
    source = PAIR.replace('d1', name).replace('d2', other).replace(' g ', ' 001 ')
    results['cases'].append(native(FloorplanProject.from_text(source), output / f'numeric-nets-{index}'))

results['rejected_numeric_net_names'] = []
for index, (name, normalized) in enumerate([
    ('1.0', '1'), ('1e3', '1000'), ('1k', '1000'), ('1X', '1000000'),
    ('9007199254740993', '9007199254740992'), ('1e999', 'INF'), ('1e-999', '0'),
]):
    for location in ('port', 'internal'):
        source = PAIR.replace('d2', normalized)
        source = source.replace('d1', name) if location == 'port' else source.replace('M1 d1', f'M1 {name}')
        parser = SpiceParser()
        parser.parse((args.pdk / 'models.sp').read_text())
        parser.parse(source)
        top = parser.library.find('PAIR')
        assert top.get_element('M1').pins['D'] == normalized
        assert top.get_element('M2').pins['D'] == normalized
        if location == 'port':
            assert top.pins[:2] == [normalized, normalized]
        project = FloorplanProject.from_dict(FloorplanProject.from_text(source).to_dict())
        destination = output / f'rejected-numeric-net-{index}-{location}'
        try:
            project.export_align(destination)
        except ValueError as exc:
            assert 'ALIGN 0.9.8 changes net name' in str(exc), str(exc)
        else:
            raise AssertionError('Unsafe numeric net name exported: ' + name)
        assert not destination.exists()
        results['rejected_numeric_net_names'].append({'name': name, 'native_name': normalized, 'location': location})

for index, criticality in enumerate(({}, {'criticality': None})):
    for weight in (0, 3):
        project = FloorplanProject.from_text(PAIR)
        project.set_constraints([
            {'constraint': 'NetConst', 'nets': ['d1'], 'shield': 's', **criticality},
            {'constraint': 'NetPriority', 'nets': ['d1'], 'weight': weight},
        ])
        result = native(project, output / f'null-criticality-{index}-{weight}')
        assert result['native_mapping'] == [
            {'const_name': 'ShieldNet', 'net_name': 'D1', 'shield_net': 'S'},
            {'const_name': 'CritNet', 'net_name': 'D1', 'priority': weight},
        ]
        results['cases'].append(result)

for rule in CAPABILITIES + EXPORT_ONLY:
    project = FloorplanProject.from_text(PAIR)
    project.set_constraints([rule])
    result = native(project, output / ('native-' + rule['constraint']))
    result['rule'] = rule
    mapped = result['native_mapping']
    kind = rule['constraint']
    if kind == 'PortLocation':
        assert mapped == [{'const_name': 'PortLocation', 'location': 'TC', 'terminal_name': 'D1'}]
    elif kind == 'NetPriority':
        assert mapped == [{'const_name': 'CritNet', 'net_name': 'D1', 'priority': 3}]
    elif kind == 'NetConst':
        assert mapped == [{'const_name': 'ShieldNet', 'net_name': 'D1', 'shield_net': 'S'},
                          {'const_name': 'CritNet', 'net_name': 'D1', 'priority': 3}]
    elif kind == 'Route':
        assert mapped[0]['min_layer'] == 'M1' and mapped[0]['max_layer'] == 'M4'
        assert mapped[0]['customize'][0]['nets'] == ['D1']
    elif kind == 'DoNotUseLib':
        assert result['annotated_constraints'][0]['libraries'] == ['DP_NMOS']
    results['cases'].append(result)


def placed_boxes(data, module):
    templates = {row['concrete_name']: row['bbox'] for row in data['leaves'] + data['modules']}
    boxes = {}
    for instance in module['instances']:
        llx, lly, urx, ury = templates[instance['concrete_template_name']]
        transform = instance['transformation']
        xs = [transform['oX'] + transform['sX'] * x for x in (llx, urx)]
        ys = [transform['oY'] + transform['sY'] * y for y in (lly, ury)]
        boxes[instance['instance_name']] = [min(xs), min(ys), max(xs), max(ys)]
    return boxes


def terminal_positions(path):
    """Read native placer terminal coordinates, in its original database units."""
    positions, die = {}, None
    for line in path.read_text().splitlines():
        if line.startswith('DIE '):
            die = [int(value) for value in re.findall(r'-?\d+', line)]
        fields = line.split()
        if len(fields) == 3:
            positions[fields[0]] = [int(value) for value in fields[1:]]
    assert die is not None, path
    return die, positions


def placement_case(name, rules, verify, source=PAIR, compiler_controls=True):
    project = FloorplanProject.from_text(source)
    if compiler_controls:
        rules = [
            {'constraint': 'ConfigureCompiler', 'auto_constraint': False,
             'merge_series_devices': False, 'merge_parallel_devices': False},
            {'constraint': 'DoNotIdentify', 'instances': ['M1', 'M2']},
        ] + rules
    project.set_constraints(rules)
    netlist, _ = project.export_align(output / name / 'input')
    work = output / name / 'run'
    work.mkdir()
    align.schematic2layout(netlist_dir=netlist.parent, pdk_dir=args.pdk, subckt=project.top,
        working_dir=work, nvariants=4, placer_sa_iterations=100, seed=0,
        gui=False, flow_stop='3_pnr:place')
    dump = work / '3_pnr/__placer_dump__.json'
    top, _leaves, variants, _metrics = json.loads(dump.read_text())
    records = []
    for concrete, data in variants:
        module = next(row for row in data['modules'] if row['abstract_name'] == top)
        boxes = placed_boxes(data, module)
        die, terminals = terminal_positions(work / '3_pnr/Results' / (concrete + '.pl'))
        verify(module, boxes, die, terminals)
        records.append({'variant': concrete, 'bbox_nm': module['bbox'], 'instance_boxes_nm': boxes,
                        'terminal_positions_database_units': terminals})
    assert records, f'No placement candidates for {name}'
    compiled = json.loads((work / '1_topology' / (project.top.lower() + '.const.json')).read_text())
    return {'case': name, 'status': 'PASS', 'rules': rules, 'dump': str(dump),
            'compiled_constraints': compiled, 'verified_candidates': records}


if args.placement:
    results['placements'] = []

    def horizontal(module, boxes, die, terminals):
        first, second = boxes['X_M1'], boxes['X_M2']
        assert second[0] - first[2] >= 840, boxes
        assert first[1] == second[1], boxes
        width, height = module['bbox'][2:]
        assert width <= 20000 and height <= 20000 and .5 <= width / height < 2, module['bbox']
        for llx, lly, urx, ury in boxes.values():
            assert llx >= 430 and lly >= 420 and urx <= width - 430 and ury <= height - 420, boxes
        assert len({row['concrete_template_name'] for row in module['instances']}) == 1

    results['placements'].append(placement_case('placement-horizontal', [
        {'constraint': 'Boundary', 'max_width': 20, 'max_height': 20, 'halo_horizontal': .43, 'halo_vertical': .42},
        {'constraint': 'AspectRatio', 'ratio_low': .5, 'ratio_high': 2, 'weight': 2},
        {'constraint': 'AlignInOrder', 'instances': ['M1', 'M2'], 'line': 'bottom', 'direction': 'horizontal'},
        {'constraint': 'Spread', 'instances': ['M1', 'M2'], 'direction': 'horizontal', 'distance': 420},
        {'constraint': 'HorizontalDistance', 'abs_distance': 840},
        {'constraint': 'SameTemplate', 'instances': ['M1', 'M2']},
    ], horizontal))

    def vertical(module, boxes, die, terminals):
        first, second = boxes['X_M1'], boxes['X_M2']
        assert first[1] - second[3] >= 1120, boxes
        assert first[0] + first[2] == second[0] + second[2], boxes

    results['placements'].append(placement_case('placement-vertical', [
        {'constraint': 'VerticalDistance', 'abs_distance': 1120},
        {'constraint': 'AlignInOrder', 'instances': ['M1', 'M2'], 'line': 'center', 'direction': 'vertical'},
    ], vertical))

    def block_distance(module, boxes, die, terminals):
        assert boxes['X_M2'][0] - boxes['X_M1'][2] >= 1260, boxes

    results['placements'].append(placement_case('placement-block-distance', [
        {'constraint': 'BlockDistance', 'abs_distance': 1260},
        {'constraint': 'AlignInOrder', 'instances': ['M1', 'M2'], 'line': 'bottom', 'direction': 'horizontal'},
    ], block_distance))

    def spread(module, boxes, die, terminals):
        assert boxes['X_M2'][0] - boxes['X_M1'][2] >= 1720, boxes

    results['placements'].append(placement_case('placement-spread', [
        {'constraint': 'Spread', 'instances': ['M1', 'M2'], 'direction': 'horizontal', 'distance': 1720},
        {'constraint': 'AlignInOrder', 'instances': ['M1', 'M2'], 'line': 'bottom', 'direction': 'horizontal'},
    ], spread))

    def ports(module, boxes, die, terminals):
        assert terminals['D1'][1] == die[3] and terminals['D2'][1] == die[1], (die, terminals)
        for port in ('D1', 'D2'):
            assert die[2] / 3 <= terminals[port][0] <= 2 * die[2] / 3, (die, terminals)

    results['placements'].append(placement_case('placement-ports', [
        {'constraint': 'PortLocation', 'ports': ['d1'], 'location': 'TC'},
        {'constraint': 'PortLocation', 'ports': ['d2'], 'location': 'BC'},
    ], ports))

    def symmetry(module, boxes, die, terminals):
        first, second = boxes['X_M1'], boxes['X_M2']
        assert first[1::2] == second[1::2], boxes
        assert first[0] + second[2] == first[2] + second[0] == module['bbox'][0] + module['bbox'][2], boxes
        assert terminals['D1'][1] == terminals['D2'][1], terminals
        assert terminals['D1'][0] + terminals['D2'][0] == die[0] + die[2], (die, terminals)
        transforms = [row['transformation'] for row in module['instances']]
        assert transforms[0]['sX'] == -transforms[1]['sX'] and transforms[0]['sY'] == transforms[1]['sY']
        assert any(rule['constraint'] == 'SymmetricBlocks' for rule in module['constraints'])

    results['placements'].append(placement_case('placement-net-symmetry', [CAPABILITIES[9]], symmetry))

    def clock(module, boxes, die, terminals):
        assert any(rule['constraint'] == 'ClockPorts' and rule['ports'] == ['G'] for rule in module['constraints'])
        assert len(boxes) == 2

    results['placements'].append(placement_case('placement-clock', [CAPABILITIES[10]], clock))

    parallel_source = PAIR.replace('M2 d2', 'M2 d1')
    for merge in (True, False):
        def compiler_effect(module, boxes, die, terminals, merge=merge):
            assert len(boxes) == (1 if merge else 2), boxes
        results['placements'].append(placement_case('placement-merge-' + str(merge).lower(), [
            {'constraint': 'ConfigureCompiler', 'auto_constraint': False, 'merge_parallel_devices': merge},
        ], compiler_effect, source=parallel_source, compiler_controls=False))

(output / 'receipt.json').write_text(json.dumps(results, indent=2) + '\n')
print(json.dumps(results, indent=2))
