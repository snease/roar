"""Unscaled drawing state and a small, Qt-free native relationship adapter.

Coordinates are rectangle centers in screen space (positive y is down). Equal
placeholder sizes deliberately carry no inference about physical device area.
Kiwi is imported only when solving, keeping document/headless imports optional.
"""
from __future__ import annotations

import math

from .constraints import FIELDS

BLOCK_WIDTH = 120
BLOCK_HEIGHT = 72
COORDINATE_LIMIT = 10_000_000
_LIVE = {'Align', 'Order', 'AlignInOrder', 'SymmetricBlocks'}


def _coordinate(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not math.isfinite(value) or abs(value) > COORDINATE_LIMIT):
        raise ValueError(f'Sketch coordinates must be finite numbers within ±{COORDINATE_LIMIT}')
    return float(value)


def _positions(project, values, *, base=None):
    if not isinstance(values, dict):
        raise ValueError('Sketch positions must map instance names to coordinate pairs')
    names = {device.name.upper(): device.name for device in project.devices}
    result = dict(base or {})
    seen = set()
    for name, pair in values.items():
        if not isinstance(name, str) or name.upper() not in names:
            raise ValueError(f'Sketch position names an unknown instance: {name!r}')
        canonical = names[name.upper()]
        if canonical in seen:
            raise ValueError(f'Duplicate sketch instance: {name}')
        seen.add(canonical)
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError(f'{name}: sketch position must contain x and y')
        result[canonical] = [_coordinate(value) for value in pair]
    return result


def validate_sketch(project, state):
    """Return canonical detached state, filling missing positions deterministically."""
    if not isinstance(state, dict) or set(state) != {'positions', 'groups'}:
        raise ValueError('Sketch must contain positions and groups only')
    columns = max(1, math.ceil(math.sqrt(len(project.devices))))
    defaults = {device.name: [float((index % columns) * 180), float((index // columns) * 120)]
                for index, device in enumerate(project.devices)}
    positions = _positions(project, state['positions'], base=defaults)
    groups = state['groups']
    if not isinstance(groups, list):
        raise ValueError('Sketch groups must be a list')
    known = {device.name.upper(): device.name for device in project.devices}
    ids, members, result = set(), set(), []
    for group in groups:
        if not isinstance(group, dict) or set(group) != {'id', 'label', 'instances'}:
            raise ValueError('Sketch groups require id, label, and instances')
        identity, label, instances = group['id'], group['label'], group['instances']
        if not isinstance(identity, str) or not identity.strip() or len(identity) > 128 or identity in ids:
            raise ValueError('Sketch group IDs must be unique nonempty strings')
        if not isinstance(label, str) or not label.strip() or len(label) > 256:
            raise ValueError('Sketch group labels must be nonempty text up to 256 characters')
        if (not isinstance(instances, list) or not instances or
                any(not isinstance(name, str) or name.upper() not in known for name in instances)):
            raise ValueError('Sketch groups must name existing instances')
        canonical = [known[name.upper()] for name in instances]
        if len(set(canonical)) != len(canonical) or members.intersection(canonical):
            raise ValueError('Each instance may belong to only one non-nested sketch group')
        ids.add(identity)
        members.update(canonical)
        result.append({'id': identity, 'label': label, 'instances': canonical})
    return {'positions': positions, 'groups': result}


def sketch_capability(constraint):
    """Return enforced, exported, or unsupported for a native record's shape.

    This describes the adapter, not native validation or physical feasibility.
    Unsupported extra fields are never silently treated as an enforced rule.
    """
    if not isinstance(constraint, dict):
        return 'unsupported'
    kind = constraint.get('constraint')
    if not isinstance(kind, str) or kind not in FIELDS or set(constraint) - FIELDS[kind] - {'constraint'}:
        return 'unsupported'
    if kind not in _LIVE:
        return 'exported'
    if kind == 'Align' and constraint.get('line') not in ('h_top', 'h_bottom', 'h_center', 'v_left', 'v_right', 'v_center'):
        return 'unsupported'
    if kind == 'Order' and constraint.get('direction') not in ('left_to_right', 'top_to_bottom'):
        return 'unsupported'
    return 'enforced'


def capability_note(constraint):
    status = sketch_capability(constraint)
    if status == 'enforced':
        return 'Enforced in the sketch with equal placeholder rectangles; ALIGN checks physical feasibility.'
    if status == 'exported':
        return 'Exported to ALIGN; this rule is not enforced in the unscaled sketch.'
    return 'No validated export support; preserve this rule disabled or edit its native fields.'


class SketchConflict(ValueError):
    """An infeasible/invalid live rule; the project itself is never modified."""

    def __init__(self, message, rule_ids=(), instances=()):
        self.rule_ids = tuple(rule_ids)
        self.instances = tuple(instances)
        super().__init__(message)


def _participant_names(constraint):
    """Keep useful names in diagnostics even when a rule's structure is bad."""
    if 'instances' in constraint:
        values = constraint['instances']
        return [value for value in values if isinstance(value, str)] if isinstance(values, list) else []
    pairs = constraint.get('pairs')
    return ([name for pair in pairs if isinstance(pair, list) for name in pair if isinstance(name, str)]
            if isinstance(pairs, list) else [])


def _validate_live_constraint(constraint, known):
    """Check drawing semantics without imposing device sizing/export checks.

    Native validation still checks model/size matching before generation. A
    repairable sizing draft must remain movable with placeholder rectangles.
    """
    kind = constraint['constraint']
    if 'abut' in constraint and type(constraint['abut']) is not bool:
        raise ValueError('abut must be boolean')
    if kind == 'SymmetricBlocks':
        pairs = constraint.get('pairs')
        if (not isinstance(pairs, list) or not pairs or
                any(not isinstance(pair, list) or not 1 <= len(pair) <= 2 for pair in pairs)):
            raise ValueError('Symmetry requires nonempty pairs of one or two instances')
        if len(pairs) == 1 and len(pairs[0]) != 2:
            raise ValueError('A single symmetry group must contain two instances')
        if constraint.get('direction') not in ('H', 'V'):
            raise ValueError('Symmetry direction must be H or V')
        names = [name for pair in pairs for name in pair]
    else:
        names = constraint.get('instances')
        if not isinstance(names, list) or len(names) < 2:
            raise ValueError('Expected at least two instance names')
    if any(not isinstance(name, str) or name.upper() not in known for name in names):
        raise ValueError('Rule participants must name existing instances')
    if len({name.upper() for name in names}) != len(names):
        raise ValueError('An instance cannot repeat within one constraint')
    if kind == 'AlignInOrder':
        line = constraint.get('line', 'bottom')
        direction = constraint.get('direction') or ('horizontal' if line in ('top', 'bottom') else
                                                    'vertical' if line in ('left', 'right') else None)
        if line not in ('top', 'bottom', 'left', 'right', 'center') or direction not in ('horizontal', 'vertical'):
            raise ValueError('AlignInOrder requires an edge or center and a valid direction')
        if ((line in ('top', 'bottom') and direction != 'horizontal') or
                (line in ('left', 'right') and direction != 'vertical')):
            raise ValueError('AlignInOrder edge and direction do not agree')
    return names


def solve_positions(project, positions=None, suggestions=None):
    """Solve enabled live native rules and return detached center coordinates.

    Positional stays are weak; temporary drag/grid suggestions are strong and
    always yield to required relationships. Explicit order keeps boxes apart
    (or abuts them); unrelated boxes may overlap. Physical spacing is ignored.
    Symmetry uses ALIGN's exact shared axis and quarter-size orthogonal center
    tolerance. Already feasible saved positions stay unchanged; exact pair
    center alignment is an explicit Align relationship.
    """
    import kiwisolver as kiwi

    start = _positions(project, {} if positions is None else positions, base=project.sketch['positions'])
    desired = _positions(project, {} if suggestions is None else suggestions)
    variables = {name: (kiwi.Variable(f'{name}.x'), kiwi.Variable(f'{name}.y')) for name in start}
    canonical = {name.upper(): name for name in start}
    solver = kiwi.Solver()
    for name, (x, y) in variables.items():
        for variable, value in zip((x, y), start[name]):
            solver.addConstraint((variable == value) | kiwi.strength.weak)
            solver.addConstraint(variable >= -COORDINATE_LIMIT)
            solver.addConstraint(variable <= COORDINATE_LIMIT)
        if name in desired:
            for variable, value in zip((x, y), desired[name]):
                solver.addConstraint((variable == value) | kiwi.strength.strong)

    def xy(name):
        return variables[canonical[name.upper()]]

    def align(names, axis):
        return [xy(first)[axis] == xy(other)[axis] for first, other in zip(names, names[1:])]

    def order(names, axis, abut):
        distance = (BLOCK_WIDTH, BLOCK_HEIGHT)[axis]
        return [(xy(second)[axis] - xy(first)[axis] == distance if abut else
                 xy(second)[axis] - xy(first)[axis] >= distance)
                for first, second in zip(names, names[1:])]

    applied_ids, involved = [], set()
    for index, (constraint, enabled, identity) in enumerate(zip(project.constraints, project._constraint_flags(), project.constraint_ids)):
        if not enabled or sketch_capability(constraint) != 'enforced':
            continue
        kind = constraint['constraint']
        applied_ids.append(identity)
        involved.update(_participant_names(constraint))
        try:
            names = _validate_live_constraint(constraint, canonical)
            required = []
            if kind == 'Align':
                required = align(names, 1 if constraint['line'].startswith('h_') else 0)
            elif kind in ('Order', 'AlignInOrder'):
                if kind == 'Order':
                    axis = 0 if constraint['direction'] == 'left_to_right' else 1
                else:
                    line = constraint.get('line', 'bottom')
                    direction = constraint.get('direction') or ('horizontal' if line in ('top', 'bottom') else 'vertical')
                    axis = 0 if direction == 'horizontal' else 1
                    required.extend(align(names, 1 - axis))
                required.extend(order(names, axis, constraint.get('abut', False)))
            elif kind == 'SymmetricBlocks':
                axis = 0 if constraint['direction'] == 'V' else 1
                symmetry_axis = kiwi.Variable(f'{identity}.axis')
                for pair in constraint['pairs']:
                    first = xy(pair[0])
                    if len(pair) == 1:
                        required.append(first[axis] == symmetry_axis)
                    else:
                        second = xy(pair[1])
                        required.append(first[axis] + second[axis] == 2 * symmetry_axis)
                        tolerance = (BLOCK_WIDTH, BLOCK_HEIGHT)[1 - axis] / 4
                        required.extend((first[1 - axis] - second[1 - axis] <= tolerance,
                                         second[1 - axis] - first[1 - axis] <= tolerance))
            for relationship in required:
                solver.addConstraint(relationship)
        except (ValueError, KeyError, TypeError, kiwi.UnsatisfiableConstraint) as exc:
            instances = sorted(involved)
            raise SketchConflict(f'Sketch conflict at rule {index + 1} ({kind}, {identity}) involving '
                                 f'{", ".join(instances)}. Check enabled rules {", ".join(applied_ids)}: {exc}',
                                 applied_ids, instances) from None
    solver.updateVariables()
    return {name: [x.value(), y.value()] for name, (x, y) in variables.items()}
