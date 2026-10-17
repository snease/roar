"""Placement controls use mocked processes and browser opens, always offscreen."""

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    from PyQt6.QtCore import QSettings, QTimer, QUrl, Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication, QDialog, QLabel, QWidget
except ImportError:
    QApplication = None

from roar_api.floorplan import FloorplanProject
from roar_api.floorplan.align_runner import AlignConfig, PROFILE_ID, RunState, project_fingerprint

if QApplication is not None:
    from roar_api.floorplan.align_dialog import AlignSettingsDialog, PlacementPanel, open_align_browser


class FakeRunner:
    def __init__(self, config, project_path=None):
        self.config, self.project_path = config, project_path
        self.state = self.placement = None
        self.starts = self.opens = self.stops = self.shutdowns = self.rebinds = 0

    def _current(self, state, project):
        return replace(state, stale=bool(project and project_fingerprint(project) != state.fingerprint)) if state else None

    def start(self, project):
        self.starts += 1
        self.state = RunState(str(Path(self.config.run_root) / f"run-{self.starts}"), "starting", None, None,
                              "align.log", project_fingerprint(project), can_stop=True)
        return self.state

    def serve(self):
        self.state = replace(self.state, status="serving", url="http://127.0.0.1:9001/", has_placement=True)
        self.placement = replace(self.state, status="completed", url=None, can_stop=False)

    def last_run(self, project=None):
        return self._current(self.state, project)

    def last_placement(self, project=None):
        return self._current(self.placement, project)

    def open_last(self, project):
        if self.state and self.state.status == "serving":
            return self._current(self.state, project)
        if self.placement is None:
            raise ValueError("No saved placement")
        self.opens += 1
        self.state = replace(self.placement, status="starting", can_stop=True)
        return self._current(self.state, project)

    def poll(self, project=None):
        if self.state and self.state.status == "stopping":
            self.state = replace(self.state, status="stopped", can_stop=False)
        return self._current(self.state, project)

    def stop(self):
        self.stops += 1
        self.state = replace(self.state, status="stopping", url=None, can_stop=False)
        return self.state

    def shutdown(self):
        self.shutdowns += 1
        if self.state:
            self.state = replace(self.state, status="stopped", url=None, can_stop=False)
        return self.state

    def rebind_project_path(self, path):
        self.rebinds += 1
        self.project_path = path

    def log_tail(self, max_chars=12000):
        return "isolated ALIGN log\n"


@unittest.skipIf(QApplication is None, "PyQt6 GUI extra is not installed")
class PlacementPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.path = self.root / "ota.roar.floorplan"
        netlist = ".subckt ota d g s b\n" + "".join(
            f"M{i} d g s b sky130_fd_pr__nfet_01v8 W=2u L=0.15u nf=2\n" for i in range(1, 3)) + ".ends ota\n"
        self.project = FloorplanProject.from_text(netlist)
        self.project.align_profile = PROFILE_ID
        self.owner = QWidget()
        self.editor = QDialog(self.owner)
        self.settings = QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat)
        self.config = AlignConfig("/isolated/python", "/tested/backend", str(self.root / "runs"))
        self.runners, self.opened, self.profiles = [], [], []
        self.technologies = {"SKY130A": {}, "IHP-SG13G2": {}}
        def factory(config, project_path=None):
            runner = FakeRunner(config, project_path)
            self.runners.append(runner)
            return runner
        def set_profile(profile):
            self.profiles.append(profile)
            self.project.align_profile = profile
        self.panel = PlacementPanel(lambda: self.project, lambda: self.path, set_profile, self.editor,
                                    settings=self.settings, config=self.config, runner_factory=factory,
                                    technology_provider=lambda: self.technologies,
                                    browser_opener=lambda url: self.opened.append(url.toString()) or True)
        self.validate = patch.object(AlignConfig, "validate", return_value=None).start()
        self.panel.refresh()

    def tearDown(self):
        self.panel.shutdown()
        self.editor.deleteLater()
        self.owner.deleteLater()
        self.app.processEvents()
        patch.stopall()
        self.directory.cleanup()

    def test_generate_waits_for_verified_readiness_and_reuses_active_run(self):
        self.assertTrue(self.panel.generate_button.isEnabled())
        self.panel.generate()
        runner = self.runners[-1]
        self.assertEqual(runner.starts, 1)
        self.assertEqual(self.opened, [])
        self.panel.generate()
        self.assertEqual(runner.starts, 1)
        runner.serve()
        self.panel._poll()
        self.assertEqual(self.opened, ["http://127.0.0.1:9001/"])
        self.assertEqual(self.panel._view.url.text(), self.opened[0])
        self.assertIn("isolated ALIGN log", self.panel._view.log.toPlainText())
        self.panel._poll()
        self.assertEqual(len(self.opened), 1)

    def test_log_display_removes_terminal_codes_and_preserves_unicode_and_raw_log(self):
        self.panel.generate()
        runner = self.runners[-1]
        raw = ('\x1b[32mwerkzeug INFO\x1b[0m: W=1 µm\r\n'
               '\x1b]8;;http://127.0.0.1:9001/\x1b\\viewer\x1b]8;;\x1b\\\n'
               '\x9b31merror\x9b0m\tπ\x00\x07\n')
        with patch.object(runner, "log_tail", return_value=raw):
            runner.serve()
            self.panel._poll()
            self.assertEqual(self.panel._view.log.toPlainText(),
                             'werkzeug INFO: W=1 µm\nviewer\nerror\tπ\n')
            self.assertEqual(runner.log_tail(), raw)

    def test_stale_last_placement_is_explicit_and_reopening_does_not_generate(self):
        self.panel.generate()
        runner = self.runners[-1]
        runner.serve()
        self.panel._poll()
        self.panel.stop()
        self.panel._poll()
        self.project.set_mos_parameters("M1", width_um=3, length_um=0.15, nf=2)
        self.panel.refresh()
        self.assertIn("out of date", self.panel.last_label.text())
        self.panel.open_last()
        self.assertEqual(runner.starts, 1)
        self.assertEqual(runner.opens, 1)
        self.assertEqual(len(self.opened), 1)
        runner.serve()
        self.panel._poll()
        self.assertEqual(len(self.opened), 2)
        self.assertIn("out of date", self.panel._view.status.text())

    def test_failed_latest_attempt_keeps_previous_saved_placement_available(self):
        self.panel.generate()
        runner = self.runners[-1]
        runner.serve()
        self.panel._poll()
        previous = runner.placement
        runner.state = replace(runner.state, status="failed", run_dir="failed-new-run", url=None,
                               error="Missing primitive", can_stop=False)
        self.panel.refresh()
        self.assertIn("Missing primitive", self.panel.status.text())
        self.assertTrue(self.panel.open_last_button.isEnabled())
        self.assertIn(previous.run_dir, self.panel.open_last_button.toolTip())
        self.assertNotIn("failed-new-run", self.panel.open_last_button.toolTip())

    def test_profile_and_complete_input_gate_generation_and_missing_tools_are_actionable(self):
        self.project.align_profile = None
        self.panel.refresh()
        self.assertFalse(self.panel.generate_button.isEnabled())
        self.assertIn("SKY130", self.panel.status.text())
        self.project.align_profile = PROFILE_ID
        original = self.project.devices[0].model
        self.project.devices[0].model = "unsupported_model"
        self.panel.refresh()
        self.assertFalse(self.panel.generate_button.isEnabled())
        self.project.devices[0].model = original
        self.validate.side_effect = ValueError("Select isolated ALIGN Python")
        self.panel._guard(self.panel.generate)
        self.assertIn("isolated ALIGN Python", self.panel.status.text())
        self.assertEqual(self.opened, [])
        self.assertEqual(self.runners[-1].starts, 0)

    def test_library_technologies_show_backend_eligibility_and_legacy_profile_uses_sky130_label(self):
        self.assertEqual(self.panel.profile_label.text(), "Layout technology: SKY130A")
        self.assertNotIn(PROFILE_ID, self.panel.profile_label.text())
        self.technologies["LocalProcess"] = {}
        def inspect():
            dialog = QApplication.activeModalWidget()
            self.assertEqual(dialog.technology.currentText(), "SKY130A")
            self.assertEqual(dialog.technology.currentData(), PROFILE_ID)
            for name in ("IHP-SG13G2", "LocalProcess"):
                index = dialog.technology.findText(f"{name} — no ALIGN backend")
                self.assertGreaterEqual(index, 0)
                item = dialog.technology.model().item(index)
                self.assertFalse(item.isEnabled())
                self.assertIn("sizing library", item.toolTip())
            labels = " ".join(label.text() for label in dialog.findChildren(QLabel))
            self.assertNotIn("OTA", labels)
            self.assertNotIn("tested", labels.lower())
            dialog.reject()
        QTimer.singleShot(0, inspect)
        self.panel.edit_settings()
        self.assertEqual(self.settings.allKeys(), [])

    def test_supported_technology_remains_available_without_a_library_provider(self):
        dialog = AlignSettingsDialog(self.config, PROFILE_ID)
        self.assertEqual(dialog.technology.currentText(), "SKY130A")
        self.assertEqual(dialog.technology.currentData(), PROFILE_ID)
        dialog.deleteLater()

    def test_choosing_library_technology_persists_compatible_backend_id_and_machine_paths_only(self):
        self.project.align_profile = None
        def choose():
            dialog = QApplication.activeModalWidget()
            dialog.technology.setCurrentIndex(dialog.technology.findText("SKY130A"))
            dialog.accept()
        QTimer.singleShot(0, choose)
        self.panel.edit_settings()
        self.assertEqual(self.profiles, [PROFILE_ID])
        self.assertEqual(self.project.align_profile, PROFILE_ID)
        self.assertEqual(set(self.settings.allKeys()), {"python", "backend_path", "run_root"})
        self.assertEqual(self.panel.profile_label.text(), "Layout technology: SKY130A")

    def test_unsupported_or_unknown_backend_cannot_be_accepted_programmatically(self):
        for profile, technologies, choice in (
                (None, {"IHP-SG13G2": {}}, "unavailable:IHP-SG13G2"),
                ("old-unavailable-profile", (), "old-unavailable-profile")):
            dialog = AlignSettingsDialog(self.config, profile, technologies=technologies)
            dialog.technology.setCurrentIndex(dialog.technology.findData(choice))
            dialog.accept()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
            self.assertIsNone(dialog.config)
            self.assertIn("No ALIGN backend", dialog.error.text())
            dialog.deleteLater()

    def test_before_generate_captures_current_sizes_once_and_failures_prevent_launch(self):
        calls = []
        def update():
            calls.append(True)
            self.project.set_mos_parameters("M1", 3, 0.15, 2)
        self.panel._before_generate = update
        self.panel.generate()
        runner = self.runners[-1]
        self.assertEqual(runner.state.fingerprint, project_fingerprint(self.project))
        self.assertEqual(self.project.devices[0].width_um, 3)
        self.panel.generate()  # Showing the active run must not change its input.
        self.assertEqual(len(calls), 1)
        self.assertEqual(runner.starts, 1)
        self.panel.stop()
        self.panel._poll()
        def fail():
            raise ValueError("ROAR M1.W has no current assigned value")
        self.panel._before_generate = fail
        self.panel._guard(self.panel.generate)
        self.assertEqual(runner.starts, 1)
        self.assertIn("no current assigned value", self.panel.status.text())
        self.assertEqual(self.opened, [])

    def test_cancel_run_window_close_owner_close_and_shutdown_cleanup(self):
        self.panel.generate()
        runner = self.runners[-1]
        self.panel._view.close()
        self.assertEqual(runner.stops, 1)
        self.assertEqual(self.panel._state.status, "stopping")
        self.panel._poll()
        self.assertFalse(self.panel._poll_timer.isActive())
        self.panel.generate()
        self.owner.close()
        self.assertGreaterEqual(runner.shutdowns, 1)
        self.assertFalse(self.panel._poll_timer.isActive())
        self.panel.generate()
        self.panel.shutdown()  # Same slot is connected to QApplication.aboutToQuit.
        self.assertEqual(self.panel._state.status, "stopped")

    def test_project_change_stops_managed_run_and_save_as_rebinds_without_restart(self):
        self.panel.generate()
        runner = self.runners[-1]
        runner.serve()
        self.panel._poll()
        self.path = self.root / "copy.roar.floorplan"
        self.panel.saved_as(self.path)
        self.panel.refresh()
        self.assertIs(self.panel._runner, runner)
        self.assertEqual(runner.rebinds, 1)
        self.assertEqual(runner.starts, 1)
        self.assertEqual(runner.project_path, self.path)
        self.panel.project_changed()
        self.assertGreaterEqual(runner.shutdowns, 1)
        self.assertIsNone(self.panel._runner)
        self.assertEqual(self.panel._view.url.text(), "")
        self.assertEqual(self.panel._view.log.toPlainText(), "")
        self.assertFalse(self.panel._view.stop_button.isEnabled())

    def test_escape_stops_the_managed_run_like_closing_its_window(self):
        self.panel.generate()
        runner = self.runners[-1]
        QTest.keyClick(self.panel._view, Qt.Key.Key_Escape)
        self.assertEqual(runner.stops, 1)
        self.assertEqual(self.panel._state.status, "stopping")
        self.panel._poll()
        self.assertEqual(self.panel._state.status, "stopped")

    def test_source_link_change_is_stale_without_claiming_geometry_changed(self):
        self.panel.generate()
        self.runners[-1].serve()
        self.panel._poll()
        self.project.roar_project_path = str(self.root / "other-source.roar")
        self.panel.refresh()
        self.assertIn("out of date", self.panel.last_label.text())

    def test_observing_existing_run_does_not_take_lifecycle_ownership(self):
        runner = self.runners[-1]
        runner.start(self.project)
        runner.serve()
        self.panel.refresh()
        self.assertFalse(self.panel._managed)
        self.panel.shutdown()
        self.assertEqual(runner.shutdowns, 0)
        self.assertEqual(runner.stops, 0)

    def test_settings_cancel_is_transactional_and_profile_removal_needs_no_toolchain(self):
        original = self.panel.config
        def cancel():
            dialog = QApplication.activeModalWidget()
            dialog.profile.setCurrentIndex(dialog.profile.findData(None))
            dialog.python.setText("/different/python")
            dialog.reject()
        QTimer.singleShot(0, cancel)
        self.panel.edit_settings()
        self.assertEqual(self.panel.config, original)
        self.assertEqual(self.settings.allKeys(), [])
        self.assertEqual(self.profiles, [])
        self.validate.side_effect = ValueError("No toolchain")
        def remove_profile():
            dialog = QApplication.activeModalWidget()
            dialog.profile.setCurrentIndex(dialog.profile.findData(None))
            dialog.accept()
        QTimer.singleShot(0, remove_profile)
        self.panel.edit_settings()
        self.assertEqual(self.profiles, [None])
        self.assertFalse(self.panel.generate_button.isEnabled())
        self.assertEqual(self.settings.value("python"), original.python)

    def test_browser_failure_keeps_ready_url_and_error_visible(self):
        self.panel._browser_opener = lambda _url: False
        self.panel.generate()
        self.runners[-1].serve()
        self.panel._poll()
        self.assertIn("Could not open the browser", self.panel.status.text())
        self.assertEqual(self.panel._view.url.text(), "http://127.0.0.1:9001/")
        self.panel._poll()
        self.assertIn("Could not open the browser", self.panel.status.text())

    def test_browser_exception_keeps_copy_url_error_visible_after_polling(self):
        def fail(_url):
            raise OSError("Browser helper is unavailable")
        self.panel._browser_opener = fail
        self.panel.generate()
        self.runners[-1].serve()
        self.panel._poll()
        self.panel._poll()
        self.assertIn("Could not open the browser", self.panel.status.text())
        self.assertIn("Copy the viewer URL", self.panel._view.status.text())
        self.assertEqual(self.panel._view.url.text(), "http://127.0.0.1:9001/")

    def test_start_failure_is_visible_in_the_open_run_window(self):
        with patch.object(self.runners[-1], "start", side_effect=ValueError("Worker could not start")):
            self.panel._guard(self.panel.generate)
        self.assertIn("Worker could not start", self.panel._view.status.text())
        self.assertFalse(self.panel._view.stop_button.isEnabled())
        self.assertFalse(self.panel._open_when_ready)
        self.assertEqual(self.panel._view.url.text(), "")


@unittest.skipIf(QApplication is None, "PyQt6 GUI extra is not installed")
class BrowserHelperTests(unittest.TestCase):
    def test_wsl_uses_windows_helper_with_one_url_argument_and_no_shell(self):
        with patch("roar_api.floorplan.align_dialog.platform.release", return_value="microsoft-WSL2"), \
             patch("roar_api.floorplan.align_dialog.shutil.which", side_effect=[None, "/mnt/c/WINDOWS/explorer.exe"]), \
             patch("roar_api.floorplan.align_dialog.subprocess.Popen") as launch, \
             patch("roar_api.floorplan.align_dialog.QDesktopServices.openUrl") as desktop:
            self.assertTrue(open_align_browser(QUrl("http://127.0.0.1:9001/")))
        self.assertEqual(launch.call_args.args[0], ["/mnt/c/WINDOWS/explorer.exe", "http://127.0.0.1:9001/"])
        self.assertNotIn("shell", launch.call_args.kwargs)
        desktop.assert_not_called()

    def test_helper_failure_falls_back_to_qt_and_rejects_nonlocal_urls(self):
        with patch("roar_api.floorplan.align_dialog.platform.release", return_value="microsoft-WSL2"), \
             patch("roar_api.floorplan.align_dialog.shutil.which", return_value="/usr/bin/wslview"), \
             patch("roar_api.floorplan.align_dialog.subprocess.Popen", side_effect=OSError("unavailable")), \
             patch("roar_api.floorplan.align_dialog.QDesktopServices.openUrl", return_value=False) as desktop:
            self.assertFalse(open_align_browser(QUrl("http://127.0.0.1:9001/")))
            desktop.assert_called_once()
        with patch("roar_api.floorplan.align_dialog.subprocess.Popen") as launch:
            self.assertFalse(open_align_browser(QUrl("https://example.com/")))
        launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
