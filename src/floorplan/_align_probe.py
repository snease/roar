"""Read-only installation / constraint checks in the isolated ALIGN Python."""
import argparse
import hashlib
import json
from pathlib import Path
import runpy
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend', required=True)
    parser.add_argument('--manifest')
    args = parser.parse_args()
    result = {'ok': False, 'python': sys.version.split()[0]}
    try:
        import align
        result['align'] = align.__version__
        import dash
        result['dash'] = dash.__version__
        from align.pnr import placer
        result.update(align=align.__version__, dash=dash.__version__)
        profile = json.loads(Path(__file__).with_name('align_profile.json').read_text())
        if align.__version__ != profile['align_version'] or int(dash.__version__.split('.')[0]) != profile['dash_major']:
            raise ValueError(f"Requires ALIGN {profile['align_version']} and Dash {profile['dash_major']}.x")
        backend = Path(args.backend).expanduser()
        for name, expected in profile['backend_hashes'].items():
            path = backend / name
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError(f'Backend does not match {profile["backend_repository"]}@{profile["backend_revision"]}: {name}')
        from align.compiler.util import get_generator
        if get_generator('MOSGenerator', backend) is None:
            raise ValueError('The selected backend cannot load its MOS generator')
        worker = runpy.run_path(str(Path(__file__).with_name('_align_worker.py')))
        if args.manifest:
            manifest = json.loads(Path(args.manifest).read_text())
            worker['native_preflight'](manifest)
            result['message'] = 'Native ALIGN schema and combined constraint checks passed. Placement may still be infeasible for the generated geometry.'
        else:
            result['message'] = 'Installation is ready for physical preview generation.'
            try:
                from align.gui.mockup import AppWithCallbacksAndState
                from werkzeug.serving import make_server
                worker['validate_viewer_assets']()
                result['browser_viewer'] = True
            except Exception as error:
                result['browser_viewer'] = False
                result['message'] += '\nOptional ALIGN browser viewer is unavailable: ' + str(error)
        result['ok'] = True
    except Exception as error:
        result['message'] = f'{type(error).__name__}: {error}'
        if isinstance(error, ModuleNotFoundError):
            result['message'] += '\nSelect the Python from the ALIGN environment and install its dependencies there; see Setup help in ALIGN settings.'
        elif isinstance(error, ImportError):
            result['message'] += '\nThe ALIGN environment could not load a required component. Check its native libraries and Python compatibility; see Setup help in ALIGN settings.'
        elif not args.manifest:
            result['message'] += '\nCheck the pinned versions and SKY130_PDK folder in Setup help, then test again.'
    print('ROAR_PROBE ' + json.dumps(result), flush=True)
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
