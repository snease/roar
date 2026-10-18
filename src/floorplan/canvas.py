"""Native Qt sketch of placement intent; gestures never modify the circuit.

The owner applies emitted document edits and supplies the resulting project again.
Only explicit relationship actions produce native ALIGN constraint dictionaries.
"""
from __future__ import annotations

from copy import deepcopy
from html import escape
import math
from uuid import uuid4

from PyQt6.QtCore import QEvent, QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QGraphicsItem, QGraphicsScene, QGraphicsView,
    QHBoxLayout, QInputDialog, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from .sketch import (BLOCK_HEIGHT, BLOCK_WIDTH, SketchConflict, capability_note,
                     sketch_capability, solve_positions)
from .constraint_editor import constraint_summary


def _participants(constraint):
    names = constraint.get("instances", [])
    names = [name for name in names if isinstance(name, str)] if isinstance(names, list) else []
    for key in ("pairs", "regions"):
        groups = constraint.get(key, [])
        if isinstance(groups, list):
            names.extend(name for group in groups if isinstance(group, list)
                         for name in group if isinstance(name, str))
    return list(dict.fromkeys(names))


class _Block(QGraphicsItem):
    def __init__(self, device, role=""):
        super().__init__()
        self.name = device.name
        self.role = role
        self.rule_count = 0
        self.detail = ""
        if device.kind == "mos" and device.width_um is not None and device.length_um is not None:
            self.detail = f"W {device.width_um:g} / L {device.length_um:g} µm"
            if device.nf is not None:
                self.detail += f" · NF {device.nf}"
        self.highlighted = False
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.base_tip = f"{device.name}" + (f" · {role}" if role else "")
        self.base_tip += f"\n{device.model}\n{self.detail}\nFixed sketch box; sizing is edited in Devices."

    def boundingRect(self):
        return QRectF(-BLOCK_WIDTH / 2, -BLOCK_HEIGHT / 2, BLOCK_WIDTH, BLOCK_HEIGHT)

    def paint(self, painter, option, widget=None):
        selected = self.isSelected()
        fill = QColor("#d9eafa") if selected else QColor("#f2f5f7")
        edge = QColor("#1565a0") if selected else QColor("#657887")
        if self.highlighted:
            edge = QColor("#9a5800")
        painter.setPen(QPen(edge, 2.5 if selected or self.highlighted else 1.2))
        painter.setBrush(fill)
        painter.drawRoundedRect(self.boundingRect().adjusted(1, 1, -1, -1), 5, 5)
        painter.setPen(QColor("#192d3c"))
        font = painter.font()
        font.setBold(True)
        painter.setFont(font)
        rect = self.boundingRect().adjusted(5, 6, -5, -6)
        painter.drawText(rect.adjusted(0, -12 if self.role else 0, 0, 0), Qt.AlignmentFlag.AlignCenter, self.name)
        if self.role or self.detail and painter.worldTransform().m11() >= 0.8:
            font.setBold(False)
            font.setPointSizeF(max(7, font.pointSizeF() - 2))
            painter.setFont(font)
            text = self.role or self.detail
            text = painter.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, int(rect.width()))
            painter.drawText(rect.adjusted(0, 26, 0, 0), Qt.AlignmentFlag.AlignHCenter, text)
        if self.rule_count:
            font.setPointSizeF(7)
            painter.setFont(font)
            painter.drawText(rect.adjusted(0, -4, 0, 0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop, f"{self.rule_count} rules")


class SketchView(QGraphicsView):
    """Pan/zoom foundation and one transaction for each placement gesture."""
    SNAP_PIXELS = 7
    GRID_STEP = 20

    def __init__(self, panel):
        super().__init__(panel.scene, panel)
        self.panel = panel
        self.grid_visible = True
        self.snap_enabled = True
        self._gesture = None
        self._pan = None
        self._guides = []
        self._fit_on_show = False
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setMinimumSize(420, 300)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setBackgroundBrush(QColor("#fcfcfd"))

    def drawBackground(self, painter, rect):
        super().drawBackground(painter, rect)
        if not self.grid_visible or self.transform().m11() < 0.25:
            return
        painter.setPen(QPen(QColor("#e5e9ed"), 0))
        step = self.GRID_STEP
        left, top = math.floor(rect.left() / step) * step, math.floor(rect.top() / step) * step
        for x in range(left, math.ceil(rect.right()), step):
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
        for y in range(top, math.ceil(rect.bottom()), step):
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))

    def wheelEvent(self, event):
        if not event.angleDelta().y():
            event.ignore()
            return
        factor = 1.18 if event.angleDelta().y() > 0 else 1 / 1.18
        if 0.12 <= self.transform().m11() * factor <= 6:
            self.scale(factor, factor)
        event.accept()

    def fit_all(self):
        self._fit_on_show = not self.isVisible()
        if self._fit_on_show:
            return
        bounds = self.panel.content_bounds()
        self.fitInView(bounds.adjusted(-50, -50, 50, 50), Qt.AspectRatioMode.KeepAspectRatio)
        if self.transform().m11() > 1.5:
            self.resetTransform()
            self.scale(1.5, 1.5)
        self.centerOn(bounds.center())

    def showEvent(self, event):
        super().showEvent(event)
        if self._fit_on_show:
            self.fit_all()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton:
            self._pan = event.position().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        hit = self.itemAt(event.position().toPoint())
        super().mousePressEvent(event)
        if event.button() == Qt.MouseButton.LeftButton and isinstance(hit, _Block) and hit.isSelected():
            names = set(self.panel.selected_names())
            for group in self.panel._state.get("groups", []):
                if names.intersection(group["instances"]):
                    names.update(group["instances"])
            self._gesture = {
                "start": self.mapToScene(event.position().toPoint()),
                "positions": self.panel.display_positions(),
                "names": names,
                "state": deepcopy(self.panel._state),
            }

    def mouseMoveEvent(self, event):
        if self._pan is not None:
            if not event.buttons() & Qt.MouseButton.MiddleButton:
                self.cancel_drag()
                event.accept()
                return
            now = event.position().toPoint()
            delta = now - self._pan
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            self._pan = now
            event.accept()
            return
        if self._gesture is not None:
            if not event.buttons() & Qt.MouseButton.LeftButton:
                self.cancel_drag()
                event.accept()
                return
            delta = self.mapToScene(event.position().toPoint()) - self._gesture["start"]
            self.preview_drag(delta, bypass=bool(event.modifiers() & Qt.KeyboardModifier.AltModifier))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._pan is not None and event.button() == Qt.MouseButton.MiddleButton:
            self._pan = None
            self.unsetCursor()
            event.accept()
            return
        if self._gesture is not None and event.button() == Qt.MouseButton.LeftButton:
            # Release the scene's selection gesture before the owner refreshes it.
            super().mouseReleaseEvent(event)
            self.finish_drag()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and self._gesture is not None:
            self.cancel_drag()
            event.accept()
            return
        super().keyPressEvent(event)

    def event(self, event):
        # A release can occur outside the viewport while a modal dialog, a tab
        # change, or another window takes focus. Such an interrupted preview is
        # cancelled rather than left available for a later hover to move/commit.
        if (event.type() in (QEvent.Type.FocusOut, QEvent.Type.Hide, QEvent.Type.WindowDeactivate)
                and hasattr(self, "_gesture")):
            self.cancel_drag()
        return super().event(event)

    def _clear_guides(self):
        for guide in self._guides:
            self.scene().removeItem(guide)
        self._guides = []

    def _snap(self, suggestions, positions):
        """Snap a rigid selection to grid or stationary box centers/edges."""
        self._clear_guides()
        tolerance = self.SNAP_PIXELS / self.transform().m11()
        moving = set(suggestions)
        correction, targets = [None, None], [None, None]
        offsets = ((-BLOCK_WIDTH / 2, 0, BLOCK_WIDTH / 2),
                   (-BLOCK_HEIGHT / 2, 0, BLOCK_HEIGHT / 2))
        for axis in (0, 1):
            candidates = []
            for name, point in suggestions.items():
                if self.grid_visible:
                    target = round(point[axis] / self.GRID_STEP) * self.GRID_STEP
                    candidates.append((target - point[axis], target))
                for other, stationary in positions.items():
                    if other in moving:
                        continue
                    for own_edge in offsets[axis]:
                        for other_edge in offsets[axis]:
                            target = stationary[axis] + other_edge
                            candidates.append((target - point[axis] - own_edge, target))
            if candidates:
                distance, target = min(candidates, key=lambda value: abs(value[0]))
                if abs(distance) <= tolerance:
                    correction[axis], targets[axis] = distance, target
        snapped = {name: [point[axis] + (correction[axis] or 0) for axis in (0, 1)]
                   for name, point in suggestions.items()}
        return snapped, targets

    def preview_drag(self, delta, *, bypass=False):
        """Preview a scene-space displacement; used by pointer events and tests."""
        gesture = self._gesture
        if gesture is None or self.panel.project is None:
            return
        self._clear_guides()
        suggestions = {name: [gesture["positions"][name][0] + delta.x(),
                              gesture["positions"][name][1] + delta.y()]
                       for name in gesture["names"]}
        targets = [None, None]
        if self.snap_enabled and not bypass:
            suggestions, targets = self._snap(suggestions, gesture["positions"])
        try:
            solved = solve_positions(self.panel.project, positions=gesture["positions"], suggestions=suggestions)
        except (SketchConflict, ValueError) as exc:
            self.panel._message(str(exc))
            return  # Keep the last feasible preview, including on imported conflicts.
        self.panel._set_positions(solved)
        # A required relation may override the snapped suggestion. Show only guides
        # actually reached, so a guide never promises an unenforced alignment.
        bounds = self.panel.content_bounds().adjusted(-80, -80, 80, 80)
        for axis, target in enumerate(targets):
            if target is None or not any(abs(solved[name][axis] - suggestions[name][axis]) < 0.01
                                         for name in suggestions):
                continue
            line = (target, bounds.top(), target, bounds.bottom()) if axis == 0 else (bounds.left(), target, bounds.right(), target)
            guide = self.scene().addLine(*line, QPen(QColor("#008b83"), 0, Qt.PenStyle.DashLine))
            guide.setZValue(5)
            guide.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            self._guides.append(guide)

    def finish_drag(self):
        gesture, self._gesture = self._gesture, None
        self._clear_guides()
        if gesture is None:
            return
        positions = self.panel.display_positions()
        if any(abs(positions[name][axis] - gesture["positions"][name][axis]) > 1e-7
               for name in positions for axis in (0, 1)):
            state = deepcopy(gesture["state"])
            state["positions"] = positions
            self.panel._state = deepcopy(state)
            self.panel.sketchEdited.emit(state)

    def cancel_drag(self):
        gesture, self._gesture = self._gesture, None
        if gesture is not None:
            self.panel._set_positions(gesture["positions"])
            grabber = self.scene().mouseGrabberItem()
            if grabber is not None:
                grabber.ungrabMouse()
        self._pan = None
        self.unsetCursor()
        self._clear_guides()


class SketchPanel(QWidget):
    sketchEdited = pyqtSignal(object)
    selectionChanged = pyqtSignal(list)
    relationshipRequested = pyqtSignal(dict)
    statusMessage = pyqtSignal(str)
    labelRequested = pyqtSignal(str)
    constraintRequested = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.project = None
        self._state = {"positions": {}, "groups": []}
        self.items_by_name = {}
        self._group_items = []
        self._relationship_items = []
        self._highlighted_constraint = None
        self._highlighted_enabled = None
        self._last_selection = []
        self._conflict = ""
        self._fit_pending = True
        self.scene = QGraphicsScene(self)
        self.view = SketchView(self)
        self.scene.selectionChanged.connect(self._selection_changed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        title = QLabel("<b>Sketch — not to scale</b> · Only explicit relationships are exported to ALIGN.")
        title.setWordWrap(True)
        layout.addWidget(title)
        navigation = QHBoxLayout()
        self.grid = QCheckBox("Drawing grid")
        self.grid.setChecked(True)
        self.grid.toggled.connect(self._grid_changed)
        navigation.addWidget(self.grid)
        self.snap = QCheckBox("Snap guides (Alt bypass)")
        self.snap.setChecked(True)
        self.snap.toggled.connect(lambda enabled: setattr(self.view, "snap_enabled", enabled))
        navigation.addWidget(self.snap)
        fit = QPushButton("Fit all")
        fit.clicked.connect(self.view.fit_all)
        navigation.addWidget(fit)
        navigation.addStretch()
        layout.addLayout(navigation)
        relationships = QHBoxLayout()
        self.alignment = QComboBox()
        for label, value in [("Horizontal centers", "h_center"), ("Vertical centers", "v_center"),
                             ("Top edges", "h_top"), ("Bottom edges", "h_bottom"),
                             ("Left edges", "v_left"), ("Right edges", "v_right")]:
            self.alignment.addItem(label, value)
        relationships.addWidget(self.alignment)
        self.align_button = self._button(relationships, "Align", self.align_selected)
        self.direction = QComboBox()
        self.direction.addItem("Left → right", "left_to_right")
        self.direction.addItem("Top → bottom", "top_to_bottom")
        relationships.addWidget(self.direction)
        self.order_button = self._button(relationships, "Order", self.order_selected)
        layout.addLayout(relationships)
        groups = QHBoxLayout()
        self.symmetry_axis = QComboBox()
        self.symmetry_axis.addItem("Vertical symmetry axis", "V")
        self.symmetry_axis.addItem("Horizontal symmetry axis", "H")
        groups.addWidget(self.symmetry_axis)
        self.symmetry_button = self._button(groups, "Make symmetric…", self.symmetry_selected)
        self.row_button = self._button(groups, "Make row", self.row_selected)
        groups.addStretch()
        layout.addLayout(groups)
        drawing = QHBoxLayout()
        self.group_button = self._button(drawing, "Group for drawing…", self._ask_group)
        self.ungroup_button = self._button(drawing, "Ungroup", self.ungroup_selected)
        self.role_button = self._button(drawing, "Role label…", self._request_label)
        drawing.addStretch()
        layout.addLayout(drawing)
        rules = QHBoxLayout()
        rules.addWidget(QLabel("Rules for selection"))
        self.rule_combo = QComboBox()
        self.rule_combo.setMinimumWidth(80)
        self.rule_combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        rules.addWidget(self.rule_combo, 1)
        self.inspect_button = self._button(rules, "Inspect rule…", self._inspect_rule)
        self.rule_combo.currentIndexChanged.connect(self._rule_selected)
        layout.addLayout(rules)
        layout.addWidget(self.view, 1)
        self.capabilities = QLabel("Live sketch: alignment, order, rows and block symmetry. Physical dimensions are for ALIGN to check.")
        self.capabilities.setTextFormat(Qt.TextFormat.PlainText)
        self.capabilities.setWordWrap(True)
        layout.addWidget(self.capabilities)
        self.status = QLabel("Select devices. Drag to arrange; middle drag pans; wheel zooms.")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self._selection_changed()

    @staticmethod
    def _button(layout, label, slot):
        button = QPushButton(label)
        button.clicked.connect(lambda _checked=False: slot())
        layout.addWidget(button)
        return button

    def _message(self, text):
        if self._conflict and self._conflict not in text:
            text = self._conflict + "\n" + text
        self.status.setText(text)
        self.statusMessage.emit(text)

    def _grid_changed(self, enabled):
        self.view.grid_visible = enabled
        self.view.viewport().update()

    def set_project(self, project):
        """Refresh from the owner's project; preserve session selection and view."""
        selected = self.selected_names()
        first = self._fit_pending or not self.items_by_name
        self.view.cancel_drag()
        self.scene.blockSignals(True)
        self.scene.clear()
        self.items_by_name, self._group_items, self._relationship_items = {}, [], []
        self.project = project
        self._conflict = ""
        self._state = deepcopy(project.sketch) if project else {"positions": {}, "groups": []}
        if project:
            for device in project.devices:
                item = _Block(device, project.device_labels.get(device.name, ""))
                self.scene.addItem(item)
                self.items_by_name[device.name] = item
            try:
                positions = solve_positions(project)
                message = "Drag to arrange; middle drag pans; wheel zooms. Guides are temporary."
            except (SketchConflict, ValueError) as exc:
                positions = self._state["positions"]
                message = f"Sketch conflict: {exc} Edit or disable a conflicting rule in Constraints."
                self._conflict = message
            self._set_positions(positions)
            self._message(message)
            counts = {key: 0 for key in ("enforced", "exported", "unsupported", "disabled")}
            for constraint, enabled in zip(project.constraints, project._constraint_flags()):
                counts[sketch_capability(constraint) if enabled else "disabled"] += 1
            self.capabilities.setText(
                f"Rules: {counts['enforced']} {'blocked by sketch conflict' if self._conflict else 'enforced in sketch'} · {counts['exported']} for ALIGN to check · "
                f"{counts['unsupported']} unsupported for export · {counts['disabled']} disabled. "
                "Box badges count enabled rules naming that device; hover for details.")
            for name, item in self.items_by_name.items():
                related = self._related_rules([name])
                item.rule_count = sum(enabled for _, _, enabled in related)
                details = [f"Rule {row + 1}: {' · '.join(constraint_summary(rule))} · {self._rule_status(rule, enabled)}"
                           for row, rule, enabled in related]
                tip = item.base_tip + "\n" + ("\n".join(details) or "No rules name this device directly.")
                item.setToolTip('<qt>' + escape(tip).replace('\n', '<br>') + '</qt>')
        self.scene.blockSignals(False)
        self.set_selection(selected)
        if self._highlighted_constraint is not None:
            native = self._highlighted_constraint
            self.highlight_constraint(native if project and native in project.constraints else None)
        if first and self.items_by_name:
            self.view.fit_all()
            self._fit_pending = False

    def reset_document(self):
        """A new document gets its own camera and rule selection."""
        self.view.cancel_drag()
        self.set_selection([])
        self._highlighted_constraint = self._highlighted_enabled = None
        self._fit_pending = True

    def _related_rules(self, names):
        names = {name.casefold() for name in names}
        if not self.project:
            return []
        return [(row, rule, enabled) for row, (rule, enabled) in
                enumerate(zip(self.project.constraints, self.project._constraint_flags()))
                if names.intersection(name.casefold() for name in _participants(rule))]

    def _rule_status(self, rule, enabled):
        if not enabled:
            return "disabled"
        capability = sketch_capability(rule)
        if capability == "enforced":
            return "blocked by sketch conflict" if self._conflict else "enforced in sketch"
        return "for ALIGN to check" if capability == "exported" else "unsupported for export"

    def _request_label(self):
        names = self.selected_names()
        if len(names) == 1:
            self.labelRequested.emit(names[0])

    def _inspect_rule(self):
        row = self.rule_combo.currentData()
        if row is not None:
            self.constraintRequested.emit(row)

    def _rule_selected(self, *_args):
        row = self.rule_combo.currentData()
        self.inspect_button.setEnabled(row is not None)
        if row is not None and self.project:
            self.highlight_constraint(self.project.constraints[row], self.project._constraint_flags()[row])

    def selected_names(self):
        return [name for name, item in self.items_by_name.items() if item.isSelected()]

    def set_selection(self, names):
        names = {name.upper() for name in names}
        self.scene.blockSignals(True)
        for name, item in self.items_by_name.items():
            item.setSelected(name.upper() in names)
        self.scene.blockSignals(False)
        self._selection_changed()

    def _selection_changed(self):
        names = self.selected_names()
        for button in (self.align_button, self.order_button, self.row_button, self.symmetry_button, self.group_button):
            button.setEnabled(len(names) >= 2)
        self.ungroup_button.setEnabled(any(set(names).intersection(group["instances"])
                                          for group in self._state.get("groups", [])))
        self.role_button.setEnabled(len(names) == 1)
        previous = self.rule_combo.currentData()
        self.rule_combo.blockSignals(True)
        self.rule_combo.clear()
        self.rule_combo.addItem("Select a related rule…", None)
        for row, rule, enabled in self._related_rules(names):
            title, _names, settings = constraint_summary(rule)
            self.rule_combo.addItem(f"{row + 1}. {title} · {settings} · {self._rule_status(rule, enabled)}", row)
        self.rule_combo.setCurrentIndex(max(0, self.rule_combo.findData(previous)))
        self.rule_combo.setEnabled(self.rule_combo.count() > 1)
        self.inspect_button.setEnabled(self.rule_combo.currentData() is not None)
        self.rule_combo.blockSignals(False)
        if names != self._last_selection:
            self._last_selection = list(names)
            self.selectionChanged.emit(names)

    def display_positions(self):
        return {name: [item.pos().x(), item.pos().y()] for name, item in self.items_by_name.items()}

    def content_bounds(self):
        bounds = QRectF()
        for item in self.items_by_name.values():
            bounds = bounds.united(item.sceneBoundingRect())
        return bounds if not bounds.isNull() else QRectF(-100, -100, 200, 200)

    def _set_positions(self, positions):
        for name, point in positions.items():
            if name in self.items_by_name:
                self.items_by_name[name].setPos(*point)
        self._draw_groups()
        self._draw_relationship()
        self.scene.setSceneRect(self.content_bounds().adjusted(-400, -400, 400, 400))

    def _draw_groups(self):
        for item in self._group_items:
            self.scene.removeItem(item)
        self._group_items = []
        for group in self._state.get("groups", []):
            bounds = QRectF()
            for name in group["instances"]:
                if name in self.items_by_name:
                    bounds = bounds.united(self.items_by_name[name].sceneBoundingRect())
            if bounds.isNull():
                continue
            box = self.scene.addRect(bounds.adjusted(-12, -28, 12, 12), QPen(QColor("#7a8996"), 0, Qt.PenStyle.DashLine))
            box.setZValue(-2)
            box.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            label = self.scene.addSimpleText(group["label"] + " · drawing group")
            label.setBrush(QColor("#546777"))
            label.setPos(bounds.left() - 7, bounds.top() - 27)
            label.setZValue(-1)
            label.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            self._group_items.extend((box, label))

    def highlight_constraint(self, native, enabled=None):
        self._highlighted_constraint = deepcopy(native) if native else None
        if enabled is None and native and self.project:
            matches = [flag for rule, flag in zip(self.project.constraints, self.project._constraint_flags()) if rule == native]
            enabled = any(matches) if matches else True
        self._highlighted_enabled = enabled
        names = {name.upper() for name in _participants(native)} if native else set()
        for name, item in self.items_by_name.items():
            item.highlighted = name.upper() in names
            item.update()
        self._draw_relationship()
        if native:
            note = capability_note(native) if enabled else "Disabled: this rule is not applied in the sketch or exported."
            if enabled and self._conflict and sketch_capability(native) == "enforced":
                note = "Blocked by sketch conflict; displayed positions do not satisfy all enabled rules."
            self._message(f"{native.get('constraint', 'Rule')}: {note}")
        else:
            self._message("Drag to arrange; middle drag pans; wheel zooms. Guides are temporary.")

    def _draw_relationship(self):
        for item in self._relationship_items:
            self.scene.removeItem(item)
        self._relationship_items = []
        native = self._highlighted_constraint
        if not native:
            return
        lookup = {name.upper(): item for name, item in self.items_by_name.items()}
        points = [lookup[name.upper()].pos() for name in _participants(native) if name.upper() in lookup]
        if len(points) < 2:
            return
        color = QColor("#a56519") if self._highlighted_enabled else QColor("#8b9197")
        pen = QPen(color, 0, Qt.PenStyle.DashLine)
        left, right = min(p.x() for p in points), max(p.x() for p in points)
        top, bottom = min(p.y() for p in points), max(p.y() for p in points)
        kind = native.get("constraint", "Rule")
        label = str(kind)
        lines = []
        live = self._highlighted_enabled and not self._conflict and sketch_capability(native) == "enforced"
        if kind == "Align" and live:
            line = native.get("line", "")
            label = "Align " + str(line).replace("_", " ")
            if line.startswith("h_"):
                offset = {"h_top": -BLOCK_HEIGHT / 2, "h_bottom": BLOCK_HEIGHT / 2}.get(line, 0)
                lines = [(left - BLOCK_WIDTH / 2 - 20, top + offset, right + BLOCK_WIDTH / 2 + 20, top + offset)]
            elif line.startswith("v_"):
                offset = {"v_left": -BLOCK_WIDTH / 2, "v_right": BLOCK_WIDTH / 2}.get(line, 0)
                lines = [(left + offset, top - BLOCK_HEIGHT / 2 - 20, left + offset, bottom + BLOCK_HEIGHT / 2 + 20)]
        elif kind == "SymmetricBlocks" and live:
            vertical = native.get("direction") == "V"
            label = "Shared vertical symmetry axis" if vertical else "Shared horizontal symmetry axis"
            pairs = native.get("pairs", [])
            for pair in pairs if isinstance(pairs, list) else []:
                if not isinstance(pair, list):
                    continue
                members = [lookup[name.upper()].pos() for name in pair if isinstance(name, str) and name.upper() in lookup]
                if members:
                    center = sum((p.x() if vertical else p.y()) for p in members) / len(members)
                    lines = [(center, top - BLOCK_HEIGHT / 2 - 20, center, bottom + BLOCK_HEIGHT / 2 + 20)] if vertical else [(left - BLOCK_WIDTH / 2 - 20, center, right + BLOCK_WIDTH / 2 + 20, center)]
                    break
        elif kind in ("Order", "AlignInOrder"):
            direction = native.get("direction", "horizontal")
            label = ("Row · " if kind == "AlignInOrder" else "Order · ") + ("left → right" if direction in ("horizontal", "left_to_right") else "top → bottom")
        if not lines:
            lines = [(a.x(), a.y(), b.x(), b.y()) for a, b in zip(points, points[1:])]
        for coordinates in lines:
            line = self.scene.addLine(*coordinates, pen)
            line.setZValue(-0.5)
            line.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            self._relationship_items.append(line)
        if not self._highlighted_enabled:
            label += " · disabled"
        elif self._conflict and sketch_capability(native) == "enforced":
            label += " · blocked by conflict"
        elif sketch_capability(native) != "enforced":
            label += " · " + ("for ALIGN to check" if sketch_capability(native) == "exported" else "unsupported for export")
        text = self.scene.addSimpleText(label)
        text.setBrush(color)
        text.setPos(left - BLOCK_WIDTH / 2, top - BLOCK_HEIGHT / 2 - 52)
        text.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self._relationship_items.append(text)

    def _ordered_selection(self, direction="left_to_right"):
        axis = 0 if direction == "left_to_right" else 1
        positions = self.display_positions()
        return sorted(self.selected_names(), key=lambda name: (positions[name][axis], positions[name][1 - axis], name))

    def align_selected(self):
        if len(self.selected_names()) >= 2:
            self.relationshipRequested.emit({"constraint": "Align", "instances": self.selected_names(), "line": self.alignment.currentData()})

    def order_selected(self):
        if len(self.selected_names()) >= 2:
            direction = self.direction.currentData()
            self.relationshipRequested.emit({"constraint": "Order", "instances": self._ordered_selection(direction), "direction": direction})

    def row_selected(self):
        if len(self.selected_names()) >= 2:
            self.relationshipRequested.emit({"constraint": "AlignInOrder", "instances": self._ordered_selection(), "line": "center", "direction": "horizontal"})

    def symmetry_selected(self):
        names = self._ordered_selection()
        if len(names) == 2:
            self.relationshipRequested.emit({"constraint": "SymmetricBlocks", "pairs": [names], "direction": self.symmetry_axis.currentData()})
        elif len(names) > 2:
            self.relationshipRequested.emit({"action": "symmetry", "instances": names, "direction": self.symmetry_axis.currentData()})

    def _ask_group(self):
        label, accepted = QInputDialog.getText(self, "Group for drawing", "Group label (drawing convenience only):")
        if accepted and label.strip():
            self.create_group(label.strip())

    def create_group(self, label):
        names = self.selected_names()
        if len(names) < 2 or not label.strip():
            return
        if len(label.strip()) > 256:
            self._message("Use a drawing group label of at most 256 characters.")
            return
        if any(set(names).intersection(group["instances"]) for group in self._state["groups"]):
            self._message("Ungroup the existing drawing group before regrouping these devices.")
            return
        state = deepcopy(self._state)
        state["positions"] = self.display_positions()
        state["groups"].append({"id": uuid4().hex, "label": label.strip(), "instances": names})
        self._state = deepcopy(state)
        self._draw_groups()
        self._selection_changed()
        self.sketchEdited.emit(state)

    def ungroup_selected(self):
        names = set(self.selected_names())
        state = deepcopy(self._state)
        state["groups"] = [group for group in state["groups"] if not names.intersection(group["instances"])]
        if state["groups"] != self._state["groups"]:
            state["positions"] = self.display_positions()
            self._state = deepcopy(state)
            self._draw_groups()
            self._selection_changed()
            self.sketchEdited.emit(state)
