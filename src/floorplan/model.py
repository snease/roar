"""A detached editor for flat MOS/resistor circuits and native ALIGN constraints.

Dimensions held by this module are micrometres, with W always per finger.
ALIGN is an optional consumer, not an import-time dependency or a solver here.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
import tempfile

from parse_netlist import MOSFETInstance, _split_tokens, parse_spice_netlist


_SUFFIX = {'': 1, 't': 1e12, 'g': 1e9, 'meg': 1e6, 'k': 1e3,
           'm': 1e-3, 'u': 1e-6, 'n': 1e-9, 'p': 1e-12, 'f': 1e-15}
_NUMBER = re.compile(r'([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?)(meg|[tgkmunpf])?', re.I)
_PARAMETER = re.compile(r"([A-Za-z_]\w*)\s*=\s*('[^']*'|[^\s'=]+)")


def _positive(value, label):
    if isinstance(value, bool):
        raise ValueError(f'{label} must be a finite positive scalar')
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'{label} must be a finite positive scalar; resolve expressions first') from None
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f'{label} must be a finite positive scalar')
    return number


def _scalar(value, label):
    match = _NUMBER.fullmatch(str(value).strip())
    if not match:
        raise ValueError(f'{label} must be a concrete SPICE number; resolve expressions first')
    return _positive(float(match[1]) * _SUFFIX[(match[2] or '').lower()], label)


def _dimension(value, unit, label):
    if unit not in ('m', 'um'):
        raise ValueError('dimension_unit must be m or um')
    match = _NUMBER.fullmatch(str(value).strip())
    if not match:
        raise ValueError(f'{label} must be a concrete dimension; resolve expressions first')
    # Explicit SPICE suffixes are absolute; the selector affects bare numbers.
    return _positive(_scalar(value, label) * (1e6 if match[2] or unit == 'm' else 1), label)


def _fingers(value):
    number = _positive(value, 'NF')
    if not number.is_integer() or number < 2 or int(number) % 2:
        raise ValueError('NF must be an even integer of at least 2 for the SKY130 generator')
    return int(number)


def _convention(value):
    if value not in ('total', 'per_finger'):
        raise ValueError('width_convention must be total or per_finger')


def _read_blocks(source):
    """Read wrappers and continuations shared by discovery and import."""
    lines = []
    for raw in source.splitlines():
        line = raw.strip()
        if line.startswith('+'):
            if not lines:
                raise ValueError('Continuation without a preceding line')
            lines[-1] += ' ' + line[1:].strip()
        else:
            lines.append(line)
    blocks, current = {}, None
    for line in lines:
        if re.match(r'^\*+\.(subckt|ends)\b', line, re.I):
            line = line.lstrip('*')  # xschem commented circuit wrapper
        if not line or line.startswith('*'):
            continue
        tokens = line.split()
        directive = tokens[0].lower()
        if directive == '.subckt':
            if current is not None or len(tokens) < 2:
                raise ValueError('Nested or malformed .subckt is unsupported')
            current = tokens[1]
            if current.upper() in blocks:
                raise ValueError(f'Duplicate subcircuit {current}')
            blocks[current.upper()] = [tokens[2:], [], current]
        elif directive == '.ends':
            if current is None:
                raise ValueError('.ends without .subckt')
            if len(tokens) > 1 and tokens[1].upper() != current.upper():
                raise ValueError('.ends name does not match .subckt')
            current = None
        elif current is not None:
            blocks[current.upper()][1].append(line)
        elif directive.startswith('.') and directive != '.end':
            raise ValueError(f'Unsupported global directive {tokens[0]}; provide a self-contained flat subcircuit')
    if current is not None:
        raise ValueError('Missing .ends')
    if not blocks:
        raise ValueError('A flat .subckt/.ends circuit wrapper is required')
    return blocks


@dataclass
class Device:
    name: str
    kind: str
    terminals: tuple[str, ...]
    model: str = ''
    width_um: float | None = None
    length_um: float | None = None
    nf: int | None = None
    parameters: dict = field(default_factory=dict)
    value: str = ''


class FloorplanProject:
    """A selected flat subcircuit plus independently editable native constraints.

    Editing sizes does not evaluate ROAR expressions. Call validate before using
    the result; export does this automatically. Save/open preserve unsupported
    imported constraint dictionaries so they can be inspected and corrected.
    """

    @staticmethod
    def subcircuit_names(path):
        """List circuit wrappers without requiring resolved device sizing."""
        source = Path(path).expanduser().read_text(encoding='utf-8')
        return [block[2] for block in _read_blocks(source).values()]

    @classmethod
    def from_netlist(cls, path, top=None, dimension_unit='m', width_convention='per_finger'):
        path = Path(path).expanduser().resolve()
        project = cls._parse(path, top, dimension_unit, width_convention)
        project.source_path = str(path)
        return project

    @classmethod
    def from_text(cls, text, top=None, dimension_unit='m', width_convention='per_finger'):
        with tempfile.TemporaryDirectory(prefix='roar-floorplan-') as directory:
            path = Path(directory) / 'circuit.sp'
            path.write_text(text, encoding='utf-8')
            project = cls._parse(path, top, dimension_unit, width_convention)
        project.source_path = None
        return project

    @classmethod
    def _parse(cls, path, top, dimension_unit, width_convention):
        _convention(width_convention)
        if dimension_unit not in ('m', 'um'):
            raise ValueError('dimension_unit must be m or um')
        source = path.read_text(encoding='utf-8')
        blocks = _read_blocks(source)
        if top is None and len(blocks) != 1:
            raise ValueError('Select top explicitly when the netlist contains multiple subcircuits')
        selected = str(top).upper() if top is not None else next(iter(blocks))
        if selected not in blocks:
            raise ValueError(f'Subcircuit {top!r} not found; choose from {", ".join(blocks)}')
        ports, selected_lines, circuit_name = blocks[selected]
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', circuit_name):
            raise ValueError('Circuit name must contain letters, digits and underscores, starting with a letter or underscore')
        if any('=' in p or p.lower() == 'params:' for p in ports):
            raise ValueError('Parameterized .subckt headers are unsupported; resolve parameters first')
        if not ports or len({p.upper() for p in ports}) != len(ports):
            raise ValueError('Circuit ports must be nonempty and unique')
        if not selected_lines:
            raise ValueError('Selected circuit has no devices')
        for line in selected_lines:
            tokens = _split_tokens(line)
            prefix = tokens[0][0].upper()
            if prefix not in ('M', 'X', 'R'):
                raise ValueError(f'Unsupported selected circuit line: {line}')
            if prefix in ('M', 'X'):
                if len(tokens) < 6 or any('=' in token for token in tokens[:6]) or tokens[5].upper() in blocks:
                    raise ValueError(f'{tokens[0]}: only flat four-terminal MOS devices are supported; hierarchy is unsupported')
                # The shared tokenizer keeps quoted xschem expressions intact.
                # Bound its parameter section without evaluating any expression.
                section = ' '.join(tokens[6:])
                parameter_names, position = [], 0
                for match in _PARAMETER.finditer(section):
                    gap = section[position:match.start()]
                    if gap.strip() or (position and not gap):
                        raise ValueError(f'{tokens[0]}: malformed MOS parameter assignments')
                    parameter_names.append(match[1].upper())
                    position = match.end()
                if section[position:].strip():
                    raise ValueError(f'{tokens[0]}: malformed MOS parameter assignments')
                if len(set(parameter_names)) != len(parameter_names):
                    raise ValueError(f'{tokens[0]}: duplicate MOS parameter names')
            elif len(tokens) != 4 or any('=' in token for token in tokens):
                raise ValueError(f'{tokens[0]}: only two-terminal numeric resistors are supported')
        # Feed only the validated selection to the shared parser. This also
        # normalizes xschem comment wrappers and indented continuations without
        # modifying the retained source text.
        canonical = '\n'.join(['.subckt ' + circuit_name + ' ' + ' '.join(ports),
                               *selected_lines, '.ends ' + circuit_name])
        with tempfile.TemporaryDirectory(prefix='roar-floorplan-parse-') as directory:
            selected_path = Path(directory) / 'selected.sp'
            selected_path.write_text(canonical, encoding='utf-8')
            circuit = parse_spice_netlist(str(selected_path)).subcircuits[0]
        project = cls()
        project.source_netlist, project.top = source, circuit.name
        project.dimension_unit, project.width_convention = dimension_unit, width_convention
        project.ports, project.devices, project.constraints = list(circuit.ports), [], []
        for instance in circuit.instances:
            if isinstance(instance, MOSFETInstance):
                params = {key.upper(): value for key, value in instance.parameters.items()}
                if not {'W', 'L', 'NF'} <= params.keys():
                    raise ValueError(f'{instance.name}: concrete W, L and NF are required')
                unresolved = [f'{key}={params[key]!r}' for key in ('W', 'L', 'NF')
                              if not _NUMBER.fullmatch(str(params[key]).strip())]
                if unresolved:
                    raise ValueError(f'{instance.name}: unresolved sizing parameters {", ".join(unresolved)}. '
                                     'Use a resolved sizing netlist with numeric W/L/NF; symbolic xschem parameters '
                                     'are not evaluated. For the geometry-only OTA example, use '
                                     'design/floorplan/ota/current_mirror_ota.sp.')
                unknown = set(params) - {'W', 'L', 'NF', 'M'}
                if unknown:
                    raise ValueError(f'{instance.name}: unsupported MOS parameters {", ".join(sorted(unknown))}; provide W/L/NF and optional M only')
                nf = _fingers(params['NF'])
                width = _dimension(params['W'], dimension_unit, f'{instance.name} W')
                if width_convention == 'total':
                    width /= nf
                extra = {}
                if 'M' in params:
                    multiplier = _scalar(params['M'], f'{instance.name} M')
                    if not multiplier.is_integer():
                        raise ValueError(f'{instance.name}: M must be a positive integer for the SKY130 generator')
                    extra['M'] = int(multiplier)
                project.devices.append(Device(instance.name, 'mos',
                    (instance.drain, instance.gate, instance.source, instance.body), instance.model,
                    width, _dimension(params['L'], dimension_unit, f'{instance.name} L'), nf, extra))
            else:
                value = instance.nodes[2]
                _scalar(value, f'{instance.name} resistance')
                project.devices.append(Device(instance.name, 'resistor', tuple(instance.nodes[:2]), value=value))
        if len(project.devices) != len(selected_lines):
            raise ValueError('The selected circuit contains unsupported or unparsed lines')
        names = [device.name.upper() for device in project.devices]
        if len(names) != len(set(names)):
            raise ValueError('Duplicate device names')
        return project

    def _device(self, name):
        for device in self.devices:
            if device.name.upper() == str(name).upper():
                return device
        raise ValueError(f'Unknown circuit instance {name!r}')

    def set_mos_parameters(self, name, width_um, length_um, nf):
        device = self._device(name)
        if device.kind != 'mos':
            raise ValueError(f'{name} is not a MOS device')
        width, length, fingers = _positive(width_um, 'W per finger'), _positive(length_um, 'L'), _fingers(nf)
        device.width_um, device.length_um, device.nf = width, length, fingers

    def import_roar_parameters(self, design, source_instance, targets, dimension_unit='m', width_convention='total'):
        """Copy concrete assigned W/L/NF from one explicitly chosen ROAR row.

        Bare W/L values default to metres; W defaults to total device width.
        Targets are explicit, including grouped rows such as M1_2. No automatic
        name expansion, expression evaluation or design solver is performed.
        """
        _convention(width_convention)
        row = next((row for row in design.instances if row['Instance'] == source_instance), None)
        if row is None or row.get('disabled'):
            raise ValueError('Choose an enabled ROAR instance row')
        attrs = {key.upper(): info['value'] for key, info in
                 design.get_instance_parameters().get(source_instance, {}).items()}
        if not {'W', 'L', 'NF'} <= attrs.keys():
            raise ValueError('Selected ROAR row must have concrete W, L and NF values')
        nf = _fingers(attrs['NF'])
        width = _dimension(attrs['W'], dimension_unit, 'ROAR W')
        if width_convention == 'total':
            width /= nf
        length = _dimension(attrs['L'], dimension_unit, 'ROAR L')
        if isinstance(targets, str) or not targets:
            raise ValueError('Select a nonempty list of target MOS instances')
        devices = [self._device(target) for target in targets]
        if any(device.kind != 'mos' for device in devices):
            raise ValueError('ROAR size targets must all be MOS instances')
        for device in devices:
            self.set_mos_parameters(device.name, width, length, nf)

    def set_constraints(self, constraints):
        candidate = deepcopy(constraints)
        self._validate_constraints(candidate)
        self.constraints = candidate

    def add_constraint(self, constraint):
        self.set_constraints(self.constraints + [constraint])

    def remove_constraint(self, index):
        del self.constraints[index]

    def validate(self):
        for device in self.devices:
            if device.kind == 'mos':
                _positive(device.width_um, f'{device.name} W')
                _positive(device.length_um, f'{device.name} L')
                _fingers(device.nf)
        self._validate_constraints(self.constraints)

    def _validate_constraints(self, constraints):
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
            return [self._device(value) for value in values]

        def symmetric(pairs):
            if not isinstance(pairs, list) or not pairs or any(not isinstance(p, list) or not 1 <= len(p) <= 2 for p in pairs):
                raise ValueError('Symmetry requires nonempty pairs of one or two instances')
            if len(pairs) == 1 and len(pairs[0]) != 2:
                raise ValueError('A single symmetry group must contain two instances')
            ids([name for pair in pairs for name in pair])
            for pair in pairs:
                devices = ids(pair)
                if len(devices) == 2:
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
                if not isinstance(ports, list) or not ports or any(not isinstance(p, str) or p.upper() not in {v.upper() for v in self.ports} for p in ports):
                    raise ValueError(f'{kind} ports must name circuit ports')
                if len({p.upper() for p in ports}) != len(ports):
                    raise ValueError(f'{kind} ports must be unique')

    def to_dict(self):
        return {'version': 1, 'source_netlist': self.source_netlist, 'source_path': self.source_path,
                'top': self.top, 'dimension_unit': self.dimension_unit, 'width_convention': self.width_convention,
                'devices': [{'name': d.name, 'width_um': d.width_um, 'length_um': d.length_um, 'nf': d.nf}
                            for d in self.devices if d.kind == 'mos'], 'constraints': deepcopy(self.constraints)}

    @classmethod
    def from_dict(cls, state):
        expected = {'version', 'source_netlist', 'source_path', 'top', 'dimension_unit', 'width_convention', 'devices', 'constraints'}
        if not isinstance(state, dict) or state.get('version') != 1 or set(state) != expected:
            raise ValueError('Unsupported or malformed floorplan project version/fields')
        project = cls.from_text(state['source_netlist'], state['top'], state['dimension_unit'], state['width_convention'])
        project.source_path = state['source_path']
        sizes = state['devices']
        expected_names = {device.name for device in project.devices if device.kind == 'mos'}
        if not isinstance(sizes, list) or any(not isinstance(row, dict) or set(row) != {'name', 'width_um', 'length_um', 'nf'} for row in sizes):
            raise ValueError('Malformed saved MOS sizes')
        if len(sizes) != len(expected_names) or {row['name'] for row in sizes} != expected_names:
            raise ValueError('Saved sizes do not match the source circuit instances')
        for row in sizes:
            project.set_mos_parameters(**row)
        constraints = state['constraints']
        if not isinstance(constraints, list) or any(not isinstance(c, dict) for c in constraints):
            raise ValueError('Saved constraints must be native dictionaries')
        project.constraints = deepcopy(constraints)
        return project

    def save(self, path):
        path = Path(path).expanduser().resolve()
        if self.source_path and path == Path(self.source_path):
            raise ValueError('Project save cannot overwrite the source netlist')
        content = json.dumps(self.to_dict(), indent=2, allow_nan=False) + '\n'
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, encoding='utf-8', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        try:
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    @classmethod
    def load(cls, path):
        return cls.from_dict(json.loads(Path(path).expanduser().read_text(encoding='utf-8')))

    def export_align(self, directory):
        """Write a new two-file bundle, refusing collisions or source overwrites.

        JSON validity and concrete dimensions do not guarantee the selected PDK
        provides a primitive generator (notably for resistors).
        """
        self.validate()
        lines = ['* ROAR floorplan export: SI dimensions; W is per finger',
                 '.subckt ' + self.top + ' ' + ' '.join(self.ports)]
        for device in self.devices:
            prefix = device.name + ' ' + ' '.join(device.terminals)
            if device.kind == 'mos':
                line = (f'{prefix} {device.model} W={device.width_um * 1e-6:.12g} '
                        f'L={device.length_um * 1e-6:.12g} nf={device.nf}')
                for key, value in device.parameters.items():
                    line += f' {key}={value:.12g}'
            else:
                line = f'{prefix} {device.value}'
            lines.append(line)
        lines.append('.ends ' + self.top)
        netlist_text = '\n'.join(lines) + '\n'
        constraint_text = json.dumps(self.constraints, indent=2, allow_nan=False) + '\n'
        destination = Path(directory).expanduser().resolve() / self.top
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.mkdir()  # no reuse of stale or partially matching artifacts
        netlist = destination / (self.top + '.sp')
        constraints = destination / (self.top + '.const.json')
        try:
            netlist.write_text(netlist_text, encoding='utf-8')
            constraints.write_text(constraint_text, encoding='utf-8')
        except Exception:
            netlist.unlink(missing_ok=True)
            constraints.unlink(missing_ok=True)
            destination.rmdir()
            raise
        return netlist, constraints
