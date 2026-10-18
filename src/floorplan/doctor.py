"""Read-only desktop and optional ALIGN diagnostics, without importing Qt here."""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile


_QT_CHECK = r'''
import importlib, json, sys
try:
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
except ImportError:
    pass
try:
    from PyQt6.QtCore import QSettings
    settings = QSettings("ROAR", "FloorplanALIGN")
    print("ROAR_SETTINGS " + json.dumps({key: settings.value(key) for key in
          ("python", "backend_path", "run_root") if settings.contains(key)}), flush=True)
    for module in ("PyQt6.QtWidgets", "PyQt6.Qsci", "pyqtgraph.opengl", "jedi",
                   "colorama", "qdarktheme", "kiwisolver"):
        importlib.import_module(module)
    print("ROAR_IMPORTS ready", flush=True)
    from PyQt6.QtWidgets import QApplication
    application = QApplication([])
    print("ROAR_GUI " + json.dumps({"ok": True, "platform": application.platformName()}), flush=True)
except Exception as error:
    print("ROAR_GUI " + json.dumps({"ok": False, "error": type(error).__name__ + ": " + str(error),
          "missing_module": error.name if isinstance(error, ModuleNotFoundError) else None}), flush=True)
    sys.exit(1)
'''

_UBUNTU_LIBRARIES = (
    "libegl1 libgl1 libfontconfig1 libdbus-1-3 libglib2.0-0 libx11-xcb1 "
    "libxkbcommon-x11-0 libxcb-cursor0 libxcb-icccm4 libxcb-image0 "
    "libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-shape0 "
    "libxcb-sync1 libxcb-xfixes0 libxcb-xkb1")


def _record(output, prefix):
    for line in reversed(output.splitlines()):
        if line.startswith(prefix):
            try:
                result = json.loads(line[len(prefix):])
                return result if isinstance(result, dict) else None
            except ValueError:
                return None
    return None


def _run(command, *, timeout=30):
    # ALIGN writes LOG/ on import. Keep diagnostics out of the user's checkout,
    # settings, environments, and placement history, even when the child aborts.
    with tempfile.TemporaryDirectory(prefix='roar-setup-check-') as directory:
        environment = dict(os.environ)
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


def check_setup(*, output=None):
    """Print actionable checks and return 0 when all configured checks pass."""
    output = output or sys.stdout
    def report(level, message):
        print(f'{level}: {message}', file=output)
    report('PASS', f'Python {platform.python_version()} ({sys.executable})')
    settings = {}
    failed = False
    try:
        child = _run([sys.executable, '-I', '-B', '-c', _QT_CHECK])
        settings = _record(child.stdout, 'ROAR_SETTINGS ') or {}
        gui = _record(child.stdout, 'ROAR_GUI ')
        if gui and gui.get('ok') and child.returncode == 0:
            report('PASS', 'GUI dependencies import successfully.')
            backend = gui.get('platform', 'unknown')
            if backend in ('offscreen', 'minimal'):
                report('WARN', f'Qt initialized with {backend}; desktop display access was not tested. '
                       'Run again in your desktop terminal without this QT_QPA_PLATFORM override.')
            else:
                report('PASS', f'Qt can open the current display ({backend}).')
        else:
            failed = True
            detail = gui.get('error') if gui else (child.stderr.strip() or f'Qt child exited with code {child.returncode}')
            report('FAIL', 'GUI startup: ' + str(detail)[-2500:])
            if gui and gui.get('missing_module'):
                report('FIX', "Activate the ROAR environment and run python -m pip install -e '.[gui]' from the checkout root.")
            display_unavailable = 'could not connect to display' in str(detail).lower()
            if (not gui or '.so' in str(detail)) and not display_unavailable and sys.platform == 'linux':
                report('FIX', 'For Ubuntu 22.04, install the desktop runtime libraries: sudo apt install ' + _UBUNTU_LIBRARIES)
            if 'ROAR_IMPORTS ready' in child.stdout:
                report('FIX', 'Launch from a desktop terminal with a working DISPLAY or WAYLAND_DISPLAY. '
                       'On WSL use WSL2 with WSLg; if Wayland/OpenGL fails, try QT_QPA_PLATFORM=xcb python -m roar_api. '
                       'QT_QPA_PLATFORM=offscreen is only a headless check.')
    except (OSError, subprocess.TimeoutExpired) as error:
        failed = True
        report('FAIL', f'GUI check could not complete: {error}. Check the selected Python and desktop session.')

    python = settings.get('python', os.environ.get('ROAR_ALIGN_PYTHON', ''))
    backend = settings.get('backend_path', os.environ.get('ROAR_ALIGN_PDK_FIXED', ''))
    if not python and not backend:
        report('INFO', 'ALIGN is not configured; it is optional for authoring and export. '
               'For placement, follow ALIGN settings → Setup help.')
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
            child = _run(command)
            result = _record(child.stdout, 'ROAR_PROBE ')
            if result and result.get('ok') and child.returncode == 0:
                report('PASS', f'ALIGN {result.get("align", "unknown")}, Dash {result.get("dash", "unknown")}; backend checked.')
                if result.get('browser_viewer') is False:
                    report('WARN', result.get('message', 'Optional ALIGN browser viewer unavailable.'))
                state = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local' / 'state'))
                runs = settings.get('run_root', str(state / 'roar' / 'align' / 'runs'))
                try:
                    report('PASS', f'Placement storage is writable: {_check_runs(runs)} (no files created).')
                except (ValueError, OSError) as error:
                    failed = True
                    report('FAIL', str(error))
            else:
                failed = True
                detail = result.get('message') if result else (child.stderr.strip() or child.stdout.strip() or f'Worker exited with code {child.returncode}')
                report('FAIL', 'ALIGN: ' + str(detail)[-2500:])
                report('FIX', 'Check the separate Python and pinned SKY130_PDK folder in ALIGN settings → Setup help.')
        except (OSError, subprocess.TimeoutExpired) as error:
            failed = True
            report('FAIL', f'ALIGN check could not complete: {error}. Check the Python path in ALIGN settings.')
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(check_setup())
