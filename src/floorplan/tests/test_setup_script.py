"""Run setup against disposable environments and mock installers, never host apt/pip."""
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

# Every Python and package-manager command is handled here. Unknown commands
# fail closed, so a script change cannot accidentally invoke a real installer.
MOCK_COMMAND = r'''
import json, os, pathlib, sys
executable, args = pathlib.Path(sys.argv[1]), sys.argv[2:]
name = executable.name
with open(os.environ['SETUP_LOG'], 'a') as output:
    output.write(json.dumps({'executable': str(executable), 'args': args}) + '\n')

def finish(stage):
    if os.environ.get('FAIL_STAGE') == stage:
        print('Mock failure: ' + stage, file=sys.stderr)
        sys.exit(23)

if name == 'sudo':
    assert args[0] == 'apt-get', args
    target = pathlib.Path(os.environ['SETUP_MOCK_BIN']) / 'apt-get'
    os.execv(str(target), [str(target), *args[1:]])
elif name == 'apt-get':
    assert args[0] in ('update', 'install'), args
    finish('apt-' + args[0])
elif name == 'dpkg-query':
    if args[-1] in os.environ.get('MISSING_PACKAGES', '').split():
        sys.exit(1)
    print('install ok installed')
elif name.startswith('python'):
    config = executable.parent.parent / 'pyvenv.cfg'
    version = config.read_text().strip().split(' = ')[-1] if config.exists() else (
        '3.11' if name == 'python3.11' else '3.10')
    if args[:1] == ['-c']:
        code = args[1]
        if 'runpy.run_path' in code:
            print('libegl1 libgl1')
        elif code == 'import ensurepip':
            sys.exit(int(os.environ.get('NO_ENSUREPIP', '0')))
        elif 'sys.prefix == sys.base_prefix' in code:
            sys.exit(int(os.environ.get('NOT_A_VENV', '0')))
        elif 'sys.version_info' in code and 'raise SystemExit' in code:
            sys.exit(int(os.environ.get('OLD_PYTHON', '0')))
        elif 'sys.version_info' in code and 'print(' in code:
            print(version)
        else:
            raise AssertionError(('Unexpected Python code', code))
    elif args[:2] == ['-m', 'venv']:
        finish('venv')
        directory = pathlib.Path(args[2])
        (directory / 'bin').mkdir(parents=True)
        (directory / 'pyvenv.cfg').write_text('version = ' + version)
        target = directory / 'bin' / 'python3'
        target.write_text(executable.read_text())
        target.chmod(0o755)
    elif args[:3] == ['-m', 'pip', 'install']:
        finish('dependencies' if '-e' in args else 'tools')
    elif len(args) == 2 and args[0].endswith('/src/roar.py') and args[1] == '--check-gui':
        finish('gui')
    else:
        raise AssertionError(('Unexpected Python arguments', args))
else:
    raise AssertionError(('Unexpected command', name, args))
'''


@unittest.skipUnless(os.name == 'posix' and shutil.which('bash'), 'Requires Bash')
class SetupScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='roar-setup-test-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.checkout = self.base / 'checkout with spaces'
        (self.checkout / 'bin').mkdir(parents=True)
        self.os_release = self.base / 'os-release'
        self.os_release.write_text('ID=ubuntu\nVERSION_ID="22.04"\n')
        self.script = self.checkout / 'bin' / 'setup_roar.sh'
        # Replace only the OS metadata path in the disposable script; the real
        # setup retains its OS-owned configuration and has no test-only override.
        self.script.write_text((REPO_ROOT / 'bin' / 'setup_roar.sh').read_text().replace(
            '/etc/os-release', shlex.quote(str(self.os_release))))
        self.script.chmod(0o755)
        self.log = self.base / 'commands.jsonl'
        self.mock_bin = self.base / 'mockbin'
        self.mock_bin.mkdir()
        self.dispatcher = self.base / 'mock_command.py'
        self.dispatcher.write_text(MOCK_COMMAND)
        self.wrapper = ('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' '
                        + shlex.quote(str(self.dispatcher)) + ' "$0" "$@"\n')
        for name in ('python3', 'python3.11', 'dpkg-query', 'sudo', 'apt-get'):
            self.make_executable(self.mock_bin / name)
        self.env = dict(os.environ)
        for name in ('VIRTUAL_ENV', 'ROAR_HOME', 'PYTHONPATH', 'PYTHONHOME', 'BASH_ENV', 'ENV'):
            self.env.pop(name, None)
        self.env.update(PATH=str(self.mock_bin) + ':/usr/bin:/bin',
                        SETUP_LOG=str(self.log), SETUP_MOCK_BIN=str(self.mock_bin),
                        HOME=str(self.base))

    def make_executable(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.wrapper)
        path.chmod(0o755)
        return path

    def make_environment(self, path, *, version='3.11'):
        executable = self.make_executable(path / 'bin' / 'python3')
        (path / 'pyvenv.cfg').write_text('version = ' + version)
        return executable

    def run_setup(self, *args, answer=None):
        if answer is None:
            return subprocess.run([str(self.script), *args], cwd=self.base, env=self.env,
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)
        master, slave = os.openpty()
        try:
            os.write(master, (answer + '\n').encode())
            return subprocess.run([str(self.script), *args], cwd=self.base, env=self.env,
                                  stdin=slave, capture_output=True, text=True, timeout=15)
        finally:
            os.close(master)
            os.close(slave)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def named_calls(self, name):
        return [call for call in self.calls() if Path(call['executable']).name == name]

    def test_help_and_bad_arguments_do_not_run_installers(self):
        for args, code in ((['--help'], 0), (['--python'], 1), (['--unknown'], 1)):
            with self.subTest(args=args):
                result = self.run_setup(*args)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual(self.calls(), [])
        self.assertFalse((self.checkout / '.venv').exists())

    def test_selected_python_creates_environment_and_installs_only_through_it(self):
        chosen = self.mock_bin / 'python3.11'
        result = self.run_setup('--skip-system-packages', '--python', str(chosen))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        environment = self.checkout / '.venv'
        interpreter = str(environment / 'bin' / 'python3')
        calls = self.calls()
        creation = [call for call in calls if call['args'][:2] == ['-m', 'venv']]
        self.assertEqual(creation, [{'executable': str(chosen), 'args': ['-m', 'venv', str(environment)]}])
        self.assertEqual((environment / 'pyvenv.cfg').read_text(), 'version = 3.11')
        self.assertIn('Using Python 3.11', result.stdout)
        pip = [call for call in calls if call['args'][:2] == ['-m', 'pip']]
        self.assertEqual(len(pip), 2)
        self.assertTrue(all(call['executable'] == interpreter for call in pip))
        self.assertEqual(pip[-1]['args'], ['-m', 'pip', 'install', '-e', str(self.checkout) + '[gui]'])
        self.assertEqual(calls[-1], {'executable': interpreter,
                                    'args': [str(self.checkout / 'src' / 'roar.py'), '--check-gui']})
        self.assertTrue(all(Path(call['executable']).name.startswith('python') for call in calls))

    def test_active_environment_takes_precedence_and_preserves_python_311(self):
        self.make_environment(self.checkout / '.venv', version='3.10')
        active = self.base / 'CAD environment with spaces'
        interpreter = self.make_environment(active)
        original_config = (active / 'pyvenv.cfg').read_bytes()
        self.env['VIRTUAL_ENV'] = str(active)
        result = self.run_setup('--skip-system-packages')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Using Python 3.11', result.stdout)
        self.assertTrue(all(call['executable'] == str(interpreter) for call in self.calls()))
        self.assertFalse(any(call['args'][:2] == ['-m', 'venv'] for call in self.calls()))
        self.assertEqual((active / 'pyvenv.cfg').read_bytes(), original_config)

    def test_python_option_never_replaces_an_existing_environment(self):
        environment = self.checkout / '.venv'
        self.make_environment(environment)
        original_config = (environment / 'pyvenv.cfg').read_bytes()
        result = self.run_setup('--python', 'python3.11', '--skip-system-packages')
        self.assertEqual(result.returncode, 1)
        self.assertIn('Omit --python to reuse it', result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertEqual((environment / 'pyvenv.cfg').read_bytes(), original_config)

    def test_missing_active_environment_and_incomplete_directory_are_not_replaced(self):
        self.env['VIRTUAL_ENV'] = str(self.base / 'missing active environment')
        result = self.run_setup('--skip-system-packages')
        self.assertEqual(result.returncode, 1)
        self.assertIn('active VIRTUAL_ENV does not exist', result.stderr)
        self.env.pop('VIRTUAL_ENV')
        (self.checkout / '.venv').mkdir()
        result = self.run_setup('--skip-system-packages')
        self.assertEqual(result.returncode, 1)
        self.assertIn('not a usable virtual environment', result.stderr)
        self.assertEqual(self.calls(), [])

    def test_old_python_and_false_virtual_environment_fail_before_pip(self):
        self.env['OLD_PYTHON'] = '1'
        result = self.run_setup('--skip-system-packages')
        self.assertEqual(result.returncode, 1)
        self.assertIn('Python 3.10 or newer', result.stderr)
        self.assertEqual(len(self.calls()), 1)
        self.env.pop('OLD_PYTHON')
        self.env['NOT_A_VENV'] = '1'
        self.make_environment(self.checkout / '.venv')
        result = self.run_setup('--skip-system-packages')
        self.assertEqual(result.returncode, 1)
        self.assertIn('not running in a virtual environment', result.stderr)
        self.assertFalse(any(call['args'][:2] in (['-m', 'pip'], ['-m', 'venv']) for call in self.calls()))

    def test_missing_native_packages_noninteractive_prints_commands_without_installing(self):
        self.env['MISSING_PACKAGES'] = 'libegl1'
        result = self.run_setup()
        self.assertEqual(result.returncode, 1)
        self.assertIn('sudo apt-get update', result.stdout)
        self.assertIn('sudo apt-get install libegl1', result.stdout)
        self.assertIn('Run setup interactively', result.stderr)
        self.assertFalse(self.named_calls('sudo'))
        self.assertFalse(self.named_calls('apt-get'))
        self.assertFalse((self.checkout / '.venv').exists())

    def test_missing_ensurepip_requests_the_selected_pythons_venv_package(self):
        self.env.update(NO_ENSUREPIP='1', MISSING_PACKAGES='python3.11-venv')
        result = self.run_setup('--python', 'python3.11')
        self.assertEqual(result.returncode, 1)
        self.assertIn('sudo apt-get install python3.11-venv', result.stdout)
        self.assertFalse(self.named_calls('sudo'))
        self.assertFalse(self.named_calls('apt-get'))

    def test_satisfied_native_packages_and_rolling_os_need_no_apt(self):
        for release in ('ID=ubuntu\nVERSION_ID=22.04\n', 'ID=arch\n'):
            with self.subTest(release=release):
                self.os_release.write_text(release)
                result = self.run_setup()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(self.named_calls('apt-get'))
                self.assertFalse(self.named_calls('sudo'))
                self.assertEqual(self.calls()[-1]['args'][-1], '--check-gui')
                self.log.unlink()
                shutil.rmtree(self.checkout / '.venv')

    def test_each_python_failure_stops_before_later_steps(self):
        for stage in ('venv', 'tools', 'dependencies', 'gui'):
            with self.subTest(stage=stage):
                self.env['FAIL_STAGE'] = stage
                result = self.run_setup('--skip-system-packages')
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn('Mock failure: ' + stage, result.stderr)
                self.assertNotIn('Setup check passed', result.stdout)
                final = self.calls()[-1]['args']
                if stage == 'venv':
                    self.assertEqual(final[:2], ['-m', 'venv'])
                elif stage == 'tools':
                    self.assertEqual(final[:4], ['-m', 'pip', 'install', '--upgrade'])
                elif stage == 'dependencies':
                    self.assertEqual(final[:4], ['-m', 'pip', 'install', '-e'])
                else:
                    self.assertEqual(final[-1], '--check-gui')
                self.log.unlink()
                shutil.rmtree(self.checkout / '.venv', ignore_errors=True)

    def test_interactive_approval_and_apt_failures_gate_python_installation(self):
        self.env['MISSING_PACKAGES'] = 'libegl1'
        for answer, stage in (('n', ''), ('y', 'apt-update'), ('yes', 'apt-install'), ('y', '')):
            with self.subTest(answer=answer, stage=stage):
                self.env['FAIL_STAGE'] = stage
                result = self.run_setup(answer=answer)
                approved = answer != 'n'
                succeeded = approved and not stage
                self.assertEqual(result.returncode, 0 if succeeded else 1, result.stdout + result.stderr)
                apt = [call['args'] for call in self.named_calls('apt-get')]
                expected = [] if not approved else [['update']]
                if approved and stage != 'apt-update':
                    expected.append(['install', '-y', '--no-install-recommends', 'libegl1'])
                self.assertEqual(apt, expected)
                self.assertEqual((self.checkout / '.venv').exists(), succeeded)
                if succeeded:
                    self.assertEqual(self.calls()[-1]['args'][-1], '--check-gui')
                else:
                    self.assertFalse(any(call['args'][:2] == ['-m', 'pip'] for call in self.calls()))
                self.log.unlink()
                shutil.rmtree(self.checkout / '.venv', ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
