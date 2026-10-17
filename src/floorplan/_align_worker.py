"""Executed as a file by the separate ALIGN Python; never imports ROAR/Qt."""
import argparse
import json
import os
from pathlib import Path
import signal
import sys
import traceback
import uuid


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

        def native_gui(*, tagged_bboxes, module_name, lambda_coeff, port):
            # Upstream startup_gui prepares the real native metrics/geometries.
            # Only its HTTP hosting is adapted: local, no debugger/reloader.
            application = AppWithCallbacksAndState(tagged_bboxes=tagged_bboxes,
                module_name=module_name, lambda_coeff=lambda_coeff)
            server = make_server('127.0.0.1', port, application.app.server, threaded=True)
            try:
                status('serving', url=f'http://127.0.0.1:{server.server_port}/')
                server.serve_forever()
            finally:
                server.server_close()
            return application.sel

        placer.run_gui = native_gui
        options = dict(profile['run_defaults'])
        options.update(netlist_dir=manifest['input_dir'], pdk_dir=active['backend_path'],
            subckt=manifest['top'], working_dir=str(path.parent), gui=True,
            flow_stop='3_pnr:gui', port=active['port'])
        if active['mode'] == 'reopen':
            options['flow_start'] = '3_pnr:gui'
        align.schematic2layout(**options)
        status('completed')
    except Exception as error:
        traceback.print_exc()
        status('failed', error=f'{type(error).__name__}: {error}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
