#!/usr/bin/env python3
"""Configure the bundled tools at setup; verify readiness and launch offline.

Pixi owns package installation. This helper owns only ROAR's wiring between
the GUI, the isolated ALIGN interpreter, and the pinned backend assets.
It never writes desktop preferences or installs anything during launch.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


INPUTS = ('pyproject.toml', 'pixi.lock', 'bin/configure_runtime.py',
          'bin/provision_align.py', 'src/floorplan/align_profile.json')


def runtime_paths(root):
    profile = json.loads((root / 'src/floorplan/align_profile.json').read_text())
    return (root / '.pixi/envs/default/bin/python',
            root / '.pixi/envs/align/bin/python',
            root / '.pixi/align-backend' / profile['backend_revision'] / 'SKY130_PDK')


def runtime_environment(root):
    """Paths are derived from this checkout, never accepted from a receipt."""
    _, align_python, backend = runtime_paths(root)
    environment = dict(os.environ)
    for key in ('PYTHONPATH', 'PYTHONHOME', 'ALIGN_HOME', 'VIRTUAL_ENV', 'LD_PRELOAD'):
        environment.pop(key, None)
    environment.update(ROAR_HOME=str(root), ROAR_MANAGED_RUNTIME='1',
                       ROAR_ALIGN_PYTHON=str(align_python), ROAR_ALIGN_PDK_FIXED=str(backend),
                       PYTHONNOUSERSITE='1')
    return environment


def fingerprint(root):
    return {'schema': 1, 'checkout': str(root), 'inputs': {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in INPUTS}}


def check_ready(root):
    """A setup receipt is a freshness check, not a security signature."""
    try:
        receipt = json.loads((root / '.pixi/roar-runtime.json').read_text())
        if receipt != fingerprint(root):
            raise ValueError('The managed runtime needs setup after a checkout or dependency change.')
        gui_python, align_python, backend = runtime_paths(root)
        for python in (gui_python, align_python):
            if not python.is_file() or not os.access(python, os.X_OK):
                raise ValueError(f'The managed interpreter is missing: {python}')
        profile = json.loads((root / 'src/floorplan/align_profile.json').read_text())
        for name, expected in profile['backend_hashes'].items():
            if hashlib.sha256((backend / name).read_bytes()).hexdigest() != expected:
                raise ValueError(f'The managed ALIGN backend needs repair: {name}')
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise ValueError('Managed setup is incomplete or out of date. '
                         'Run ./bin/setup_roar.sh from this checkout.\n' + str(error)) from error


def configure(root):
    """Only the explicit setup command may download assets or write readiness."""
    receipt = root / '.pixi/roar-runtime.json'
    gui_python, align_python, backend = runtime_paths(root)
    if Path(sys.executable).absolute() != gui_python:
        raise ValueError('Run ./bin/setup_roar.sh so configuration uses the managed Python 3.11 environment.')
    if sys.version_info[:2] != (3, 11):
        raise ValueError('The managed GUI requires Python 3.11. Restore pixi.lock and rerun setup.')
    receipt.unlink(missing_ok=True)
    os.environ.update(runtime_environment(root))
    sys.path.insert(0, str(root))
    from roar_api.floorplan.align_runner import align_process_environment
    from roar_api.floorplan.doctor import check_setup
    from roar_api.floorplan.probe_result import parse_probe_result

    environment = align_process_environment(str(align_python))
    with tempfile.TemporaryDirectory(prefix='roar-managed-setup-') as directory:
        # ALIGN may create LOG/ on import; keep all probes away from the checkout.
        provision = subprocess.run(
            [str(align_python), '-I', str(root / 'bin/provision_align.py'),
             '--backend-dir', str(backend)], cwd=directory, env=environment,
            capture_output=True, text=True, errors='replace', timeout=300)
        if provision.returncode:
            raise ValueError('ALIGN assets could not be configured:\n' + (provision.stderr or provision.stdout)[-2500:])
        probe = subprocess.run(
            [str(align_python), '-I', '-B', str(root / 'src/floorplan/_align_probe.py'),
             '--backend', str(backend)], cwd=directory, env=environment,
            capture_output=True, text=True, errors='replace', timeout=60)
        result = parse_probe_result(probe.stdout, stderr=probe.stderr, returncode=probe.returncode)
        if not result['ok'] or not result.get('browser_viewer') or not result.get('align'):
            raise ValueError('Managed ALIGN check failed:\n' +
                             str(result.get('message') or probe.stderr or probe.stdout)[-2500:])
        if str(result.get('python', '')).split('.')[:2] != ['3', '11']:
            raise ValueError('The managed ALIGN runtime requires Python 3.11. Restore pixi.lock and rerun setup.')
        print(f'PASS: Managed ALIGN {result["align"]}, Python {result["python"]}; backend and viewer assets checked.')
    if check_setup(gui_only=True):
        raise ValueError('Desktop startup check failed. Follow the diagnostic above and rerun setup.')

    # A failed or interrupted setup must never publish a successful receipt.
    descriptor, temporary = tempfile.mkstemp(prefix='.roar-runtime-', dir=receipt.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(fingerprint(root), stream, indent=2)
            stream.write('\n')
        os.replace(temporary, receipt)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--launch', action='store_true')
    parser.add_argument('--cwd', type=Path)
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    options = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    try:
        if options.launch:
            check_ready(root)
            if options.cwd is None:
                raise ValueError('Launch through ./bin/roar to preserve relative design paths.')
            os.chdir(options.cwd)
            arguments = options.arguments
            if arguments[:1] == ['--']:
                arguments = arguments[1:]
            python = str(runtime_paths(root)[0])
            os.execve(python, [python, '-I', str(root / 'src/roar.py'), *arguments], runtime_environment(root))
        else:
            if options.arguments or options.cwd:
                parser.error('Setup accepts no application arguments.')
            configure(root)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
