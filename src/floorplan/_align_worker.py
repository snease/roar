"""Executed as a file by the separate ALIGN Python; never imports ROAR/Qt."""
import argparse
import errno
import json
import os
from pathlib import Path
import runpy
import signal
import secrets
import sys
import traceback
import uuid


class ProtectedViewer:
    """Authenticate every native Dash route, including assets and callbacks."""
    def __init__(self, application, path_prefix):
        self.application, self.path_prefix = application, path_prefix
        self.port = None

    def __call__(self, environ, start_response):
        host = f'127.0.0.1:{self.port}'
        headers = [('Content-Type', 'text/plain; charset=utf-8'), ('Cache-Control', 'no-store'),
                   ('Referrer-Policy', 'no-referrer'), ('X-Frame-Options', 'DENY')]
        def reject(status, text):
            start_response(status, headers)
            return [text.encode()]
        if environ.get('HTTP_HOST') != host:
            return reject('403 Forbidden', 'This viewer only accepts its loopback address.')
        if environ.get('HTTP_ORIGIN') not in (None, 'http://' + host):
            return reject('403 Forbidden', 'Cross-origin requests are not permitted.')
        if environ.get('HTTP_SEC_FETCH_SITE') == 'cross-site':
            return reject('403 Forbidden', 'Open the viewer from ROAR.')
        # Cookies are shared by every port on 127.0.0.1. Keep the capability in
        # this viewer's URL instead, including all Dash assets and callbacks.
        prefix = environ.get('PATH_INFO', '')[:len(self.path_prefix)]
        if not secrets.compare_digest(prefix.encode(), self.path_prefix.encode()):
            return reject('403 Forbidden', 'Open the viewer from ROAR to authenticate.')
        def secured_start(status, response_headers, exc_info=None):
            response_headers = [(k, v) for k, v in response_headers
                                if k.lower() not in {'cache-control', 'referrer-policy', 'x-frame-options'}]
            return start_response(status, response_headers + headers[1:], exc_info)
        return self.application(environ, secured_start)


def _viewer_application(factory, path_prefix, **kwargs):
    """Configure native Dash routes before its constructor registers them."""
    names = ('DASH_URL_BASE_PATHNAME', 'DASH_ROUTES_PATHNAME_PREFIX',
             'DASH_REQUESTS_PATHNAME_PREFIX', 'DASH_APP_NAME')
    previous = {key: os.environ.get(key) for name in names for key in (name, name.lower())}
    try:
        for key in previous:
            os.environ.pop(key, None)
        os.environ['DASH_URL_BASE_PATHNAME'] = path_prefix
        return factory(**kwargs)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def validate_viewer_assets():
    from align.gui.mockup import __file__ as viewer_module
    assets = Path(viewer_module).with_name('assets')
    missing = [name for name in ('00_from_codepen.css', '01_style.css', '02_typography.css', 'align.png')
               if not (assets / name).is_file()]
    if missing:
        raise ValueError('ALIGN viewer assets are missing: ' + ', '.join(missing) +
                         '. Complete the viewer-assets step in ALIGN settings → Setup help, then test installation again.')


def native_preflight(manifest):
    """Use the installed native schema/solver before any primitive generation."""
    from align.schema.parser import SpiceParser
    from align.compiler.user_const import ConstraintParser
    parser = SpiceParser()
    parser.parse((Path(manifest['active']['backend_path']) / 'models.sp').read_text())
    parser.parse(Path(manifest['netlist']).read_text())
    top = parser.library.find(manifest['top'].upper())
    try:
        ConstraintParser(Path(manifest['active']['backend_path']), Path(manifest['input_dir'])).annotate_user_constraints(top)
        top.verify()
    except Exception as error:
        constraints = json.loads(Path(manifest['constraints']).read_text())
        indices = manifest.get('constraint_rows', list(range(1, len(constraints) + 1)))
        rows = ', '.join(f'{index}: {row.get("constraint", "unknown")}' for index, row in zip(indices, constraints))
        raise ValueError(f'ALIGN constraint preflight failed ({rows}): {error}') from error


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--token', required=True)
    arguments = parser.parse_args()
    path = Path(arguments.manifest).resolve()
    manifest = json.loads(path.read_text())
    active = manifest['active']
    if active['mode'] not in ('preview', 'reopen'):
        raise ValueError('Worker operation must generate a preview or reopen a captured placement')
    if active['token'] != arguments.token:
        raise ValueError('Worker activation no longer owns this run')

    preserve_invalid = runpy.run_path(str(Path(__file__).with_name('run_records.py')))['preserve_invalid_record']

    def status(value, url=None, error=None):
        target = path.with_name('status.json')
        temporary = target.with_name('status.' + uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_text(json.dumps({'token': arguments.token, 'status': value, 'url': url, 'error': error}))
            preserve_invalid(target)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        print(f'ROAR ALIGN {value}' + (f': {url}' if url else '') + (f': {error}' if error else ''), flush=True)

    def stopped(signum, frame):
        status('stopped')
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stopped)
    signal.signal(signal.SIGINT, stopped)
    try:
        status('generating' if active['mode'] == 'preview' else 'starting')
        import align
        import dash
        profile = manifest['generation_config']['profile']
        # Captured geometry keeps its ALIGN version. The HTTP viewer uses the
        # currently supported Dash runtime, including when reopening a capture
        # made before the viewer dependency was upgraded.
        runtime_profile = json.loads(Path(__file__).with_name('align_profile.json').read_text())
        if align.__version__ != profile['align_version'] or int(dash.__version__.split('.')[0]) != runtime_profile['dash_major']:
            raise ValueError(f"This profile requires ALIGN {profile['align_version']} and Dash {runtime_profile['dash_major']}.x")
        options = dict(profile['run_defaults'])
        options.update(manifest['generation_config'].get('run_options', {}))
        options.update(netlist_dir=manifest['input_dir'], pdk_dir=active['backend_path'],
            subckt=manifest['top'], working_dir=str(path.parent))
        if active['mode'] == 'preview':
            # ALIGN itself imports Dash, but generation needs neither viewer
            # assets nor a listening server. Normalize data before publishing.
            native_preflight(manifest)
            align.schematic2layout(**dict(options, gui=False, flow_stop='3_pnr:place'))
            adapter = runpy.run_path(str(Path(__file__).with_name('geometry.py')))
            preview = adapter['build_preview'](path.parent, manifest)
            target = path.with_name('preview.json')
            temporary = target.with_name('preview.' + uuid.uuid4().hex + '.tmp')
            try:
                temporary.write_text(json.dumps(preview, allow_nan=False), encoding='utf-8')
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            status('completed')
            return 0
        from align.pnr import placer
        from align.gui.mockup import AppWithCallbacksAndState
        from werkzeug.serving import make_server
        validate_viewer_assets()

        def native_gui(*, tagged_bboxes, module_name, lambda_coeff, port):
            # Upstream startup_gui prepares the real native metrics/geometries.
            # Only its HTTP hosting is adapted: local, no debugger/reloader.
            # Process ownership tokens are visible in /proc command lines.
            # Viewer credentials are independent and only stored in the private
            # run directory, never passed in process arguments.
            path_prefix = f'/roar-align/{secrets.token_hex(32)}/'
            application = _viewer_application(AppWithCallbacksAndState, path_prefix,
                tagged_bboxes=tagged_bboxes, module_name=module_name, lambda_coeff=lambda_coeff)
            protected = ProtectedViewer(application.app.server, path_prefix)
            try:
                server = make_server('127.0.0.1', port, protected, threaded=True)
            except OSError as error:
                if error.errno in (errno.EPERM, errno.EACCES):
                    raise RuntimeError('Placement was generated, but this environment blocks the local browser viewer. '
                        'Run ROAR where loopback networking is permitted, then reopen the saved placement.') from error
                raise RuntimeError(f'Placement was generated, but the browser viewer could not listen on '
                    f'127.0.0.1:{port}: {error}. Select an available port or automatic port (0).') from error
            except SystemExit as error:
                if error.code in (None, 0):
                    raise  # Preserve cancellation from our signal handler.
                # Werkzeug exits on a bind failure such as an occupied port.
                raise RuntimeError(f'Placement was generated, but the browser viewer could not bind '
                    f'127.0.0.1:{port}. Select an available port or automatic port (0); see align.log.') from error
            try:
                protected.port = server.server_port
                print('ROAR placement viewer: stopping closes this viewer; this flow does not run routing or produce final GDS.', flush=True)
                status('serving', url=f'http://127.0.0.1:{server.server_port}{path_prefix}')
                server.serve_forever()
            finally:
                server.server_close()
            return application.sel

        placer.run_gui = native_gui
        options.update(gui=True, flow_start='3_pnr:gui', flow_stop='3_pnr:gui', port=active['port'])
        align.schematic2layout(**options)
        status('completed')
    except Exception as error:
        traceback.print_exc()
        status('failed', error=f'{type(error).__name__}: {error}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
