"""Launcher diagnostics must work before importing newer Python syntax or Qt."""
from pathlib import Path
import subprocess
import sys
import unittest


class LauncherTests(unittest.TestCase):
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
