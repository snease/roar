"""Exercise checkout launchers without importing Qt or installing packages."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[3]


class CheckoutLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.checkout = self.base / "checkout with spaces"
        (self.checkout / "bin").mkdir(parents=True)
        for name in ("roar", "run_roar.csh"):
            shutil.copy2(REPO_ROOT / "bin" / name, self.checkout / "bin" / name)
        self.log = self.base / "invocations.jsonl"
        self.probe = self.base / "fake_python.py"
        self.probe.write_text(
            "import json, os, sys\n"
            "args = sys.argv[2:]\n"
            "with open(os.environ['LAUNCH_LOG'], 'a') as output:\n"
            "    output.write(json.dumps({'python': sys.argv[1], 'args': args,\n"
            "        'home': os.environ.get('ROAR_HOME'),\n"
            "        'ld_library_path': os.environ.get('LD_LIBRARY_PATH')}) + '\\n')\n"
            "if args[:1] == ['-c']:\n"
            "    print(os.environ.get('FAKE_QT_LIB_DIR', ''))\n"
            "    sys.exit(int(os.environ.get('FAKE_QT_STATUS', '0')))\n"
            "sys.exit(int(os.environ.get('FAKE_EXIT_CODE', '0')))\n"
        )
        self.env = dict(os.environ)
        for name in ("VIRTUAL_ENV", "ROAR_HOME", "ROAR_FORCE_VENV_QT", "LD_LIBRARY_PATH"):
            self.env.pop(name, None)
        self.env.update(LAUNCH_LOG=str(self.log), XDG_DATA_HOME=str(self.base / "desktop data"))

    def fake_interpreter(self, environment):
        executable = environment / "bin" / "python3"
        executable.parent.mkdir(parents=True)
        executable.write_text(
            "#!/bin/sh\nexec " + shlex.quote(sys.executable) + " "
            + shlex.quote(str(self.probe)) + ' "$0" "$@"\n'
        )
        executable.chmod(0o755)
        return executable

    def launch(self, *args, entry="roar"):
        return subprocess.run(
            [str(self.checkout / "bin" / entry), *args],
            cwd=self.base, env=self.env, capture_output=True, text=True, check=False,
        )

    def invocations(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_checkout_environment_launches_without_activation_and_forwards_arguments(self):
        executable = self.fake_interpreter(self.checkout / ".venv")
        args = ["design with spaces.json", "", "--home", "literal $HOME; `whoami` * %f"]
        result = self.launch(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.invocations(), [{
            "python": str(executable),
            "args": [str(self.checkout / "src" / "roar.py"), *args],
            "home": str(self.checkout), "ld_library_path": None,
        }])

    def test_active_environment_takes_precedence_and_preserves_exit_status(self):
        self.fake_interpreter(self.checkout / ".venv")
        active = self.base / "active environment"
        executable = self.fake_interpreter(active)
        self.env.update(VIRTUAL_ENV=str(active), FAKE_EXIT_CODE="23")
        result = self.launch()
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(self.invocations()[0]["python"], str(executable))

    def test_missing_environment_points_to_setup_without_running_python(self):
        result = self.launch()
        self.assertEqual(result.returncode, 1)
        self.assertIn(str(self.checkout / ".venv" / "bin" / "python3"), result.stderr)
        self.assertIn("bin/setup_roar.sh", result.stderr)
        self.assertFalse(self.log.exists())
        self.assertFalse((self.checkout / ".venv").exists())

    def test_broken_active_environment_does_not_silently_fall_back(self):
        self.fake_interpreter(self.checkout / ".venv")
        active = self.base / "broken active environment"
        self.env["VIRTUAL_ENV"] = str(active)
        result = self.launch()
        self.assertEqual(result.returncode, 1)
        self.assertIn(str(active / "bin" / "python3"), result.stderr)
        self.assertIn("bin/setup_roar.sh", result.stderr)
        self.assertFalse(self.log.exists())

    def test_explicit_roar_home_controls_checkout_and_default_environment(self):
        alternate = self.base / "other checkout"
        executable = self.fake_interpreter(alternate / ".venv")
        self.env["ROAR_HOME"] = str(alternate)
        result = self.launch("--check-setup")
        self.assertEqual(result.returncode, 0, result.stderr)
        invocation = self.invocations()[0]
        self.assertEqual(invocation["python"], str(executable))
        self.assertEqual(invocation["home"], str(alternate))
        self.assertEqual(invocation["args"], [str(alternate / "src" / "roar.py"), "--check-setup"])

    def test_launch_does_not_write_desktop_metadata_or_require_home(self):
        self.fake_interpreter(self.checkout / ".venv")
        self.env.pop("HOME", None)
        blocked_data_home = self.base / "not a directory"
        blocked_data_home.write_text("keep this file")
        self.env["XDG_DATA_HOME"] = str(blocked_data_home)
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(blocked_data_home.read_text(), "keep this file")

    def test_forced_qt_uses_selected_interpreter_and_preserves_library_search_path(self):
        executable = self.fake_interpreter(self.checkout / ".venv")
        qt_lib = self.base / "Qt6 libraries"
        qt_lib.mkdir()
        self.env.update(ROAR_FORCE_VENV_QT="1", FAKE_QT_LIB_DIR=str(qt_lib),
                        LD_LIBRARY_PATH="/existing/lib:/another/lib")
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        probe, launch = self.invocations()
        self.assertEqual(probe["python"], str(executable))
        self.assertEqual(probe["args"][0], "-c")
        self.assertEqual(launch["ld_library_path"], str(qt_lib) + ":/existing/lib:/another/lib")

    def test_failed_optional_qt_probe_still_launches(self):
        self.fake_interpreter(self.checkout / ".venv")
        self.env.update(ROAR_FORCE_VENV_QT="1", FAKE_QT_STATUS="1")
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.invocations()), 2)
        self.assertIsNone(self.invocations()[-1]["ld_library_path"])

    def test_historical_csh_entry_forwards_to_launcher_without_generated_env_files(self):
        self.fake_interpreter(self.checkout / ".venv")
        result = self.launch("a design.json", "", entry="run_roar.csh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.invocations()[0]["args"],
                         [str(self.checkout / "src" / "roar.py"), "a design.json", ""])


if __name__ == "__main__":
    unittest.main()
