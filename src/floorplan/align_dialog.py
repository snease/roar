"""Qt controls for isolated, managed ALIGN runs of the supported OTA profile."""

import os
from pathlib import Path
import platform
import shutil
import subprocess

from PyQt6.QtCore import QEvent, QSettings, QTimer, QUrl, Qt
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QVBoxLayout, QWidget,
)

from .align_runner import AlignConfig, AlignRunner, PROFILE, PROFILE_ID, validate_ota_project


ACTIVE = {"starting", "generating", "serving", "stopping"}


def open_align_browser(url):
    """Open only a ready loopback viewer URL, including the Windows WSL host."""
    if (url.scheme() != "http" or url.host() != "127.0.0.1"
            or not 1 <= url.port() <= 65535 or url.userName() or url.password()):
        return False
    if os.environ.get("WSL_INTEROP") or "microsoft" in platform.release().lower():
        executable = shutil.which("wslview") or shutil.which("explorer.exe")
        if executable is None:
            executable = next((path for path in (
                "/mnt/c/WINDOWS/explorer.exe", "/mnt/c/Windows/explorer.exe")
                if Path(path).is_file()), None)
        if executable:
            try:
                subprocess.Popen([executable, url.toString()], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
                return True
            except OSError:
                pass
    return QDesktopServices.openUrl(url)


def _label(text=""):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class AlignSettingsDialog(QDialog):
    """Machine paths remain local; only the profile ID belongs to a floorplan."""

    def __init__(self, config, profile, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ALIGN placement settings")
        self.setMinimumWidth(660)
        self.config = None
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.profile = QComboBox()
        self.profile.addItem("No placement profile", None)
        self.profile.addItem(f"SKY130 OTA — {PROFILE_ID}", PROFILE_ID)
        if profile and profile != PROFILE_ID:
            self.profile.addItem(f"Unavailable profile: {profile}", profile)
        self.profile.setCurrentIndex(max(0, self.profile.findData(profile)))
        form.addRow("Floorplan profile:", self.profile)
        self.python = QLineEdit(str(config.python))
        self.backend = QLineEdit(str(config.backend_path))
        self.run_root = QLineEdit(str(config.run_root))
        for title, field, directory in (
                ("Isolated ALIGN Python:", self.python, False),
                ("Tested backend folder:", self.backend, True),
                ("Placement runs folder:", self.run_root, True)):
            row = QHBoxLayout()
            row.addWidget(field)
            browse = QPushButton("Browse…")
            browse.clicked.connect(lambda _checked=False, field=field, directory=directory: self._browse(field, directory))
            row.addWidget(browse)
            form.addRow(title, row)
        layout.addLayout(form)
        layout.addWidget(_label(
            "Only the SKY130 OTA profile is supported. Python, backend and run folders "
            "are saved on this machine; the floorplan stores only its profile ID. "
            "ALIGN runs separately from ROAR."))
        defaults = PROFILE["run_defaults"]
        layout.addWidget(_label(
            f"Profile defaults: seed {defaults['seed']}, {defaults['placer_sa_iterations']} placement iterations, "
            f"{defaults['nvariants']} alternatives."))
        self.error = _label()
        layout.addWidget(self.error)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def _browse(self, field, directory):
        if directory:
            path = QFileDialog.getExistingDirectory(self, "Choose ALIGN folder", field.text())
        else:
            path, _ = QFileDialog.getOpenFileName(self, "Choose isolated ALIGN Python", field.text(), "All files (*)")
        if path:
            field.setText(path)

    def accept(self):
        try:
            config = AlignConfig(python=self.python.text().strip(),
                                 backend_path=self.backend.text().strip(),
                                 run_root=self.run_root.text().strip())
            if self.profile.currentData() is not None:
                config.validate()
        except (ValueError, TypeError, OSError, RuntimeError) as exc:
            self.error.setText(str(exc))
            return
        self.config = config
        super().accept()


class PlacementRunDialog(QDialog):
    def __init__(self, stop, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ALIGN placement")
        self.resize(820, 540)
        self._stop = stop
        layout = QVBoxLayout(self)
        self.status = _label()
        layout.addWidget(self.status)
        self.url = QLineEdit()
        self.url.setReadOnly(True)
        self.url.setPlaceholderText("Browser address appears after ALIGN is ready")
        layout.addWidget(self.url)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log)
        layout.addWidget(_label("Closing this window stops the managed ALIGN process. Run files are kept; completed placements can be reopened."))
        buttons = QHBoxLayout()
        self.stop_button = QPushButton("Cancel generation")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(lambda: self._stop())
        buttons.addWidget(self.stop_button)
        buttons.addStretch()
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        buttons.addWidget(close)
        layout.addLayout(buttons)

    def update_state(self, state, text, log):
        self.status.setText(text)
        self.url.setText(str(state.url or ""))
        self.stop_button.setEnabled(state.can_stop and state.status != "stopping")
        self.stop_button.setText("Stop ALIGN" if state.status == "serving" else "Cancel generation")
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

    def __init__(self, project_provider, path_provider, profile_setter, parent=None,
                 *, settings=None, runner_factory=AlignRunner,
                 browser_opener=None, config=None, validator=validate_ota_project):
        super().__init__(parent)
        self._project_provider = project_provider
        self._path_provider = path_provider
        self._profile_setter = profile_setter
        self._runner_factory = runner_factory
        self._validator = validator
        self._browser_opener = browser_opener or open_align_browser
        self._settings = settings if settings is not None else QSettings("ROAR", "FloorplanALIGN")
        defaults = config or AlignConfig.from_environment()
        self.config = AlignConfig(
            python=str(self._settings.value("python", defaults.python)),
            backend_path=str(self._settings.value("backend_path", defaults.backend_path)),
            run_root=str(self._settings.value("run_root", defaults.run_root)))
        self._runner = None
        self._runner_path = None
        self._managed = False
        self._state = None
        self._view = None
        self._open_when_ready = False
        self._browser_error = ""
        self._input_error = ""
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
        layout.addWidget(_label(
            "Generate placement with the tested OTA profile, then inspect it in ALIGN's "
            "browser viewer. ROAR stays available while ALIGN runs. Saved runs use the "
            "floorplan input captured when generation started."))
        buttons = QHBoxLayout()
        self.generate_button = QPushButton("Generate placement and open ALIGN")
        self.generate_button.clicked.connect(lambda: self._guard(self.generate))
        buttons.addWidget(self.generate_button)
        self.open_last_button = QPushButton("Open last placement")
        self.open_last_button.clicked.connect(lambda: self._guard(self.open_last))
        buttons.addWidget(self.open_last_button)
        self.settings_button = QPushButton("ALIGN settings…")
        self.settings_button.clicked.connect(lambda: self._guard(self.edit_settings))
        buttons.addWidget(self.settings_button)
        layout.addLayout(buttons)
        self.status = _label("Open an OTA floorplan to generate placement.")
        layout.addWidget(self.status)
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

    def _guard(self, action):
        try:
            action()
        except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
            message = str(exc) + " Check ALIGN settings and the selected floorplan profile."
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
            self._input_error = str(exc)
            self.generate_button.setEnabled(self._active())
            self.open_last_button.setEnabled(False)
            self.status.setText(str(exc))
            self.last_label.setText("Current edits cannot be validated. Existing placement uses the captured run input.")
            return
        self._input_error = ""
        self.profile_label.setText("Placement profile: " + (
            PROFILE["label"] if project.align_profile == PROFILE_ID else
            project.align_profile or "None — select the OTA profile in ALIGN settings"))
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
        self.generate_button.setText("Show active ALIGN run…" if self._active() else "Generate placement and open ALIGN")
        self.generate_button.setToolTip(error)
        self.settings_button.setEnabled(not self._active())
        state = runner.last_placement(project)
        self.open_last_button.setEnabled(state is not None and not (self._active() and self._state.status != "serving"))
        if state is None:
            self.last_label.setText("No saved placement for this floorplan.")
        else:
            self.last_label.setText(
                ("Floorplan changed since this placement. Open last shows the saved run; regenerate to apply current input.\n" if state.stale else "Saved placement matches the current floorplan input.\n")
                + str(state.run_dir))
        if error and not self._active():
            self.status.setText(error)
        elif self._state is None:
            self.status.setText("Ready to generate placement with the selected OTA profile.")

    def _show_view(self):
        if self._view is None:
            self._view = PlacementRunDialog(lambda: self._guard(self.stop), self._owner)
        self._view.show()
        self._view.raise_()
        self._view.activateWindow()

    def generate(self):
        if self._active():
            self._managed = True
            self._show_view()
            return
        project = self._project_provider()
        self._validator(project)
        project.validate()
        self.config.validate()
        self._closing = False
        self._open_when_ready = True
        self._browser_error = ""
        self._show_view()
        state = self._ensure_runner().start(project)
        self._managed = True
        self._apply_state(state)
        self._poll_timer.start()
        self.refresh()

    def open_last(self):
        project = self._project_provider()
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
        if state is None:
            return
        labels = {"starting": "Starting isolated ALIGN…", "generating": "Generating placement…",
                  "serving": "ALIGN placement is ready.", "stopping": "Stopping ALIGN…",
                  "stopped": "ALIGN stopped. Run files are kept.",
                  "failed": "ALIGN failed.", "completed": "ALIGN process completed; run files are saved."}
        text = labels.get(state.status, state.status)
        if state.stale:
            text += " This run uses saved input that differs from the current floorplan; current edits are not applied."
        elif self._input_error:
            text += " Current edits cannot be validated; this run displays its captured input."
        if state.error:
            text += " " + str(state.error)
        if self._open_when_ready and state.status == "serving" and state.url:
            self._open_when_ready = False
            try:
                opened = self._browser_opener(QUrl(str(state.url)))
            except (OSError, ValueError, RuntimeError):
                opened = False
            if not opened:
                self._browser_error = "Could not open the browser. Copy the ready address from this window."
        if state.status in ("failed", "stopped"):
            self._open_when_ready = False
        if self._browser_error:
            text += " " + self._browser_error
        self.status.setText(text)
        if self._view is not None:
            self._view.update_state(state, text, self._runner.log_tail())
        if state.status not in ACTIVE:
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
        if self._managed and self._runner is not None and self._active() and self._state.status != "stopping":
            self._apply_state(self._runner.stop())
            self._poll_timer.start()

    def edit_settings(self):
        if self._active():
            self._show_view()
            return
        project = self._project_provider()
        options = AlignSettingsDialog(self.config, project.align_profile, self._owner)
        if options.exec() == QDialog.DialogCode.Accepted:
            self._profile_setter(options.profile.currentData())
            self.config = options.config
            for key in ("python", "backend_path", "run_root"):
                self._settings.setValue(key, str(getattr(self.config, key)))
            self._settings.sync()
            self._runner = None
            self._state = None
            self.refresh()
        options.deleteLater()

    def shutdown(self, *_args):
        self._closing = True
        self._refresh_timer.stop()
        self._poll_timer.stop()
        self._open_when_ready = False
        if self._managed and self._runner is not None:
            try:
                self._state = self._runner.shutdown()
            except (ValueError, TypeError, OSError, RuntimeError) as exc:
                self.status.setText("ALIGN cleanup failed: " + str(exc))
        if self._view is not None:
            self._view.hide()

    def project_changed(self):
        """Changing documents ends the old managed run; Save As uses saved_as."""
        self.shutdown()
        self._runner = None
        self._runner_path = None
        self._state = None
        self._managed = False
        self._browser_error = ""
        self._input_error = ""
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
        if watched is self._main_window and event.type() == QEvent.Type.Close:
            self.shutdown()
        return super().eventFilter(watched, event)
