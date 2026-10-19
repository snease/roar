#!/usr/bin/env python3
"""Generate an OTA physical preview, then explicitly open its real Dash viewer.

No browser, Qt, mocks or user settings are used. The worker is always stopped;
retained diagnostics redact the expired viewer capability.
"""
import argparse
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from roar_api.floorplan import FloorplanProject
from roar_api.floorplan.align_runner import AlignConfig, AlignRunner, PROFILE, PROFILE_ID
from roar_api.floorplan.geometry import load_preview


def wait_for(runner, project, expected):
    deadline = time.monotonic() + 180
    while True:
        state = runner.poll(project)
        if state.status == expected:
            return state
        if state.status in ('failed', 'stopped', 'interrupted') or time.monotonic() > deadline:
            raise RuntimeError(f'Native ALIGN did not reach {expected}: {state.status}: {state.error}\n{runner.log_tail()}')
        time.sleep(0.1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    options = parser.parse_args()
    work = options.output.resolve()
    work.mkdir(parents=True)
    root = Path(__file__).resolve().parent.parent
    python = root / '.pixi/envs/align/bin/python'
    backend = root / '.pixi/align-backend' / PROFILE['backend_revision'] / 'SKY130_PDK'
    os.environ.update(ROAR_MANAGED_RUNTIME='1', ROAR_ALIGN_PYTHON=str(python),
                      ROAR_ALIGN_PDK_FIXED=str(backend))
    project = FloorplanProject.load(root / 'examples/floorplan/ota/cm_ota.roar.floorplan')
    project.align_profile = PROFILE_ID
    runner = AlignRunner(AlignConfig(str(python), str(backend), str(work / 'runs')),
                         work / 'smoke.floorplan')
    report = {'status': 'FAIL', 'checks': []}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        runner.start(project)
        preview = wait_for(runner, project, 'completed')
        assert preview.url is None and preview.has_preview and preview.has_placement, preview
        data = load_preview(Path(preview.run_dir) / 'preview.json')
        assert data['candidates'], 'Native preview has no placement candidates'
        report['checks'].append('Native physical preview without a browser or viewer server')
        runner.open_last(project)
        state = wait_for(runner, project, 'serving')
        assert state.url and state.run_dir == preview.run_dir, state
        report['checks'].append('Explicit viewer open reuses the captured placement')
        base = state.url
        parsed = urllib.parse.urlsplit(base)
        origin = f'{parsed.scheme}://{parsed.netloc}'

        def request(path, *, data=None, headers=None, status=200):
            url = path if path.startswith('http://') else base + path
            payload = json.dumps(data).encode() if data is not None else None
            request_headers = {'Content-Type': 'application/json'} if payload else {}
            request_headers.update(headers or {})
            request = urllib.request.Request(url, data=payload, headers=request_headers)
            try:
                response = opener.open(request, timeout=15)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                body = response.read()
                if response.status != status:
                    raise RuntimeError(f'Viewer HTTP {response.status}, expected {status}: {body[:500]!r}')
                if response.headers.get('Cache-Control') != 'no-store':
                    raise RuntimeError('Viewer response lost its no-store policy')
                return body

        assert b'_dash-config' in request('')
        layout = json.loads(request('_dash-layout'))
        dependencies = json.loads(request('_dash-dependencies'))
        properties = {}

        def visit(value):
            if isinstance(value, dict):
                props = value.get('props', {})
                if isinstance(props.get('id'), str):
                    properties[props['id']] = props
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(layout)
        assert {'Placement', 'tradeoff-graph', 'module-name'} <= properties.keys()
        for asset in ('00_from_codepen.css', '01_style.css', '02_typography.css', 'align.png'):
            body = request('assets/' + asset)
            if asset == 'align.png':
                assert body.startswith(b'\x89PNG\r\n\x1a\n')
        report['checks'].append('Native layout, dependency metadata and pinned assets')

        for component, changed in [('tradeoff-graph', 'colorscale.value'), ('Placement', 'display-type.value')]:
            dependency = next(item for item in dependencies if component + '.figure' in item['output'])
            output = dependency['output']
            output_names = output[2:-2].split('...') if output.startswith('..') else [output]
            outputs = [{'id': name.rsplit('.', 1)[0], 'property': name.rsplit('.', 1)[1]} for name in output_names]
            inputs = [dict(item, value=properties[item['id']].get(item['property'])) for item in dependency['inputs']]
            result = json.loads(request('_dash-update-component', data={
                'output': output, 'outputs': outputs if output.startswith('..') else outputs[0],
                'inputs': inputs, 'state': [], 'changedPropIds': [changed]}, headers={'Origin': origin}))
            figure = result['response'][component]['figure']
            assert figure.get('data') or figure.get('layout', {}).get('shapes'), component
            report['checks'].append(component + ' real Dash callback')

        for endpoint in ('_dash-layout', 'assets/align.png', '_dash-update-component'):
            data = {} if endpoint == '_dash-update-component' else None
            request(origin + '/' + endpoint, data=data, status=403)
            request(endpoint, data=data, headers={'Host': 'attacker.invalid'}, status=403)
            request(endpoint, data=data, headers={'Origin': 'http://attacker.invalid'}, status=403)
            request(endpoint, data=data, headers={'Sec-Fetch-Site': 'cross-site'}, status=403)
        report['checks'].append('Layout, assets and callback capability/Host/Origin/cross-site denials')
        report['status'] = 'PASS'
    finally:
        try:
            state = runner.shutdown()
            if state is not None and state.can_stop:
                raise RuntimeError('Native viewer worker remained active after shutdown')
        except BaseException:
            report['status'] = 'FAIL'
            raise
        finally:
            # Run logs/status contain the URL while serving; artifact consumers
            # receive redacted diagnostics even if worker teardown fails.
            for path in work.rglob('*'):
                if path.is_file() and path.suffix in ('.json', '.log'):
                    original = path.read_text(errors='replace')
                    redacted = re.sub(r'/roar-align/[0-9a-f]{64}/', '/roar-align/<redacted>/', original)
                    if redacted != original:
                        path.write_text(redacted)
            (work / 'receipt.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
