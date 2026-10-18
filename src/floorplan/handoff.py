"""Portable, static floorplan review bundles; no Qt, ALIGN, or browser required."""
from copy import deepcopy
from html import escape
import json
import math
import os
from pathlib import Path
import re
import tempfile
import zipfile

from .align_runner import project_fingerprint
from .geometry import _confined, _validate_preview
from .model import FloorplanProject
from .sketch import BLOCK_HEIGHT, BLOCK_WIDTH


def _json(value):
    return (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')


def _text(value):
    # Group labels may contain XML control characters; render them literally.
    return escape(''.join(char if char in '\t\n\r' or 0x20 <= ord(char) <= 0xD7FF or
                          0xE000 <= ord(char) <= 0xFFFD or 0x10000 <= ord(char) <= 0x10FFFF
                          else '\ufffd' for char in str(value)))


def _svg(boxes, title, groups=(), bounds=None):
    """Render detached rectangles deterministically, using escaped text only."""
    if not boxes:
        raise ValueError('A handoff drawing needs at least one device')
    extents = [*boxes, *groups, *([bounds] if bounds else [])]
    xmin, ymin = (min(box[axis] for box in extents) for axis in (0, 1))
    xmax, ymax = (max(box[axis] for box in extents) for axis in (2, 3))
    size = max(xmax - xmin, ymax - ymin, 1e-12)
    margin = size * .06
    number = lambda value: format(value, '.12g')
    lines = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{number(xmin-margin)} {number(ymin-2*margin)} '
             f'{number(xmax-xmin+2*margin)} {number(ymax-ymin+3*margin)}" role="img">',
             f'<title>{_text(title)}</title>',
             f'<rect x="{number(xmin-margin)}" y="{number(ymin-2*margin)}" '
             f'width="{number(xmax-xmin+2*margin)}" height="{number(ymax-ymin+3*margin)}" fill="white"/>',
             f'<g font-family="sans-serif" stroke-width="{number(size/900)}">',
             f'<text x="{number(xmin)}" y="{number(ymin-margin)}" font-size="{number(size/65)}">{_text(title)}</text>']
    if bounds:
        x0, y0, x1, y1 = bounds
        lines.append(f'<rect x="{number(x0)}" y="{number(y0)}" width="{number(x1-x0)}" '
                     f'height="{number(y1-y0)}" fill="none" stroke="#9aa6b8"/>')
    for x0, y0, x1, y1, label in groups:
        lines.append(f'<rect x="{number(x0)}" y="{number(y0)}" width="{number(x1-x0)}" height="{number(y1-y0)}" '
                     'fill="#f0f4fa" stroke="#aab7cb" stroke-dasharray="4 3"/>')
        lines.append(f'<text x="{number(x0)}" y="{number(y0-size/180)}" font-size="{number(size/90)}">{_text(label)}</text>')
    for x0, y0, x1, y1, label, detail in boxes:
        short = label if len(label) <= 26 else label[:25] + '…'
        font = min((y1-y0) * .24, (x1-x0) / max(8, len(short)) * 1.4)
        lines.extend([f'<g><title>{_text(detail)}</title>',
                      f'<rect x="{number(x0)}" y="{number(y0)}" width="{number(x1-x0)}" height="{number(y1-y0)}" '
                      'fill="#e6eef8" stroke="#33445b"/>',
                      f'<text x="{number((x0+x1)/2)}" y="{number((y0+y1)/2)}" font-size="{number(font)}" '
                      f'text-anchor="middle" dominant-baseline="middle">{_text(short)}</text></g>'])
    return ('\n'.join(lines + ['</g></svg>']) + '\n').encode('utf-8')


def _sketch_svg(project):
    positions = project.sketch['positions']
    labels = project.device_labels
    boxes = []
    for device in project.devices:
        x, y = positions[device.name]
        label = device.name + (': ' + labels[device.name] if device.name in labels else '')
        detail = f'{label}\n{device.model}\n' + ', '.join(device.terminals)
        boxes.append((x-BLOCK_WIDTH/2, y-BLOCK_HEIGHT/2, x+BLOCK_WIDTH/2, y+BLOCK_HEIGHT/2, label, detail))
    groups = []
    for group in project.sketch['groups']:
        points = [positions[name] for name in group['instances']]
        groups.append((min(p[0] for p in points)-BLOCK_WIDTH/2-12,
                       min(p[1] for p in points)-BLOCK_HEIGHT/2-12,
                       max(p[0] for p in points)+BLOCK_WIDTH/2+12,
                       max(p[1] for p in points)+BLOCK_HEIGHT/2+12, group['label']))
    return _svg(boxes, 'Current sketch — not to scale', groups)


def _table(headers, rows):
    return ('<table><thead><tr>' + ''.join(f'<th>{_text(value)}</th>' for value in headers) +
            '</tr></thead><tbody>' + ''.join('<tr>' + ''.join(f'<td>{_text(value)}</td>' for value in row) + '</tr>'
                                            for row in rows) + '</tbody></table>')


def _intent_summary(project):
    labels = project.device_labels
    devices = []
    for device in project.devices:
        mos = device.kind == 'mos'
        sizes = [format(value, '.12g') for value in (device.width_um, device.length_um)] if mos else ['—', '—']
        pins = ('D', 'G', 'S', 'B') if mos else ('+', '−')
        devices.append([device.name, labels.get(device.name, ''), device.model or device.kind,
                        *sizes, device.nf if mos else '—',
                        f'M={device.parameters.get("M", 1)}' if mos else f'R={device.value}',
                        '; '.join(f'{pin}: {net}' for pin, net in zip(pins, device.terminals))])
    constraints = [[index, 'Enabled' if enabled else 'Disabled', rule.get('constraint', 'Unknown'),
                    json.dumps({key: value for key, value in rule.items() if key != 'constraint'},
                               ensure_ascii=False, sort_keys=True, allow_nan=False)]
                   for index, (rule, enabled) in enumerate(zip(project.constraints, project._constraint_flags()), 1)]
    return ('<h2>Current devices</h2><p>W is per finger; W and L are in micrometres. '
            'Resistance uses SPICE notation.</p>' +
            _table(('Instance', 'Role', 'Model', 'W / finger (µm)', 'L (µm)', 'NF', 'M / resistance', 'Terminals'), devices) +
            '<h2>Current placement constraints</h2>' +
            (_table(('Row', 'State', 'Kind', 'Settings'), constraints) if constraints else '<p>No placement constraints.</p>'))


def _portable_inputs(project, directory):
    netlist, constraints = project.export_align(directory)
    netlist_text = netlist.read_text(encoding='utf-8')
    # Rebuild a numeric source: raw comments and parameter provenance can contain
    # workstation paths. The handoff is a frozen snapshot, with no live bindings.
    frozen = FloorplanProject.from_text(netlist_text, dimension_unit='m', width_convention='per_finger')
    for device in project.devices:
        if device.kind == 'mos':
            frozen.set_mos_parameters(device.name, device.width_um, device.length_um, device.nf)
    state, original = frozen.to_dict(), project.to_dict()
    for key in ('constraints', 'constraint_enabled', 'constraint_ids', 'sketch',
                'device_labels', 'align_profile', 'run_options'):
        state[key] = deepcopy(original[key])
    state.update(source_path=None, roar_project_path=None, source_mode='saved')
    FloorplanProject.from_dict(state)  # Verify that recipients can reopen it.
    return state, netlist_text.encode('utf-8'), constraints.read_bytes()


def _selected_preview(preview, root, candidate_index):
    _validate_preview(preview, root)
    if type(candidate_index) is not int or not 0 <= candidate_index < len(preview['candidates']):
        raise ValueError('Choose an existing physical placement candidate')
    candidate = preview['candidates'][candidate_index]
    # Copy only the normalized contract, never arbitrary native/run metadata.
    selected = {key: deepcopy(candidate[key]) for key in ('name', 'bbox')}
    selected['mapping'] = {key: deepcopy(candidate['mapping'][key]) for key in ('status', 'unmapped_source_instances')}
    selected['blocks'] = []
    for block in candidate['blocks']:
        item = {key: deepcopy(block[key]) for key in ('id', 'path', 'label', 'concrete_name', 'abstract_name',
                'bbox', 'transform', 'source_instances', 'mapping_status', 'mapping_reason')}
        item['pins'] = [{key: deepcopy(pin[key]) for key in ('name', 'net', 'bbox', 'position')} for pin in block['pins']]
        selected['blocks'].append(item)
    selected['nets'] = [{'name': net['name'], 'connections': [
        {key: deepcopy(connection[key]) for key in ('block_id', 'pin', 'kind', 'position', 'bbox') if key in connection}
        for connection in net['connections']]} for net in candidate['nets']]
    provenance = {'artifacts': []}
    for key in ('adapter_contract', 'align_version', 'profile_id', 'technology', 'backend_revision'):
        value = preview['provenance'].get(key)
        if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:+-]{1,128}', value):
            provenance[key] = value
    for key in ('created_at', 'native_to_um'):
        value = preview['provenance'].get(key)
        if type(value) in (int, float) and abs(value) < 1e15 and math.isfinite(value):
            provenance[key] = value
    return {'schema_version': 1, 'units': 'um', 'coordinate_system': 'y-up',
            'top': preview['top'], 'run_id': 'captured-placement',
            'generation_fingerprint': preview.get('generation_fingerprint'),
            'provenance': provenance, 'candidates': [selected]}


def export_handoff(project, destination, *, preview=None, state=None, candidate_index=0):
    """Atomically write a ZIP of current intent and an optional captured candidate.

    ``project`` must have concrete valid export inputs. Pass the displayed
    sketch positions in its sketch state. ``preview`` is normalized geometry;
    ``state.run_dir`` must identify its original run containing floorplan.json.
    Any validation/write failure leaves an existing destination untouched.
    """
    if not str(destination).strip():
        raise ValueError('Choose a handoff ZIP destination')
    target = Path(destination).expanduser().absolute()
    if target.suffix.lower() != '.zip':
        raise ValueError('A handoff bundle must use the .zip extension')
    current = FloorplanProject.from_dict(project.to_dict())
    current.validate()
    fingerprint = project_fingerprint(current)
    files = {}
    manifest = {'schema_version': 1, 'top': current.top, 'current_fingerprint': fingerprint,
                'current_floorplan': 'current/floorplan.roar.floorplan', 'placement': None}
    with tempfile.TemporaryDirectory(prefix='roar-handoff-') as directory:
        stage = Path(directory)

        def inputs(folder, captured):
            portable, netlist, constraints = _portable_inputs(captured, stage / folder)
            files[f'{folder}/floorplan.roar.floorplan'] = _json(portable)
            files[f'{folder}/input/{captured.top}.sp'] = netlist
            files[f'{folder}/input/{captured.top}.const.json'] = constraints

        inputs('current', current)
        files['current/sketch.svg'] = _sketch_svg(current)
        placement_note = 'No physical placement is included.'
        physical = ''
        if preview is not None:
            run_dir = getattr(state, 'run_dir', None)
            if not isinstance(run_dir, str) or not run_dir or not Path(run_dir).is_absolute():
                raise ValueError('The displayed placement needs its original captured run to export')
            root = Path(run_dir).resolve()
            captured = FloorplanProject.load(_confined(root, 'floorplan.json'))
            captured.validate()
            captured_fingerprint = project_fingerprint(captured)
            selected = _selected_preview(preview, root, candidate_index)
            if (selected['generation_fingerprint'] != captured_fingerprint or
                    selected['top'].casefold() != captured.top.casefold()):
                raise ValueError('The displayed placement does not match the captured floorplan inputs')
            earlier = captured_fingerprint != fingerprint
            stale = earlier or bool(getattr(state, 'stale', False))
            placement_note = ('EARLIER INPUTS / SETTINGS: this candidate is paired with its captured inputs in placement/; '
                              'it does not establish placement of the current design.' if stale else
                              'This candidate matches the current generation inputs; its captured inputs are in placement/.')
            inputs('placement', captured)
            files['placement/preview.json'] = _json(selected)
            candidate = selected['candidates'][0]
            labels = {name.casefold(): label for name, label in captured.device_labels.items()}
            boxes = []
            for block in candidate['blocks']:
                label = ', '.join(source + (': ' + labels[source.casefold()] if source.casefold() in labels else '')
                                  for source in block['source_instances']) or block['label']
                boxes.append((block['bbox'][0], -block['bbox'][3], block['bbox'][2], -block['bbox'][1],
                              label, f"{block['id']}: {label}"))
            x0, y0, x1, y1 = candidate['bbox']
            dimensions = f'{x1-x0:g} × {y1-y0:g} µm'
            files['placement/placement.svg'] = _svg(boxes, f"{candidate['name']} — {dimensions}, y up",
                                                    bounds=(x0, -y1, x1, -y0))
            mapping = candidate['mapping']
            mapping_note = ('Complete source mapping.' if mapping['status'] == 'complete' else
                            'INCOMPLETE SOURCE MAPPING: known membership may be partial. Unmapped sources: ' +
                            (', '.join(mapping['unmapped_source_instances']) or 'not established'))
            physical = (f'<h2>Captured physical candidate: {_text(candidate["name"])}</h2>'
                        f'<p>Width × height: {_text(dimensions)}. Physical coordinates have y pointing up.</p>'
                        f'<p>{_text(mapping_note)}</p><img src="placement/placement.svg" alt="Captured physical placement">')
            manifest['placement'] = {'candidate': candidate['name'], 'captured_fingerprint': captured_fingerprint,
                                     'earlier_inputs': earlier, 'stale': stale,
                                     'floorplan': 'placement/floorplan.roar.floorplan',
                                     'preview': 'placement/preview.json'}
        manifest['files'] = sorted([*files, 'manifest.json', 'README.txt', 'index.html'])
        files['manifest.json'] = _json(manifest)
        explanation = ('Current sketch positions express intent and are not physical dimensions. '
                       'Physical candidates are placement only: connections are not routing, and no final GDS is included. '
                       'Floorplans contain frozen numeric sizes and need no original ROAR design file. '
                       'Open a .roar.floorplan in ROAR to continue editing; ALIGN inputs are supplied for external tools.')
        files['README.txt'] = (f'ROAR floorplan handoff: {current.top}\n\n{placement_note}\n\n{explanation}\n\n'
                               + '\n'.join(manifest['files']) + '\n').encode('utf-8')
        links = '\n'.join(f'<li><a href="{_text(name)}">{_text(name)}</a></li>' for name in manifest['files']
                          if name != 'index.html')
        files['index.html'] = (f'<!doctype html><html lang="en"><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1"><title>ROAR handoff: {_text(current.top)}</title>'
            '<style>body{font:16px sans-serif;max-width:1100px;margin:2em auto;padding:0 1em;line-height:1.5}'
            'img{width:100%;max-height:70vh;border:1px solid #ccd}strong{display:block;padding:1em;background:#fff3cf}'
            'table{width:100%;border-collapse:collapse;font-size:14px}th,td{border:1px solid #ccd;padding:.5em;text-align:left;'
            'vertical-align:top;overflow-wrap:anywhere}th{background:#eef2f7}</style>'
            f'<body><h1>ROAR floorplan handoff: {_text(current.top)}</h1><strong>{_text(placement_note)}</strong>'
            f'<p>{_text(explanation)}</p><h2>Current sketch — not to scale</h2>'
            '<img src="current/sketch.svg" alt="Current unscaled sketch">'
            f'{_intent_summary(current)}{physical}<h2>Bundle files</h2><ul>{links}</ul></body></html>\n').encode('utf-8')

        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.' + target.name + '.', suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                    for name, content in sorted(files.items()):
                        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                        info.compress_type = zipfile.ZIP_DEFLATED
                        info.external_attr = 0o100644 << 16
                        archive.writestr(info, content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return target
