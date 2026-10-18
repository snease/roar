"""Desktop acceptance cannot pass through a headless Qt fallback."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]


class RuntimeCheckTests(unittest.TestCase):
    @unittest.skipUnless(sys.version_info[:2] == (3, 11), 'Runtime GUI check requires Python 3.11')
    def test_headless_platform_fallbacks_fail_before_attempting_a_display(self):
        for platform in ('offscreen', 'offscreen;xcb', 'xcb;minimal', 'vnc:port=5901;xcb',
                         'OFFSCREEN', 'XCB;MINIMAL'):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                result = subprocess.run(
                    [sys.executable, '-I', '-B', str(ROOT / 'bin/check_runtime.py'),
                     'gui', '--output', directory],
                    env={**os.environ, 'QT_QPA_PLATFORM': platform},
                    capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn('requires a visible desktop', result.stderr)
                receipt, = Path(directory).glob('*/receipt.json')
                self.assertEqual(json.loads(receipt.read_text())['status'], 'FAIL')
                self.assertFalse(receipt.with_name('check.log').exists())
