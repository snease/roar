"""Headless lifecycle tests use a small child process, never import ALIGN/Qt."""
import dataclasses
import hashlib
import errno
import contextlib
import io
from types import SimpleNamespace
import json
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from roar_api.floorplan import FloorplanProject
from roar_api.floorplan.align_runner import AlignConfig, AlignRunner, PROFILE, PROFILE_ID, project_fingerprint, validate_ota_project


def _race_start(config, snapshot, path, barrier, results):
    runner = AlignRunner(config, path)
    launch = runner._launch
    def slow_launch(*args):
        time.sleep(0.15)  # Make the unpublished-PID window deterministic.
        return launch(*args)
    runner._launch = slow_launch
    barrier.wait()
    try:
        result = runner.start(FloorplanProject.from_dict(snapshot))
        results.put(('started', result.run_dir))
    except ValueError as error:
        results.put(('rejected', str(error)))


FAKE_PYTHON = '''#!PYTHON
import json, os, pathlib, signal, sys, time
p=pathlib.Path(sys.argv[sys.argv.index('--manifest')+1])
m=json.loads(p.read_text()); token=m['active']['token']
mode=(pathlib.Path(__file__).parent/'mode').read_text()
def write(status, error=None):
    t=p.parent/'fake.tmp'
    t.write_text(json.dumps({'token':token,'status':status,'error':error,'url':'http://127.0.0.1:12345/'}))
    os.replace(t,p.parent/'status.json')
def stop(*args):
    write('stopped'); sys.exit(0)
signal.signal(signal.SIGTERM, signal.SIG_IGN if mode=='ignore' else stop)
print('captured child log: '+mode,flush=True)
write('generating')
if mode=='fail' and m['active']['mode']=='generate':
    write('failed','intentional backend failure'); sys.exit(2)
if mode not in ('pending','ignore'):
    dump=p.parent/'3_pnr'/'__placer_dump__.json'
    if m['active']['mode']=='generate':
        dump.parent.mkdir(); dump.write_text(json.dumps([m['top'].upper(),{},[['PLACEMENT_0',{}]],{}]))
    write('serving')
while True: time.sleep(.02)
'''


@unittest.skipUnless(sys.platform == 'linux', 'ALIGN process management requires Linux or WSL')
class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.python = self.base / 'python'
        self.python.write_text(FAKE_PYTHON.replace('PYTHON', sys.executable, 1))
        self.python.chmod(0o755)
        (self.base / 'mode').write_text('good')
        self.config = AlignConfig(str(self.python), str(self.base / 'backend'), str(self.base / 'runs'))
        fixture = Path(__file__).resolve().parents[3] / 'examples/floorplan/ota/cm_ota.roar.floorplan'
        self.project = FloorplanProject.load(fixture)
        self.project.align_profile = PROFILE_ID
        self.runner = AlignRunner(self.config, self.base / 'project.floorplan')
        self.addCleanup(self.runner.shutdown)
        self.validation = patch.object(AlignConfig, 'validate')
        self.validation.start()
        self.addCleanup(self.validation.stop)
        self.readiness = patch('roar_api.floorplan.align_runner._ready', return_value=True)
        self.ready = self.readiness.start()
        self.addCleanup(self.readiness.stop)

    def until(self, runner, status):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            state = runner.poll(self.project)
            if state.status == status:
                return state
            time.sleep(.02)
        self.fail(f'Expected {status}: {state}; {runner.log_tail()}')

    def test_technology_gate_accepts_different_circuit_sizes_and_reports_unsupported_devices(self):
        self.project.align_profile = None
        with self.assertRaisesRegex(ValueError, 'Select layout technology SKY130'):
            self.runner.start(self.project)
        self.project.align_profile = PROFILE_ID
        self.project.devices.pop()
        validate_ota_project(self.project)
        first = self.project.devices[0]
        first.kind = 'resistor'
        with self.assertRaisesRegex(ValueError, first.name + ': .*no supported resistor generator'):
            self.runner.start(self.project)
        first.kind = 'mos'
        first.model = 'unsupported_model'
        with self.assertRaisesRegex(ValueError, first.name + ': unsupported device model'):
            validate_ota_project(self.project)
        self.assertFalse(Path(self.config.run_root).exists())

    def test_generator_geometry_preflight_preserves_repairable_drafts(self):
        for key, value, message in [('width_um', 0.41, '0.42'), ('width_um', 2.53, '2.52'),
                                    ('width_um', 1.015, 'multiple of 0.01'),
                                    ('length_um', 0.145, 'at least 0.15'),
                                    ('length_um', 0.152, 'multiple of 0.005'), ('nf', 1, 'even finger count')]:
            project = FloorplanProject.from_dict(self.project.to_dict())
            setattr(project.devices[0], key, value)
            with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, message):
                validate_ota_project(project)
            self.assertEqual(getattr(project.devices[0], key), value)

    def test_boundary_halos_follow_backend_grid_and_disabled_constraints_are_ignored(self):
        for horizontal, vertical in [(0, 0), (0.43, 0.42), (0.86, 0.84), (1.72, 2.52)]:
            self.project.set_constraints([{'constraint': 'Boundary', 'halo_horizontal': horizontal, 'halo_vertical': vertical}])
            validate_ota_project(self.project)
        self.project.set_constraints([{'constraint': 'Boundary', 'halo_horizontal': 1.5, 'halo_vertical': 2}])
        with self.assertRaisesRegex(ValueError, 'Constraint 1.*horizontal.*0.43 µm'):
            validate_ota_project(self.project)
        self.project.set_constraint_enabled(0, False)
        validate_ota_project(self.project)

    def test_concurrent_processes_publish_only_one_worker(self):
        context = multiprocessing.get_context('fork')
        barrier, results = context.Barrier(2), context.Queue()
        workers = [context.Process(target=_race_start, args=(self.config, self.project.to_dict(),
                   self.runner.project_path, barrier, results)) for _ in range(2)]
        for worker in workers:
            worker.start()
        try:
            outcomes = [results.get(timeout=6) for _ in workers]
            for worker in workers:
                worker.join(3)
                self.assertEqual(worker.exitcode, 0)
            self.assertEqual(sorted(item[0] for item in outcomes), ['rejected', 'started'])
            self.assertIn('Stop the current ALIGN run', next(item[1] for item in outcomes if item[0] == 'rejected'))
            self.runner.last_run(self.project)
            self.assertEqual(len(list(Path(self.config.run_root).glob('*/run.json'))), 1)
        finally:
            for worker in workers:
                if worker.is_alive():
                    worker.kill()
                    worker.join()
            self.runner.last_run(self.project)

    def test_save_as_preflight_conflict_cannot_write_destination(self):
        self.runner.start(self.project)
        target = self.base / 'occupied.floorplan'
        target.write_text('original destination')
        other = AlignRunner(self.config, target)
        self.addCleanup(other.shutdown)
        other.start(self.project)
        with self.assertRaisesRegex(ValueError, 'already has an active ALIGN run'):
            with self.runner.save_as_transaction(target):
                target.write_text('must not be written')
        self.assertEqual(target.read_text(), 'original destination')
        previous = self.runner.project_path
        with self.assertRaisesRegex(OSError, 'write failed'):
            with self.runner.save_as_transaction(self.base / 'new.floorplan'):
                raise OSError('write failed')
        self.assertEqual(self.runner.project_path, previous)

    def test_unconfigured_draft_save_does_not_require_align_storage(self):
        root = self.base / 'unconfigured-runs'
        runner = AlignRunner(AlignConfig('', '', str(root)))
        self.assertIsNone(runner.last_run(self.project))
        self.assertIsNone(runner.last_placement(self.project))
        target = self.base / 'draft.floorplan'
        with runner.save_as_transaction(target):
            self.project.save(target)
        self.assertTrue(target.is_file())
        self.assertFalse(root.exists())

    def test_run_options_are_captured_and_invalid_settings_rejected(self):
        self.project.run_options = {'nvariants': 7, 'placer_sa_iterations': 500, 'seed': 42, 'lambda_coeff': 2.5}
        state = self.runner.start(self.project)
        manifest = json.loads((Path(state.run_dir) / 'run.json').read_text())
        self.assertEqual(manifest['generation_config']['run_options'], self.project.run_options)
        self.runner.shutdown()
        self.project.run_options['lambda_coeff'] = 0
        with self.assertRaisesRegex(ValueError, 'weight'):
            self.runner.start(self.project)

    def test_save_as_rolls_back_document_and_history_if_ownership_commit_fails(self):
        from roar_api.floorplan import align_runner
        state = self.runner.start(self.project)
        target = self.base / 'target.floorplan'
        target.write_text('previous contents')
        target.chmod(0o600)
        manifest = Path(state.run_dir) / 'run.json'
        original = manifest.read_bytes()
        original_path = self.runner.project_path
        write = align_runner._write_json
        def fail_index(path, value):
            if Path(path) == self.runner._index(str(target.resolve())):
                raise OSError('ownership write failed')
            return write(path, value)
        with patch.object(align_runner, '_write_json', side_effect=fail_index):
            with self.assertRaisesRegex(OSError, 'ownership write failed'):
                with self.runner.save_as_transaction(target):
                    target.write_text('new contents')
        self.assertEqual(target.read_text(), 'previous contents')
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(manifest.read_bytes(), original)
        self.assertEqual(self.runner.project_path, original_path)

    def test_owned_live_process_does_not_fail_when_proc_observation_is_unavailable(self):
        (self.base / 'mode').write_text('pending')
        self.runner.start(self.project)
        self.until(self.runner, 'generating')
        with patch('roar_api.floorplan.align_runner._alive', return_value=False):
            state = self.runner.poll(self.project)
            self.assertEqual(state.status, 'generating')
            self.assertTrue(state.can_stop)
            self.runner.stop()
            self.until(self.runner, 'stopped')

    def test_worker_terminal_error_published_during_exit_is_preserved(self):
        self.runner.start(self.project)
        state = self.until(self.runner, 'serving')
        self.runner.shutdown()
        path = Path(state.run_dir) / 'status.json'
        manifest = json.loads((Path(state.run_dir) / 'run.json').read_text())
        manifest['active']['stop_requested'] = None
        (Path(state.run_dir) / 'run.json').write_text(json.dumps(manifest))
        token = manifest['active']['token']
        path.write_text(json.dumps({'token': token, 'status': 'generating', 'error': None}))
        def exit_after_status_read():
            path.write_text(json.dumps({'token': token, 'status': 'failed', 'error': 'specific native diagnostic'}))
            return 2
        process = SimpleNamespace(pid=manifest['active']['pid'], returncode=2, poll=exit_after_status_read)
        with patch.object(self.runner, '_process', process):
            result = self.runner.poll(self.project)
        self.assertEqual(result.status, 'failed')
        self.assertEqual(result.error, 'specific native diagnostic')
        self.assertEqual(json.loads(path.read_text())['error'], 'specific native diagnostic')

    def test_snapshot_isolated_command_readiness_cache_and_restart(self):
        before = self.project.to_dict()
        state = self.runner.start(self.project)
        state = self.until(self.runner, 'serving')
        self.assertEqual(state.url, 'http://127.0.0.1:12345/')
        self.assertTrue(state.has_placement)
        self.assertEqual(self.project.to_dict(), before)
        manifest = json.loads((Path(state.run_dir) / 'run.json').read_text())
        arguments = Path('/proc') / str(manifest['active']['pid']) / 'cmdline'
        self.assertIn(b'-I', arguments.read_bytes().split(b'\0'))
        self.assertEqual(manifest['generation_config']['python'], str(self.python))
        self.assertEqual(json.loads((Path(state.run_dir) / 'floorplan.json').read_text()), before)
        calls = self.ready.call_count
        index = self.runner._index(self.runner.project_path)
        index_mtime = index.stat().st_mtime_ns
        self.runner.poll(self.project)
        self.runner.poll(self.project)
        self.assertEqual(self.ready.call_count, calls)
        self.assertEqual(index.stat().st_mtime_ns, index_mtime)
        self.assertIn('captured child log', self.runner.log_tail())
        restarted = AlignRunner(self.config, self.base / 'project.floorplan')
        restored = restarted.last_run(self.project)
        self.assertEqual(restored.run_dir, state.run_dir)
        self.assertEqual(restored.status, 'serving')
        self.assertTrue(restored.can_stop)
        self.runner.stop()
        self.until(self.runner, 'stopped')

    def test_older_window_cannot_replace_new_history_or_launch_duplicate_worker(self):
        self.runner.start(self.project)
        old = self.until(self.runner, 'serving')
        self.runner.shutdown()
        older_observer = AlignRunner(self.config, self.base / 'project.floorplan')
        older_observer.last_run(self.project)
        newer = AlignRunner(self.config, self.base / 'project.floorplan')
        self.addCleanup(newer.shutdown)
        newer.start(self.project)
        current = self.until(newer, 'serving')
        manifest_path = Path(current.run_dir) / 'run.json'
        pid = json.loads(manifest_path.read_text())['active']['pid']
        self.assertNotEqual(old.run_dir, current.run_dir)
        self.runner.poll(self.project)
        self.assertEqual(self.runner.last_placement(self.project).run_dir, current.run_dir)
        with self.assertRaisesRegex(ValueError, 'Stop the current ALIGN run'):
            self.runner.start(self.project)
        reconnected = older_observer.open_last(self.project)
        self.assertEqual(reconnected.run_dir, current.run_dir)
        self.assertEqual(reconnected.status, 'serving')
        self.assertEqual(json.loads(manifest_path.read_text())['active']['pid'], pid)
        newer.shutdown()
        self.runner.open_last(self.project)
        reopened = self.until(self.runner, 'serving')
        self.assertEqual(reopened.run_dir, current.run_dir)
        self.assertEqual(json.loads(manifest_path.read_text())['active']['mode'], 'reopen')

    def test_save_as_rebinds_live_run_without_restarting_worker(self):
        self.runner.start(self.project)
        original = self.until(self.runner, 'serving')
        path = Path(original.run_dir) / 'run.json'
        pid = json.loads(path.read_text())['active']['pid']
        new_project = self.base / 'saved_as.floorplan'
        self.runner.rebind_project_path(new_project)
        self.assertEqual(json.loads(path.read_text())['active']['pid'], pid)
        restarted = AlignRunner(self.config, new_project)
        self.assertEqual(restarted.last_run(self.project).status, 'serving')
        self.assertEqual(restarted.last_placement(self.project).run_dir, original.run_dir)
        old = AlignRunner(self.config, self.base / 'project.floorplan')
        self.assertIsNone(old.last_run(self.project))

    def test_stale_reopen_keeps_artifacts_and_last_placement_after_failure(self):
        self.runner.start(self.project)
        good = self.until(self.runner, 'serving')
        dump = Path(good.run_dir) / '3_pnr/__placer_dump__.json'
        original = (dump.read_bytes(), dump.stat().st_mtime_ns)
        self.runner.shutdown()
        self.project.set_parameter('w1_2', 2.4e-6)
        (self.base / 'mode').write_text('fail')
        self.runner.start(self.project)
        failed = self.until(self.runner, 'failed')
        self.assertEqual(failed.error, 'intentional backend failure')
        self.assertNotEqual(failed.run_dir, good.run_dir)
        self.assertTrue(failed.has_placement)
        placement = self.runner.last_placement(self.project)
        self.assertEqual(placement.run_dir, good.run_dir)
        self.assertTrue(placement.stale)
        self.runner.open_last(self.project)
        reopened = self.until(self.runner, 'serving')
        self.assertEqual(reopened.run_dir, good.run_dir)
        self.assertTrue(reopened.stale)
        self.assertEqual((dump.read_bytes(), dump.stat().st_mtime_ns), original)
        self.assertEqual(json.loads((Path(good.run_dir) / 'run.json').read_text())['active']['mode'], 'reopen')

    def test_pending_cancel_shutdown_and_failed_attempt_without_placement(self):
        (self.base / 'mode').write_text('pending')
        self.runner.start(self.project)
        self.until(self.runner, 'generating')
        self.runner.stop()
        state = self.until(self.runner, 'stopped')
        self.assertFalse(state.has_placement)
        with self.assertRaisesRegex(ValueError, 'no generated placement'):
            self.runner.open_last(self.project)
        (self.base / 'mode').write_text('ignore')
        self.runner.start(self.project)
        self.until(self.runner, 'generating')
        started = time.monotonic()
        stopped = self.runner.shutdown()
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(stopped.status, 'stopped')
        self.assertFalse(stopped.can_stop)

    def test_named_floorplans_are_scoped_and_tampered_inputs_refuse_reopen(self):
        self.runner.start(self.project)
        state = self.until(self.runner, 'serving')
        other = AlignRunner(self.config, self.base / 'different.floorplan')
        self.assertIsNone(other.last_run(self.project))
        self.runner.shutdown()
        manifest = json.loads((Path(state.run_dir) / 'run.json').read_text())
        Path(manifest['netlist']).write_text('changed')
        with self.assertRaisesRegex(ValueError, 'input files changed'):
            self.runner.open_last(self.project)

    def test_unsaved_run_adoption_and_fingerprint_incomplete_project(self):
        unsaved = AlignRunner(self.config)
        self.addCleanup(unsaved.shutdown)
        unsaved.start(self.project)
        state = self.until(unsaved, 'serving')
        adopted = AlignRunner(self.config, self.base / 'newly_saved.floorplan')
        self.assertEqual(adopted.last_run(self.project).run_dir, state.run_dir)
        self.project.set_parameter('w1_2', None)
        self.assertIsInstance(project_fingerprint(self.project), str)
        self.assertTrue(adopted.poll(self.project).stale)
        self.assertTrue(adopted.open_last(self.project).stale)


class WorkerErrorTests(unittest.TestCase):
    def test_viewer_credential_is_not_the_process_argument_token(self):
        from roar_api.floorplan import _align_worker
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'run.json'
            activation = 'public-process-activation'
            path.write_text(json.dumps({'active': {'token': activation, 'mode': 'generate', 'backend_path': directory, 'port': 0},
                'generation_config': {'profile': PROFILE, 'run_options': {'nvariants': 7, 'seed': 19}},
                'input_dir': directory, 'top': 'test'}))
            placer, captured = SimpleNamespace(), {}
            def schematic2layout(**kwargs):
                captured['options'] = kwargs
                placer.run_gui(tagged_bboxes={}, module_name='TEST', lambda_coeff=1, port=0)
            def server(host, port, application, **kwargs):
                captured['viewer'] = application
                return SimpleNamespace(server_port=9012, serve_forever=lambda: None, server_close=lambda: None)
            modules = {
                'align': SimpleNamespace(__version__=PROFILE['align_version'], schematic2layout=schematic2layout),
                'dash': SimpleNamespace(__version__='2.18.2'), 'align.pnr': SimpleNamespace(placer=placer),
                'align.gui.mockup': SimpleNamespace(AppWithCallbacksAndState=lambda **kwargs: SimpleNamespace(app=SimpleNamespace(server=object()), sel='TEST_0')),
                'werkzeug.serving': SimpleNamespace(make_server=server),
            }
            with patch.dict(sys.modules, modules), patch.object(sys, 'argv', ['worker', '--manifest', str(path), '--token', activation]), \
                    patch.object(_align_worker.signal, 'signal'), patch.object(_align_worker, 'validate_viewer_assets'), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(_align_worker.main(), 0)
            self.assertNotEqual(captured['viewer'].token, activation)
            self.assertEqual(len(captured['viewer'].token), 64)
            self.assertEqual(captured['options']['nvariants'], 7)
            self.assertEqual(captured['options']['seed'], 19)

    def test_viewer_protects_layout_assets_and_callbacks_with_host_origin_and_authentication(self):
        from roar_api.floorplan._align_worker import ProtectedViewer
        token = 'a' * 32
        calls = []
        def application(environ, start):
            calls.append(environ['PATH_INFO'])
            start('200 OK', [('Content-Type', 'application/json')])
            return [b'{"private": "placement"}']
        viewer = ProtectedViewer(application, token)
        viewer.port = 9012
        def request(path='/', **updates):
            environ = {'HTTP_HOST': '127.0.0.1:9012', 'PATH_INFO': path, 'REQUEST_METHOD': 'GET', 'QUERY_STRING': ''}
            environ.update(updates)
            response = {}
            def start(status, headers, exc_info=None):
                response.update(status=status, headers=dict(headers))
            response['body'] = b''.join(viewer(environ, start))
            return response
        for path in ('/', '/_dash-layout', '/assets/test.js', '/_dash-update-component'):
            self.assertEqual(request(path)['status'], '403 Forbidden')
        self.assertEqual(calls, [])
        self.assertEqual(request('/_dash-layout', HTTP_HOST='untrusted.example:9012', HTTP_AUTHORIZATION='Bearer ' + token)['status'], '403 Forbidden')
        self.assertEqual(request('/_dash-layout', HTTP_ORIGIN='http://evil.example', HTTP_AUTHORIZATION='Bearer ' + token)['status'], '403 Forbidden')
        authenticated = request(QUERY_STRING='token=' + token)
        self.assertEqual(authenticated['status'], '303 See Other')
        self.assertEqual(authenticated['headers']['Location'], '/')
        cookie = authenticated['headers']['Set-Cookie']
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)
        for path in ('/', '/_dash-layout', '/assets/test.js', '/_dash-update-component'):
            response = request(path, HTTP_COOKIE=cookie.split(';')[0], REQUEST_METHOD='POST' if 'update' in path else 'GET')
            self.assertEqual(response['status'], '200 OK')
            self.assertEqual(response['headers']['Referrer-Policy'], 'no-referrer')
        self.assertEqual(request('/_dash-layout', HTTP_AUTHORIZATION='Bearer ' + token)['status'], '200 OK')

    def test_loopback_permission_error_preserves_placement_and_explains_reopen(self):
        from roar_api.floorplan import _align_worker
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'run.json'
            token = 'test-activation'
            path.write_text(json.dumps({'active': {'token': token, 'mode': 'generate', 'backend_path': directory, 'port': 0},
                'generation_config': {'profile': PROFILE}, 'input_dir': directory, 'top': 'test'}))
            placer = SimpleNamespace()
            def schematic2layout(**kwargs):
                placer.run_gui(tagged_bboxes={}, module_name='TEST', lambda_coeff=1, port=0)
            modules = {
                'align': SimpleNamespace(__version__=PROFILE['align_version'], schematic2layout=schematic2layout),
                'dash': SimpleNamespace(__version__='2.18.2'),
                'align.pnr': SimpleNamespace(placer=placer),
                'align.gui.mockup': SimpleNamespace(AppWithCallbacksAndState=lambda **kwargs: SimpleNamespace(app=SimpleNamespace(server=object()))),
                'werkzeug.serving': SimpleNamespace(make_server=lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError(errno.EPERM, 'Operation not permitted'))),
            }
            with patch.dict(sys.modules, modules), patch.object(sys, 'argv', ['worker', '--manifest', str(path), '--token', token]), \
                    patch.object(_align_worker.signal, 'signal'), patch.object(_align_worker, 'validate_viewer_assets'), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(_align_worker.main(), 1)
            status = json.loads(path.with_name('status.json').read_text())
            self.assertEqual(status['status'], 'failed')
            self.assertIn('Placement was generated', status['error'])
            self.assertIn('loopback networking', status['error'])
            self.assertIn('reopen the saved placement', status['error'])


class ConfigTests(unittest.TestCase):
    def test_missing_native_viewer_assets_explain_setup_repair(self):
        from roar_api.floorplan._align_worker import validate_viewer_assets
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            with patch.dict(sys.modules, {'align.gui.mockup': SimpleNamespace(__file__=str(base / 'mockup.py'))}):
                with self.assertRaisesRegex(ValueError, 'viewer assets are missing.*Setup help'):
                    validate_viewer_assets()
                (base / 'assets').mkdir()
                for name in ('00_from_codepen.css', '01_style.css', '02_typography.css', 'align.png'):
                    (base / 'assets' / name).touch()
                validate_viewer_assets()

    def test_unsupported_host_is_rejected_before_launch(self):
        with patch('roar_api.floorplan.align_runner.sys.platform', 'win32'):
            with self.assertRaisesRegex(ValueError, 'Linux or WSL'):
                AlignConfig('', '', '').validate()

    def test_environment_uses_fixed_backend_only_and_portable_profile(self):
        with patch.dict(os.environ, {'ROAR_ALIGN_PYTHON': '/tools/align/python', 'ROAR_ALIGN_PDK': '/wrong/official',
                                     'ROAR_ALIGN_PDK_FIXED': '/tested/fixed', 'XDG_STATE_HOME': '/user/state'}, clear=True):
            config = AlignConfig.from_environment()
        self.assertEqual(config.backend_path, '/tested/fixed')
        self.assertEqual(config.python, '/tools/align/python')
        self.assertEqual(config.run_root, '/user/state/roar/align/runs')
        self.assertEqual(PROFILE['run_defaults']['nvariants'], 4)
        with patch.dict(os.environ, {'ROAR_ALIGN_PDK': '/wrong/official'}, clear=True):
            self.assertEqual(AlignConfig.from_environment().backend_path, '')

    @unittest.skipUnless(sys.platform == 'linux', 'ALIGN launcher requires Linux or WSL')
    def test_backend_content_identity_is_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / 'gen_param.py').write_text('tested')
            hashes = {'gen_param.py': hashlib.sha256(b'tested').hexdigest()}
            with patch.dict(PROFILE, {'backend_hashes': hashes}):
                config = AlignConfig(sys.executable, directory, str(base / 'runs'))
                config.validate()
                blocked = base / 'file-not-folder'
                blocked.write_text('keep')
                for root in (blocked, blocked / 'child'):
                    with self.assertRaisesRegex(ValueError, 'Cannot write.*Choose a writable folder'):
                        dataclasses.replace(config, run_root=str(root)).validate()
                with patch('roar_api.floorplan.align_runner.tempfile.TemporaryFile', side_effect=PermissionError('denied')):
                    with self.assertRaisesRegex(ValueError, 'Cannot write.*denied'):
                        config.validate()
                self.assertEqual(blocked.read_text(), 'keep')
                (base / 'gen_param.py').write_text('changed')
                with self.assertRaisesRegex(ValueError, 'Backend does not match'):
                    config.validate()
                with self.assertRaises(ValueError):
                    dataclasses.replace(config, python='').validate()


if __name__ == '__main__':
    unittest.main()
