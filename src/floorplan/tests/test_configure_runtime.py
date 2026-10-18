"""Managed setup publishes readiness only after checks; launch stays offline."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from roar_api.floorplan import doctor
from roar_api.floorplan.align_runner import align_process_environment


ROOT = Path(__file__).resolve().parents[3]
specification = importlib.util.spec_from_file_location('configure_runtime', ROOT / 'bin/configure_runtime.py')
runtime = importlib.util.module_from_spec(specification)
specification.loader.exec_module(runtime)


class ConfigureRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'checkout with spaces $(literal)'
        self.root.mkdir()
        for name in runtime.INPUTS:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('fixture ' + name)
        self.backend_data = b'verified backend source\n'
        profile = {'backend_revision': 'pinned-revision',
                   'backend_hashes': {'mos.py': hashlib.sha256(self.backend_data).hexdigest()}}
        (self.root / 'src/floorplan/align_profile.json').write_text(json.dumps(profile))
        self.gui, self.align, self.backend = runtime.runtime_paths(self.root)
        for executable in (self.gui, self.align):
            executable.parent.mkdir(parents=True)
            executable.write_text('#!/bin/sh\nexit 0\n')
            executable.chmod(0o755)
        self.backend.mkdir(parents=True)
        (self.backend / 'mos.py').write_bytes(self.backend_data)
        self.receipt = self.root / '.pixi/roar-runtime.json'
        self.write_receipt()

    def write_receipt(self):
        self.receipt.write_text(json.dumps(runtime.fingerprint(self.root)))

    def children(self):
        return [subprocess.CompletedProcess([], 0, '{}', ''),
                subprocess.CompletedProcess([], 0, 'ROAR_PROBE ' + json.dumps({
                    'ok': True, 'browser_viewer': True, 'align': '0.9.8', 'python': '3.10.22'}), '')]

    def configure(self, children, gui_result=0):
        with patch.object(sys, 'executable', str(self.gui)), patch.object(sys, 'version_info', (3, 11, 0)), patch.object(sys, 'path', sys.path[:]), patch.dict(os.environ, {}, clear=True), patch.object(runtime.subprocess, 'run', side_effect=children) as run, patch.object(doctor, 'check_setup', return_value=gui_result), contextlib.redirect_stdout(io.StringIO()):
            runtime.configure(self.root)
        return run

    def test_success_receipt_matches_inputs_and_subprocess_paths_remain_literal(self):
        run = self.configure(self.children())
        self.assertEqual(json.loads(self.receipt.read_text()), runtime.fingerprint(self.root))
        runtime.check_ready(self.root)
        command = run.call_args_list[0].args[0]
        self.assertEqual(command, [str(self.align), '-I', str(self.root / 'bin/provision_align.py'),
                                   '--backend-dir', str(self.backend)])
        self.assertNotIn('shell', run.call_args_list[0].kwargs)
        self.assertEqual(run.call_args_list[0].kwargs['env']['LD_LIBRARY_PATH'], str(self.align.parent.parent / 'lib'))

    def test_failed_setup_cannot_leave_a_success_receipt(self):
        cases = {
            'provision': ([subprocess.CompletedProcess([], 1, '', 'download failed')], 0),
            'probe': ([self.children()[0], subprocess.CompletedProcess([], 1, '', 'native import failed')], 0),
            'viewer': ([self.children()[0], subprocess.CompletedProcess([], 0, 'ROAR_PROBE {"ok": true, "browser_viewer": false}', '')], 0),
            'display': (self.children(), 1),
            'timeout': ([subprocess.TimeoutExpired(['align-python'], 300)], 0),
        }
        for name, (children, gui_result) in cases.items():
            with self.subTest(stage=name):
                self.write_receipt()
                with self.assertRaises((ValueError, subprocess.TimeoutExpired)):
                    self.configure(children, gui_result)
                self.assertFalse(self.receipt.exists())

    def test_wrong_setup_interpreter_does_not_invalidate_a_working_runtime(self):
        before = self.receipt.read_bytes()
        with patch.object(sys, 'executable', '/unmanaged/python'):
            with self.assertRaisesRegex(ValueError, 'managed Python'):
                runtime.configure(self.root)
        self.assertEqual(self.receipt.read_bytes(), before)

    def test_changed_manifest_or_lock_rejects_readiness_without_writes(self):
        for name in ('pyproject.toml', 'pixi.lock'):
            with self.subTest(input=name):
                self.write_receipt()
                path = self.root / name
                path.write_text(path.read_text() + '\nchanged')
                before = self.receipt.read_bytes()
                with self.assertRaisesRegex(ValueError, 'out of date'):
                    runtime.check_ready(self.root)
                self.assertEqual(self.receipt.read_bytes(), before)

    def test_corrupt_backend_or_missing_interpreter_rejects_readiness(self):
        (self.backend / 'mos.py').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'backend needs repair'):
            runtime.check_ready(self.root)
        (self.backend / 'mos.py').write_bytes(self.backend_data)
        self.align.unlink()
        with self.assertRaisesRegex(ValueError, 'interpreter is missing'):
            runtime.check_ready(self.root)

    def test_launch_never_installs_and_forwards_arguments_without_shell_parsing(self):
        arguments = ['relative design $(literal).roar', '--home', 'assets with spaces']
        with patch.object(runtime, '__file__', str(self.root / 'bin/configure_runtime.py')), patch.object(runtime, 'configure') as configure, patch.object(runtime.subprocess, 'run') as run, patch.object(runtime.os, 'chdir') as chdir, patch.object(runtime.os, 'execve') as execute:
            self.assertEqual(runtime.main(['--launch', '--cwd', str(self.root), '--', *arguments]), 0)
        configure.assert_not_called()
        run.assert_not_called()
        chdir.assert_called_once_with(self.root)
        self.assertEqual(execute.call_args.args[:2],
                         (str(self.gui), [str(self.gui), '-I', str(self.root / 'src/roar.py'), *arguments]))
        self.assertEqual(execute.call_args.args[2]['ROAR_ALIGN_PYTHON'], str(self.align))

    def test_receipt_cannot_supply_executable_paths(self):
        value = runtime.fingerprint(self.root)
        value['align_python'] = '/untrusted/python'
        self.receipt.write_text(json.dumps(value))
        with patch.object(runtime, '__file__', str(self.root / 'bin/configure_runtime.py')), patch.object(runtime.os, 'execve') as execute, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(runtime.main(['--launch', '--cwd', str(self.root)]), 1)
        execute.assert_not_called()


class ManagedAlignEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.python = '/checkout with spaces/.pixi/envs/align/bin/python'
        self.prefix = str(Path(self.python).parent.parent)
        self.environment = {'ROAR_MANAGED_RUNTIME': '1', 'ROAR_ALIGN_PYTHON': self.python,
            'LD_LIBRARY_PATH': '/gui/lib', 'LD_PRELOAD': '/gui/injected.so',
            'CONDA_PREFIX': '/gui', 'CONDA_DEFAULT_ENV': 'default', 'VIRTUAL_ENV': '/old/venv',
            'PATH': '/gui/bin:/usr/bin', 'PYTHONPATH': '/external/modules',
            'PYTHONHOME': '/external/python', 'ALIGN_HOME': '/old/align'}

    def test_managed_worker_gets_only_its_own_native_prefix(self):
        with patch.dict(os.environ, self.environment, clear=True):
            result = align_process_environment(self.python)
            self.assertEqual(dict(os.environ), self.environment)
        self.assertEqual(result['LD_LIBRARY_PATH'], self.prefix + '/lib')
        self.assertEqual(result['CONDA_PREFIX'], self.prefix)
        self.assertEqual(result['PATH'], self.prefix + '/bin' + os.pathsep + os.defpath)
        for key in ('LD_PRELOAD', 'CONDA_DEFAULT_ENV', 'VIRTUAL_ENV', 'PYTHONPATH', 'PYTHONHOME', 'ALIGN_HOME'):
            self.assertNotIn(key, result)

    def test_custom_worker_does_not_receive_a_guessed_native_prefix(self):
        with patch.dict(os.environ, self.environment, clear=True):
            result = align_process_environment('/external/align/bin/python')
        self.assertNotIn('LD_LIBRARY_PATH', result)
        self.assertNotIn('CONDA_PREFIX', result)
        self.assertNotIn('LD_PRELOAD', result)

    def test_unmanaged_installation_preserves_its_native_library_configuration(self):
        environment = dict(self.environment, ROAR_MANAGED_RUNTIME='0', LD_LIBRARY_PATH='/custom/align/lib')
        with patch.dict(os.environ, environment, clear=True):
            result = align_process_environment('/custom/align/bin/python')
        self.assertEqual(result['LD_LIBRARY_PATH'], '/custom/align/lib')
        self.assertNotIn('PYTHONPATH', result)
