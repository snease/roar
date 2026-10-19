"""Read-only ALIGN 0.9.8 placement adapter, independent of Qt and ALIGN.

The contract is ``Results/*.scaled_placement_verilog.json`` plus the captured
``inputs/layers.json`` (coordinates per micron = 1000 * ScaleFactor). This is
ALIGN's render_placement.scale_placement_verilog conversion, not LEF's nominal
unit header. Compiler ``1_topology/*.const.json`` GroupBlocks records supply
source membership; instance prefixes and primitive internal names do not.

Composite instances remain compiled blocks: canonical primitive generation can
rename their internals without preserving per-source geometry. Their terminal
rectangles are resolved through the hierarchy, including mirror transforms.
All normalized coordinates are physical micrometres, with y pointing upward.
"""
from __future__ import annotations

import json
import math
import stat
from pathlib import Path

SCHEMA_VERSION = 1
MAX_BYTES = 16 * 1024 * 1024
MAX_ITEMS = 300_000
MAX_CANDIDATES = 100
MAX_BLOCKS = 20_000
MAX_COORDINATE = 1e9
MAX_LINEAGE_DEPTH = 64
MAX_LINEAGE_WORK = MAX_ITEMS


class PreviewError(ValueError):
    """Unsupported, unsafe, or malformed captured placement data."""


def _fail(message):
    raise PreviewError(message)


def _number(value, where):
    if type(value) not in (int, float) or abs(value) > MAX_COORDINATE or not math.isfinite(value):
        _fail(f'{where}: expected a bounded finite coordinate')
    return value


def _name(value, where):
    # Circuit identifiers are data, not artifact paths. In particular SPICE
    # allows slash-separated net/instance names; _confined handles file paths.
    if (not isinstance(value, str) or not value or len(value) > 1024
            or any(ord(c) < 32 for c in value) or '\\' in value
            or any(part in ('.', '..') for part in value.split('/'))):
        _fail(f'{where}: invalid identifier {str(value)[:120]!r}')
    return value


def _list(value, where, limit=MAX_ITEMS):
    if not isinstance(value, list) or len(value) > limit:
        _fail(f'{where}: expected a list of at most {limit} entries')
    return value


def _dict(value, where):
    if not isinstance(value, dict):
        _fail(f'{where}: expected an object')
    return value


def _bbox(value, where):
    if not isinstance(value, list) or len(value) != 4:
        _fail(f'{where}: expected [xmin, ymin, xmax, ymax]')
    r = [_number(v, where) for v in value]
    if r[0] >= r[2] or r[1] >= r[3]:
        _fail(f'{where}: rectangle must have positive width and height')
    return r


def _point(value, where):
    if not isinstance(value, list) or len(value) != 2:
        _fail(f'{where}: expected [x, y]')
    return [_number(v, where) for v in value]


def _bounded_tree(value):
    stack, count = [(value, 0)], 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if count > MAX_ITEMS or depth > 48:
            _fail('Placement JSON exceeds the supported complexity limit')
        if isinstance(item, dict):
            stack.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            stack.extend((v, depth + 1) for v in item)
        elif isinstance(item, float) and not math.isfinite(item):
            _fail('Placement JSON contains nonfinite numbers')
        elif isinstance(item, str) and len(item) > MAX_BYTES:
            _fail('Placement JSON contains oversized text')


def _confined(root, path):
    try:
        path = Path(path)
        if '\\' in str(path) or any(ord(c) < 32 for c in str(path)):
            _fail('Invalid placement artifact path')
        result = (root / path).resolve()
    except (OSError, RuntimeError, ValueError) as error:
        raise PreviewError(f'Invalid placement artifact path: {error}') from error
    if not result.is_relative_to(root):
        _fail('Placement artifact path escapes the captured run directory')
    return result


def _read(path, *, is_json=True):
    try:
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode):
            _fail(f'{path.name}: placement artifact must be a regular file')
        if metadata.st_size > MAX_BYTES:
            _fail(f'{path.name}: exceeds the {MAX_BYTES // 1024 // 1024} MiB artifact limit')
        with path.open('r', encoding='utf-8') as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw.encode('utf-8')) > MAX_BYTES:
            _fail(f'{path.name}: artifact is too large')
        if not is_json:
            return raw
        value = json.loads(raw, parse_constant=lambda x: _fail(f'{path.name}: nonfinite number {x}'))
        _bounded_tree(value)
        return value
    except PreviewError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        raise PreviewError(f'Cannot read placement artifact {path.name}: {error}') from error


def _transform(value, where):
    value = _dict(value, where)
    if set(value) != {'oX', 'oY', 'sX', 'sY'}:
        _fail(f'{where}: unsupported transform; expected oX/oY/sX/sY')
    for key in ('sX', 'sY'):
        if type(value[key]) is not int or value[key] not in (-1, 1):
            _fail(f'{where}: mirror scales must be +1 or -1')
    return {key: _number(value[key], where) for key in ('oX', 'oY', 'sX', 'sY')}


def _compose(parent, child):
    return {'oX': parent['oX'] + parent['sX'] * child['oX'],
            'oY': parent['oY'] + parent['sY'] * child['oY'],
            'sX': parent['sX'] * child['sX'], 'sY': parent['sY'] * child['sY']}


def _rect(r, tr, scale):
    x = [tr['oX'] + tr['sX'] * r[i] for i in (0, 2)]
    y = [tr['oY'] + tr['sY'] * r[i] for i in (1, 3)]
    return [min(x) * scale, min(y) * scale, max(x) * scale, max(y) * scale]


def _center(r):
    return [(r[0] + r[2]) / 2, (r[1] + r[3]) / 2]


def _index(rows, field, where):
    result = {}
    for row in _list(rows, where, MAX_BLOCKS):
        row = _dict(row, where)
        key = _name(row.get(field), where)
        if key in result:
            _fail(f'{where}: duplicate {field} {key}')
        result[key] = row
    return result


def _fa_map(instance):
    result = {}
    for row in _list(instance.get('fa_map'), 'instance fa_map'):
        row = _dict(row, 'fa_map entry')
        formal, actual = _name(row.get('formal'), 'formal pin'), _name(row.get('actual'), 'actual net')
        if formal in result:
            _fail(f'Duplicate formal pin {formal}')
        result[formal] = actual
    return result


def _source_names(netlist, top):
    """Read only instance tokens from ROAR's detached, flat SPICE export."""
    names, selected, found = {}, False, False
    for raw in netlist.splitlines():
        words = raw.strip().split()
        if not words or words[0].startswith(('*', '+')):
            continue
        command = words[0].lower()
        if command == '.subckt':
            if len(words) < 2:
                _fail('Malformed captured .subckt header')
            selected = words[1].casefold() == top.casefold()
            found |= selected
        elif command == '.ends':
            selected = False
        elif selected and not command.startswith('.'):
            name = _name(words[0], 'source instance')
            if name.casefold() in names:
                _fail(f'Duplicate source instance {name}')
            names[name.casefold()] = name
    if not found or not names:
        _fail(f'Captured input has no flat source instances for {top}')
    return names


def _groups(rows):
    groups = {}
    for row in _list(rows, 'compiler constraints'):
        row = _dict(row, 'compiler constraint')
        if row.get('constraint') != 'GroupBlocks':
            continue
        key = _name(row.get('instance_name'), 'GroupBlocks instance').casefold()
        members = [_name(n, 'GroupBlocks source') for n in _list(row.get('instances'), 'GroupBlocks instances')]
        if not members or len({n.casefold() for n in members}) != len(members) or key in groups:
            _fail('Ambiguous or empty compiler GroupBlocks lineage')
        groups[key] = members
    return groups


def _membership_resolver(sources, groups):
    """Memoize the lineage DAG with bounded depth and total expansion work."""
    cache, active = {}, set()
    work = 0

    def spend(amount=1):
        nonlocal work
        work += amount
        if work > MAX_LINEAGE_WORK:
            _fail('Compiler GroupBlocks lineage exceeds the supported work limit')

    def resolve(name, depth=0):
        key = name.casefold()
        if depth > MAX_LINEAGE_DEPTH:
            _fail('Compiler GroupBlocks lineage exceeds the supported depth limit')
        spend()
        if key in active:
            _fail('Cyclic compiler GroupBlocks lineage')
        if key in cache:
            return cache[key]
        if key not in groups:
            result = ((sources[key],), True) if key in sources else ((), False)
            cache[key] = result
            return result
        active.add(key)
        known, seen, complete = [], set(), True
        for member in groups[key]:
            # A compiler record may wrap a source with the same instance name.
            if member.casefold() == key and key in sources:
                spend()
                found, ok = (sources[key],), True
            else:
                found, ok = resolve(member, depth + 1)
            spend(len(found))
            for source in found:
                if source.casefold() in seen:
                    _fail(f'Compiler GroupBlocks {name} has overlapping source membership: {source}')
                seen.add(source.casefold())
                known.append(source)
            complete &= ok
        active.remove(key)
        cache[key] = tuple(known), complete
        return cache[key]

    return resolve


def _candidate(data, top, sources, membership, scale, spend_geometry):
    data = _dict(data, 'native placement')
    modules = _index(data.get('modules'), 'concrete_name', 'placement modules')
    leaves = _index(data.get('leaves'), 'concrete_name', 'placement leaves')
    if set(modules) & set(leaves):
        _fail('Concrete template identifies both a module and a primitive')
    roots = [m for m in modules.values() if str(m.get('abstract_name', '')).casefold() == top.casefold()]
    # Each native Results file represents one selected top candidate. Additional
    # modules are descendants, not independent top-level candidates.
    if not roots:
        return None  # Results may also contain independently placed child modules.
    if len(roots) != 1:
        _fail(f'Placement must identify exactly one concrete top candidate for {top}')
    root = roots[0]
    identity = {'oX': 0, 'oY': 0, 'sX': 1, 'sY': 1}
    for template in (*modules.values(), *leaves.values()):
        _bbox(template.get('bbox'), 'native template bbox')
        _name(template.get('abstract_name'), 'abstract template')
    blocks, net_connections, source_owners = [], {}, {}
    visits = 0

    def terminals(concrete, tr, trail=()):
        nonlocal visits
        visits += 1
        spend_geometry()
        if visits > MAX_BLOCKS or len(trail) > 32 or concrete in trail:
            _fail('Placement hierarchy is cyclic or exceeds the supported size')
        if concrete in leaves:
            result = []
            for pin in _list(leaves[concrete].get('terminals', []), 'primitive terminals'):
                spend_geometry(40)  # Bound expansion before constructing pins and net connections.
                pin = _dict(pin, 'primitive terminal')
                result.append((_name(pin.get('name'), 'terminal name'),
                               _rect(_bbox(pin.get('rect'), 'terminal rectangle'), tr, scale)))
            return result
        if concrete not in modules:
            _fail(f'Placement references missing concrete template {concrete}')
        result = []
        for inst in _list(modules[concrete].get('instances'), 'module instances', MAX_BLOCKS):
            inst = _dict(inst, 'module instance')
            _name(inst.get('instance_name'), 'instance name')
            child = _name(inst.get('concrete_template_name'), 'concrete template')
            mapping = _fa_map(inst)
            for formal, rect in terminals(child, _compose(tr, _transform(inst.get('transformation'), 'nested transform')), (*trail, concrete)):
                if formal in mapping:
                    result.append((mapping[formal], rect))
        return result

    instances = _index(root.get('instances'), 'instance_name', 'top instances')
    if not instances:
        _fail('Placement candidate has no blocks')
    for name, inst in instances.items():
        spend_geometry(40)
        concrete = _name(inst.get('concrete_template_name'), 'concrete template')
        template = leaves.get(concrete, modules.get(concrete))
        if template is None:
            _fail(f'Placement references missing concrete template {concrete}')
        tr = _transform(inst.get('transformation'), 'instance transform')
        bbox = _rect(_bbox(template['bbox'], 'template bbox'), tr, scale)
        source, complete = membership(name)
        spend_geometry(len(source) + sum(len(original) for original in source))
        source = list(source)
        for original in source:
            key = original.casefold()
            if key in source_owners:
                _fail(f'Compiler lineage assigns source {original} to multiple placed blocks: {source_owners[key]}, {name}')
            source_owners[key] = name
        mapping = _fa_map(inst)
        pins, physical_formals = [], set()
        for formal, rect in terminals(concrete, tr):
            if formal not in mapping:
                continue
            net = mapping[formal]
            physical_formals.add(formal)
            pin = {'name': formal, 'net': net, 'bbox': rect, 'position': _center(rect)}
            pins.append(pin)
            net_connections.setdefault(net, []).append({'block_id': name, 'pin': formal,
                                                        'bbox': rect, 'position': pin['position'], 'kind': 'pin'})
        for formal, net in mapping.items():
            if formal not in physical_formals:
                spend_geometry(16)
                net_connections.setdefault(net, []).append({'block_id': name, 'pin': formal,
                                                            'position': _center(bbox), 'kind': 'block'})
        blocks.append({'id': name, 'path': name, 'label': ', '.join(source) if source else name,
                       'concrete_name': concrete, 'abstract_name': _name(template.get('abstract_name'), 'abstract template'),
                       'bbox': bbox, 'transform': dict(tr, oX=tr['oX'] * scale, oY=tr['oY'] * scale),
                       'source_instances': source, 'mapping_status': 'complete' if complete else 'incomplete',
                       'mapping_reason': '' if complete else 'Compiler records do not establish complete source membership.',
                       'pins': pins})
    accounted = set(source_owners)
    missing = [name for key, name in sources.items() if key not in accounted]
    # Merging/removal happens before GroupBlocks creation in ALIGN 0.9.8. A
    # surviving singleton record cannot prove that another source was not merged
    # into it. Retain the known membership, but do not claim complete lineage.
    if missing:
        reason = 'Compiler lineage omits ' + ', '.join(missing) + '; merging or removal may have made source membership partial.'
        for block in blocks:
            block['mapping_status'], block['mapping_reason'] = 'incomplete', reason
    return {'name': _name(root.get('concrete_name'), 'candidate name'),
            'bbox': _rect(_bbox(root.get('bbox'), 'candidate bbox'), identity, scale),
            'blocks': blocks, 'nets': [{'name': name, 'connections': connections} for name, connections in sorted(net_connections.items())],
            'mapping': {'status': 'complete' if not missing and all(b['mapping_status'] == 'complete' for b in blocks) else 'incomplete',
                        'unmapped_source_instances': missing}}


def build_preview(run_dir, manifest):
    """Normalize a captured placement without executing code or changing inputs.

    ``manifest`` is the runner's run.json dictionary, with top, netlist, and
    generation_config.profile (including align_version). All artifact paths,
    including absolute manifest.netlist paths, must resolve inside run_dir.
    """
    root = Path(run_dir).resolve()
    manifest = _dict(manifest, 'run manifest')
    profile = _dict(_dict(manifest.get('generation_config'), 'generation config').get('profile'), 'ALIGN profile')
    version = profile.get('align_version')
    if version != '0.9.8':
        _fail(f'Unsupported ALIGN placement version {version!r}; this adapter supports 0.9.8')
    top = _name(manifest.get('top'), 'selected top')
    artifacts = []

    def read(relative, *, is_json=True):
        path = _confined(root, relative)
        value = _read(path, is_json=is_json)
        artifacts.append(path.relative_to(root).as_posix())
        return value

    netlist_path = manifest.get('netlist')
    if not isinstance(netlist_path, str) or not netlist_path:
        _fail('Captured run manifest is missing its detached netlist path')
    sources = _source_names(read(netlist_path, is_json=False), top)
    layers = _dict(read('3_pnr/inputs/layers.json'), 'captured technology layers')
    factor = _number(layers.get('ScaleFactor'), 'captured technology ScaleFactor')
    if factor <= 0:
        _fail('Captured technology ScaleFactor must be positive')
    scale = 1 / (1000 * factor)
    topology_dir = _confined(root, '1_topology')
    const_paths = sorted(p for p in topology_dir.glob('*.const.json') if p.name[:-11].casefold() == top.casefold())
    if len(const_paths) != 1:
        _fail(f'Missing or ambiguous compiler lineage file 1_topology/{top.lower()}.const.json')
    membership = _membership_resolver(sources, _groups(read(const_paths[0])))
    result_dir = _confined(root, '3_pnr/Results')
    paths = sorted(result_dir.glob('*.scaled_placement_verilog.json'))
    if not paths or len(paths) > 256:
        _fail('No supported placement candidates, or too many candidates; generate through placement first')
    geometry_work = 0

    def spend_geometry(amount=1):
        nonlocal geometry_work
        geometry_work += amount
        if geometry_work > MAX_ITEMS:
            _fail('Placement geometry expansion exceeds the supported output limit')

    candidates = [candidate for path in paths
                  if (candidate := _candidate(read(path), top, sources, membership, scale, spend_geometry)) is not None]
    preview = {'schema_version': SCHEMA_VERSION, 'units': 'um', 'coordinate_system': 'y-up', 'top': top,
               'run_id': root.name, 'generation_fingerprint': manifest.get('generation_fingerprint', manifest.get('fingerprint')),
               'provenance': {'adapter_contract': 'align-0.9.8-scaled-placement-v1', 'align_version': version,
                              'profile_id': manifest.get('profile_id', profile.get('id')),
                              'technology': profile.get('technology'), 'backend_revision': profile.get('backend_revision'),
                              'created_at': manifest.get('created_at'),
                              'unit_source': '3_pnr/inputs/layers.json:ScaleFactor', 'native_to_um': scale,
                              'artifacts': list(dict.fromkeys(artifacts))},
               'candidates': candidates}
    _validate_preview(preview, root)
    return preview


def _validate_preview(data, root):
    _bounded_tree(data)
    data = _dict(data, 'preview')
    if type(data.get('schema_version')) is not int or data['schema_version'] != SCHEMA_VERSION:
        _fail('Unsupported physical preview schema; expected schema_version 1')
    if data.get('units') != 'um' or data.get('coordinate_system') != 'y-up':
        _fail('Physical preview must use micrometres and upward y coordinates')
    _name(data.get('top'), 'preview top')
    _name(data.get('run_id'), 'preview run identity')
    fingerprint = data.get('generation_fingerprint')
    if fingerprint is not None and (not isinstance(fingerprint, str) or len(fingerprint) > 256):
        _fail('Malformed preview generation identity')
    provenance = _dict(data.get('provenance'), 'preview provenance')
    for relative in _list(provenance.get('artifacts'), 'provenance artifacts', 256):
        if not isinstance(relative, str) or Path(relative).is_absolute() or '..' in Path(relative).parts:
            _fail('Preview artifact paths must be relative to the captured run')
        _confined(root, relative)
    candidates = _index(_list(data.get('candidates'), 'preview candidates', MAX_CANDIDATES), 'name', 'preview candidates')
    if not candidates:
        _fail('Physical preview has no candidates')
    for candidate in candidates.values():
        _bbox(candidate.get('bbox'), 'preview candidate bbox')
        blocks = _index(candidate.get('blocks'), 'id', 'preview blocks')
        if not blocks:
            _fail('Physical preview candidate has no blocks')
        mapping = _dict(candidate.get('mapping'), 'candidate source mapping')
        if mapping.get('status') not in ('complete', 'incomplete'):
            _fail('Invalid candidate source mapping status')
        unmapped = {_name(source, 'source instance').casefold() for source in
                    _list(mapping.get('unmapped_source_instances'), 'unmapped sources', MAX_BLOCKS)}
        if mapping['status'] == 'complete' and unmapped:
            _fail('Complete candidate mapping cannot contain unmapped sources')
        physical_pins, source_owners = {}, {}
        for block in blocks.values():
            _bbox(block.get('bbox'), 'preview block bbox')
            _name(block.get('path'), 'compiled block path')
            _name(block.get('concrete_name'), 'concrete template')
            _name(block.get('abstract_name'), 'abstract template')
            _transform(block.get('transform'), 'normalized transform')
            if not isinstance(block.get('label'), str) or len(block['label']) > 65536:
                _fail('Invalid block label')
            members = _list(block.get('source_instances'), 'source membership', MAX_BLOCKS)
            for source in members:
                key = _name(source, 'source instance').casefold()
                if key in source_owners or key in unmapped:
                    _fail('Conflicting or overlapping normalized source membership')
                source_owners[key] = block['id']
            if block.get('mapping_status') not in ('complete', 'incomplete') or not isinstance(block.get('mapping_reason'), str):
                _fail('Invalid source mapping status/reason')
            if block['mapping_status'] == 'complete' and not members:
                _fail('Complete block mapping must identify source instances')
            if mapping['status'] == 'complete' and block['mapping_status'] != 'complete':
                _fail('Complete candidate mapping contains an incomplete block mapping')
            physical_pins[block['id']] = set()
            for pin in _list(block.get('pins'), 'block pins'):
                pin = _dict(pin, 'pin')
                _name(pin.get('name'), 'pin name')
                _name(pin.get('net'), 'pin net')
                _bbox(pin.get('bbox'), 'pin bbox')
                _point(pin.get('position'), 'pin position')
                physical_pins[block['id']].add((pin['name'], pin['net'], tuple(pin['bbox']), tuple(pin['position'])))
        nets = _index(candidate.get('nets'), 'name', 'preview nets')
        for net in nets.values():
            for connection in _list(net.get('connections'), 'net connections'):
                connection = _dict(connection, 'net connection')
                if _name(connection.get('block_id'), 'connection block') not in blocks:
                    _fail('Net connection references an unknown compiled block')
                _name(connection.get('pin'), 'net terminal')
                _point(connection.get('position'), 'connection position')
                if connection.get('kind') == 'pin':
                    _bbox(connection.get('bbox'), 'connection bbox')
                    signature = (connection['pin'], net['name'], tuple(connection['bbox']), tuple(connection['position']))
                    if signature not in physical_pins[connection['block_id']]:
                        _fail('Physical net connection does not match a recorded block pin')
                elif connection.get('kind') != 'block' or 'bbox' in connection:
                    _fail('Net connection must identify pin geometry or explicit block-level connectivity')
    return data


def load_preview(path):
    """Read a bounded normalized preview, rejecting unsafe geometry and paths."""
    path = Path(path).resolve()
    return _validate_preview(_read(path), path.parent)
