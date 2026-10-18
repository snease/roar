"""Offscreen placement gestures and native relationship actions, without ALIGN."""
from copy import deepcopy
import unittest

try:
    from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
    from PyQt6.QtGui import QFocusEvent, QMouseEvent
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication, QDialog, QVBoxLayout
except ImportError:
    QApplication = None

from roar_api.floorplan.model import FloorplanProject

if QApplication is not None:
    from roar_api.floorplan.canvas import SketchPanel


NETLIST = ".subckt pair d g s b\n" + "".join(
    f"M{index} d g s b nmos W=2u L=.5u nf=2\n" for index in range(1, 5)) + ".ends pair\n"


@unittest.skipIf(QApplication is None, "PyQt6 GUI extra is not installed")
class SketchCanvasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.project = FloorplanProject.from_text(NETLIST)
        self.project.set_sketch({"positions": {"M1": [0, 0], "M2": [200, 0],
                                                "M3": [400, 0], "M4": [200, 200]}, "groups": []})
        self.panel = SketchPanel()
        self.panel.resize(1000, 650)
        self.panel.set_project(self.project)
        self.panel.show()
        self.app.processEvents()
        self.panel.view.fit_all()
        self.edits, self.requests = [], []
        self.panel.sketchEdited.connect(lambda state: self.edits.append(deepcopy(state)))
        self.panel.relationshipRequested.connect(lambda record: self.requests.append(deepcopy(record)))

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()
        self.app.processEvents()

    def press(self, name="M1", modifiers=Qt.KeyboardModifier.NoModifier):
        position = self.panel.view.mapFromScene(self.panel.items_by_name[name].pos())
        QTest.mousePress(self.panel.view.viewport(), Qt.MouseButton.LeftButton, modifiers, position)
        self.assertIsNotNone(self.panel.view._gesture)
        return position

    def release(self, position):
        QTest.mouseRelease(self.panel.view.viewport(), Qt.MouseButton.LeftButton, pos=position)

    def pointer_move(self, position, buttons=Qt.MouseButton.LeftButton):
        event = QMouseEvent(QEvent.Type.MouseMove, QPointF(position),
                            QPointF(self.panel.view.viewport().mapToGlobal(position)),
                            Qt.MouseButton.NoButton, buttons, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(self.panel.view.viewport(), event)

    def test_interrupted_pointer_gesture_restores_before_later_hover_or_release(self):
        before = self.project.to_dict()
        positions = self.panel.display_positions()
        self.panel.snap.setChecked(False)
        view = self.panel.view
        for interruption in ("focus", "hide", "deactivate", "buttonless move"):
            with self.subTest(interruption=interruption):
                start = self.press()
                self.pointer_move(start + QPoint(55, 35))
                self.assertNotEqual(self.panel.display_positions(), positions)
                if interruption == "focus":
                    QApplication.sendEvent(view, QFocusEvent(QEvent.Type.FocusOut))
                elif interruption == "hide":
                    view.hide()
                    view.show()
                elif interruption == "deactivate":
                    QApplication.sendEvent(self.panel.window(), QEvent(QEvent.Type.WindowDeactivate))
                else:
                    self.pointer_move(start + QPoint(65, 45), Qt.MouseButton.NoButton)
                self.assertIsNone(view._gesture)
                self.assertFalse(view._guides)
                self.assertEqual(self.panel.display_positions(), positions)
                self.pointer_move(start + QPoint(75, 55), Qt.MouseButton.NoButton)
                self.release(start + QPoint(75, 55))
                self.assertEqual(self.panel.display_positions(), positions)
                self.assertFalse(self.edits)
                self.assertEqual(self.project.to_dict(), before)

    def test_escape_cancels_pointer_gesture_without_rejecting_containing_dialog(self):
        self.panel.hide()
        dialog = QDialog()
        layout = QVBoxLayout(dialog)
        layout.addWidget(self.panel)
        rejected = []
        dialog.rejected.connect(lambda: rejected.append(True))
        dialog.show()
        self.app.processEvents()
        positions = self.panel.display_positions()
        start = self.press()
        self.pointer_move(start + QPoint(55, 35))
        self.assertNotEqual(self.panel.display_positions(), positions)
        QTest.keyClick(self.panel.view, Qt.Key.Key_Escape)
        self.assertTrue(dialog.isVisible())
        self.assertFalse(rejected)
        self.assertIsNone(self.panel.view._gesture)
        self.assertEqual(self.panel.display_positions(), positions)
        self.release(start + QPoint(55, 35))
        self.assertFalse(self.edits)
        self.panel.setParent(None)
        dialog.deleteLater()

    def test_one_commit_per_drag_and_escape_restores_without_document_mutation(self):
        before = self.project.to_dict()
        self.panel.snap.setChecked(False)
        start = self.press()
        QTest.mouseMove(self.panel.view.viewport(), start + self.panel.view.mapFromScene(QPointF(30, 25)) - self.panel.view.mapFromScene(QPointF()))
        self.panel.view.preview_drag(QPointF(61, 38), bypass=True)
        self.assertFalse(self.edits)
        self.assertEqual(self.project.to_dict(), before)
        self.assertAlmostEqual(self.panel.display_positions()["M1"][0], 61)
        self.release(start)
        self.assertEqual(len(self.edits), 1)
        self.assertEqual(self.edits[0]["positions"]["M1"], [61, 38])
        self.assertEqual(self.project.to_dict(), before)

        previous = self.panel.display_positions()
        start = self.press()
        self.panel.view.preview_drag(QPointF(50, -90), bypass=True)
        QTest.keyClick(self.panel.view, Qt.Key.Key_Escape)
        self.release(start)
        self.assertEqual(self.panel.display_positions(), previous)
        self.assertEqual(len(self.edits), 1)
        self.panel.view.fit_all()
        self.panel.grid.setChecked(False)
        self.assertEqual(len(self.edits), 1)
        self.assertEqual(self.project.to_dict(), before)

    def test_selection_and_drawing_group_move_members_and_required_outside_relation(self):
        selected = []
        self.panel.selectionChanged.connect(selected.append)
        self.panel.set_selection(["m1", "M2"])
        self.assertEqual(selected[-1], ["M1", "M2"])
        self.panel.create_group("Input pair")
        state = self.edits.pop()
        self.project.set_sketch(state)
        self.project.set_constraints([{"constraint": "Align", "instances": ["M2", "M3"], "line": "h_center"}])
        self.panel.set_project(self.project)
        self.panel.set_selection(["M1"])
        start = self.press()
        self.panel.view.preview_drag(QPointF(45, 60), bypass=True)
        self.release(start)
        positions = self.edits[-1]["positions"]
        self.assertEqual(positions["M1"], [45, 60])
        self.assertEqual(positions["M2"], [245, 60])
        self.assertEqual(positions["M3"][1], 60)
        self.assertEqual(positions["M4"], [200, 200])
        self.assertEqual(self.project.sketch, state)
        self.panel.ungroup_selected()
        self.assertEqual(self.edits[-1]["groups"], [])

    def test_snap_tolerance_is_in_screen_pixels_and_alt_temporarily_bypasses(self):
        view = self.panel.view
        self.panel.grid.setChecked(False)
        positions = self.panel.display_positions()
        for zoom in (0.5, 1, 2):
            view.resetTransform()
            view.scale(zoom, zoom)
            snapped, _targets = view._snap({"M1": [200 - 6 / zoom, 100]}, positions)
            self.assertAlmostEqual(snapped["M1"][0], 200)
            unsnapped, _targets = view._snap({"M1": [200 - 8 / zoom, 100]}, positions)
            self.assertAlmostEqual(unsnapped["M1"][0], 200 - 8 / zoom)
        view._clear_guides()
        view.resetTransform()
        view.fit_all()
        start = self.press()
        view.preview_drag(QPointF(197, 100))
        self.assertEqual(self.panel.display_positions()["M1"][0], 200)
        view.preview_drag(QPointF(197, 100), bypass=True)
        self.assertEqual(self.panel.display_positions()["M1"][0], 197)
        self.assertFalse(view._guides)
        QTest.keyClick(view, Qt.Key.Key_Escape)
        self.release(start)
        self.assertFalse(self.edits)

    def test_snapping_yields_to_required_order_and_disabled_rules_have_no_effect(self):
        order = {"constraint": "Order", "instances": ["M1", "M2"], "direction": "left_to_right"}
        self.project.set_constraints([order])
        self.panel.set_project(self.project)
        start = self.press()
        self.panel.view.preview_drag(QPointF(198, 0))
        positions = self.panel.display_positions()
        self.assertGreaterEqual(positions["M2"][0] - positions["M1"][0], 120 - 1e-6)
        self.release(start)
        self.project.set_constraint_enabled(0, False)
        self.panel.set_project(self.project)
        self.panel.highlight_constraint(order)
        self.assertIn("Disabled", self.panel.status.text())
        self.assertIn("1 disabled", self.panel.capabilities.text())
        start = self.press()
        self.panel.view.preview_drag(QPointF(200, 0), bypass=True)
        self.assertEqual(self.panel.display_positions()["M1"], [200, 0])
        self.assertEqual(self.panel.display_positions()["M2"], [200, 0])
        self.release(start)

    def test_imported_conflict_preserves_document_and_previous_drawing(self):
        self.project.constraints = [
            {"constraint": "Order", "instances": ["M1", "M2"], "direction": "left_to_right"},
            {"constraint": "Align", "instances": ["M1", "M2"], "line": "v_center"}]
        before = self.project.to_dict()
        self.panel.set_project(self.project)
        positions = self.panel.display_positions()
        self.assertIn("Sketch conflict", self.panel.status.text())
        start = self.press()
        self.panel.view.preview_drag(QPointF(30, 10))
        self.release(start)
        self.assertEqual(self.project.to_dict(), before)
        self.assertEqual(self.panel.display_positions(), positions)
        self.assertFalse(self.edits)
        self.assertIn("M1", self.panel.status.text())

        # Imported future/malformed native fields remain inspectable as plain
        # text, rather than being interpreted as rich text or live geometry.
        unknown = {"constraint": "<b>future</b>", "instances": ["M1", "M2"], "pairs": None}
        self.project.constraints = [unknown]
        self.panel.set_project(self.project)
        self.panel.highlight_constraint(unknown)
        self.assertIn("<b>future</b>", self.panel.status.text())
        self.assertEqual(self.panel.status.textFormat(), Qt.TextFormat.PlainText)
        self.assertIn("unsupported for export", self.panel.capabilities.text())

    def test_explicit_toolbar_actions_emit_valid_native_rules_and_group_is_metadata(self):
        self.panel.set_selection(["M3", "M1", "M2"])
        before = self.project.to_dict()
        self.panel.row_selected()
        self.panel.order_selected()
        self.panel.align_selected()
        self.panel.symmetry_selected()
        self.assertEqual(self.requests[0], {"constraint": "AlignInOrder", "instances": ["M1", "M2", "M3"], "line": "center", "direction": "horizontal"})
        self.assertEqual(self.requests[1]["instances"], ["M1", "M2", "M3"])
        self.project._validate_constraints(self.requests[:3])
        self.assertEqual(self.requests[-1]["action"], "symmetry")
        self.panel.set_selection(["M1", "M2"])
        self.panel.symmetry_selected()
        self.project._validate_constraints([self.requests[-1]])
        self.panel.create_group("Input pair")
        self.assertEqual(self.edits[-1]["groups"][0]["label"], "Input pair")
        self.assertEqual(self.project.to_dict(), before)
        self.assertFalse(self.project.constraints)


if __name__ == "__main__":
    unittest.main()
