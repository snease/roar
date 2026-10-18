"""Native constraint capabilities and structural checks, independent of Qt/ALIGN.

The supported fields are checked against ALIGN 0.9.8. Native feasibility and
backend eligibility remain separate checks; this module is not a placer.
"""
import math

COMPILER_DEFAULTS = {
    'is_digital': False, 'auto_constraint': True, 'identify_array': True,
    'fix_source_drain': True, 'remove_dummy_hierarchies': True,
    'remove_dummy_devices': True, 'merge_series_devices': True,
    'merge_parallel_devices': True, 'same_template': True, 'propagate': True,
}

# A field whitelist prevents imported settings from being silently discarded.
KINDS = {
    'SymmetricBlocks': ('Device symmetry', {'pairs', 'direction'}),
    'Order': ('Relative order', {'instances', 'direction', 'abut'}),
    'Align': ('Align centers / edges', {'instances', 'line'}),
    'AlignInOrder': ('Align and order', {'instances', 'line', 'direction', 'abut'}),
    'Floorplan': ('Rows / regions', {'regions', 'order', 'symmetrize'}),
    'CompactPlacement': ('Packing', {'style'}),
    'PlaceCloser': ('Place closer', {'instances'}),
    'Boundary': ('Boundary and margins', {'max_width', 'max_height', 'halo_horizontal', 'halo_vertical'}),
    'AspectRatio': ('Aspect ratio', {'ratio_low', 'ratio_high', 'weight'}),
    'Spread': ('Minimum device spacing', {'instances', 'direction', 'distance'}),
    'BlockDistance': ('Circuit spacing', {'abs_distance'}),
    'HorizontalDistance': ('Horizontal circuit spacing', {'abs_distance'}),
    'VerticalDistance': ('Vertical circuit spacing', {'abs_distance'}),
    'SameTemplate': ('Same device layout template', {'instances'}),
    'PortLocation': ('Port location', {'ports', 'location'}),
    'SymmetricNets': ('Net symmetry', {'net1', 'net2', 'pins1', 'pins2', 'direction'}),
    'NetPriority': ('Net placement priority', {'nets', 'weight'}),
    'PowerPorts': ('Power ports', {'ports', 'propagate'}),
    'GroundPorts': ('Ground ports', {'ports', 'propagate'}),
    'ClockPorts': ('Clock ports', {'ports', 'propagate'}),
    'DoNotIdentify': ('Keep devices out of pattern recognition', {'instances'}),
    'ConfigureCompiler': ('Compiler transformations', set(COMPILER_DEFAULTS)),
}

# These can be inspected and exported without a form. The current flow stops
# before routing: routing rules are preserved for an external ALIGN continuation.
EXPORT_FIELDS = {
    'NetConst': {'nets', 'shield', 'criticality'},
    'MultiConnection': {'nets', 'multiplier'}, 'DoNotRoute': {'nets'},
    'DoNotUseLib': {'libraries', 'propagate'},
    'Route': {'min_layer', 'max_layer', 'customize'},
}
FIELDS = {kind: fields for kind, (_title, fields) in KINDS.items()} | EXPORT_FIELDS
PORT_LOCATIONS = {
    'TL': 'Top edge, left', 'TC': 'Top edge, center', 'TR': 'Top edge, right',
    'RT': 'Right edge, top', 'RC': 'Right edge, center', 'RB': 'Right edge, bottom',
    'BL': 'Bottom edge, left', 'BC': 'Bottom edge, center', 'BR': 'Bottom edge, right',
    'LB': 'Left edge, bottom', 'LC': 'Left edge, center', 'LT': 'Left edge, top',
}
HELP = {
    'SymmetricBlocks': 'Pairs share one symmetry axis. Matching models and sizes are required. This does not specify a common-centroid pattern.',
    'Boundary': 'Maximum circuit size and margins to the boundary, all in µm. Leave an optional field blank to use ALIGN’s default. The supported SKY130 backend requires left/right margins in multiples of 0.43 µm and top/bottom margins in multiples of 0.42 µm; values are never rounded automatically.',
    'AspectRatio': 'Circuit width / height must lie between the lower and upper bounds. ALIGN uses a strict upper bound during its feasibility check.',
    'Spread': 'Minimum spacing between selected devices when they overlap in the other direction. This does not force that overlap. Distances are in nm.',
    'BlockDistance': 'Minimum gap applied across the circuit, in nm. ALIGN rounds spacing up to the routing grid.',
    'HorizontalDistance': 'Minimum horizontal gap across the circuit, in nm. ALIGN rounds spacing up to the routing grid.',
    'VerticalDistance': 'Minimum vertical gap across the circuit, in nm. ALIGN rounds spacing up to the routing grid.',
    'SameTemplate': 'Use the same physical layout template for matching devices. This does not create a common-centroid pattern.',
    'AlignInOrder': 'Align the selected centers or edges and order devices left to right or top to bottom.',
    'PortLocation': 'Preferred boundary region for signal ports. Power, ground, and clock ports are handled by their port-role constraints.',
    'SymmetricNets': 'Match two nets about a shared axis. Order connected pins in corresponding pairs; every selected pin must belong to its net. ALIGN can derive additional device symmetry.',
    'ConfigureCompiler': 'Control automatic circuit transformations. Defaults match ALIGN 0.9.8; changing these can alter grouping and generated layouts.',
    'DoNotIdentify': 'Prevent these devices from being combined into recognized circuit patterns. Other compiler transformations are controlled separately.',
    'ClockPorts': 'Mark clock nets so ALIGN excludes them from ordinary signal symmetry inference.',
    'NetPriority': 'Give selected nets more weight in placement wirelength optimization. Larger weights favor shorter connections; this is a preference, not a length or matching guarantee. Unspecified nets have weight 1.',
}


def capability_note(constraint):
    kind = constraint.get('constraint')
    if not isinstance(kind, str):
        return 'Malformed native constraint type. Fields are preserved; disable this row to export the other constraints.'
    if kind in KINDS:
        return HELP.get(kind, 'Supported by the placement editor; native feasibility is checked by ALIGN.')
    if kind in EXPORT_FIELDS:
        if kind == 'NetConst':
            return 'Supported for native export; no graphical form. Criticality weights placement wirelength; shielding applies when routing is run outside this tool. Use Check constraints to validate these fields.'
        return ('Supported for native export; inspect fields here and use Check constraints to validate them. Routing settings take effect only when routing is run outside this placement-only tool.'
                if kind in ('Route', 'DoNotRoute', 'MultiConnection') else
                'Supported for native export; no graphical form is available yet. Use Check constraints to validate these fields.')
    if kind == 'PlaceOnBoundary':
        return 'ALIGN recognizes this rule, but its C++ placer used here drops it. Preserve it disabled; use supported ordering/alignment constraints for this flow.'
    return 'No validated export support in this editor. Fields are preserved; disable this row to export the other constraints.'


def net_pins(project):
    """Names accepted by native SymmetricNets, grouped by connected net."""
    result = {}
    for port in project.ports:
        choices = result.setdefault(port.upper(), [])
        if '/' not in port:
            choices.append(port)
    for device in project.devices:
        terminals = ('D', 'G', 'S', 'B') if device.kind == 'mos' else ('PLUS', 'MINUS')
        for terminal, net in zip(terminals, device.terminals):
            choices = result.setdefault(net.upper(), [])
            if '/' not in device.name:
                choices.append(f'{device.name}/{terminal}')
    return result


def _number(value, label, *, minimum=0, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum:
        raise ValueError(f'{label} must be a finite number of at least {minimum}')
    if integer and int(value) != value:
        raise ValueError(f'{label} must be an integer')
    return value

def validate_constraints(project, constraints, *, row_numbers=None):
    from .model import _scalar
    if not isinstance(constraints, list) or any(not isinstance(c, dict) for c in constraints):
        raise ValueError('Constraints must be a list of native ALIGN dictionaries')
    allowed = FIELDS
    row_numbers = list(row_numbers) if row_numbers is not None else list(range(1, len(constraints) + 1))
    if len(row_numbers) != len(constraints):
        raise ValueError('Constraint row numbers must match the constraints')

    def names(values, known, label, minimum=1):
        if not isinstance(values, list) or len(values) < minimum or any(not isinstance(v, str) or v.upper() not in known for v in values):
            raise ValueError(f'{label} must name at least {minimum} existing entries')
        if len({v.upper() for v in values}) != len(values):
            raise ValueError(f'{label} must be unique')

    nets = net_pins(project)
    port_names = {v.upper() for v in project.ports}

    def ids(values, minimum=1):
        if not isinstance(values, list) or len(values) < minimum or any(not isinstance(v, str) for v in values):
            raise ValueError(f'Expected at least {minimum} instance names')
        if len({v.upper() for v in values}) != len(values):
            raise ValueError('An instance cannot repeat within one constraint')
        return [project._device(value) for value in values]

    def symmetric(pairs):
        if not isinstance(pairs, list) or not pairs or any(not isinstance(p, list) or not 1 <= len(p) <= 2 for p in pairs):
            raise ValueError('Symmetry requires nonempty pairs of one or two instances')
        if len(pairs) == 1 and len(pairs[0]) != 2:
            raise ValueError('A single symmetry group must contain two instances')
        ids([name for pair in pairs for name in pair])
        for pair in pairs:
            devices = ids(pair)
            if len(devices) == 2 and all(d.kind != 'mos' or (d.width_um is not None and d.length_um is not None and d.nf is not None and d.parameters.get('M', 1) is not None) for d in devices):
                # Compare the exact SI strings written to ALIGN, so harmless
                # arithmetic rounding during ROAR import cannot break a pair.
                signatures = [(d.kind, d.model.upper(),
                               f'{d.width_um * 1e-6:.12g}' if d.kind == 'mos' else None,
                               f'{d.length_um * 1e-6:.12g}' if d.kind == 'mos' else None, d.nf,
                               d.parameters.get('M', 1), _scalar(d.value, 'resistance') if d.kind == 'resistor' else None)
                              for d in devices]
                if signatures[0] != signatures[1]:
                    raise ValueError(f'Symmetry pair {pair} must have matching models and parameters')

    for row, constraint in zip(row_numbers, constraints):
        try:
            kind = constraint.get('constraint')
            if not isinstance(kind, str) or kind not in allowed:
                raise ValueError(f'Constraint {row}: unsupported native constraint {kind!r}. ' + capability_note(constraint))
            unknown = set(constraint) - allowed[kind] - {'constraint'}
            if unknown:
                raise ValueError(f'{kind}: unsupported fields {", ".join(sorted(unknown))}')
            for key in ('abut', 'order', 'symmetrize', 'propagate'):
                if key in constraint and type(constraint[key]) is not bool:
                    raise ValueError(f'{kind}.{key} must be boolean')
            if kind == 'SymmetricBlocks':
                if constraint.get('direction') not in ('H', 'V'):
                    raise ValueError('Symmetry direction must be H or V')
                symmetric(constraint.get('pairs'))
            elif kind in ('Order', 'Align', 'PlaceCloser', 'SameTemplate', 'DoNotIdentify', 'Spread', 'AlignInOrder'):
                instances = constraint.get('instances')
                ids(instances, 1 if kind == 'DoNotIdentify' else 2)
                if kind == 'Order' and constraint.get('direction') not in ('left_to_right', 'top_to_bottom'):
                    raise ValueError('Order direction must be left_to_right or top_to_bottom; reverse the instance list for the opposite direction')
                if kind == 'Align' and constraint.get('line') not in ('h_top', 'h_bottom', 'h_center', 'v_left', 'v_right', 'v_center'):
                    raise ValueError('Align needs an explicit h_top/h_bottom/h_center/v_left/v_right/v_center line')
                if kind == 'SameTemplate':
                    for first, other in zip(instances, instances[1:]):
                        symmetric([[first, other]])
                if kind == 'Spread':
                    if constraint.get('direction') not in ('horizontal', 'vertical'):
                        raise ValueError('Spread direction must be horizontal or vertical')
                    _number(constraint.get('distance'), 'Spacing (nm)', integer=True)
                if kind == 'AlignInOrder':
                    line = constraint.get('line', 'bottom')
                    direction = constraint.get('direction') or ('horizontal' if line in ('top', 'bottom') else 'vertical' if line in ('left', 'right') else None)
                    if line not in ('top', 'bottom', 'left', 'right', 'center') or direction not in ('horizontal', 'vertical'):
                        raise ValueError('AlignInOrder needs an edge or center and an explicit direction for center alignment')
                    if (line in ('top', 'bottom') and direction != 'horizontal') or (line in ('left', 'right') and direction != 'vertical'):
                        raise ValueError('Top/bottom edges require horizontal order; left/right edges require vertical order')
            elif kind == 'Floorplan':
                rows = constraint.get('regions')
                if not isinstance(rows, list) or not rows:
                    raise ValueError('Floorplan needs nonempty rows in regions')
                for region in rows:
                    ids(region)
                ids([name for region in rows for name in region])
                if constraint.get('symmetrize'):
                    pairs = []
                    for region in rows:
                        pairs.extend([[region[i], region[-1-i]] for i in range(len(region)//2)])
                        if len(region) % 2:
                            pairs.append([region[len(region)//2]])
                    symmetric(pairs)
            elif kind == 'CompactPlacement':
                if constraint.get('style', 'left') not in ('left', 'right', 'center'):
                    raise ValueError('CompactPlacement style must be left, right or center')
            elif kind in ('PowerPorts', 'GroundPorts', 'ClockPorts', 'PortLocation'):
                ports = constraint.get('ports')
                if not isinstance(ports, list) or not ports or any(not isinstance(p, str) or p.upper() not in {v.upper() for v in project.ports} for p in ports):
                    raise ValueError(f'{kind} ports must name circuit ports')
                if len({p.upper() for p in ports}) != len(ports):
                    raise ValueError(f'{kind} ports must be unique')
                if kind == 'PortLocation' and constraint.get('location') not in PORT_LOCATIONS:
                    raise ValueError('Choose a boundary region for the ports')
            elif kind == 'Boundary':
                for key in allowed[kind]:
                    if key in constraint:
                        _number(constraint[key], f'Boundary {key} (µm)')
                for dimension, halo in (('max_width', 'halo_horizontal'), ('max_height', 'halo_vertical')):
                    if constraint.get(dimension, 100000) <= 2 * constraint.get(halo, 0):
                        raise ValueError(f'Boundary {dimension} must exceed twice {halo} to leave room for devices')
            elif kind == 'AspectRatio':
                low = _number(constraint.get('ratio_low', 0.1), 'Minimum aspect ratio')
                high = _number(constraint.get('ratio_high', 10), 'Maximum aspect ratio')
                if high <= low:
                    raise ValueError('Maximum aspect ratio must exceed minimum aspect ratio')
                _number(constraint.get('weight', 1), 'Aspect ratio weight', integer=True)
            elif kind in ('BlockDistance', 'HorizontalDistance', 'VerticalDistance'):
                _number(constraint.get('abs_distance'), f'{kind} spacing (nm)', integer=True)
            elif kind == 'ConfigureCompiler':
                for key, value in constraint.items():
                    if key != 'constraint' and type(value) is not bool:
                        raise ValueError(f'ConfigureCompiler.{key} must be boolean')
            elif kind == 'SymmetricNets':
                first, second = constraint.get('net1'), constraint.get('net2')
                names([first, second], set(nets), 'Symmetric nets', 2)
                if constraint.get('direction') not in ('H', 'V'):
                    raise ValueError('Net symmetry direction must be H or V')
                pins1, pins2 = constraint.get('pins1'), constraint.get('pins2')
                for pins in (pins1, pins2):
                    if isinstance(pins, list) and any(isinstance(pin, str) and
                            (pin.count('/') > 1 or ('/' in pin and pin.upper() in port_names)) for pin in pins):
                        raise ValueError('ALIGN uses / between an instance and terminal; matched pins cannot use instance or port names containing /')
                # Native writer cannot handle omitted/null pins. Require explicit
                # connectivity to avoid guessing correspondences for a designer.
                names(pins1, {p.upper() for p in nets[first.upper()]}, f'Pins on {first}')
                names(pins2, {p.upper() for p in nets[second.upper()]}, f'Pins on {second}')
                if len(pins1) != len(pins2):
                    raise ValueError('Symmetric nets must have the same number of matched pins')
            elif kind == 'DoNotUseLib':
                libraries = constraint.get('libraries')
                if not isinstance(libraries, list) or not libraries or any(not isinstance(v, str) or not v.strip() for v in libraries):
                    raise ValueError('DoNotUseLib requires library names')
            elif kind in ('NetPriority', 'NetConst', 'MultiConnection', 'DoNotRoute'):
                names(constraint.get('nets'), set(nets), f'{kind} nets')
                if kind == 'NetPriority':
                    _number(constraint.get('weight'), 'Net priority', integer=True)
                elif kind == 'MultiConnection':
                    _number(constraint.get('multiplier'), 'Connection multiplier', minimum=1, integer=True)
                elif kind == 'NetConst':
                    if constraint.get('shield') is not None:
                        names([constraint['shield']], set(nets), 'Shield net')
                    if constraint.get('criticality') is not None:
                        _number(constraint['criticality'], 'Net criticality', integer=True)
            elif kind == 'Route':
                def route_fields(values, keys):
                    if not isinstance(values, dict) or set(values) - keys:
                        raise ValueError('Route contains unsupported fields')
                    for key in ('min_layer', 'max_layer'):
                        if values.get(key) is not None and (not isinstance(values[key], str) or not values[key].strip()):
                            raise ValueError(f'Route {key} must name a layer')
                    for key in ('shield', 'match'):
                        if key in values and type(values[key]) is not bool:
                            raise ValueError(f'Route {key} must be boolean')
                route_fields(constraint, {'constraint', 'min_layer', 'max_layer', 'customize'})
                custom = constraint.get('customize', [])
                if not isinstance(custom, list):
                    raise ValueError('Route customize must be a list')
                for entry in custom:
                    route_fields(entry, {'nets', 'min_layer', 'max_layer', 'shield', 'match'})
                    names(entry.get('nets'), set(nets), 'Customized route nets')
        except ValueError as exc:
            raise ValueError(f"Constraint {row} ({constraint.get('constraint', 'unnamed')}): {exc}") from None
    _check_combinations(constraints, row_numbers)


def _check_combinations(constraints, row_numbers):
    """Reject obvious contradictions, without claiming physical feasibility."""
    graphs = {'horizontal': {}, 'vertical': {}}
    roles = {}
    settings = {}

    def reserve(key, row, remedy):
        previous = settings.setdefault(key, row)
        if previous != row:
            raise ValueError(f'Constraints {previous} and {row}: overlapping {key} settings. {remedy}')

    def edge(direction, before, after, row):
        graphs[direction].setdefault(before.upper(), {})[after.upper()] = row

    for row, constraint in zip(row_numbers, constraints):
        kind = constraint['constraint']
        if kind in ('Boundary', 'AspectRatio', 'CompactPlacement', 'ConfigureCompiler'):
            reserve(kind, row, 'Keep one enabled row for this circuit-wide setting; combine its fields in that row.')
        axes = {'BlockDistance': ('horizontal', 'vertical'),
                'HorizontalDistance': ('horizontal',), 'VerticalDistance': ('vertical',)}
        for axis in axes.get(kind, ()):
            reserve(f'{axis} circuit spacing', row,
                    'Use either one Circuit spacing row or separate Horizontal and Vertical circuit spacing rows. ALIGN can overwrite overlapping settings.')
        if kind == 'NetPriority' or (kind == 'NetConst' and 'criticality' in constraint):
            for net in constraint['nets']:
                reserve(f'placement weight for net {net.upper()}', row,
                        'Set each net’s placement weight in only one enabled NetPriority or NetConst row.')
        if kind in ('PowerPorts', 'GroundPorts', 'ClockPorts'):
            for port in constraint['ports']:
                previous = roles.setdefault(port.upper(), (kind, row))
                if previous[0] != kind:
                    raise ValueError(f'Constraints {previous[1]} and {row}: port {port} has conflicting {previous[0]} and {kind} roles')
        if kind in ('Order', 'AlignInOrder'):
            direction = constraint.get('direction')
            if kind == 'AlignInOrder' and direction is None:
                direction = 'horizontal' if constraint.get('line', 'bottom') in ('top', 'bottom') else 'vertical'
            axis = 'horizontal' if direction in ('left_to_right', 'horizontal') else 'vertical'
            for first, second in zip(constraint['instances'], constraint['instances'][1:]):
                edge(axis, first, second, row)
        elif kind == 'Floorplan':
            regions = constraint['regions']
            for upper, lower in zip(regions, regions[1:]):
                for first in upper:
                    for second in lower:
                        edge('vertical', first, second, row)
            if constraint.get('order'):
                for region in regions:
                    for first, second in zip(region, region[1:]):
                        edge('horizontal', first, second, row)

    for axis, graph in graphs.items():
        completed = set()
        def visit(node, path, rows):
            if node in path:
                start = path.index(node)
                involved = ', '.join(str(r) for r in sorted(set(rows[start:])))
                raise ValueError(f'Constraints {involved}: contradictory {axis} order cycle: ' + ' → '.join(path[start:] + [node]))
            if node in completed:
                return
            for following, row in graph.get(node, {}).items():
                visit(following, path + [node], rows + [row])
            completed.add(node)
        for node in graph:
            visit(node, [], [])

