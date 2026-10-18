"""Executed as a file by the separate ALIGN Python; never imports ROAR/Qt."""
import argparse
import errno
from http.cookies import SimpleCookie, CookieError
import json
import os
from pathlib import Path
import signal
import secrets
import sys
import traceback
import uuid
from urllib.parse import parse_qs


class ProtectedViewer:
    """Authenticate every native Dash route, including assets and callbacks."""
    def __init__(self, application, token):
        self.application, self.token = application, token
        self.port = None
        self.cookie_name = 'roar_align_' + token[:16]

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
        query_token = parse_qs(environ.get('QUERY_STRING', '')).get('token', [''])[0]
        if (environ.get('REQUEST_METHOD') == 'GET' and environ.get('PATH_INFO') == '/'
                and secrets.compare_digest(query_token.encode(), self.token.encode())):
            # Strip the secret from subsequent resource URLs and referrers.
            cookie = f'{self.cookie_name}={self.token}; Path=/; HttpOnly; SameSite=Strict'
            start_response('303 See Other', headers + [('Location', '/'), ('Set-Cookie', cookie)])
            return [b'Opening the authenticated placement viewer.']
        cookie = SimpleCookie()
        try:
            cookie.load(environ.get('HTTP_COOKIE', ''))
        except CookieError:
            return reject('403 Forbidden', 'Open the viewer from ROAR to authenticate.')
        value = cookie[self.cookie_name].value if self.cookie_name in cookie else ''
        bearer = environ.get('HTTP_AUTHORIZATION', '')
        if not (secrets.compare_digest(value.encode(), self.token.encode())
                or secrets.compare_digest(bearer.encode(), ('Bearer ' + self.token).encode())):
            return reject('403 Forbidden', 'Open the viewer from ROAR to authenticate.')
        def secured_start(status, response_headers, exc_info=None):
            response_headers = [(k, v) for k, v in response_headers
                                if k.lower() not in {'cache-control', 'referrer-policy', 'x-frame-options'}]
            return start_response(status, response_headers + headers[1:], exc_info)
        return self.application(environ, secured_start)


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
    if active['token'] != arguments.token:
        raise ValueError('Worker activation no longer owns this run')

    def status(value, url=None, error=None):
        target = path.with_name('status.json')
        temporary = target.with_name('status.' + uuid.uuid4().hex + '.tmp')
        temporary.write_text(json.dumps({'token': arguments.token, 'status': value, 'url': url, 'error': error}))
        os.replace(temporary, target)
        print(f'ROAR ALIGN {value}' + (f': {url}' if url else '') + (f': {error}' if error else ''), flush=True)

    def stopped(signum, frame):
        status('stopped')
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stopped)
    signal.signal(signal.SIGINT, stopped)
    try:
        status('generating' if active['mode'] == 'generate' else 'starting')
        import align
        import dash
        profile = manifest['generation_config']['profile']
        if align.__version__ != profile['align_version'] or int(dash.__version__.split('.')[0]) != profile['dash_major']:
            raise ValueError(f"This profile requires ALIGN {profile['align_version']} and Dash {profile['dash_major']}.x")
        from align.pnr import placer
        from align.gui.mockup import AppWithCallbacksAndState
        from werkzeug.serving import make_server
        validate_viewer_assets()

        def native_gui(*, tagged_bboxes, module_name, lambda_coeff, port):
            # Upstream startup_gui prepares the real native metrics/geometries.
            # Only its HTTP hosting is adapted: local, no debugger/reloader.
            application = AppWithCallbacksAndState(tagged_bboxes=tagged_bboxes,
                module_name=module_name, lambda_coeff=lambda_coeff)
            # Process ownership tokens are visible in /proc command lines.
            # Viewer credentials are independent and only stored in the private
            # run directory, never passed in process arguments.
            viewer_secret = secrets.token_hex(32)
            protected = ProtectedViewer(application.app.server, viewer_secret)
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
                status('serving', url=f'http://127.0.0.1:{server.server_port}/?token={viewer_secret}')
                server.serve_forever()
            finally:
                server.server_close()
            return application.sel

        placer.run_gui = native_gui
        options = dict(profile['run_defaults'])
        options.update(manifest['generation_config'].get('run_options', {}))
        options.update(netlist_dir=manifest['input_dir'], pdk_dir=active['backend_path'],
            subckt=manifest['top'], working_dir=str(path.parent), gui=True,
            flow_stop='3_pnr:gui', port=active['port'])
        if active['mode'] == 'reopen':
            options['flow_start'] = '3_pnr:gui'
        elif 'netlist' in manifest:
            native_preflight(manifest)
        align.schematic2layout(**options)
        status('completed')
    except Exception as error:
        traceback.print_exc()
        status('failed', error=f'{type(error).__name__}: {error}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
