#!/usr/bin/env python3
"""Repeatable checks of the already installed, locked Pixi environments.

All logs, builds and settings live in a new temporary artifact directory.
No downloads, managed environment mutations or user preferences are needed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import venv


ROOT = Path(__file__).resolve().parent.parent


def run(command, directory, environment, log):
    print('+ ' + ' '.join(map(str, command)), flush=True)
    with log.open('w') as stream:
        result = subprocess.run(list(map(str, command)), cwd=directory, env=environment,
                                stdout=stream, stderr=subprocess.STDOUT)
    import re
    output = re.sub(r'/roar-align/[0-9a-f]{64}/', '/roar-align/<redacted>/', log.read_text(errors='replace'))
    log.write_text(output)
    print(output, end='', flush=True)
    if result.returncode:
        raise RuntimeError(f'Command failed ({result.returncode}); see {log}')


PACKAGE_SMOKE = r'''
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import roar_api
from roar_api import Design
from roar_api.floorplan import FloorplanProject
from roar_api.floorplan.align_runner import PROFILE

prefix = Path(sys.prefix).resolve()
assert Path(roar_api.__file__).resolve().is_relative_to(prefix), roar_api.__file__
assert importlib.util.find_spec('PyQt6') is None, 'Package check must use the GUI-free headless environment'
assert importlib.util.find_spec('align') is None, 'ALIGN leaked into the core package check'
assert not any(name.startswith(('PyQt6', 'align.')) for name in sys.modules)
assert importlib.metadata.version('roar-cad') == roar_api.__version__
design = Design('wheel smoke')
design.save('smoke.roar')
loaded = Design.load('smoke.roar').to_state()
loaded.pop('saved_at')
assert loaded == design.to_state()
project = FloorplanProject.from_text('.subckt pair d g s b\nM1 d g s b nmos W=1u L=.15u NF=1\n.ends pair\n')
assert len(project.devices) == 1
assert PROFILE['align_version'] == '0.9.8'
assert (Path(roar_api.__file__).parent / 'floorplan' / 'ALIGN_SETUP.md').is_file()
result = subprocess.run([sys.executable, '-I', '-B', '-m', 'roar_api'],
                        capture_output=True, text=True, timeout=40)
assert result.returncode == 1, result.stdout + result.stderr
assert "No module named 'PyQt6'" in result.stderr, result.stderr
assert "pip install --upgrade 'roar-cad[gui]'" in result.stderr, result.stderr
assert 'checkout root' not in result.stderr, result.stderr
assert 'Traceback' not in result.stderr, result.stderr
assert 'sudo apt install' not in result.stderr, result.stderr
assert not result.stdout, result.stdout
print(json.dumps({'installed_module': roar_api.__file__, 'python': sys.version,
                  'core_api': 'PASS', 'missing_gui_launcher': 'PASS'}))
'''


def check_package(work, environment):
    # Fail if core requirements have been added without updating the GUI-free
    # test environment. Actual versions remain governed by pixi.lock.
    import re
    import tomllib
    manifest = tomllib.loads((ROOT / 'pyproject.toml').read_text())
    required = {re.split(r'[<>=!~;\[ ]', item, maxsplit=1)[0].lower().replace('_', '-')
                for item in manifest['project']['dependencies']}
    supplied = set(manifest['tool']['pixi']['feature']['headless']['pypi-dependencies'])
    if required != supplied:
        raise ValueError('Keep the headless check dependencies equal to project.dependencies')

    # Build a copy so setuptools cannot leave build/ or egg-info in the checkout.
    source = work / 'source'
    source.mkdir()
    for name in ('pyproject.toml', 'README.md'):
        shutil.copy2(ROOT / name, source / name)
    for name in ('roar_api', 'src'):
        shutil.copytree(ROOT / name, source / name,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.egg-info'))
    wheels = work / 'wheels'
    run([sys.executable, '-I', '-m', 'pip', 'wheel', '--no-index', '--no-deps',
         '--no-build-isolation', '--wheel-dir', wheels, source], work, environment, work / 'build.log')
    wheel, = wheels.glob('*.whl')
    prefix = work / 'installed'
    # Only the locked headless environment supplies dependencies; this venv
    # owns the wheel installation and is discarded independently of Pixi.
    venv.EnvBuilder(with_pip=True, system_site_packages=True).create(prefix)
    python = prefix / 'bin/python'
    run([python, '-I', '-m', 'pip', 'install', '--no-index', '--no-deps', wheel],
        work, environment, work / 'install.log')
    run([python, '-I', '-m', 'pip', 'check'], work, environment, work / 'dependencies.log')
    run([python, '-I', '-B', '-c', PACKAGE_SMOKE], work, environment, work / 'smoke.log')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('check', choices=('floorplan', 'gui', 'align', 'align-placement', 'align-viewer', 'package'))
    parser.add_argument('--output', type=Path, help='Parent directory for retained check artifacts')
    options = parser.parse_args()
    parent = options.output or os.environ.get('ROAR_CHECK_OUTPUT_DIR')
    if parent:
        Path(parent).mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f'roar-check-{options.check}-', dir=parent))
    print(f'Check artifacts: {work}', flush=True)
    environment = dict(os.environ)
    for name in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'ROAR_ALIGN_PYTHON',
                 'ROAR_ALIGN_PDK_FIXED', 'ROAR_MANAGED_RUNTIME'):
        environment.pop(name, None)
    environment.update(PYTHONNOUSERSITE='1', XDG_CONFIG_HOME=str(work / 'config'),
                       XDG_CACHE_HOME=str(work / 'cache'), XDG_DATA_HOME=str(work / 'data'),
                       PIP_CONFIG_FILE=os.devnull, PIP_DISABLE_PIP_VERSION_CHECK='1',
                       PIP_NO_INDEX='1', PIP_NO_CACHE_DIR='1')
    receipt = {'check': options.check, 'python': sys.version, 'executable': sys.executable,
               'checkout': str(ROOT), 'artifacts': str(work), 'status': 'FAIL'}
    started = time.monotonic()
    try:
        expected = (3, 11)
        if sys.version_info[:2] != expected:
            raise ValueError(f'{options.check} requires managed Python {expected[0]}.{expected[1]}')
        if options.check == 'package':
            check_package(work, environment)
        elif options.check == 'align-viewer':
            run([sys.executable, '-I', '-B', ROOT / 'bin/check_align_viewer.py', '--output', work / 'viewer'],
                work, environment, work / 'check.log')
        elif options.check in ('align', 'align-placement'):
            profile = json.loads((ROOT / 'src/floorplan/align_profile.json').read_text())
            backend = ROOT / '.pixi/align-backend' / profile['backend_revision'] / 'SKY130_PDK'
            run([sys.executable, '-I', ROOT / 'bin/provision_align.py', '--check', '--backend-dir', backend],
                work, environment, work / 'backend.log')
            command = [sys.executable, '-I', ROOT / 'src/floorplan/tests/check_align.py',
                       '--repo', ROOT, '--pdk', backend, '--output', work / 'native']
            if options.check == 'align-placement':
                command.append('--placement')
            run(command, work, environment, work / 'check.log')
        elif options.check == 'floorplan':
            environment['QT_QPA_PLATFORM'] = 'offscreen'
            run([sys.executable, '-I', '-B', '-m', 'unittest', 'discover',
                 '-s', ROOT / 'src/floorplan/tests', '-v'], ROOT, environment, work / 'check.log')
        else:
            # Qt accepts semicolon-separated fallback plugins, each with
            # optional colon-separated arguments. None may accept a desktop
            # check without actually rendering on the user's display.
            backends = {option.split(':', 1)[0].strip().lower() for option in
                        environment.get('QT_QPA_PLATFORM', '').split(';')}
            if backends & {'offscreen', 'minimal', 'minimalegl', 'vnc'}:
                raise ValueError('check-gui requires a visible desktop. Remove the headless QT_QPA_PLATFORM override.')
            environment.update(ROAR_MANAGED_RUNTIME='1', ROAR_HOME=str(ROOT))
            run([sys.executable, '-I', '-B', '-m', 'roar_api', '--check-gui'],
                work, environment, work / 'check.log')
        receipt['status'] = 'PASS'
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        receipt['error'] = str(error)
        print(f'FAIL: {error}', file=sys.stderr)
        return 1
    finally:
        receipt['seconds'] = round(time.monotonic() - started, 2)
        (work / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
        print(f'{receipt["status"]}: {options.check}; receipt: {work / "receipt.json"}', flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
