"""Real SIGINT must wake an idle Qt loop and use the normal close path."""
import importlib.util
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import unittest


CHILD = r'''
import resource
import signal
import sys
from types import SimpleNamespace
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
sys.path.insert(0, sys.argv[1])
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QDialog, QWidget
from roar_api import Session
mode = sys.argv[2]
application = QApplication([])
class Window(QWidget):
    attempts = 0
    editor_window = SimpleNamespace()
    def closeEvent(self, event):
        self.attempts += 1
        if mode == 'cancel' and self.attempts == 1:
            dialog = QDialog(self)
            print('PROMPT', flush=True)
            QTimer.singleShot(300, dialog.reject)
            dialog.exec()
            event.ignore()
            QTimer.singleShot(0, lambda: print('CANCELLED', flush=True))
        else:
            print('CLOSED', flush=True)
            event.accept()
window = Window()
session = Session()
session._ensure = lambda: None
session._qapp, session._app, session._owns_qapp = application, window, True
application.aboutToQuit.connect(lambda: print('CLEANUP', flush=True))
if mode == 'custom':
    signal.signal(signal.SIGINT, signal.SIG_IGN)
previous = signal.getsignal(signal.SIGINT)
def ready():
    if mode == 'custom':
        assert signal.getsignal(signal.SIGINT) == previous
        QTimer.singleShot(100, application.quit)
    print('READY', flush=True)
QTimer.singleShot(0, ready)
code = session.run()
assert signal.getsignal(signal.SIGINT) == previous
print('RESTORED', flush=True)
sys.exit(code)
'''


@unittest.skipUnless(os.name == 'posix' and importlib.util.find_spec('PyQt6'), 'Requires POSIX and Qt')
class InterruptTests(unittest.TestCase):
    def start(self, mode):
        child = subprocess.Popen([sys.executable, '-I', '-B', '-u', '-c', CHILD,
            str(Path(__file__).resolve().parents[3]), mode],
            env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen'},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        def cleanup():
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
        self.addCleanup(cleanup)
        self.wait_line(child, 'READY')
        return child

    def wait_line(self, child, expected):
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(timeout=5), f'Child did not report {expected}')
        self.assertEqual(child.stdout.readline().strip(), expected)

    def assert_closed(self, child):
        output, errors = child.communicate(timeout=3)
        self.assertEqual(child.returncode, 130, output + errors)
        self.assertIn('CLOSED', output)
        self.assertIn('CLEANUP', output)
        self.assertIn('RESTORED', output)
        self.assertNotIn('Traceback', errors)

    def test_interrupt_closes_idle_window_and_runs_cleanup(self):
        child = self.start('idle')
        child.send_signal(signal.SIGINT)
        self.assert_closed(child)

    def test_repeated_interrupts_during_cancelled_close_do_not_force_discard(self):
        child = self.start('cancel')
        child.send_signal(signal.SIGINT)
        self.wait_line(child, 'PROMPT')
        child.send_signal(signal.SIGINT)
        self.wait_line(child, 'CANCELLED')
        self.assertIsNone(child.poll())
        child.send_signal(signal.SIGINT)
        self.assert_closed(child)

    def test_custom_signal_policy_is_preserved(self):
        child = self.start('custom')
        output, errors = child.communicate(timeout=3)
        self.assertEqual(child.returncode, 0, output + errors)
        self.assertIn('CLEANUP', output)
        self.assertIn('RESTORED', output)
