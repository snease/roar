"""Qt controls for isolated, managed ALIGN placement runs."""

from contextlib import contextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import platform
import re
import shutil
import tempfile

from PyQt6.QtCore import QEvent, QProcess, QProcessEnvironment, QSettings, QTimer, QUrl, Qt, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QSpinBox, QTextBrowser, QVBoxLayout, QWidget,
)

from .align_runner import (
    AlignConfig, AlignRunner, PROFILE, PROFILE_ID, align_process_environment,
    managed_installation, project_fingerprint, run_options, validate_ota_project,
)


ACTIVE = {"starting", "generating", "serving", "stopping"}

_TERMINAL_ESCAPE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC, including terminal hyperlinks
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"  # CSI, including ANSI colours
    r"|\x1b[ -/]*[@-Z\\-_]")


def _plain_log(text):
    """Render terminal output as plain text without changing the saved log."""
    text = _TERMINAL_ESCAPE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", text)


def open_align_browser(url, parent=None):
    """Return a desktop-open result or an asynchronous WSL browser helper."""
    if (url.scheme() != "http" or url.host() != "127.0.0.1"
            or not 1 <= url.port() <= 65535 or url.userName() or url.password()):
        return False
    if os.environ.get("WSL_INTEROP") or "microsoft" in platform.release().lower():
        executable = shutil.which("wslview")
        windows = executable is None
        if windows:
            executable = shutil.which("powershell.exe") or next((path for path in (
                "/mnt/c/WINDOWS/System32/WindowsPowerShell/v1.0/powershell.exe",
                "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
                if Path(path).is_file()), None)
        if executable is None:
            return False
        # Explorer can open Documents instead of the URL under WSL. Ask Windows
        # to use the HTTP association; keep the URL out of PowerShell source.
        arguments = (["-NoLogo", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                      "-Command", "$ErrorActionPreference = 'Stop'; "
                      "Start-Process -FilePath ([Console]::In.ReadToEnd())"]
                     if windows else [url.toString(QUrl.ComponentFormattingOption.FullyEncoded)])
        process = QProcess(parent)
        process.setStandardOutputFile(QProcess.nullDevice())
        process.setStandardErrorFile(QProcess.nullDevice())
        process.start(executable, arguments)
        if windows:
            process.write(url.toEncoded())
        process.closeWriteChannel()
        return process
    return QDesktopServices.openUrl(url)


def _label(text=""):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def _probe(parent, config, completed, *, manifest=None):
    """Run the isolated interpreter without blocking the Qt event loop."""
    process = QProcess(parent)
    process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
    environment = QProcessEnvironment()
    for name, value in align_process_environment(config.python).items():
        environment.insert(name, value)
    process.setProcessEnvironment(environment)
    directory = tempfile.TemporaryDirectory(prefix='roar-align-check-')
    process.setWorkingDirectory(directory.name)
    arguments = ['-I', '-u', str(Path(__file__).with_name('_align_probe.py').resolve()),
                 '--backend', str(Path(config.backend_path).expanduser().resolve())]
    if manifest is not None:
        arguments += ['--manifest', str(Path(manifest).expanduser().resolve())]
    finished = False
    timed_out = False
    timer = QTimer(process)
    timer.setSingleShot(True)
    timer.setInterval(30000)
    def finish(*_args):
        nonlocal finished
        if finished:
            return
        finished = True
        timer.stop()
        output = bytes(process.readAllStandardOutput()).decode('utf-8', errors='replace')
        lines = [line[11:] for line in output.splitlines() if line.startswith('ROAR_PROBE ')]
        try:
            result = json.loads(lines[-1]) if lines else {'ok': False, 'message': process.errorString() + '\n' + output[-2500:]}
        except ValueError:
            result = {'ok': False, 'message': 'Invalid installation-check response.\n' + output[-2500:]}
        if timed_out:
            result = {'ok': False, 'message': 'ALIGN check exceeded 30 seconds and was stopped. Check the selected installation and retry.'}
        completed(result)
        directory.cleanup()
        process.deleteLater()
    def timeout():
        nonlocal timed_out
        timed_out = True
        process.kill()
    timer.timeout.connect(timeout)
    process.finished.connect(finish)
    process.errorOccurred.connect(lambda error: finish() if error == QProcess.ProcessError.FailedToStart else None)
    process.destroyed.connect(directory.cleanup)
    # Keep the venv executable path rather than resolving its symlink to the
    # system Python; resolve relative paths before changing the child's cwd.
    process.start(str(Path(config.python).expanduser().absolute()), arguments)
    timer.start()
    return process


def _probe_text(result):
    versions = ', '.join(f'{name.upper() if name == "align" else name.title()} {result[name]}'
                         for name in ('python', 'align', 'dash') if name in result)
    return versions + ('\n' if versions else '') + result.get('message', 'No diagnostic was returned.')


class AlignSettingsDialog(QDialog):
    """Machine paths remain local; only the profile ID belongs to a floorplan."""

    def __init__(self, config, profile, parent=None, *, technologies=(), placement_options=None,
                 managed_config=None, custom_config=None, custom_installation=False):
        super().__init__(parent)
        self.setWindowTitle("ALIGN placement settings")
        self.setMinimumWidth(660)
        self.config = None
        self._probe = None
        self._probe_epoch = 0
        self._managed_config = managed_config
        custom_config = custom_config or config
        self._custom_paths = (custom_config.python, custom_config.backend_path)
        self.custom_installation = managed_config is None or custom_installation
        self.run_options = dict(PROFILE['run_defaults'], lambda_coeff=1.0)
        self.run_options.update(placement_options or {})
        layout = QVBoxLayout(self)
        introduction = (
            'ROAR setup supplies the separate ALIGN installation and matching backend. '
            'Keep Managed by ROAR selected for the supplied tools, or choose Custom installation '
            'to use tools you configured separately.' if managed_config is not None else
            'Physical previews require a separate ALIGN installation. Select its Python executable '
            'and the matching backend folder below, or use Setup help to install ALIGN.')
        layout.addWidget(_label(introduction + ' Floorplan editing and export remain available without ALIGN.'))
        form = QFormLayout()
        self.installation = None
        if managed_config is not None:
            self.installation = QComboBox()
            self.installation.addItem('Managed by ROAR', False)
            self.installation.addItem('Custom installation', True)
            self.installation.setCurrentIndex(int(self.custom_installation))
            form.addRow('ALIGN installation:', self.installation)
        self.technology = QComboBox()
        # Keep the persisted backend ID independent from its library label.
        self.profile = self.technology
        self.technology.addItem("None", None)
        for name in sorted(set(technologies) | {PROFILE["technology"]}, key=str.casefold):
            supported = name == PROFILE["technology"]
            label = name if supported else f"{name} — no ALIGN backend"
            self.technology.addItem(label, PROFILE_ID if supported else f"unavailable:{name}")
            item = self.technology.model().item(self.technology.count() - 1)
            if not supported:
                item.setEnabled(False)
                item.setToolTip(f"{name} is a ROAR sizing library. No ALIGN backend is configured for it.")
        if profile and profile != PROFILE_ID:
            self.technology.addItem(f"Unavailable backend: {profile}", profile)
            self.technology.model().item(self.technology.count() - 1).setEnabled(False)
        self.technology.setCurrentIndex(max(0, self.technology.findData(profile)))
        form.addRow("Layout technology:", self.technology)
        self.python = QLineEdit(str(config.python))
        self.backend = QLineEdit(str(config.backend_path))
        self.run_root = QLineEdit(str(config.run_root))
        self._installation_controls = []
        for title, field, directory in (
                ("ALIGN Python:", self.python, False),
                ("ALIGN backend folder:", self.backend, True),
                ("Placement runs folder:", self.run_root, True)):
            row = QHBoxLayout()
            row.addWidget(field)
            browse = QPushButton("Browse…")
            browse.clicked.connect(lambda _checked=False, field=field, directory=directory: self._browse(field, directory))
            row.addWidget(browse)
            form.addRow(title, row)
            if field is not self.run_root:
                self._installation_controls.extend((field, browse))
        layout.addLayout(form)
        self.option_fields = {}
        for key, title, low, high, tip in (
                ('nvariants', 'Placement candidates:', 1, 100, 'Maximum number of candidates requested; ALIGN may find fewer.'),
                ('placer_sa_iterations', 'Placement effort (iterations):', 1, 1000000, 'More simulated-annealing iterations can improve placement and take longer. The initial quick-preview setting is 100; ALIGN normally uses 10000.'),
                ('seed', 'Random seed:', 0, 2147483647, 'Use the same seed, inputs, and tool versions to reproduce a run.')):
            field = QSpinBox()
            field.setRange(low, high)
            field.setValue(self.run_options[key])
            field.setToolTip(tip)
            self.option_fields[key] = field
            form.addRow(title, field)
        weight = QDoubleSpinBox()
        weight.setRange(0.01, 1000)
        weight.setDecimals(2)
        weight.setValue(self.run_options['lambda_coeff'])
        weight.setToolTip('ALIGN minimizes log(area) + λ × log(estimated wirelength). Larger values favor shorter wires. Default: 1.')
        self.option_fields['lambda_coeff'] = weight
        form.addRow('Wirelength weight (λ):', weight)
        layout.addWidget(_label('Run options are saved with this floorplan. This flow generates placement candidates; it does not route or produce final GDS.'))
        self.python.setToolTip("Python executable from the separate ALIGN environment.")
        self.test_button = QPushButton('Test installation')
        self.test_button.clicked.connect(self.test_installation)
        checks = QHBoxLayout()
        checks.addWidget(self.test_button)
        help_button = QPushButton('Setup help…')
        help_button.clicked.connect(self.show_setup_help)
        checks.addWidget(help_button)
        layout.addLayout(checks)
        self.error = _label()
        self.error.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.error)
        for field in (self.python, self.backend, self.run_root):
            field.textChanged.connect(self._installation_changed)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        if self.installation is not None:
            self.installation.currentIndexChanged.connect(self._installation_mode_changed)
            self._installation_mode_changed()

    def _installation_mode_changed(self):
        if self.custom_installation:
            self._custom_paths = (self.python.text(), self.backend.text())
        self.custom_installation = bool(self.installation.currentData())
        paths = self._custom_paths if self.custom_installation else (
            self._managed_config.python, self._managed_config.backend_path)
        self.python.setText(paths[0])
        self.backend.setText(paths[1])
        for control in self._installation_controls:
            control.setEnabled(self.custom_installation)
        self._installation_changed()

    def _selected_config(self):
        if self._managed_config is not None and not self.custom_installation:
            return replace(self._managed_config, run_root=self.run_root.text().strip())
        return AlignConfig(self.python.text().strip(), self.backend.text().strip(), self.run_root.text().strip())

    def show_setup_help(self):
        dialog = QDialog(self)
        dialog.setWindowTitle('Set up ALIGN')
        dialog.resize(740, 630)
        layout = QVBoxLayout(dialog)
        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        browser.setMarkdown(Path(__file__).with_name('ALIGN_SETUP.md').read_text(encoding='utf-8'))
        layout.addWidget(browser)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()
        dialog.deleteLater()

    def _installation_changed(self):
        self._probe_epoch += 1
        if self.error.text():
            self.error.setText('Installation settings changed. Test installation again.')

    def test_installation(self):
        config = self._selected_config()
        try:
            config.validate()
        except (ValueError, OSError) as error:
            self.error.setText(str(error))
            return
        self.test_button.setEnabled(False)
        self.error.setText('Checking the separate ALIGN installation…')
        epoch = self._probe_epoch
        def completed(result):
            self._probe = None
            self.test_button.setEnabled(True)
            if epoch == self._probe_epoch:
                self.error.setText(_probe_text(result))
        self._probe = _probe(self, config, completed)

    def done(self, result):
        if self._probe is not None:
            self._probe.kill()
        super().done(result)

    def _browse(self, field, directory):
        if directory:
            path = QFileDialog.getExistingDirectory(self, "Choose ALIGN folder", field.text())
        else:
            path, _ = QFileDialog.getOpenFileName(self, "Choose isolated ALIGN Python", field.text(), "All files (*)")
        if path:
            field.setText(path)

    def accept(self):
        try:
            if not self.technology.model().item(self.technology.currentIndex()).isEnabled():
                raise ValueError("No ALIGN backend is configured for the selected technology.")
            config = self._selected_config()
            if self.profile.currentData() is not None:
                config.validate()
        except (ValueError, TypeError, OSError, RuntimeError) as exc:
            self.error.setText(str(exc))
            return
        self.config = config
        self.run_options = {key: field.value() for key, field in self.option_fields.items()}
        super().accept()


class PlacementRunDialog(QDialog):
    def __init__(self, stop, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ALIGN placement")
        self.resize(820, 540)
        self._stop = stop
        self._diagnostics = ''
        layout = QVBoxLayout(self)
        self.status = _label()
        layout.addWidget(self.status)
        self.url = QLineEdit()
        self.url.setReadOnly(True)
        self.url.setPlaceholderText("Viewer URL")
        layout.addWidget(self.url)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log)
        buttons = QHBoxLayout()
        self.stop_button = QPushButton("Cancel generation")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(lambda: self._stop())
        buttons.addWidget(self.stop_button)
        copy_button = QPushButton('Copy diagnostics')
        copy_button.clicked.connect(lambda: QApplication.clipboard().setText(self._diagnostics))
        buttons.addWidget(copy_button)
        buttons.addStretch()
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.close)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

    def update_state(self, state, text, log):
        self.status.setText(text)
        self.url.setText(str(state.url or ""))
        self.stop_button.setEnabled(state.can_stop and state.status != "stopping")
        self.stop_button.setText("Stop ALIGN" if state.status == "serving" else "Cancel generation")
        self.close_button.setText("Stop and close" if state.status in ACTIVE else "Close")
        log = _plain_log(log)
        # The authenticated browser URL is for local use, not support reports.
        diagnostics = f'{text}\nRun folder: {state.run_dir}\nLog: {state.log_path}\n\nDisplayed log tail:\n{log}'
        self._diagnostics = re.sub(r'([?&]token=)[^\s&"<>]+', r'\1[redacted]', diagnostics)
        self._diagnostics = re.sub(r'/roar-align/[0-9a-f]{64}/', '/roar-align/[redacted]/', self._diagnostics)
        if self.log.toPlainText() != log:
            scroll = self.log.verticalScrollBar()
            at_end, position = scroll.value() == scroll.maximum(), scroll.value()
            self.log.setPlainText(log)
            scroll.setValue(scroll.maximum() if at_end else position)

    def closeEvent(self, event):
        self._stop()
        super().closeEvent(event)

    def reject(self):
        self._stop()
        super().reject()


class PlacementPanel(QWidget):
    """Poll the headless runner without importing ALIGN or blocking Qt."""

    previewReady = pyqtSignal(object, object)

    def __init__(self, project_provider, path_provider, profile_setter, parent=None,
                 *, settings=None, runner_factory=AlignRunner,
                 browser_opener=None, config=None, validator=validate_ota_project,
                 technology_provider=None, before_generate=None, run_options_setter=None):
        super().__init__(parent)
        self._project_provider = project_provider
        self._path_provider = path_provider
        self._profile_setter = profile_setter
        self._technology_provider = technology_provider
        self._before_generate = before_generate
        self._run_options_setter = run_options_setter
        self._constraint_probe = None
        self._constraint_directory = None
        self._probe_epoch = 0
        self._checked_fingerprint = None
        self._runner_factory = runner_factory
        self._validator = validator
        self._browser_opener = browser_opener or (lambda url: open_align_browser(url, self))
        self._browser_process = None
        self._settings = settings if settings is not None else QSettings("ROAR", "FloorplanALIGN")
        self._managed_config = (AlignConfig.from_environment()
                                if config is None and os.environ.get('ROAR_MANAGED_RUNTIME') == '1' else None)
        self.config = AlignConfig.from_settings(self._installation_settings(), defaults=config)
        self._runner = None
        self._runner_path = None
        self._managed = False
        self._state = None
        self._view = None
        self._open_when_ready = False
        self._browser_error = ""
        self._input_error = ""
        self._preview_identity = None
        self._preview_data = None
        self._preview_state = None
        self._closing = False
        self._owner = parent
        self._main_window = parent.parentWidget().window() if parent and parent.parentWidget() else None
        if self._main_window is not None and self._main_window is not parent:
            self._main_window.installEventFilter(self)
        if parent is not None:
            parent.destroyed.connect(self.shutdown)
        qapp = QApplication.instance()
        if qapp is not None:
            qapp.aboutToQuit.connect(self.shutdown)
        layout = QVBoxLayout(self)
        self.profile_label = _label()
        layout.addWidget(self.profile_label)
        buttons = QHBoxLayout()
        self.generate_button = QPushButton("Generate physical preview")
        self.generate_button.clicked.connect(lambda: self._guard(self.generate_preview))
        buttons.addWidget(self.generate_button)
        self.open_last_button = QPushButton("Open in ALIGN browser…")
        self.open_last_button.clicked.connect(lambda: self._guard(self.open_last))
        buttons.addWidget(self.open_last_button)
        self.log_button = QPushButton('View last run / log…')
        self.log_button.setEnabled(False)
        self.log_button.clicked.connect(lambda: self._guard(self.show_last_run))
        buttons.addWidget(self.log_button)
        self.settings_button = QPushButton("ALIGN settings…")
        self.settings_button.clicked.connect(lambda: self._guard(self.edit_settings))
        buttons.addWidget(self.settings_button)
        layout.addLayout(buttons)
        self.status = _label()
        layout.addWidget(self.status)
        self.constraint_status = _label()
        self.constraint_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.constraint_status)
        self.last_label = _label()
        layout.addWidget(self.last_label)
        layout.addStretch()
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(250)
        self._poll_timer.timeout.connect(lambda: self._guard(self._poll))
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(150)
        self._refresh_timer.timeout.connect(lambda: self._guard(self.refresh))
        self.generate_button.setEnabled(False)
        self.open_last_button.setEnabled(False)

    def _installation_settings(self):
        return {key: self._settings.value(key) for key in
                ('python', 'backend_path', 'run_root', 'custom_installation') if self._settings.contains(key)}

    def _guard(self, action):
        try:
            action()
        except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
            message = str(exc)
            self.status.setText(message)
            if self._view is not None and self._view.isVisible():
                self._view.status.setText(message)
            self._open_when_ready = False

    def schedule_refresh(self):
        if not self._closing:
            self._refresh_timer.start()

    def _active(self):
        return self._state is not None and self._state.status in ACTIVE

    def _ensure_runner(self):
        path = self._path_provider()
        key = str(path) if path else None
        if self._runner is None or (key != self._runner_path and not self._active()):
            self._runner = self._runner_factory(self.config, project_path=path)
            self._runner_path = key
            self._managed = False
        return self._runner

    def refresh(self):
        try:
            project = self._project_provider()
        except (ValueError, TypeError, KeyError) as exc:
            self._invalidate_check(None)
            self._input_error = str(exc)
            runner = self._ensure_runner()
            self._apply_state(runner.last_run())
            self._refresh_preview(None)
            historical = runner.last_placement()
            self.generate_button.setEnabled(self._active())
            self.open_last_button.setEnabled(historical is not None and not (self._active() and self._state.status != 'serving'))
            self.open_last_button.setToolTip(str(historical.run_dir) if historical else '')
            self.status.setText(str(exc))
            self.last_label.setText("Current edits are invalid. Open last placement uses its captured input." if historical else "")
            self.settings_button.setEnabled(not self._active())
            if self._active():
                self._poll_timer.start()
            return
        self._input_error = ""
        self._invalidate_check(project)
        self.profile_label.setText("Layout technology: " + (
            PROFILE["technology"] if project.align_profile == PROFILE_ID else
            ("Unavailable backend" if project.align_profile else "None")))
        self.profile_label.setToolTip(project.align_profile or "")
        error = ""
        try:
            self._validator(project)
            project.validate()
        except (ValueError, TypeError, KeyError) as exc:
            error = str(exc)
        runner = self._ensure_runner()
        self._apply_state(runner.last_run(project))
        if self._active():
            self._poll_timer.start()
        self.generate_button.setEnabled(self._active() or not error)
        self.generate_button.setText("Show active ALIGN run…" if self._active() else "Generate physical preview")
        self.generate_button.setToolTip(error)
        self.settings_button.setEnabled(not self._active())
        state = runner.last_placement(project)
        self.open_last_button.setEnabled(state is not None and not (self._active() and self._state.status != "serving"))
        self.open_last_button.setToolTip(str(state.run_dir) if state else "")
        self.last_label.setText("Placement out of date. Regenerate to apply changes." if state and state.stale else "")
        if error and not self._active():
            self.status.setText(error)
        elif self._state is None:
            self.status.clear()
        self._refresh_preview(project)

    def _refresh_preview(self, project):
        state = self._ensure_runner().last_preview(project)
        if state is None:
            if self._preview_data is not None:
                self._preview_identity = self._preview_data = self._preview_state = None
                self.previewReady.emit(None, None)
            return
        if project is None:
            # Invalid table text cannot establish that captured geometry is current.
            state = replace(state, stale=True)
        path = Path(state.run_dir) / 'preview.json'
        stat = path.stat()
        identity = (str(path), stat.st_mtime_ns, stat.st_size)
        changed = identity != self._preview_identity
        if changed:
            from .geometry import load_preview
            data = load_preview(path)
            self._preview_data, self._preview_identity = data, identity
        if changed or state != self._preview_state:
            self._preview_state = state
            self.previewReady.emit(self._preview_data, state)

    def _show_view(self):
        if self._view is None:
            self._view = PlacementRunDialog(lambda: self._guard(self.stop), self._owner)
        self._view.show()
        self._view.raise_()
        self._view.activateWindow()

    def show_last_run(self):
        state = self._ensure_runner().last_run()
        if state is not None:
            self._show_view()
            self._apply_state(state)

    def generate_preview(self):
        self.generate(preview=True)

    def generate(self, *, preview=False):
        if self._active():
            self._managed = True
            self._show_view()
            return
        try:
            self.config.validate()
        except (ValueError, OSError, RuntimeError) as error:
            if not self.edit_settings(generation_error=str(error)):
                self.status.setText(
                    'Preview not generated. ' + str(error) +
                    ' Open ALIGN settings to configure the separate installation. '
                    'Floorplan editing and export remain available.')
                return
            if self._project_provider().align_profile is None:
                return  # The user explicitly disabled ALIGN in settings.
            self.config.validate()
        # Capture live sizing only after configuration is accepted, so cancelling
        # setup leaves the authoring document and its undo history untouched.
        if self._before_generate is not None:
            self._before_generate()
        project = self._project_provider()
        self._validator(project)
        project.validate()
        self._closing = False
        self._open_when_ready = not preview
        self._browser_error = ""
        self._show_view()
        runner = self._ensure_runner()
        state = runner.start(project, preview=True) if preview else runner.start(project)
        self._managed = True
        self._apply_state(state)
        self._poll_timer.start()
        self.refresh()

    def open_last(self):
        try:
            project = self._project_provider()
        except (ValueError, TypeError, KeyError) as exc:
            project = None
            self._input_error = str(exc)
        if self._active() and self._state.status != "serving":
            self._show_view()
            return
        self._closing = False
        self._open_when_ready = True
        self._browser_error = ""
        self._show_view()
        state = self._ensure_runner().open_last(project)
        self._managed = True
        self._apply_state(state)
        self._poll_timer.start()
        self.refresh()

    def _apply_state(self, state):
        self._state = state
        self.log_button.setEnabled(state is not None)
        if state is None:
            return
        labels = {"starting": "Starting ALIGN…", "generating": "Generating placement…",
                  "serving": "Placement ready.", "stopping": "Stopping ALIGN…",
                  "stopped": "ALIGN stopped.",
                  "failed": "ALIGN failed.", "completed": "ALIGN finished."}
        text = labels.get(state.status, state.status)
        if state.stale:
            text += " Placement out of date; regenerate to apply changes."
        elif self._input_error:
            text += " Current edits are invalid; showing saved input."
        if state.error:
            text += " " + str(state.error)
        if self._open_when_ready and state.status == "serving" and state.url:
            self._open_when_ready = False
            self._stop_browser_helper()
            try:
                opened = self._browser_opener(QUrl(str(state.url)))
            except (OSError, ValueError, RuntimeError):
                opened = False
            if isinstance(opened, QProcess):
                self._browser_process = opened
            elif not opened:
                self._browser_error = "Could not open the browser. Copy the viewer URL."
        if self._browser_process is not None:
            process = self._browser_process
            if process.state() == QProcess.ProcessState.NotRunning:
                if (process.error() == QProcess.ProcessError.FailedToStart
                        or process.exitStatus() != QProcess.ExitStatus.NormalExit or process.exitCode() != 0):
                    self._browser_error = "Could not open the browser. Copy the viewer URL."
                process.deleteLater()
                self._browser_process = None
        if state.status in ("failed", "stopped"):
            self._open_when_ready = False
        if self._browser_error:
            text += " " + self._browser_error
        self.status.setText(text)
        if self._view is not None:
            self._view.update_state(state, text, self._runner.log_tail())
        if state.status not in ACTIVE and self._browser_process is None:
            self._poll_timer.stop()

    def _poll(self):
        if self._runner is None:
            return
        try:
            project = self._project_provider()
            self._input_error = ""
        except (ValueError, TypeError, KeyError) as exc:
            project = None
            self._input_error = str(exc)
        self._apply_state(self._runner.poll(project))
        self.refresh()

    def stop(self):
        self._open_when_ready = False
        self._stop_browser_helper()
        if self._managed and self._runner is not None and self._active() and self._state.status != "stopping":
            self._apply_state(self._runner.stop())
            self._poll_timer.start()

    def edit_settings(self, *, generation_error=None):
        if self._active():
            self._show_view()
            return False
        project = self._project_provider()
        technologies = self._technology_provider() if self._technology_provider is not None else ()
        settings = self._installation_settings()
        custom_config = AlignConfig.from_settings(settings, defaults=self.config)
        options = AlignSettingsDialog(self.config, project.align_profile, self._owner,
                                      technologies=technologies, placement_options=run_options(project),
                                      managed_config=self._managed_config, custom_config=custom_config,
                                      custom_installation=not managed_installation(settings))
        if generation_error is not None:
            options.error.setText(generation_error)
            options.buttons.button(QDialogButtonBox.StandardButton.Ok).setText('Save and generate')
        accepted = options.exec() == QDialog.DialogCode.Accepted
        if accepted:
            self._invalidate_check(None)
            self._profile_setter(options.profile.currentData())
            if self._run_options_setter is not None:
                self._run_options_setter(options.run_options)
            else:
                project.run_options = dict(options.run_options)
            self.config = options.config
            if self._managed_config is not None:
                self._settings.setValue('custom_installation', options.custom_installation)
            keys = ('python', 'backend_path', 'run_root') if options.custom_installation else ('run_root',)
            for key in keys:
                self._settings.setValue(key, str(getattr(self.config, key)))
            self._settings.sync()
            self._runner = None
            self._state = None
            self.refresh()
        options.deleteLater()
        return accepted

    def check_native_constraints(self, project):
        """Optional native schema/solver check; basic editing needs no ALIGN."""
        if self._constraint_probe is not None:
            return
        self.config.validate()
        directory = tempfile.TemporaryDirectory(prefix='roar-native-constraints-')
        try:
            netlist, constraints = project.export_align(Path(directory.name) / 'input')
            manifest = Path(directory.name) / 'check.json'
            manifest.write_text(json.dumps({'top': project.top, 'netlist': str(netlist),
                'constraints': str(constraints), 'input_dir': str(netlist.parent),
                'constraint_rows': [i + 1 for i, enabled in enumerate(project.to_dict().get('constraint_enabled', [True] * len(project.constraints))) if enabled],
                'active': {'backend_path': self.config.backend_path}}))
        except Exception:
            directory.cleanup()
            raise
        self._constraint_directory = directory
        epoch = self._probe_epoch
        self._checked_fingerprint = project_fingerprint(project)
        self.constraint_status.setText('Checking constraints with native ALIGN…')
        def completed(result):
            self._constraint_probe = None
            try:
                self._invalidate_check(self._project_provider())
            except (ValueError, TypeError, KeyError):
                self._invalidate_check(None)
            if epoch == self._probe_epoch:
                self.constraint_status.setText('Constraint check: ' + _probe_text(result))
            directory.cleanup()
            self._constraint_directory = None
        self._constraint_probe = _probe(self, self.config, completed, manifest=manifest)

    def _invalidate_check(self, project):
        if self._checked_fingerprint is not None and (project is None or
                project_fingerprint(project) != self._checked_fingerprint):
            self._probe_epoch += 1
            self._checked_fingerprint = None
            self.constraint_status.setText('Inputs changed. Run Check constraints again.')

    def shutdown(self, *_args):
        self._closing = True
        self._refresh_timer.stop()
        self._poll_timer.stop()
        self._open_when_ready = False
        self._stop_browser_helper()
        self._probe_epoch += 1
        if self._constraint_probe is not None:
            self._constraint_probe.kill()
        if self._managed and self._runner is not None:
            try:
                self._state = self._runner.shutdown()
            except (ValueError, TypeError, OSError, RuntimeError) as exc:
                self.status.setText("ALIGN cleanup failed: " + str(exc))
        if self._view is not None:
            self._view.hide()

    def _stop_browser_helper(self):
        if self._browser_process is not None:
            process = self._browser_process
            self._browser_process = None
            if process.state() == QProcess.ProcessState.NotRunning:
                process.deleteLater()
            else:
                process.finished.connect(process.deleteLater)
                process.kill()

    def project_changed(self):
        """Changing documents ends the old managed run; Save As uses saved_as."""
        self.shutdown()
        self._checked_fingerprint = None
        self.constraint_status.clear()
        self._runner = None
        self._runner_path = None
        self._state = None
        self._managed = False
        self._browser_error = ""
        self._input_error = ""
        self._preview_identity = self._preview_data = self._preview_state = None
        self.previewReady.emit(None, None)
        self._closing = False
        if self._view is not None:
            self._view.status.setText("No active ALIGN run for this floorplan.")
            self._view.url.clear()
            self._view.log.clear()
            self._view.stop_button.setEnabled(False)

    def saved_as(self, path):
        if self._runner is not None and self._managed:
            self._runner.rebind_project_path(path)
            self._runner_path = str(path) if path else None
        elif not self._active():
            self._runner = None
            self._state = None
        self.schedule_refresh()

    @contextmanager
    def save_as_transaction(self, path):
        runner = self._ensure_runner()
        with runner.save_as_transaction(path):
            yield
        self._runner_path = str(path) if path else None
        self.schedule_refresh()

    def close_panel(self):
        """A retained editor can hide while cancellation finishes asynchronously."""
        self._closing = True
        self._refresh_timer.stop()
        self._guard(self.stop)
        if not self._managed:
            self._poll_timer.stop()
        if self._view is not None:
            self._view.hide()

    def resume(self):
        self._closing = False
        self.schedule_refresh()

    def eventFilter(self, watched, event):
        if (watched is self._main_window and event.type() == QEvent.Type.Close
                and not hasattr(watched, '_confirm_floorplan_navigation')):
            self.shutdown()
        return super().eventFilter(watched, event)
