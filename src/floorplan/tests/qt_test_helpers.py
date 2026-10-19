"""Report scheduled modal interactions through the test runner."""
from contextlib import contextmanager

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication


@contextmanager
def modal_interaction(callback, *, timeout_ms=5000):
    """Run a modal callback, re-raising its errors after the Qt loop exits.

    Owned timers are stopped on every exit, including when an action never
    opens its dialog. A timeout also fails a dialog that remains open after
    the callback (for example because its Accept button became disabled).
    """
    previous = QApplication.activeModalWidget()
    errors = []
    invoked = False

    def fail(error):
        errors.append(error)
        # Unwind any nested dialogs opened by this interaction, without closing
        # a modal that the test had already opened before entering the context.
        seen = set()
        while (modal := QApplication.activeModalWidget()) is not None and modal is not previous:
            if id(modal) in seen:
                break
            seen.add(id(modal))
            modal.reject()

    def invoke():
        nonlocal invoked
        invoked = True
        try:
            callback()
        except BaseException as error:
            fail(error)

    timer, watchdog = QTimer(), QTimer()
    timer.setSingleShot(True)
    watchdog.setSingleShot(True)
    timer.timeout.connect(invoke)
    watchdog.timeout.connect(lambda: fail(AssertionError('Modal interaction did not close its dialog')))
    timer.start(0)
    watchdog.start(timeout_ms)
    try:
        yield
    finally:
        timer.stop()
        watchdog.stop()
        if errors:
            raise errors[0]
    if not invoked:
        raise AssertionError('Scheduled modal interaction did not run')
