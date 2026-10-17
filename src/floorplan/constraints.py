"""Structural native-constraint checks, independent of Qt and ALIGN."""

def validate_constraints(project, constraints):
    from .model import _scalar
    if not isinstance(constraints, list) or any(not isinstance(c, dict) for c in constraints):
        raise ValueError('Constraints must be a list of native ALIGN dictionaries')
    allowed = {
        'SymmetricBlocks': {'pairs', 'direction'}, 'Order': {'instances', 'direction', 'abut'},
        'Align': {'instances', 'line'}, 'Floorplan': {'regions', 'order', 'symmetrize'},
        'CompactPlacement': {'style'}, 'PlaceCloser': {'instances'},
        'PowerPorts': {'ports', 'propagate'}, 'GroundPorts': {'ports', 'propagate'}}

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

    for constraint in constraints:
        kind = constraint.get('constraint')
        if not isinstance(kind, str) or kind not in allowed:
            raise ValueError(f'Unsupported native constraint {kind!r}; it is preserved in project files')
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
        elif kind in ('Order', 'Align', 'PlaceCloser'):
            ids(constraint.get('instances'), 2)
            if kind == 'Order' and constraint.get('direction') not in ('left_to_right', 'top_to_bottom'):
                raise ValueError('Order direction must be left_to_right or top_to_bottom; reverse the instance list for the opposite direction')
            if kind == 'Align' and constraint.get('line') not in ('h_top', 'h_bottom', 'h_center', 'v_left', 'v_right', 'v_center'):
                raise ValueError('Align needs an explicit h_top/h_bottom/h_center/v_left/v_right/v_center line')
        elif kind == 'Floorplan':
            rows = constraint.get('regions')
            if not isinstance(rows, list) or not rows:
                raise ValueError('Floorplan needs nonempty rows in regions')
            for row in rows:
                ids(row)
            ids([name for row in rows for name in row])
            if constraint.get('symmetrize'):
                pairs = []
                for row in rows:
                    pairs.extend([[row[i], row[-1-i]] for i in range(len(row)//2)])
                    if len(row) % 2:
                        pairs.append([row[len(row)//2]])
                symmetric(pairs)
        elif kind == 'CompactPlacement':
            if constraint.get('style', 'left') not in ('left', 'right', 'center'):
                raise ValueError('CompactPlacement style must be left, right or center')
        else:
            ports = constraint.get('ports')
            if not isinstance(ports, list) or not ports or any(not isinstance(p, str) or p.upper() not in {v.upper() for v in project.ports} for p in ports):
                raise ValueError(f'{kind} ports must name circuit ports')
            if len({p.upper() for p in ports}) != len(ports):
                raise ValueError(f'{kind} ports must be unique')

