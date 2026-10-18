"""Exercise the managed checkout launcher without importing Qt or installing."""
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
        self.checkout = self.base / 'checkout with spaces'
        (self.checkout / 'bin').mkdir(parents=True)
        for name in ('roar', 'run_roar.csh', 'rebuild_roar.csh'):
            shutil.copy2(REPO_ROOT / 'bin' / name, self.checkout / 'bin' / name)
        self.log = self.base / 'invocations.jsonl'
        self.probe = self.base / 'fake_pixi.py'
        self.probe.write_text(
            "import json, os, sys\n"
            "with open(os.environ['LAUNCH_LOG'], 'a') as output:\n"
            "    output.write(json.dumps({'executable': sys.argv[1], 'args': sys.argv[2:],\n"
            "        'cwd': os.getcwd(), 'environment': {key: os.environ.get(key) for key in\n"
            "        ('ROAR_HOME', 'VIRTUAL_ENV', 'PYTHONHOME', 'PYTHONPATH',\n"
            "         'LD_LIBRARY_PATH', 'LD_PRELOAD', 'ROAR_ALIGN_PYTHON',\n"
            "         'ROAR_ALIGN_PDK_FIXED', 'ROAR_MANAGED_RUNTIME', 'QT_QPA_PLATFORM', 'PYTHONNOUSERSITE',\n"
            "         'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH')}}) + '\\n')\n"
            "sys.exit(int(os.environ.get('FAKE_EXIT_CODE', '0')))\n"
        )
        self.env = dict(os.environ)
        self.env['LAUNCH_LOG'] = str(self.log)

    def make_runtime(self):
        executable = self.checkout / '.pixi' / 'bin' / 'pixi'
        executable.parent.mkdir(parents=True)
        executable.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' -I -B '
                              + shlex.quote(str(self.probe)) + ' "$0" "$@"\n')
        executable.chmod(0o755)
        python = self.checkout / '.pixi' / 'envs' / 'default' / 'bin' / 'python'
        python.parent.mkdir(parents=True)
        python.write_text('# managed interpreter placeholder, never invoked by this mock\n')
        python.chmod(0o755)
        return executable, python

    def launch(self, *args, entry='roar'):
        return subprocess.run([str(self.checkout / 'bin' / entry), *args],
                              cwd=self.base, env=self.env, capture_output=True, text=True, timeout=10)

    def invocations(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def make_setup_stub(self):
        setup = self.checkout / 'bin' / 'setup_roar.sh'
        setup.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' -I -B '
                         + shlex.quote(str(self.probe)) + ' "$0" "$@"\n')
        setup.chmod(0o755)
        return setup

    def test_launch_uses_offline_locked_runtime_and_forwards_cwd_and_literal_arguments(self):
        executable, _ = self.make_runtime()
        args = ['relative design.roar', '', '--home', 'literal $HOME; `whoami` * %f', '{{ pixi.platform }}']
        result = self.launch(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        invocation, = self.invocations()
        self.assertEqual(invocation['executable'], str(executable))
        self.assertEqual(invocation['cwd'], str(self.base))
        self.assertEqual(invocation['args'], ['run', '--no-config', '--manifest-path',
            str(self.checkout / 'pyproject.toml'), '--environment', 'default', '--offline',
            '--locked', '--no-install', '--executable', 'python',
            str(self.checkout / 'bin' / 'configure_runtime.py'), '--launch', '--cwd', str(self.base), '--', *args])

    def test_active_and_legacy_environment_do_not_override_managed_runtime(self):
        self.make_runtime()
        inherited = ('VIRTUAL_ENV', 'PYTHONHOME', 'PYTHONPATH', 'LD_LIBRARY_PATH',
                     'ROAR_ALIGN_PYTHON', 'ROAR_ALIGN_PDK_FIXED', 'ROAR_MANAGED_RUNTIME',
                     'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH')
        self.env.update({key: '/old/runtime' for key in inherited})
        self.env.update(LD_PRELOAD='', ROAR_HOME='/other/checkout', QT_QPA_PLATFORM='xcb')
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        environment = self.invocations()[0]['environment']
        for key in (*inherited, 'LD_PRELOAD'):
            self.assertIsNone(environment[key], key)
        self.assertEqual(environment['ROAR_HOME'], str(self.checkout))
        self.assertEqual(environment['QT_QPA_PLATFORM'], 'xcb')
        self.assertEqual(environment['PYTHONNOUSERSITE'], '1')

    def test_missing_runtime_points_to_setup_without_running_any_interpreter(self):
        result = self.launch()
        self.assertEqual(result.returncode, 1)
        self.assertIn(str(self.checkout / 'bin' / 'setup_roar.sh'), result.stderr)
        self.assertFalse(self.log.exists())
        self.assertFalse((self.checkout / '.pixi').exists())

    def test_missing_managed_python_does_not_fall_back_or_install(self):
        _, python = self.make_runtime()
        python.unlink()
        result = self.launch()
        self.assertEqual(result.returncode, 1)
        self.assertIn('setup_roar.sh', result.stderr)
        self.assertFalse(self.log.exists())

    def test_runtime_failure_status_is_preserved(self):
        self.make_runtime()
        self.env['FAKE_EXIT_CODE'] = '23'
        result = self.launch()
        self.assertEqual(result.returncode, 23)
        self.assertEqual(len(self.invocations()), 1)

    def test_launch_does_not_write_desktop_metadata_or_require_home(self):
        self.make_runtime()
        self.env.pop('HOME', None)
        blocked_data_home = self.base / 'not a directory'
        blocked_data_home.write_text('keep this file')
        self.env['XDG_DATA_HOME'] = str(blocked_data_home)
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(blocked_data_home.read_text(), 'keep this file')

    def test_historical_entry_forwards_to_the_managed_launcher(self):
        self.make_runtime()
        result = self.launch('a design.json', '', entry='run_roar.csh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.invocations()[0]['args'][-3:], ['--', 'a design.json', ''])

    def test_rebuild_entry_keeps_environments_and_preserves_setup_status(self):
        setup = self.make_setup_stub()
        for name in ('.venv', '.appimage-build', '.pixi'):
            directory = self.checkout / name
            directory.mkdir()
            (directory / 'keep').write_text('existing installation')
        self.env['FAKE_EXIT_CODE'] = '23'
        result = self.launch(entry='rebuild_roar.csh')
        self.assertEqual(result.returncode, 23)
        self.assertEqual(self.invocations()[0]['executable'], str(setup))
        self.assertEqual(self.invocations()[0]['args'], [])
        for name in ('.venv', '.appimage-build', '.pixi'):
            self.assertEqual((self.checkout / name / 'keep').read_text(), 'existing installation')

    def test_rebuild_appimage_runs_only_after_successful_setup(self):
        setup = self.make_setup_stub()
        build = self.checkout / 'bin' / 'build_appimage.sh'
        shutil.copy2(setup, build)
        result = self.launch('--appimage', entry='rebuild_roar.csh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call['executable'] for call in self.invocations()], [str(setup), str(build)])
        self.log.unlink()
        self.env['FAKE_EXIT_CODE'] = '23'
        result = self.launch('--appimage', entry='rebuild_roar.csh')
        self.assertEqual(result.returncode, 23)
        self.assertEqual([call['executable'] for call in self.invocations()], [str(setup)])

    @unittest.skipUnless(shutil.which('make'), 'Requires make')
    def test_make_install_calls_canonical_setup_without_tcsh_or_environment_generation(self):
        shutil.copy2(REPO_ROOT / 'Makefile', self.checkout / 'Makefile')
        setup = self.make_setup_stub()
        empty_path = self.base / 'empty PATH'
        empty_path.mkdir()
        # Make can execute this simple recipe directly instead of using the
        # shell builtin. No tcsh, bash, or system Python is available on PATH.
        (empty_path / 'printf').symlink_to(shutil.which('printf'))
        environment = dict(self.env, PATH=str(empty_path))
        result = subprocess.run([shutil.which('make'), '-s', 'install'], cwd=self.checkout,
                                env=environment, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        invocation = self.invocations()[0]
        self.assertEqual((Path(invocation['cwd']) / invocation['executable']).resolve(), setup)
        self.assertFalse((self.checkout / 'roar_env.csh').exists())
        self.assertFalse((self.checkout / 'roar_env.sh').exists())

    @unittest.skipUnless(shutil.which('make'), 'Requires make')
    def test_optional_env_targets_use_distinct_files_and_bash_preserves_literal_paths(self):
        renamed = self.base / 'checkout $literal `ticks` "quote" \\backslash'
        self.checkout.rename(renamed)
        self.checkout = renamed
        shutil.copy2(REPO_ROOT / 'Makefile', self.checkout / 'Makefile')
        for target in ('generate_roar_env', 'generate_roar_env_bash'):
            result = subprocess.run([shutil.which('make'), '-s', target], cwd=self.checkout,
                                    env=self.env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('setenv ROAR_HOME ', (self.checkout / 'roar_env.csh').read_text())
        script = self.checkout / 'roar_env.sh'
        self.assertNotIn('setenv', script.read_text())
        result = subprocess.run(['/bin/sh', '-c', '. "$1"; printf "%s\\n%s\\n" "$ROAR_HOME" "$ROAR_SRC"',
                                 'sh', str(script)], cwd=self.base, env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [str(self.checkout), str(self.checkout / 'src')])


if __name__ == '__main__':
    unittest.main()
