"""Setup checks report broken optional tools without starting the desktop."""
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


class SetupCheckTests(unittest.TestCase):
    def run_check(self, children, environment=None):
        output = io.StringIO()
        with patch.dict(os.environ, environment or {}, clear=True), patch.object(doctor, '_run', side_effect=children) as run:
            code = doctor.check_setup(output=output)
        return code, output.getvalue(), run

    def gui(self, *, settings=None, platform='xcb'):
        text = 'ROAR_SETTINGS ' + json.dumps(settings or {}) + '\nROAR_IMPORTS ready\n'
        text += 'ROAR_GUI ' + json.dumps({'ok': True, 'platform': platform}) + '\n'
        return subprocess.CompletedProcess([], 0, text, '')

    def test_native_import_failure_is_a_failure_with_system_library_guidance(self):
        error = {'ok': False, 'error': 'ImportError: libEGL.so.1: cannot open shared object file', 'missing_module': None}
        child = subprocess.CompletedProcess([], 1, 'ROAR_GUI ' + json.dumps(error), '')
        code, text, _ = self.run_check([child])
        self.assertEqual(code, 1)
        self.assertIn('libEGL.so.1', text)
        self.assertNotIn('pip install', text)
        if sys.platform == 'linux':
            self.assertIn('sudo apt install libegl1', text)
        self.assertIn('ALIGN is not configured', text)

    def test_absent_gui_package_points_to_the_gui_extra(self):
        error = {'ok': False, 'error': "ModuleNotFoundError: No module named 'PyQt6'", 'missing_module': 'PyQt6'}
        code, text, _ = self.run_check([subprocess.CompletedProcess([], 1, 'ROAR_GUI ' + json.dumps(error), '')])
        self.assertEqual(code, 1)
        self.assertIn("pip install -e '.[gui]'", text)
        self.assertNotIn('sudo apt install', text)

    def test_display_abort_is_reported_without_aborting_the_checker(self):
        child = subprocess.CompletedProcess([], -6, 'ROAR_IMPORTS ready\n', 'qt.qpa.xcb: could not connect to display')
        code, text, _ = self.run_check([child])
        self.assertEqual(code, 1)
        self.assertIn('could not connect to display', text)
        self.assertIn('WSLg', text)
        self.assertNotIn('sudo apt install', text)

    def test_offscreen_success_does_not_claim_desktop_display_works(self):
        code, text, run = self.run_check([self.gui(platform='offscreen')])
        self.assertEqual(code, 0)
        self.assertIn('desktop display access was not tested', text)
        self.assertEqual(run.call_count, 1)

    @unittest.skipUnless(sys.platform == 'linux', 'ALIGN launcher requires Linux or WSL')
    def test_saved_align_settings_are_checked_without_creating_run_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'not-created'
            gui = self.gui(settings={'python': 'tools/bin/python', 'backend_path': 'tools/SKY130_PDK', 'run_root': str(root)})
            result = {'ok': True, 'align': '0.9.8', 'dash': '2.18.2', 'browser_viewer': False, 'message': 'Optional viewer assets missing'}
            code, text, run = self.run_check([gui, subprocess.CompletedProcess([], 0, 'ROAR_PROBE ' + json.dumps(result), '')])
            self.assertEqual(code, 0)
            self.assertIn('Optional viewer assets missing', text)
            self.assertFalse(root.exists())
            command = run.call_args_list[1].args[0]
            self.assertEqual(command[0], str(Path('tools/bin/python').absolute()))
            self.assertEqual(command[-1], str(Path('tools/SKY130_PDK').resolve()))

    def test_incomplete_align_configuration_is_actionable(self):
        code, text, _ = self.run_check([self.gui()], {'ROAR_ALIGN_PYTHON': '/align/bin/python'})
        self.assertEqual(code, 1)
        self.assertIn('ALIGN configuration is incomplete', text)

    def test_explicitly_cleared_settings_override_environment_paths(self):
        code, text, run = self.run_check([self.gui(settings={'python': '', 'backend_path': ''})],
                                        {'ROAR_ALIGN_PYTHON': '/align/bin/python', 'ROAR_ALIGN_PDK_FIXED': '/backend'})
        self.assertEqual(code, 0)
        self.assertIn('ALIGN is not configured', text)
        self.assertEqual(run.call_count, 1)

    def test_run_storage_check_does_not_create_or_accept_a_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'runs'
            self.assertEqual(doctor._check_runs(root), root)
            self.assertFalse(root.exists())
            root.write_text('occupied')
            with self.assertRaisesRegex(ValueError, 'Cannot write'):
                doctor._check_runs(root)

    def test_cli_check_does_not_construct_a_session(self):
        from roar_api.__main__ import main
        with patch.object(sys, 'argv', ['roar', '--check-setup']), patch.object(doctor, 'check_setup', return_value=1) as check, patch('roar_api.Session') as session:
            self.assertEqual(main(), 1)
        check.assert_called_once_with()
        session.assert_not_called()
