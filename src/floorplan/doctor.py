"""Read-only desktop and optional ALIGN diagnostics, without importing Qt here."""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

from .probe_result import parse_probe_result

_QT_CHECK = r'''
import importlib, json, sys
try:
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
except ImportError:
    pass
try:
    if '--gui-only' not in sys.argv:
        from PyQt6.QtCore import QSettings
        settings = QSettings("ROAR", "FloorplanALIGN")
        print("ROAR_SETTINGS " + json.dumps({key: settings.value(key) for key in
              ("python", "backend_path", "run_root", "custom_installation") if settings.contains(key)}), flush=True)
    for module in ("PyQt6.QtWidgets", "PyQt6.QtOpenGLWidgets", "PyQt6.Qsci", "pyqtgraph.opengl", "jedi",
                   "colorama", "qdarktheme", "kiwisolver"):
        importlib.import_module(module)
    print("ROAR_IMPORTS ready", flush=True)
    from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout
    from PyQt6.QtOpenGLWidgets import QOpenGLWidget
    from PyQt6.QtCore import QTimer
    application = QApplication([])
    backend = application.platformName()
    # QApplication alone can succeed even when the first GL widget makes Qt
    # abort. Publish the selected backend before exercising that native path.
    print("ROAR_PLATFORM " + json.dumps({"platform": backend}), flush=True)
    if backend in ('offscreen', 'minimal'):
        result = {"ok": True, "platform": backend, "opengl": False}
    else:
        from PyQt6.QtGui import QOpenGLContext
        context = QOpenGLContext()
        if not context.create():
            raise RuntimeError('Qt cannot create an OpenGL context on ' + backend + '.')
        window = QWidget()
        window.setWindowTitle('ROAR graphics check')
        layout = QVBoxLayout(window)
        view = QOpenGLWidget()
        layout.addWidget(view)
        window.resize(240, 160)
        result = {"ok": False, "platform": backend,
                  "error": "OpenGL widget did not render a frame within 5 seconds."}
        def rendered():
            try:
                if view.isValid() and not view.grabFramebuffer().isNull():
                    result.update(ok=True, opengl=True)
                    result.pop('error', None)
            except Exception as error:
                result['error'] = 'OpenGL framebuffer check: ' + str(error)
            application.quit()
        # Test window composition as well as context creation, then read back
        # the widget's framebuffer. A hidden/offscreen surface misses QRhi failures.
        view.frameSwapped.connect(lambda: QTimer.singleShot(0, rendered))
        QTimer.singleShot(5000, application.quit)
        window.show()
        application.exec()
        window.close()
    print("ROAR_GUI " + json.dumps(result), flush=True)
    sys.exit(0 if result['ok'] else 1)
except Exception as error:
    print("ROAR_GUI " + json.dumps({"ok": False, "error": type(error).__name__ + ": " + str(error),
          "missing_module": error.name if isinstance(error, ModuleNotFoundError) else None}), flush=True)
    sys.exit(1)
'''

def _source_checkout():
    root = Path(__file__).resolve().parents[2]
    return root if (root / 'pyproject.toml').is_file() and (root / 'bin/setup_roar.sh').is_file() else None


def _gui_install_advice():
    root = _source_checkout()
    if root is not None:
        return f"Activate the ROAR environment and run python3 -m pip install -e '.[gui]' from the checkout root ({root})."
    return "In the ROAR Python environment, install the desktop extra: python3 -m pip install --upgrade 'roar-cad[gui]'."


def _record(output, prefix):
    for line in reversed(output.splitlines()):
        if line.startswith(prefix):
            try:
                result = json.loads(line[len(prefix):])
                return result if isinstance(result, dict) else None
            except ValueError:
                return None
    return None


def _run(command, *, timeout=30, environment=None):
    # ALIGN writes LOG/ on import. Keep diagnostics out of the user's checkout,
    # settings, environments, and placement history, even when the child aborts.
    with tempfile.TemporaryDirectory(prefix='roar-setup-check-') as directory:
        environment = dict(os.environ) if environment is None else environment.copy()
        for name in ('PYTHONPATH', 'PYTHONHOME', 'ALIGN_HOME'):
            environment.pop(name, None)
        return subprocess.run(command, cwd=directory, env=environment,
                              capture_output=True, text=True, errors='replace', timeout=timeout)


def _check_runs(path):
    """Inspect an existing directory or its nearest parent without creating it."""
    if not path:
        raise ValueError('Choose a placement runs folder in ALIGN settings.')
    root = Path(path).expanduser().absolute()
    parent = root
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
        raise ValueError(f'Cannot write to placement runs folder {root}. Choose a writable folder in ALIGN settings.')
    return root


def check_setup(*, output=None, gui_only=False, quiet=False, prepare_launch=False):
    """Return 0 on success; quiet suppresses routine success messages.

    Qt renders in a child so a missing platform plugin, display or OpenGL
    context cannot abort the launcher. A failed default Wayland backend may
    fall back to verified X11. Only prepare_launch applies that choice to this
    process; diagnostic checks do not change the environment or preferences.
    """
    output = output or sys.stdout
    def report(level, message):
        if not quiet or level in ('FAIL', 'FIX') or (prepare_launch and level == 'INFO'):
            print(f'{level}: {message}'.replace('\u2192', '->'), file=output)
    report('PASS', f'Python {platform.python_version()} ({sys.executable})')
    settings = {}
    managed = os.environ.get('ROAR_MANAGED_RUNTIME') == '1'
    failed = False
    try:
        command = [sys.executable, '-I', '-B', '-c', _QT_CHECK]
        if gui_only:
            command.append('--gui-only')
        child = _run(command)
        settings = _record(child.stdout, 'ROAR_SETTINGS ') or {}
        gui = _record(child.stdout, 'ROAR_GUI ')
        backend = (_record(child.stdout, 'ROAR_PLATFORM ') or {}).get('platform', '')
        fallback = False
        if (sys.platform == 'linux' and backend.startswith('wayland')
                and not os.environ.get('QT_QPA_PLATFORM') and os.environ.get('DISPLAY')
                and (not gui or not gui.get('ok') or child.returncode != 0)):
            fallback = True
            child = _run(command, environment={**os.environ, 'QT_QPA_PLATFORM': 'xcb'})
            gui = _record(child.stdout, 'ROAR_GUI ')
        if gui and gui.get('ok') and child.returncode == 0:
            report('PASS', 'GUI dependencies import successfully.')
            backend = gui.get('platform', 'unknown')
            if fallback:
                report('INFO', 'Using X11 (xcb): the default Wayland backend failed the OpenGL rendering check.')
                if prepare_launch:
                    os.environ['QT_QPA_PLATFORM'] = 'xcb'
            if backend in ('offscreen', 'minimal'):
                report('WARN', f'Qt initialized with {backend}; desktop display access was not tested. '
                       'OpenGL rendering was not tested. Run again in your desktop terminal without this QT_QPA_PLATFORM override.')
            else:
                report('PASS', f'Qt can render an OpenGL widget on the current display ({backend}).')
        else:
            failed = True
            if fallback:
                report('FAIL', 'Wayland failed its OpenGL check; the X11 fallback also failed.')
            detail = gui.get('error') if gui else (child.stderr.strip() or f'Qt child exited with code {child.returncode}')
            # Qt emits several paragraphs before aborting. Keep the useful
            # cause visible without repeating its generic crash instructions.
            lines = [line.strip() for line in str(detail).splitlines() if line.strip()
                     and not line.startswith(('Available platform plugins are:',
                                              'This application failed to start'))]
            display_lines = [line for line in lines if 'could not connect to display' in line.lower()]
            if display_lines:
                # Qt also suggests xcb-cursor0 when the plugin merely cannot
                # reach the display; avoid recommending the wrong repair.
                lines = display_lines
            report('FAIL', 'GUI startup: ' + ' '.join(lines[:3])[:1200])
            graphics_failure = bool(_record(child.stdout, 'ROAR_PLATFORM '))
            if managed and not display_lines and not graphics_failure:
                report('FIX', 'Rerun ./bin/setup_roar.sh from the checkout to repair the managed runtime.')
            if not managed and gui and gui.get('missing_module'):
                report('FIX', _gui_install_advice())
            display_unavailable = 'could not connect to display' in str(detail).lower()
            unknown_platform = 'could not find the qt platform plugin' in str(detail).lower()
            native_failure = '.so' in str(detail) or 'could not load the qt platform plugin' in str(detail).lower()
            if not managed and native_failure and not display_unavailable and not unknown_platform and not graphics_failure and sys.platform == 'linux':
                report('FIX', 'Check the reported native libraries against your Linux distribution\'s Qt/OpenGL desktop packages; '
                       'install any missing shared libraries or ask your CAD administrator.')
            if 'ROAR_IMPORTS ready' in child.stdout:
                if unknown_platform:
                    report('FIX', 'Remove the QT_QPA_PLATFORM override or select an installed Qt platform plugin.')
                report('FIX', 'Launch from a desktop terminal with a working DISPLAY or WAYLAND_DISPLAY. '
                       + ('On WSL use WSL2 with WSLg. ' if os.environ.get('WSL_INTEROP') or 'microsoft' in platform.release().lower() else '') +
                       'If Wayland/OpenGL fails, try QT_QPA_PLATFORM=xcb ' +
                       ('./bin/roar. ' if managed else 'python3 -m roar_api. ') +
                       'QT_QPA_PLATFORM=offscreen is only a headless check.')
    except subprocess.TimeoutExpired as error:
        failed = True
        report('FAIL', f'GUI check timed out after {error.timeout} seconds. Check your desktop session and retry --check-gui.')
    except OSError as error:
        failed = True
        report('FAIL', f'GUI check could not complete: {error}. Check the selected Python and desktop session.')

    if gui_only:
        return int(failed)

    from .align_runner import AlignConfig, align_process_environment, managed_installation
    config = AlignConfig.from_settings(settings)
    managed_align = managed_installation(settings)
    python, backend = config.python, config.backend_path
    if not python and not backend:
        report('INFO', 'ALIGN is not configured; it is optional for authoring and export. '
               'For placement, follow ALIGN settings -> Setup help.')
    elif not python or not backend:
        failed = True
        report('FAIL', 'ALIGN configuration is incomplete. Set both ALIGN Python and ALIGN backend folder in ALIGN settings, '
               'or ROAR_ALIGN_PYTHON and ROAR_ALIGN_PDK_FIXED.')
    elif sys.platform != 'linux':
        failed = True
        report('FAIL', 'Configured ALIGN placement requires Linux or WSL; authoring and export remain available.')
    else:
        command = [str(Path(python).expanduser().absolute()), '-I', '-B', '-u',
                   str(Path(__file__).with_name('_align_probe.py').resolve()),
                   '--backend', str(Path(backend).expanduser().resolve())]
        try:
            child = _run(command, environment=align_process_environment(python))
            result = parse_probe_result(child.stdout, stderr=child.stderr, returncode=child.returncode)
            if result['ok']:
                report('PASS', f'ALIGN {result.get("align", "unknown")}, Dash {result.get("dash", "unknown")}; backend checked.')
                if result.get('browser_viewer') is False:
                    report('WARN', result.get('message', 'Optional ALIGN browser viewer unavailable.'))
                runs = config.run_root
                try:
                    report('PASS', f'Placement storage is writable: {_check_runs(runs)} (no files created).')
                except (ValueError, OSError) as error:
                    failed = True
                    report('FAIL', str(error))
            else:
                failed = True
                report('FAIL', 'ALIGN: ' + result['message'])
                report('FIX', 'Rerun ./bin/setup_roar.sh to repair the managed ALIGN installation.' if managed_align else
                       'Check the separate Python and pinned SKY130_PDK folder in ALIGN settings -> Setup help.')
        except (OSError, subprocess.TimeoutExpired) as error:
            failed = True
            report('FAIL', f'ALIGN check could not complete: {error}.')
            report('FIX', 'Rerun ./bin/setup_roar.sh to repair the managed ALIGN installation.' if managed_align else
                   'Check the Python path in ALIGN settings.')
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(check_setup())
