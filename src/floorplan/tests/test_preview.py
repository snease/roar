"""Native physical inspection with real captured geometry, without ALIGN."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

try:
    from PyQt6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt
    from PyQt6.QtGui import QFocusEvent, QImage, QMouseEvent, QPainter
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication, QGraphicsItem, QTabWidget, QWidget
except ModuleNotFoundError as exc:
    if exc.name != "PyQt6":
        raise
    QApplication = None

from roar_api.floorplan.geometry import build_preview

if QApplication is not None:
    from roar_api.floorplan.preview import PreviewPanel


FIXTURE = Path(__file__).parent / 'fixtures' / 'align098_ota'


@unittest.skipIf(QApplication is None, 'PyQt6 GUI extra is not installed')
class PreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.captured = build_preview(FIXTURE, json.loads((FIXTURE / 'manifest.json').read_text()))

    def setUp(self):
        self.data = deepcopy(self.captured)
        self.panel = PreviewPanel()
        self.panel.resize(470, 790)
        self.panel.show()
        self.app.processEvents()
        self.panel.set_preview(self.data)
        self.app.processEvents()

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()
        self.app.processEvents()

    def test_capture_physical_bounds_mirrored_pins_and_only_selected_net(self):
        panel = self.panel
        self.assertEqual(panel.content_bounds(), QRectF(0, -13.44, 25.8, 13.44))
        self.assertIn('25.8 µm', panel.dimensions.text())
        self.assertIn('13.44 µm', panel.dimensions.text())
        screen_bounds = panel.view.mapFromScene(panel.content_bounds()).boundingRect()
        self.assertLessEqual(screen_bounds.bottom(), panel.view.viewport().height() - 53)
        self.assertIsNone(panel.net_combo.currentData())
        self.assertFalse(panel.net_lines)
        # Adapter has already applied the native mirrored transform. Qt must
        # only invert y once, rather than applying the native transform again.
        block = panel.items_by_id['X_M1'].block
        self.assertEqual(block['transform']['sX'], -1)
        panel.net_combo.setCurrentIndex(panel.net_combo.findData('VINN'))
        self.assertFalse(panel.net_lines)  # One physical terminal: no invented edge.
        self.assertEqual(len(panel._net_items), 1)
        pin = next(p for p in block['pins'] if p['net'] == 'VINN')
        self.assertEqual(panel._net_items[0].rect(), QRectF(pin['bbox'][0], -pin['bbox'][3],
                                                         pin['bbox'][2] - pin['bbox'][0],
                                                         pin['bbox'][3] - pin['bbox'][1]))
        net = next(n for n in panel.current_candidate()['nets']
                   if len({tuple(c['position']) for c in n['connections'] if c['kind'] == 'pin'}) > 1)
        panel.net_combo.setCurrentIndex(panel.net_combo.findData(net['name']))
        expected = [QPointF(c['position'][0], -c['position'][1]) for c in net['connections'] if c['kind'] == 'pin']
        self.assertTrue(panel.net_lines)
        for item in panel.net_lines:
            self.assertIn(item.line().p1(), expected)
            self.assertIn(item.line().p2(), expected)
        self.assertIn('not routing', panel.status.text())
        panel.net_combo.setCurrentIndex(0)
        self.assertFalse(panel._net_items)

    def test_roles_preserve_geometry_and_source_selection(self):
        before = deepcopy(self.panel.data)
        self.panel.set_device_labels({'M1': 'diffpair <inn>'})
        self.panel.set_selection(['M1'])
        self.assertEqual(self.panel.data, before)
        self.assertEqual(self.panel.selected_names(), ['M1'])
        self.assertIn('M1: diffpair <inn>', self.panel.details.toPlainText())
        self.assertIn('diffpair &lt;inn&gt;', self.panel.items_by_id['X_M1'].toolTip())

    def test_narrow_block_labels_fit_and_keep_full_role_in_tooltip(self):
        role = 'differential input pair negative reference'
        self.panel.set_device_labels({'M1': role})
        item = self.panel.items_by_id['X_M1']
        image = QImage(100, 100, QImage.Format.Format_ARGB32)
        draws = []

        class RecordingPainter(QPainter):
            def drawText(self, rect, flags, text):
                draws.append((rect, self.fontMetrics().boundingRect(rect.toRect(), flags, text), text))
                super().drawText(rect, flags, text)

        painter = RecordingPainter(image)
        try:
            painter.scale(42 / item.rect.width(), 42 / item.rect.height())
            painter.translate(-item.rect.left(), -item.rect.top())
            item.paint(painter, None)
        finally:
            painter.end()
        self.assertEqual(len(draws), 1)
        available, rendered, text = draws[0]
        self.assertIn('M1', text)
        self.assertIn('…', text)
        self.assertTrue(available.adjusted(-1, -1, 1, 1).contains(QRectF(rendered)))
        self.assertIn(role, item.toolTip())

    def test_provenance_keeps_all_lines_when_panel_is_short_or_narrow(self):
        self.panel.clear()
        self.app.processEvents()
        self.data['run_id'] = 'current_mirror_ota_captured_for_layout_handoff'
        self.data['provenance']['created_at'] = '2026-10-18T12:13:14Z'
        self.panel.set_preview(self.data)
        # A newly completed run can be painted in this event, before Qt's next
        # queued layout pass. Its two-line provenance must already fit.
        label = self.panel.provenance
        self.assertGreaterEqual(label.height(), label.heightForWidth(label.width()))
        for width in (510, 320):
            with self.subTest(width=width):
                self.panel.resize(width, 550)
                self.app.processEvents()
                label = self.panel.provenance
                self.assertGreaterEqual(label.height(), label.heightForWidth(label.width()))
                self.assertIn('2026-10-18T12:13:14Z', label.text())

    def test_candidate_switch_handles_nonzero_origin_and_unmapped_sources(self):
        other = deepcopy(self.data['candidates'][0])
        other['name'] = 'OTHER_CANDIDATE'
        other['bbox'] = [100, 200, 125.8, 213.44]
        for block in other['blocks']:
            for bbox in [block['bbox']] + [pin['bbox'] for pin in block['pins']]:
                bbox[:] = [bbox[0] + 100, bbox[1] + 200, bbox[2] + 100, bbox[3] + 200]
            for pin in block['pins']:
                pin['position'] = [pin['position'][0] + 100, pin['position'][1] + 200]
        for net in other['nets']:
            for conn in net['connections']:
                conn['position'] = [conn['position'][0] + 100, conn['position'][1] + 200]
                if 'bbox' in conn:
                    bbox = conn['bbox']
                    conn['bbox'] = [bbox[0] + 100, bbox[1] + 200, bbox[2] + 100, bbox[3] + 200]
        unknown = other['blocks'][0]
        missing = unknown['source_instances']
        unknown['source_instances'] = []
        unknown['mapping_status'] = 'incomplete'
        unknown['mapping_reason'] = 'No compiler membership record.'
        other['mapping'] = {'status': 'incomplete', 'unmapped_source_instances': missing}
        self.data['candidates'].append(other)
        self.panel.set_preview(self.data)
        self.panel.candidate_combo.setCurrentIndex(1)
        bounds = self.panel.content_bounds()
        self.assertAlmostEqual(bounds.left(), 100)
        self.assertAlmostEqual(bounds.top(), -213.44)
        self.assertAlmostEqual(bounds.width(), 25.8)
        self.assertAlmostEqual(bounds.height(), 13.44)
        self.assertIn('Incomplete source mapping', self.panel.status.text())
        self.panel.set_selection(missing)
        self.assertFalse(self.panel.selected_names())
        self.panel.items_by_id[unknown['id']].setSelected(True)
        self.assertIn('Unknown', self.panel.details.toPlainText())
        self.assertIn('No compiler membership record.', self.panel.details.toPlainText())
        self.assertIn(unknown['concrete_name'], self.panel.details.toPlainText())

    def test_selection_groups_readonly_drag_and_signals(self):
        first, second = self.data['candidates'][0]['blocks'][:2]
        first['source_instances'].extend(second['source_instances'])
        self.data['candidates'][0]['blocks'].remove(second)
        for net in self.data['candidates'][0]['nets']:
            net['connections'] = [c for c in net['connections'] if c['block_id'] != second['id']]
        self.panel.set_preview(self.data)
        emitted = []
        self.panel.selectionChanged.connect(emitted.append)
        self.panel.set_selection([first['source_instances'][0].lower()])
        self.assertEqual(emitted[-1], first['source_instances'])
        self.assertEqual(self.panel.selected_names(), first['source_instances'])
        self.assertIn(', '.join(first['source_instances']), self.panel.details.toPlainText())
        item = self.panel.items_by_id[first['id']]
        self.assertFalse(item.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsMovable)
        before = deepcopy(self.data)
        before_rect = item.sceneBoundingRect()
        point = self.panel.view.mapFromScene(before_rect.center())
        QTest.mousePress(self.panel.view.viewport(), Qt.MouseButton.LeftButton, pos=point)
        QTest.mouseMove(self.panel.view.viewport(), point + QPoint(40, 20))
        QTest.mouseRelease(self.panel.view.viewport(), Qt.MouseButton.LeftButton, pos=point + QPoint(40, 20))
        self.assertEqual(item.sceneBoundingRect(), before_rect)
        self.assertEqual(self.data, before)
        self.assertEqual(self.panel.data, before)

    def test_stale_state_preserves_candidate_selection_net_and_camera(self):
        self.data['candidates'].append(deepcopy(self.data['candidates'][0]))
        self.data['candidates'][1]['name'] = 'SECOND'
        self.data['provenance']['created_at'] = '2026-10-18T12:13:14Z'
        self.panel.set_preview(self.data, SimpleNamespace(stale=False, run_dir='/captured/run'))
        self.panel.candidate_combo.setCurrentIndex(1)
        self.panel.set_selection(['M1'])
        self.panel.net_combo.setCurrentIndex(self.panel.net_combo.findData('VINN'))
        self.panel.view.scale(1.7, 1.7)
        transform = self.panel.view.transform()
        scroll = (self.panel.view.horizontalScrollBar().value(), self.panel.view.verticalScrollBar().value())
        block = self.panel.items_by_id['X_M1']
        self.panel.set_preview(deepcopy(self.data), SimpleNamespace(stale=True, run_dir='/captured/run'))
        self.assertEqual(self.panel.candidate_combo.currentText(), 'SECOND')
        self.assertEqual(self.panel.selected_names(), ['M1'])
        self.assertEqual(self.panel.net_combo.currentData(), 'VINN')
        self.assertEqual(self.panel.view.transform(), transform)
        self.assertEqual((self.panel.view.horizontalScrollBar().value(), self.panel.view.verticalScrollBar().value()), scroll)
        self.assertIs(self.panel.items_by_id['X_M1'], block)
        self.assertFalse(self.panel.stale_badge.isHidden())
        self.assertIn('Earlier inputs', self.panel.stale_badge.text())
        self.assertIn('SKY130A', self.panel.provenance.text())
        self.assertIn('2026-10-18T12:13:14Z', self.panel.provenance.text())
        self.assertIn('/captured/run', self.panel.provenance.toolTip())
        self.panel.set_preview(self.data, SimpleNamespace(stale=False))
        self.assertTrue(self.panel.stale_badge.isHidden())

    def test_missing_pin_geometry_is_not_drawn_and_empty_actions_are_explicit(self):
        net = self.data['candidates'][0]['nets'][0]
        net['connections'] = [{'block_id': 'X_M1', 'pin': 'unknown', 'kind': 'block', 'position': [0, 0]}]
        self.panel.set_preview(self.data)
        self.panel.net_combo.setCurrentIndex(self.panel.net_combo.findData(net['name']))
        self.assertFalse(self.panel._net_items)
        self.assertIn('no pin geometry', self.panel.status.text())
        self.panel.clear()
        signals = []
        self.panel.generationRequested.connect(lambda: signals.append('generate'))
        self.panel.settingsRequested.connect(lambda: signals.append('settings'))
        QTest.mouseClick(self.panel.generate_button, Qt.MouseButton.LeftButton)
        QTest.mouseClick(self.panel.settings_button, Qt.MouseButton.LeftButton)
        self.assertEqual(signals, ['generate', 'settings'])
        self.assertFalse(self.panel.candidate_combo.isEnabled())
        self.assertFalse(self.panel.items_by_id)

    def test_hidden_tab_fits_on_first_show_and_retains_camera_on_return(self):
        tabs = QTabWidget()
        self.addCleanup(tabs.deleteLater)
        tabs.addTab(QWidget(), 'Sketch')
        panel = PreviewPanel()
        tabs.addTab(panel, 'Physical')
        tabs.resize(1100, 850)
        tabs.show()
        self.app.processEvents()
        panel.set_preview(self.data)
        tabs.setCurrentIndex(1)
        self.app.processEvents()
        visible = panel.view.mapToScene(panel.view.viewport().rect()).boundingRect()
        self.assertTrue(visible.contains(panel.content_bounds()))
        rendered = panel.view.mapFromScene(panel.content_bounds()).boundingRect()
        self.assertGreater(rendered.width(), panel.view.viewport().width() / 2)
        panel.view.scale(1.7, 1.7)
        transform = panel.view.transform()
        scroll = (panel.view.horizontalScrollBar().value(), panel.view.verticalScrollBar().value())
        tabs.setCurrentIndex(0)
        panel.set_preview(deepcopy(self.data), SimpleNamespace(stale=True))
        tabs.setCurrentIndex(1)
        self.app.processEvents()
        self.assertEqual(panel.view.transform(), transform)
        self.assertEqual((panel.view.horizontalScrollBar().value(), panel.view.verticalScrollBar().value()), scroll)
        tabs.close()

    def test_interrupted_pan_does_not_resume_on_hover(self):
        view = self.panel.view
        view.scale(3, 3)

        def move(point, buttons):
            event = QMouseEvent(QEvent.Type.MouseMove, QPointF(point),
                                QPointF(view.viewport().mapToGlobal(point)),
                                Qt.MouseButton.NoButton, buttons, Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(view.viewport(), event)

        for interruption in ('focus', 'hide', 'deactivate', 'buttonless move'):
            with self.subTest(interruption=interruption):
                start = view.viewport().rect().center()
                QTest.mousePress(view.viewport(), Qt.MouseButton.MiddleButton, pos=start)
                move(start + QPoint(20, 15), Qt.MouseButton.MiddleButton)
                self.assertIsNotNone(view._pan)
                if interruption == 'focus':
                    QApplication.sendEvent(view, QFocusEvent(QEvent.Type.FocusOut))
                elif interruption == 'hide':
                    view.hide()
                    view.show()
                elif interruption == 'deactivate':
                    QApplication.sendEvent(self.panel.window(), QEvent(QEvent.Type.WindowDeactivate))
                else:
                    move(start + QPoint(30, 25), Qt.MouseButton.NoButton)
                self.assertIsNone(view._pan)
                self.assertNotEqual(view.cursor().shape(), Qt.CursorShape.ClosedHandCursor)
                scroll = (view.horizontalScrollBar().value(), view.verticalScrollBar().value())
                move(start + QPoint(50, 35), Qt.MouseButton.NoButton)
                QTest.mouseRelease(view.viewport(), Qt.MouseButton.MiddleButton, pos=start + QPoint(50, 35))
                self.assertEqual((view.horizontalScrollBar().value(), view.verticalScrollBar().value()), scroll)


if __name__ == '__main__':
    unittest.main()
