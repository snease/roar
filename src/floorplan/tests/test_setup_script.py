"""Exercise Pixi bootstrap with local archives and mocks; never download/install."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[3]
MOCK_COMMAND = r'''
import json, os, pathlib, shutil, sys
executable, args = pathlib.Path(sys.argv[1]), sys.argv[2:]
name = executable.name
with open(os.environ['SETUP_LOG'], 'a') as output:
    output.write(json.dumps({'command': name, 'args': args, 'cwd': os.getcwd(),
        'environment': {key: os.environ.get(key) for key in ('VIRTUAL_ENV',
            'PYTHONHOME', 'PYTHONPATH', 'LD_LIBRARY_PATH', 'LD_PRELOAD',
            'ROAR_ALIGN_PYTHON', 'ROAR_ALIGN_PDK_FIXED', 'ROAR_MANAGED_RUNTIME',
            'ROAR_HOME', 'QT_QPA_PLATFORM', 'PYTHONNOUSERSITE', 'QT_PLUGIN_PATH',
            'QT_QPA_PLATFORM_PLUGIN_PATH')}}) + '\n')
if name == 'uname':
    print(os.environ.get('MOCK_SYSTEM', 'Linux') if args == ['-s']
          else os.environ.get('MOCK_ARCH', 'x86_64'))
elif name in ('curl', 'wget'):
    if os.environ.get('FAIL_STAGE') == 'download':
        sys.exit(7)
    target = pathlib.Path(args[args.index('--output') + 1] if name == 'curl'
        else next(value.split('=', 1)[1] for value in args if value.startswith('--output-document=')))
    if os.environ.get('CORRUPT_DOWNLOAD'):
        target.write_bytes(b'corrupted archive')
    else:
        shutil.copyfile(os.environ['MOCK_ARCHIVE'], target)
elif name == 'pixi':
    stage = args[args.index('--environment') + 1] if args[0] == 'install' else 'configure'
    assert args[0] in ('install', 'run'), args
    if os.environ.get('FAIL_STAGE') == stage:
        sys.exit(23)
else:
    raise AssertionError(('Unexpected command', name, args))
'''


@unittest.skipUnless(os.name == 'posix' and shutil.which('bash'), 'Requires Bash')
class SetupScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='roar-setup-test-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.checkout = self.base / 'checkout with spaces $ [literal]'
        (self.checkout / 'bin').mkdir(parents=True)
        (self.checkout / 'pyproject.toml').write_text('# mock manifest\n')
        (self.checkout / 'pixi.lock').write_text('# mock lock\n')
        self.script = self.checkout / 'bin' / 'setup_roar.sh'
        self.source = (REPO_ROOT / 'bin' / 'setup_roar.sh').read_text()
        self.log = self.base / 'commands.jsonl'
        self.mock_bin = self.base / 'mockbin'
        self.mock_bin.mkdir()
        self.dispatcher = self.base / 'mock_command.py'
        self.dispatcher.write_text(MOCK_COMMAND)
        self.wrapper = ('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' -I -B '
                        + shlex.quote(str(self.dispatcher)) + ' "$0" "$@"\n')
        # This PATH has no real network clients, Python, pip, sudo, or apt.
        for name in ('bash', 'cat', 'dirname', 'mkdir', 'mktemp', 'rm', 'mv', 'chmod', 'tar', 'gzip', 'sha256sum'):
            executable = shutil.which(name)
            if executable is None:
                self.skipTest(f'Requires {name}')
            (self.mock_bin / name).symlink_to(executable)
        for name in ('uname', 'curl', 'wget'):
            self.make_executable(self.mock_bin / name)
        self.archive = self.base / 'release.tar.gz'
        self.make_archive()
        self.env = dict(os.environ)
        for name in ('VIRTUAL_ENV', 'ROAR_HOME', 'PYTHONPATH', 'PYTHONHOME', 'BASH_ENV', 'ENV',
                     'LD_LIBRARY_PATH', 'LD_PRELOAD'):
            self.env.pop(name, None)
        self.env.update(PATH=str(self.mock_bin), SETUP_LOG=str(self.log), MOCK_ARCHIVE=str(self.archive))

    def make_executable(self, path, content=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.wrapper if content is None else content)
        path.chmod(0o755)
        return path

    @property
    def pixi(self):
        return self.checkout / '.pixi' / 'bin' / 'pixi'

    def make_archive(self, *, binary=None, include_pixi=True, extra_members=()):
        with tarfile.open(self.archive, 'w:gz') as archive:
            members = list(extra_members)
            if include_pixi:
                members.append(('pixi', (self.wrapper if binary is None else binary).encode()))
            for name, data in members:
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        # Only the disposable script's expected digests change. Verification and
        # extraction use real sha256sum/tar against the local fixture archive.
        source = re.sub(r'(?m)^pixi_archive_sha256=[a-f0-9]+$',
                        'pixi_archive_sha256=' + hashlib.sha256(self.archive.read_bytes()).hexdigest(), self.source)
        source = re.sub(r'(?m)^pixi_binary_sha256=[a-f0-9]+$',
                        'pixi_binary_sha256=' + hashlib.sha256(self.wrapper.encode()).hexdigest(), source)
        self.script.write_text(source)
        self.script.chmod(0o755)

    def run_setup(self, *args):
        return subprocess.run([str(self.script), *args], cwd=self.base, env=self.env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)

    def calls(self, command=None):
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return [call for call in calls if call['command'] == command] if command else calls

    def assert_no_temporary_downloads(self):
        self.assertEqual(list((self.checkout / '.pixi' / 'bin').glob('.download.*')), [])

    def test_help_and_obsolete_arguments_do_not_run_any_tools(self):
        for args, code in ((['--help'], 0), (['--python', 'python3.11'], 1),
                           (['--skip-system-packages'], 1), (['--unknown'], 1)):
            with self.subTest(args=args):
                result = self.run_setup(*args)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual(self.calls(), [])
                if args[0] in ('--python', '--skip-system-packages'):
                    self.assertIn('obsolete', result.stderr)
        self.assertFalse((self.checkout / '.pixi').exists())

    def test_verified_download_installs_both_locked_environments_then_configures(self):
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.pixi.read_text(), self.wrapper)
        self.assertTrue(os.access(self.pixi, os.X_OK))
        download = self.calls('curl')[0]['args']
        self.assertEqual(download[0], '-q')
        self.assertIn('--proto-redir', download)
        self.assertEqual(download[-1], 'https://github.com/prefix-dev/pixi/releases/download/v0.75.0/pixi-x86_64-unknown-linux-musl.tar.gz')
        calls = self.calls('pixi')
        expected = ['install', '--no-config', '--manifest-path', str(self.checkout / 'pyproject.toml'), '--locked', '--environment']
        self.assertEqual(calls[0]['args'], [*expected, 'default'])
        self.assertEqual(calls[1]['args'], [*expected, 'align'])
        self.assertEqual(calls[2]['args'], ['run', '--no-config', '--manifest-path',
            str(self.checkout / 'pyproject.toml'), '--environment', 'default', '--offline',
            '--locked', '--no-install', '--executable', 'python', str(self.checkout / 'bin' / 'configure_runtime.py')])
        self.assert_no_temporary_downloads()

    def test_wget_fallback_still_verifies_and_uses_https_only(self):
        (self.mock_bin / 'curl').unlink()
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--https-only', self.calls('wget')[0]['args'])
        self.assertIn('--no-config', self.calls('wget')[0]['args'])
        self.assertEqual(len(self.calls('pixi')), 3)

    def test_existing_verified_binary_reused_without_downloading_or_replacing(self):
        self.make_executable(self.pixi)
        original = self.pixi.stat()
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.calls('curl'))
        self.assertFalse(self.calls('wget'))
        self.assertEqual(self.pixi.stat().st_ino, original.st_ino)
        self.assertEqual(self.pixi.stat().st_mtime_ns, original.st_mtime_ns)

    def test_unexpected_existing_binary_is_not_executed_or_overwritten(self):
        self.make_executable(self.pixi, 'unrecognized local binary')
        result = self.run_setup()
        self.assertEqual(result.returncode, 1)
        self.assertIn('has not been replaced', result.stderr)
        self.assertEqual(self.pixi.read_text(), 'unrecognized local binary')
        self.assertFalse(self.calls('pixi'))
        self.assertFalse(self.calls('curl'))

    def test_download_and_checksum_failures_never_execute_or_leave_a_binary(self):
        for failure in ('download', 'checksum'):
            with self.subTest(failure=failure):
                self.env['FAIL_STAGE'] = 'download' if failure == 'download' else ''
                self.env['CORRUPT_DOWNLOAD'] = '1' if failure == 'checksum' else ''
                result = self.run_setup()
                self.assertEqual(result.returncode, 1)
                self.assertIn('download Pixi' if failure == 'download' else 'archive checksum mismatch', result.stderr)
                self.assertFalse(self.pixi.exists())
                self.assertFalse(self.calls('pixi'))
                self.assert_no_temporary_downloads()

    def test_wrong_or_missing_archive_executable_is_never_installed(self):
        for include_pixi in (True, False):
            with self.subTest(include_pixi=include_pixi):
                self.make_archive(binary='unexpected executable', include_pixi=include_pixi)
                result = self.run_setup()
                self.assertEqual(result.returncode, 1)
                self.assertIn('executable checksum mismatch' if include_pixi else 'Could not unpack', result.stderr)
                self.assertFalse(self.pixi.exists())
                self.assertFalse(self.calls('pixi'))
                self.assert_no_temporary_downloads()

    def test_archive_members_are_not_extracted_as_paths(self):
        escaped = self.base / 'should-not-exist'
        self.make_archive(extra_members=((str(escaped), b'absolute member'),
                                         ('../../should-not-exist', b'traversal member')))
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(escaped.exists())
        self.assertFalse((self.checkout / '.pixi' / 'should-not-exist').exists())
        self.assert_no_temporary_downloads()

    def test_install_failures_stop_before_later_environment_or_configuration(self):
        self.make_executable(self.pixi)
        for stage, expected_calls in (('default', 1), ('align', 2), ('configure', 3)):
            with self.subTest(stage=stage):
                self.env['FAIL_STAGE'] = stage
                result = self.run_setup()
                self.assertEqual(result.returncode, 1)
                self.assertEqual(len(self.calls('pixi')), expected_calls)
                self.assertNotIn('Setup passed', result.stdout)
                self.log.unlink()

    def test_managed_commands_ignore_old_runtime_environment_but_preserve_display_override(self):
        inherited = ('VIRTUAL_ENV', 'PYTHONHOME', 'PYTHONPATH', 'LD_LIBRARY_PATH',
                     'ROAR_ALIGN_PYTHON', 'ROAR_ALIGN_PDK_FIXED', 'ROAR_MANAGED_RUNTIME',
                     'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH')
        self.env.update({key: '/old/runtime' for key in inherited})
        self.env.update(LD_PRELOAD='', ROAR_HOME='/old/checkout', QT_QPA_PLATFORM='xcb')
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        for call in self.calls('pixi'):
            for key in (*inherited, 'LD_PRELOAD'):
                self.assertIsNone(call['environment'][key], key)
            self.assertEqual(call['environment']['ROAR_HOME'], str(self.checkout))
            self.assertEqual(call['environment']['QT_QPA_PLATFORM'], 'xcb')
            self.assertEqual(call['environment']['PYTHONNOUSERSITE'], '1')

    def test_missing_lock_and_unsupported_platform_fail_before_download(self):
        self.env['MOCK_ARCH'] = 'aarch64'
        result = self.run_setup()
        self.assertEqual(result.returncode, 1)
        self.assertIn('Linux x86_64', result.stderr)
        self.env.pop('MOCK_ARCH')
        (self.checkout / 'pixi.lock').unlink()
        result = self.run_setup()
        self.assertEqual(result.returncode, 1)
        self.assertIn('missing pyproject.toml or pixi.lock', result.stderr)
        self.assertFalse(self.calls('curl'))
        self.assertFalse((self.checkout / '.pixi').exists())

    def test_managed_directory_symlink_is_not_followed(self):
        elsewhere = self.base / 'other installation'
        elsewhere.mkdir()
        (self.checkout / '.pixi').symlink_to(elsewhere, target_is_directory=True)
        result = self.run_setup()
        self.assertEqual(result.returncode, 1)
        self.assertIn('is a symlink', result.stderr)
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertFalse(self.calls('curl'))


if __name__ == '__main__':
    unittest.main()
