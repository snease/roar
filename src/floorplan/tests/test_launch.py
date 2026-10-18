"""Launcher diagnostics must work before importing newer Python syntax or Qt."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import unittest


class LauncherTests(unittest.TestCase):
    def launch(self, environment):
        return subprocess.run([sys.executable, '-B', '-m', 'roar_api'],
                              cwd=Path(__file__).resolve().parents[3], env=environment,
                              capture_output=True, text=True, timeout=40)

    @unittest.skipIf(importlib.util.find_spec('PyQt6'), 'Requires an environment without the GUI extra')
    def test_real_missing_gui_package_exits_with_install_guidance(self):
        result = self.launch(dict(os.environ))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("No module named 'PyQt6'", result.stderr)
        self.assertIn("pip install -e '.[gui]'", result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertNotIn('sudo apt install', result.stderr)
        self.assertEqual(result.stdout, '')

    @unittest.skipUnless(sys.platform == 'linux' and importlib.util.find_spec('PyQt6'),
                         'Requires a Linux GUI environment to check native prerequisites')
    def test_real_missing_native_library_exits_cleanly_without_library_override(self):
        environment = dict(os.environ)
        for name in ('LD_LIBRARY_PATH', 'LD_PRELOAD', 'PYTHONPATH', 'PYTHONHOME'):
            environment.pop(name, None)
        probe = subprocess.run([sys.executable, '-I', '-B', '-c', 'from PyQt6 import QtWidgets'],
                               env=environment, capture_output=True, text=True, timeout=15)
        if probe.returncode == 0:
            self.skipTest('Host provides the native Qt libraries without an override')
        self.assertIn('.so', probe.stderr, probe.stderr)
        result = self.launch(environment)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('GUI startup:', result.stderr)
        self.assertIn('sudo apt install', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertEqual(result.stdout, '')

    @unittest.skipUnless(importlib.util.find_spec('PyQt6'), 'Requires the GUI extra')
    def test_real_qt_platform_abort_does_not_abort_the_launcher(self):
        environment = dict(os.environ)
        environment['QT_QPA_PLATFORM'] = 'roar-intentionally-unavailable-platform'
        result = self.launch(environment)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('GUI startup:', result.stderr)
        self.assertIn('Remove the QT_QPA_PLATFORM override', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertNotIn('sudo apt install', result.stderr)
        self.assertEqual(result.stdout, '')

    def test_unsupported_python_explains_environment_before_importing_backend(self):
        root = Path(__file__).resolve().parents[3]
        script = """
import runpy
import sys
sys.version_info = (3, int(sys.argv[2]), 0)
sys.path.insert(0, sys.argv[1])
if sys.argv[3] == 'script':
    runpy.run_path(sys.argv[1] + '/src/roar.py', run_name='__main__')
else:
    runpy.run_module('roar_api', run_name='__main__')
"""
        for minor in (6, 9):
            for launch in ("script", "module"):
                with self.subTest(python=(3, minor), launch=launch):
                    result = subprocess.run(
                        [sys.executable, "-I", "-S", "-c", script, str(root), str(minor), launch],
                        capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn("ROAR requires Python 3.10 or newer", result.stderr)
                    self.assertIn("Python 3.{}.0".format(minor), result.stderr)
                    self.assertIn("Activate your ROAR environment", result.stderr)
                    self.assertNotIn("SyntaxError", result.stderr)
                    if launch == "script":
                        self.assertNotIn("Traceback", result.stderr)
