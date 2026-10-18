"""Placement controls use mocked processes and browser opens, always offscreen."""

from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

try:
    from PyQt6.QtCore import QProcess, QSettings, QTimer, QUrl, Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication, QDialog, QLabel, QPushButton, QWidget
except ModuleNotFoundError as exc:
    if exc.name != "PyQt6":
        raise
    QApplication = None

from roar_api.floorplan import FloorplanProject
from roar_api.floorplan.align_runner import AlignConfig, AlignRunner, PROFILE_ID, RunSnapshot, RunState, project_fingerprint

if QApplication is not None:
    from qt_test_helpers import modal_interaction
    from roar_api.floorplan.align_dialog import AlignSettingsDialog, PlacementPanel, open_align_browser


class FakeRunner:
    def __init__(self, config, project_path=None):
        self.config, self.project_path = config, project_path
        self.state = self.placement = None
        self.starts = self.opens = self.stops = self.shutdowns = 0

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

    def complete(self):
        self.state = replace(self.state, status="completed", has_placement=True, can_stop=False)
        self.placement = self.state

    def snapshot(self, project=None, *, advance=True):
        if advance and self.state and self.state.status == "stopping":
            self.state = replace(self.state, status="stopped", can_stop=False)
        current = self._current(self.state, project)
        return RunSnapshot(current, self._current(self.placement, project),
                           current if current and current.has_preview else None)

    def last_run(self, project=None):
        return self.snapshot(project).current

    def open_last(self, project):
        if self.state and self.state.status == "serving":
            return self._current(self.state, project)
        if self.placement is None:
            raise ValueError("No saved placement")
        self.opens += 1
        self.state = replace(self.placement, status="starting", can_stop=True)
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

    def _use_managed_panel(self):
        factory = self.panel._runner_factory
        self.panel.shutdown()
        self.panel = PlacementPanel(
            lambda: self.project, lambda: self.path, lambda value: setattr(self.project, 'align_profile', value),
            self.editor, settings=self.settings, runner_factory=factory)
        self.panel.refresh()

    def open_generated_placement(self):
        self.panel.generate_button.click()
        runner = self.runners[-1]
        runner.complete()
        self.panel._poll()
        self.panel.open_last_button.click()
        return runner

    def test_preview_then_browser_waits_for_verified_readiness_and_reuses_active_run(self):
        self.assertTrue(self.panel.generate_button.isEnabled())
        self.panel.generate_button.click()
        runner = self.runners[-1]
        self.assertEqual(runner.starts, 1)
        self.assertEqual(self.opened, [])
        self.panel.generate_button.click()
        self.assertEqual(runner.starts, 1)
        runner.complete()
        self.panel._poll()
        self.assertEqual(self.opened, [])
        self.assertTrue(self.panel.open_last_button.isEnabled())
        self.panel.open_last_button.click()
        self.assertEqual((runner.starts, runner.opens), (1, 1))
        self.assertEqual(self.opened, [])
        runner.serve()
        self.panel._poll()
        self.assertEqual(self.opened, ["http://127.0.0.1:9001/"])
        self.assertEqual(self.panel._view.url.text(), self.opened[0])
        self.assertIn("isolated ALIGN log", self.panel._view.log.toPlainText())
        self.panel._poll()
        self.assertEqual(len(self.opened), 1)

    def test_native_preview_never_opens_browser_and_invalid_edits_stay_stale(self):
        import json
        from roar_api.floorplan.geometry import build_preview
        self.panel.generate_button.click()
        runner = self.runners[-1]
        fixture = Path(__file__).parent / 'fixtures' / 'align098_ota'
        data = build_preview(fixture, json.loads((fixture / 'manifest.json').read_text()))
        directory = Path(runner.state.run_dir)
        directory.mkdir(parents=True)
        (directory / 'preview.json').write_text(json.dumps(data))
        runner.state = replace(runner.state, status='completed', has_preview=True, can_stop=False)
        emissions = []
        self.panel.previewReady.connect(lambda data, state: emissions.append((data, state)))
        self.panel._poll()
        self.assertEqual(self.opened, [])
        self.assertFalse(emissions[-1][1].stale)
        self.project.devices[0].width_um = 2.1
        self.panel.refresh()
        self.assertTrue(emissions[-1][1].stale)
        with patch.object(self.panel, '_project_provider', side_effect=ValueError('Incomplete width')):
            self.panel.refresh()
            self.assertTrue(emissions[-1][1].stale)
        self.project.devices[0].width_um = 2
        self.panel.refresh()
        self.assertFalse(emissions[-1][1].stale)
        self.panel.project_changed()
        self.assertEqual(emissions[-1], (None, None))

    def test_poll_captures_project_and_run_snapshot_once_even_with_invalid_edits(self):
        self.panel.generate_button.click()
        runner = self.runners[-1]
        for invalid in (False, True):
            with self.subTest(invalid=invalid):
                provider = Mock(side_effect=ValueError('Incomplete width')) if invalid else Mock(return_value=self.project)
                with patch.object(self.panel, '_project_provider', provider), \
                        patch.object(runner, 'snapshot', wraps=runner.snapshot) as snapshot:
                    self.panel._poll()
                provider.assert_called_once_with()
                snapshot.assert_called_once_with() if invalid else snapshot.assert_called_once_with(self.project)
                self.assertEqual(self.panel._state.status, 'starting')

    def test_valid_document_remains_editable_with_corrupt_optional_history(self):
        self.project.save(self.path)
        runner = AlignRunner(self.config, self.path)
        index = runner._index(str(self.path))
        index.parent.mkdir(parents=True)
        index.write_text('[]')
        self.panel._runner = runner
        with patch.object(self.panel, '_project_provider', side_effect=lambda: FloorplanProject.load(self.path)):
            self.panel.refresh()
        self.assertTrue(self.panel.generate_button.isEnabled())
        self.assertFalse(self.panel.open_last_button.isEnabled())
        self.assertIsNone(self.panel._state)
        self.assertEqual(index.read_text(), '[]')

    def test_log_display_removes_terminal_codes_and_preserves_unicode(self):
        self.panel.generate_button.click()
        runner = self.runners[-1]
        raw = ('\x1b[32mwerkzeug INFO\x1b[0m: W=1 µm\r\n'
               '\x1b]8;;http://127.0.0.1:9001/\x1b\\viewer\x1b]8;;\x1b\\\n'
               '\x9b31merror\x9b0m\tπ\x00\x07\n')
        with patch.object(runner, "log_tail", return_value=raw):
            self.panel._poll()
            self.assertEqual(self.panel._view.log.toPlainText(),
                             'werkzeug INFO: W=1 µm\nviewer\nerror\tπ\n')

    def test_stale_last_placement_is_explicit_and_reopening_does_not_generate(self):
        self.panel.generate_button.click()
        runner = self.runners[-1]
        runner.complete()
        self.panel._poll()
        self.project.set_mos_parameters("M1", width_um=3, length_um=0.15, nf=2)
        self.panel.refresh()
        self.assertIn("out of date", self.panel.last_label.text())
        self.panel.open_last_button.click()
        self.assertEqual(runner.starts, 1)
        self.assertEqual(runner.opens, 1)
        self.assertEqual(self.opened, [])
        runner.serve()
        self.panel._poll()
        self.assertEqual(len(self.opened), 1)
        self.assertIn("out of date", self.panel._view.status.text())

    def test_failed_latest_attempt_keeps_previous_saved_placement_available(self):
        self.panel.generate_button.click()
        runner = self.runners[-1]
        runner.complete()
        self.panel._poll()
        previous = runner.placement
        runner.state = replace(runner.state, status="failed", run_dir="failed-new-run", url=None,
                               error="Missing primitive", can_stop=False)
        self.panel.refresh()
        self.assertIn("Missing primitive", self.panel.status.text())
        self.assertTrue(self.panel.open_last_button.isEnabled())
        self.assertIn(previous.run_dir, self.panel.open_last_button.toolTip())
        self.assertNotIn("failed-new-run", self.panel.open_last_button.toolTip())

    def test_failed_run_log_can_be_reopened_and_copied_without_rerunning(self):
        self.panel.generate_button.click()
        runner = self.runners[-1]
        runner.state = replace(runner.state, status='failed', error='Cannot place M1', can_stop=False)
        self.panel.refresh()
        self.panel._view.close()
        self.assertFalse(self.panel.open_last_button.isEnabled())
        self.assertTrue(self.panel.log_button.isEnabled())
        secret_path = '/roar-align/' + 'a' * 64 + '/'
        with patch.object(runner, 'log_tail', return_value='Cannot place M1\nhttp://127.0.0.1:9001/?token=secret\nhttp://127.0.0.1:9002' + secret_path):
            self.panel.log_button.click()
        self.assertTrue(self.panel._view.isVisible())
        next(button for button in self.panel._view.findChildren(QPushButton)
             if button.text() == 'Copy diagnostics').click()
        report = QApplication.clipboard().text()
        self.assertIn('Cannot place M1', report)
        self.assertIn(runner.state.run_dir, report)
        self.assertNotIn('secret', report)
        self.assertNotIn('a' * 64, report)
        self.assertIn('/roar-align/[redacted]/', report)
        self.assertEqual((runner.starts, runner.opens, self.opened), (1, 0, []))

    def test_native_check_survives_run_polling_and_is_invalidated_by_edits(self):
        runner = self.open_generated_placement()
        runner.serve()
        self.panel._poll()
        callbacks = []
        def start(parent, config, completed, **kwargs):
            callbacks.append(completed)
            return Mock()
        with patch('roar_api.floorplan.align_dialog._probe', side_effect=start):
            self.panel.check_native_constraints(self.project)
            callbacks[-1]({'ok': False, 'message': 'Rows 2 and 3 conflict'})
            self.panel._poll()
            self.assertIn('Rows 2 and 3 conflict', self.panel.constraint_status.text())
            self.assertIn('Placement ready', self.panel.status.text())
            self.panel.check_native_constraints(self.project)
            self.project.set_constraints([{'constraint': 'NetPriority', 'nets': ['g'], 'weight': 3}])
            self.panel.refresh()
            callbacks[-1]({'ok': True, 'message': 'Native checks passed'})
            self.assertIn('Inputs changed', self.panel.constraint_status.text())
            self.assertNotIn('passed', self.panel.constraint_status.text())

    def test_invalid_draft_can_reopen_captured_placement(self):
        self.panel.generate_button.click()
        runner = self.runners[-1]
        runner.complete()
        self.panel._poll()
        def invalid():
            raise ValueError('M1: enter a numeric width')
        self.panel._project_provider = invalid
        self.panel.refresh()
        self.assertFalse(self.panel.generate_button.isEnabled())
        self.assertTrue(self.panel.open_last_button.isEnabled())
        self.assertIn('captured input', self.panel.last_label.text())
        self.panel.open_last_button.click()
        self.assertEqual(runner.opens, 1)
        self.assertEqual(runner.starts, 1)

    def test_run_options_are_edited_transactionally_and_saved_through_callback(self):
        changed = []
        self.panel._run_options_setter = lambda value: changed.append(value)
        def choose():
            dialog = QApplication.activeModalWidget()
            dialog.option_fields['nvariants'].setValue(8)
            dialog.option_fields['placer_sa_iterations'].setValue(10000)
            dialog.option_fields['seed'].setValue(19)
            dialog.option_fields['lambda_coeff'].setValue(2.5)
            dialog.accept()
        with modal_interaction(choose):
            self.panel.edit_settings()
        self.assertEqual(changed, [{'nvariants': 8, 'placer_sa_iterations': 10000, 'seed': 19, 'lambda_coeff': 2.5}])

    def test_option_defaults_and_widget_boundaries_match_valid_document_values(self):
        from roar_api.floorplan.align_runner import run_options
        from roar_api.floorplan.run_options import validate_run_options
        expected = {'nvariants': (4, 1, 100), 'placer_sa_iterations': (100, 1, 1000000),
                    'seed': (0, 0, 2147483647), 'lambda_coeff': (1.0, 0.01, 1000)}
        dialog = AlignSettingsDialog(self.config, PROFILE_ID)
        self.assertEqual(run_options(self.project), {key: values[0] for key, values in expected.items()})
        for key, (default, low, high) in expected.items():
            with self.subTest(option=key):
                field = dialog.option_fields[key]
                self.assertEqual((field.value(), field.minimum(), field.maximum()), (default, low, high))
                for value in (low, high):
                    field.setValue(value)
                    self.assertEqual(validate_run_options({key: value}), {key: field.value()})
                for value in (low - 1, high + 1, True, '1'):
                    with self.assertRaises(ValueError):
                        validate_run_options({key: value})
        for value in (float('nan'), float('inf'), -float('inf')):
            with self.assertRaises(ValueError):
                validate_run_options({'lambda_coeff': value})
        dialog.deleteLater()

    def test_installation_test_starts_asynchronously_and_displays_versions(self):
        dialog = AlignSettingsDialog(self.config, PROFILE_ID)
        callbacks = []
        def start(parent, config, completed):
            callbacks.append(completed)
            return None
        with patch('roar_api.floorplan.align_dialog._probe', side_effect=start):
            dialog.test_installation()
            self.assertFalse(dialog.test_button.isEnabled())
            self.assertIn('Checking', dialog.error.text())
            callbacks[0]({'ok': True, 'python': '3.10', 'align': '0.9.8', 'dash': '2.18', 'message': 'Installation is ready.'})
        self.assertTrue(dialog.test_button.isEnabled())
        self.assertIn('ALIGN 0.9.8', dialog.error.text())
        self.assertIn('Dash 2.18', dialog.error.text())
        with patch('roar_api.floorplan.align_dialog._probe', side_effect=start):
            dialog.test_installation()
            dialog.python.setText('/different/python')
            callbacks[-1]({'ok': True, 'message': 'Old installation is ready.'})
        self.assertIn('settings changed', dialog.error.text())
        dialog.deleteLater()

    def test_installation_probe_resolves_paths_before_changing_directory(self):
        from roar_api.floorplan.align_dialog import _probe
        from PyQt6.QtCore import QEventLoop
        fake = self.root / 'probe.py'
        fake.write_text('import json, os, sys\n'
                        'from pathlib import Path\n'
                        'backend = Path(sys.argv[sys.argv.index("--backend") + 1])\n'
                        'manifest = Path(sys.argv[sys.argv.index("--manifest") + 1])\n'
                        'print("ROAR_PROBE " + json.dumps({"ok": backend.is_dir() and manifest.is_file(), '
                        '"cwd": os.getcwd(), "prefix": sys.prefix}))\n')
        backend = self.root / 'backend'
        backend.mkdir()
        manifest = self.root / 'run.json'
        manifest.write_text('{}')
        config = AlignConfig(os.path.relpath(sys.executable), os.path.relpath(backend), self.config.run_root)
        real_path = Path
        def script_path(value):
            return fake.with_name('align_dialog.py') if str(value).endswith('align_dialog.py') else real_path(value)
        fake.rename(fake.with_name('_align_probe.py'))
        results = []
        loop = QEventLoop()
        def completed(result):
            results.append(result)
            loop.quit()
        with patch('roar_api.floorplan.align_dialog.Path', side_effect=script_path):
            process = _probe(self.owner, config, completed, manifest=os.path.relpath(manifest))
        timeout = QTimer()
        timeout.setSingleShot(True)
        timeout.timeout.connect(loop.quit)
        timeout.start(5000)
        loop.exec()
        timeout.stop()
        if not results:
            process.kill()
            process.waitForFinished(1000)
        self.assertTrue(results, 'Installation probe did not finish')
        self.assertTrue(results[0]['ok'], results[0])
        self.assertEqual(results[0]['prefix'], sys.prefix)
        self.assertNotEqual(results[0]['cwd'], os.getcwd())
        self.assertFalse(Path(results[0]['cwd']).exists(), 'Probe scratch directory was not cleaned up')

    def test_real_probe_failures_are_safe_to_display_and_clean_up(self):
        from roar_api.floorplan.align_dialog import _probe, _probe_text
        from PyQt6.QtCore import QEventLoop
        cases = (([], 0, 'Invalid installation-check response'),
                 ({'ok': True, 'message': []}, 0, 'message must be text'),
                 ({'ok': True, 'message': 'Ready'}, 7, 'exit code 7'),
                 ({'ok': False, 'message': 'Native backend unavailable'}, 1, 'Native backend unavailable'),
                 (None, 0, 'exceeded 30 seconds'),
                 (None, 0, 'could not start'))
        for record, exitcode, diagnostic in cases:
            with self.subTest(record=record, exitcode=exitcode):
                script = self.root / '_align_probe.py'
                timed_out = diagnostic == 'exceeded 30 seconds'
                script.write_text('import sys, time\n' + ('time.sleep(10)\n' if timed_out else '') +
                                  f'print({("ROAR_PROBE " + json.dumps(record))!r}, flush=True)\n'
                                  f'sys.exit({exitcode})\n')
                config = replace(self.config, python=str(self.root / 'absent-python') if diagnostic == 'could not start' else sys.executable)
                class ProbeTimer(QTimer):
                    def setInterval(self, interval):
                        super().setInterval(20 if timed_out else interval)
                real_path = Path
                def script_path(value):
                    return script.with_name('align_dialog.py') if str(value).endswith('align_dialog.py') else real_path(value)
                results = []
                loop = QEventLoop()
                def completed(result):
                    results.append(result)
                    loop.quit()
                with patch('roar_api.floorplan.align_dialog.Path', side_effect=script_path), \
                     patch('roar_api.floorplan.align_dialog.QTimer', ProbeTimer):
                    process = _probe(self.owner, config, completed)
                scratch = Path(process.workingDirectory())
                timeout = QTimer()
                timeout.setSingleShot(True)
                timeout.timeout.connect(loop.quit)
                timeout.start(5000)
                loop.exec()
                timeout.stop()
                if not results:
                    process.kill()
                    process.waitForFinished(1000)
                self.assertTrue(results, 'Probe did not finish')
                self.assertIs(results[0]['ok'], False)
                self.assertIn(diagnostic, _probe_text(results[0]))
                self.assertNotIn('Ready', _probe_text(results[0]))
                self.assertFalse(scratch.exists())

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
        self.panel.refresh()
        self.validate.side_effect = ValueError("Select isolated ALIGN Python")
        with modal_interaction(lambda: QApplication.activeModalWidget().reject()):
            self.panel.generate_button.click()
        self.assertIn("isolated ALIGN Python", self.panel.status.text())
        self.assertEqual(self.opened, [])
        self.assertEqual(self.runners[-1].starts, 0)

    def test_missing_installation_opens_settings_and_cancel_preserves_the_draft(self):
        original = self.project.to_dict()
        self.panel._before_generate = Mock()
        self.validate.side_effect = ValueError('Select the Python executable in the separate ALIGN environment')
        inspected = []
        def cancel():
            dialog = QApplication.activeModalWidget()
            inspected.append(isinstance(dialog, AlignSettingsDialog))
            labels = ' '.join(label.text() for label in dialog.findChildren(QLabel))
            self.assertIn('separate ALIGN installation', labels)
            self.assertIn('editing and export remain available', labels)
            self.assertIn('Select the Python executable', dialog.error.text())
            dialog.python.setText('/not-yet-confirmed/python')
            dialog.option_fields['nvariants'].setValue(8)
            dialog.profile.setCurrentIndex(dialog.profile.findData(None))
            dialog.reject()
        with modal_interaction(cancel):
            with patch('roar_api.floorplan.align_dialog._probe') as probe:
                self.panel.generate_button.click()
        self.assertEqual(inspected, [True])
        probe.assert_not_called()
        self.panel._before_generate.assert_not_called()
        self.assertEqual(self.project.to_dict(), original)
        self.assertEqual(self.panel.config, self.config)
        self.assertEqual(self.settings.allKeys(), [])
        self.assertEqual(self.profiles, [])
        self.assertEqual(self.runners[-1].starts, 0)
        self.assertIsNone(self.panel._view)
        self.assertIn('editing and export remain available', self.panel.status.text())

    def test_settings_recovery_resumes_preview_with_confirmed_config_and_current_inputs(self):
        self.validate.side_effect = [ValueError('Select ALIGN Python'), None, None]
        captured = []
        def capture():
            captured.append(True)
            self.project.set_mos_parameters('M1', 2.4, 0.15, 2)
        self.panel._before_generate = capture
        def configure():
            dialog = QApplication.activeModalWidget()
            self.assertTrue(any(button.text() == 'Save and generate'
                                for button in dialog.findChildren(QPushButton)))
            dialog.python.setText('/confirmed/python')
            dialog.backend.setText('/confirmed/backend')
            dialog.option_fields['nvariants'].setValue(8)
            dialog.accept()
        with modal_interaction(configure):
            self.panel.generate_button.click()
        runner = self.runners[-1]
        self.assertEqual(captured, [True])
        self.assertEqual(runner.starts, 1)
        self.assertEqual(runner.config.python, '/confirmed/python')
        self.assertEqual(runner.config.backend_path, '/confirmed/backend')
        self.assertEqual(self.settings.value('python'), '/confirmed/python')
        self.assertEqual(self.project.run_options['nvariants'], 8)
        self.assertEqual(self.project.devices[0].width_um, 2.4)
        self.assertEqual(runner.state.fingerprint, project_fingerprint(self.project))
        self.assertEqual(self.opened, [])

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
        with modal_interaction(inspect):
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
        with modal_interaction(choose):
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
            self.project.set_mos_parameters("M1", 2.4, 0.15, 2)
        self.panel._before_generate = update
        self.panel.generate_button.click()
        runner = self.runners[-1]
        self.assertEqual(runner.state.fingerprint, project_fingerprint(self.project))
        self.assertEqual(self.project.devices[0].width_um, 2.4)
        self.panel.generate_button.click()  # Showing the active run must not change its input.
        self.assertEqual(len(calls), 1)
        self.assertEqual(runner.starts, 1)
        self.panel.stop()
        self.panel._poll()
        def fail():
            raise ValueError("ROAR M1.W has no current assigned value")
        self.panel._before_generate = fail
        self.panel.generate_button.click()
        self.assertEqual(runner.starts, 1)
        self.assertIn("no current assigned value", self.panel.status.text())
        self.assertEqual(self.opened, [])

    def test_cancel_run_window_close_owner_close_and_shutdown_cleanup(self):
        self.panel.generate_button.click()
        runner = self.runners[-1]
        self.panel._view.close()
        self.assertEqual(runner.stops, 1)
        self.assertEqual(self.panel._state.status, "stopping")
        self.panel._poll()
        self.assertFalse(self.panel._poll_timer.isActive())
        self.panel.generate_button.click()
        self.owner.close()
        self.assertGreaterEqual(runner.shutdowns, 1)
        self.assertFalse(self.panel._poll_timer.isActive())
        self.panel.generate_button.click()
        self.panel.shutdown()  # Same slot is connected to QApplication.aboutToQuit.
        self.assertEqual(self.panel._state.status, "stopped")

    def test_project_change_stops_managed_run_and_clears_view(self):
        runner = self.open_generated_placement()
        runner.serve()
        self.panel._poll()
        self.panel.project_changed()
        self.assertGreaterEqual(runner.shutdowns, 1)
        self.assertIsNone(self.panel._runner)
        self.assertEqual(self.panel._view.url.text(), "")
        self.assertEqual(self.panel._view.log.toPlainText(), "")
        self.assertFalse(self.panel._view.stop_button.isEnabled())

    def test_escape_stops_the_managed_run_like_closing_its_window(self):
        self.panel.generate_button.click()
        runner = self.runners[-1]
        QTest.keyClick(self.panel._view, Qt.Key.Key_Escape)
        self.assertEqual(runner.stops, 1)
        self.assertEqual(self.panel._state.status, "stopping")
        self.panel._poll()
        self.assertEqual(self.panel._state.status, "stopped")

    def test_source_link_change_keeps_geometry_current_but_size_change_is_stale(self):
        self.panel.generate_button.click()
        self.runners[-1].complete()
        self.panel._poll()
        self.project.roar_project_path = str(self.root / "other-source.roar")
        self.panel.refresh()
        self.assertNotIn("out of date", self.panel.last_label.text())
        self.project.devices[0].width_um *= 2
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
        with modal_interaction(cancel):
            self.panel.edit_settings()
        self.assertEqual(self.panel.config, original)
        self.assertEqual(self.settings.allKeys(), [])
        self.assertEqual(self.profiles, [])
        self.validate.side_effect = ValueError("No toolchain")
        def remove_profile():
            dialog = QApplication.activeModalWidget()
            dialog.profile.setCurrentIndex(dialog.profile.findData(None))
            dialog.accept()
        with modal_interaction(remove_profile):
            self.panel.edit_settings()
        self.assertEqual(self.profiles, [None])
        self.assertFalse(self.panel.generate_button.isEnabled())
        self.assertEqual(self.settings.value("python"), original.python)

    def test_managed_installation_ignores_stale_paths_and_cancel_keeps_custom_values(self):
        self.settings.setValue('python', '/old/python')
        self.settings.setValue('backend_path', '/old/backend')
        self.settings.setValue('run_root', str(self.root / 'chosen-runs'))
        saved = {key: self.settings.value(key) for key in self.settings.allKeys()}
        environment = {'ROAR_MANAGED_RUNTIME': '1', 'ROAR_ALIGN_PYTHON': '/managed/python',
                       'ROAR_ALIGN_PDK_FIXED': '/managed/backend'}
        with patch.dict(os.environ, environment):
            self._use_managed_panel()
            self.assertEqual(self.panel.config.python, '/managed/python')
            self.assertEqual(self.panel.config.backend_path, '/managed/backend')
            self.assertEqual(self.panel.config.run_root, saved['run_root'])
            def inspect_and_cancel():
                dialog = QApplication.activeModalWidget()
                self.assertEqual(dialog.installation.currentText(), 'Managed by ROAR')
                self.assertEqual(dialog.python.text(), '/managed/python')
                self.assertEqual(dialog.backend.text(), '/managed/backend')
                self.assertFalse(dialog.python.isEnabled())
                self.assertFalse(dialog.backend.isEnabled())
                self.assertTrue(dialog.run_root.isEnabled())
                browse = [button for button in dialog.findChildren(QPushButton) if button.text() == 'Browse…']
                self.assertEqual([button.isEnabled() for button in browse], [False, False, True])
                dialog.installation.setCurrentIndex(1)
                self.assertEqual(dialog.python.text(), '/old/python')
                self.assertEqual(dialog.backend.text(), '/old/backend')
                self.assertTrue(dialog.python.isEnabled())
                dialog.python.setText('/edited/custom/python')
                dialog.installation.setCurrentIndex(0)
                self.assertEqual(dialog.python.text(), '/managed/python')
                dialog.installation.setCurrentIndex(1)
                self.assertEqual(dialog.python.text(), '/edited/custom/python')
                dialog.reject()
            with modal_interaction(inspect_and_cancel):
                with patch('roar_api.floorplan.align_dialog._probe') as probe:
                    self.assertFalse(self.panel.edit_settings())
            probe.assert_not_called()
        self.assertEqual({key: self.settings.value(key) for key in self.settings.allKeys()}, saved)
        self.assertEqual(self.panel.config.python, '/managed/python')

    def test_explicit_custom_installation_can_switch_to_managed_and_back_without_losing_paths(self):
        self.settings.setValue('python', '/custom/python')
        self.settings.setValue('backend_path', '/custom/backend')
        self.settings.setValue('custom_installation', 'TrUe')
        environment = {'ROAR_MANAGED_RUNTIME': '1', 'ROAR_ALIGN_PYTHON': '/managed/python',
                       'ROAR_ALIGN_PDK_FIXED': '/managed/backend'}
        with patch.dict(os.environ, environment):
            self._use_managed_panel()
            self.assertEqual(self.panel.config.python, '/custom/python')
            self.assertEqual(self.panel.config.backend_path, '/custom/backend')
            def choose_managed():
                dialog = QApplication.activeModalWidget()
                self.assertEqual(dialog.installation.currentText(), 'Custom installation')
                self.assertTrue(dialog.python.isEnabled())
                dialog.installation.setCurrentIndex(0)
                dialog.run_root.setText(str(self.root / 'moved-runs'))
                dialog.accept()
            with modal_interaction(choose_managed):
                self.assertTrue(self.panel.edit_settings())
            self.assertEqual(self.panel.config.python, '/managed/python')
            self.assertEqual(self.panel.config.run_root, str(self.root / 'moved-runs'))
            self.assertFalse(self.settings.value('custom_installation'))
            self.assertEqual(self.settings.value('python'), '/custom/python')
            self.assertEqual(self.settings.value('backend_path'), '/custom/backend')
            self._use_managed_panel()
            self.assertEqual(self.panel.config.python, '/managed/python')
            def choose_custom():
                dialog = QApplication.activeModalWidget()
                dialog.installation.setCurrentIndex(1)
                self.assertEqual(dialog.python.text(), '/custom/python')
                self.assertEqual(dialog.backend.text(), '/custom/backend')
                dialog.python.setText('/confirmed/custom/python')
                dialog.backend.setText('/confirmed/custom/backend')
                dialog.accept()
            with modal_interaction(choose_custom):
                self.assertTrue(self.panel.edit_settings())
            self.assertEqual(self.panel.config.python, '/confirmed/custom/python')
            self.assertTrue(self.settings.value('custom_installation'))
            self.assertEqual(self.settings.value('python'), '/confirmed/custom/python')
            self.assertEqual(self.settings.value('backend_path'), '/confirmed/custom/backend')

    def test_browser_open_failures_keep_ready_url_and_error_visible(self):
        for outcome in (False, OSError("Browser helper is unavailable")):
            with self.subTest(outcome=outcome):
                self.panel.project_changed()
                opener = Mock(side_effect=[outcome])
                self.panel._browser_opener = opener
                runner = self.open_generated_placement()
                runner.serve()
                self.panel._poll()
                self.panel._poll()
                self.assertIn("Could not open the browser", self.panel.status.text())
                self.assertIn("Copy the viewer URL", self.panel._view.status.text())
                self.assertEqual(self.panel._view.url.text(), "http://127.0.0.1:9001/")
                opener.assert_called_once()

    def test_browser_helper_exit_failure_keeps_ready_url_and_does_not_retry(self):
        process = Mock(spec=QProcess)
        process.state.return_value = QProcess.ProcessState.Running
        process.error.return_value = QProcess.ProcessError.UnknownError
        process.exitStatus.return_value = QProcess.ExitStatus.NormalExit
        process.exitCode.return_value = 1
        opener = Mock(return_value=process)
        self.panel._browser_opener = opener
        self.open_generated_placement()
        self.runners[-1].serve()
        self.panel._poll()
        self.assertNotIn("Could not open the browser", self.panel.status.text())
        process.state.return_value = QProcess.ProcessState.NotRunning
        self.panel._poll()
        self.panel._poll()
        self.assertIn("Copy the viewer URL", self.panel._view.status.text())
        self.assertEqual(self.panel._view.url.text(), "http://127.0.0.1:9001/")
        opener.assert_called_once()
        process.deleteLater.assert_called_once()

    def test_start_failure_is_visible_in_the_open_run_window(self):
        with patch.object(self.runners[-1], "start", side_effect=ValueError("Worker could not start")):
            self.panel.generate_button.click()
        self.assertIn("Worker could not start", self.panel._view.status.text())
        self.assertFalse(self.panel._view.stop_button.isEnabled())
        self.assertFalse(self.panel._open_when_ready)
        self.assertEqual(self.panel._view.url.text(), "")


@unittest.skipIf(QApplication is None, "PyQt6 GUI extra is not installed")
class BrowserHelperTests(unittest.TestCase):
    def test_wsl_uses_windows_http_association_with_url_as_data(self):
        url = QUrl("http://127.0.0.1:9001/?test=a%27%3B%24x&second=2")
        executable = "/mnt/c/WINDOWS/System32/WindowsPowerShell/v1.0/powershell.exe"
        with patch("roar_api.floorplan.align_dialog.platform.release", return_value="microsoft-WSL2"), \
             patch("roar_api.floorplan.align_dialog.shutil.which", side_effect=[None, executable]), \
             patch("roar_api.floorplan.align_dialog.QProcess") as launch, \
             patch("roar_api.floorplan.align_dialog.QDesktopServices.openUrl") as desktop:
            self.assertIs(open_align_browser(url), launch.return_value)
        process = launch.return_value
        process.start.assert_called_once_with(executable, [
            "-NoLogo", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command",
            "$ErrorActionPreference = 'Stop'; Start-Process -FilePath ([Console]::In.ReadToEnd())"])
        process.write.assert_called_once_with(url.toEncoded())
        process.closeWriteChannel.assert_called_once()
        desktop.assert_not_called()

    def test_wsl_uses_wslview_and_reports_missing_helpers(self):
        url = QUrl("http://127.0.0.1:9001/")
        with patch("roar_api.floorplan.align_dialog.platform.release", return_value="microsoft-WSL2"), \
             patch("roar_api.floorplan.align_dialog.shutil.which", return_value="/usr/bin/wslview") as which, \
             patch("roar_api.floorplan.align_dialog.Path.is_file", return_value=False), \
             patch("roar_api.floorplan.align_dialog.QProcess") as launch, \
             patch("roar_api.floorplan.align_dialog.QDesktopServices.openUrl") as desktop:
            self.assertIs(open_align_browser(url), launch.return_value)
            launch.return_value.start.assert_called_once_with("/usr/bin/wslview", [url.toString()])
            launch.return_value.write.assert_not_called()
            launch.reset_mock()
            which.return_value = None
            self.assertFalse(open_align_browser(url))
            launch.assert_not_called()
            desktop.assert_not_called()

    def test_desktop_handoff_and_rejection_of_nonlocal_or_credentialed_urls(self):
        with patch.dict("os.environ", {"WSL_INTEROP": ""}), \
             patch("roar_api.floorplan.align_dialog.platform.release", return_value="Linux"), \
             patch("roar_api.floorplan.align_dialog.QProcess") as launch, \
             patch("roar_api.floorplan.align_dialog.QDesktopServices.openUrl", return_value=True) as desktop:
            url = QUrl("http://127.0.0.1:9001/")
            self.assertTrue(open_align_browser(url))
            desktop.assert_called_once_with(url)
            desktop.reset_mock()
            for value in ("https://example.com/", "file:///tmp/test", "http://127.0.0.1/",
                          "http://user:pass@127.0.0.1:9001/"):
                with self.subTest(url=value):
                    self.assertFalse(open_align_browser(QUrl(value)))
            desktop.assert_not_called()
            launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
