"""Native, read-only inspection of the normalized physical placement contract.

The owner loads/validates captured artifacts with geometry.load_preview. This
widget consumes detached geometry only; it never imports ALIGN or runs content.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from html import escape
import math

from PyQt6.QtCore import QEvent, QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QPainter, QPalette, QPen
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QGraphicsItem, QGraphicsScene, QGraphicsView,
    QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QSizePolicy, QVBoxLayout,
    QWidget,
)


def _rect(bbox):
    """One conversion from physical y-up coordinates into Qt scene space."""
    x0, y0, x1, y1 = bbox
    return QRectF(x0, -y1, x1 - x0, y1 - y0)


def _step(value):
    base = 10 ** math.floor(math.log10(max(value, 1e-15)))
    return next((m * base for m in (1, 2, 5, 10) if m * base >= value), 10 * base)


def _tooltip(text):
    # Qt tooltips accept rich text; captured identifiers remain literal text.
    return '<qt>' + escape(text).replace('\n', '<br>') + '</qt>'


class _PhysicalBlock(QGraphicsItem):
    def __init__(self, block, panel):
        super().__init__()
        self.block, self.panel = block, panel
        self.rect = _rect(block['bbox'])
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setToolTip(_tooltip(panel.block_details(block, panel.device_labels)))

    def boundingRect(self):
        return self.rect

    def paint(self, painter, option, widget=None):
        palette = self.panel.palette()
        selected = self.isSelected()
        fill = palette.color(QPalette.ColorRole.Highlight if selected else QPalette.ColorRole.AlternateBase)
        edge = palette.color(QPalette.ColorRole.Highlight if selected else QPalette.ColorRole.Text)
        painter.setPen(QPen(edge, 2 if selected else 1, Qt.PenStyle.SolidLine))
        pen = painter.pen()
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(fill)
        painter.drawRect(self.rect)
        # Fixed-size text remains readable without dwarfing a micron-sized box.
        device_rect = painter.worldTransform().mapRect(self.rect).adjusted(3, 3, -3, -3)
        if device_rect.width() < 16 or device_rect.height() < 14:
            return
        sources = self.block['source_instances']
        text = ', '.join(sources[:3]) if sources else 'Unmapped: ' + self.block['id']
        if len(sources) > 3:
            text += f' (+{len(sources) - 3})'
        roles = [self.panel.device_labels[name].replace('_', ' ') for name in sources[:3]
                 if name in self.panel.device_labels]
        painter.save()
        painter.resetTransform()
        painter.setClipRect(device_rect)
        font = painter.font()
        font.setPointSizeF(9)
        painter.setFont(font)
        painter.setPen(palette.color(QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text))
        metrics = painter.fontMetrics()
        line_count = max(1, int(device_rect.height() // metrics.lineSpacing()))
        lines = [text, *roles]
        if len(lines) > line_count:
            lines = lines[:line_count]
            lines[-1] += ' …'
        text = '\n'.join(metrics.elidedText(line, Qt.TextElideMode.ElideRight,
                                           int(device_rect.width())) for line in lines)
        painter.drawText(device_rect, Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()


class PhysicalView(QGraphicsView):
    """Physical scene with conventional wheel zoom and middle-button panning."""
    def __init__(self, panel):
        super().__init__(panel.scene, panel)
        self.panel = panel
        self._pan = None
        self._fit_on_show = False
        self.setMinimumSize(300, 240)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)

    def fit_all(self):
        # Hidden tabs have not received their final viewport dimensions yet.
        self._fit_on_show = not self.isVisible()
        if self._fit_on_show:
            return
        bounds = self.panel.content_bounds()
        margin = max(bounds.width(), bounds.height()) * .06
        # Reserve screen space for the scale/axes legend below the geometry.
        scale = min(max(1, self.viewport().width() - 4) / (bounds.width() + 2 * margin),
                    max(1, self.viewport().height() - 64) / (bounds.height() + 2 * margin))
        self.fitInView(bounds.adjusted(-margin, -margin, margin, margin + 60 / scale), Qt.AspectRatioMode.KeepAspectRatio)

    def showEvent(self, event):
        super().showEvent(event)
        if self._fit_on_show:
            self.fit_all()

    def wheelEvent(self, event):
        if not event.angleDelta().y():
            event.ignore()
            return
        factor = 1.18 if event.angleDelta().y() > 0 else 1 / 1.18
        if 1e-7 < self.transform().m11() * factor < 1e9:
            self.scale(factor, factor)
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton:
            self._pan = event.position().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._pan is not None:
            if not event.buttons() & Qt.MouseButton.MiddleButton:
                self._cancel_pan()
                event.accept()
                return
            point = event.position().toPoint()
            delta, self._pan = point - self._pan, point
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton:
            self._cancel_pan()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def _cancel_pan(self):
        self._pan = None
        self.unsetCursor()

    def event(self, event):
        if (event.type() in (QEvent.Type.FocusOut, QEvent.Type.Hide, QEvent.Type.WindowDeactivate)
                and hasattr(self, '_pan')):
            self._cancel_pan()
        return super().event(event)

    def drawBackground(self, painter, rect):
        painter.fillRect(rect, self.palette().brush(QPalette.ColorRole.Base))
        if not self.panel.grid.isChecked() or self.panel.data is None:
            return
        step = _step(60 / max(self.transform().m11(), 1e-15))
        color = self.palette().color(QPalette.ColorRole.Text)
        color.setAlpha(35)
        painter.setPen(QPen(color, 0))
        for axis in (0, 1):
            low, high = (rect.left(), rect.right()) if axis == 0 else (rect.top(), rect.bottom())
            start = math.ceil(low / step) * step
            for i in range(min(500, max(0, math.ceil((high - start) / step)))):
                value = start + i * step
                if axis == 0:
                    painter.drawLine(QPointF(value, rect.top()), QPointF(value, rect.bottom()))
                else:
                    painter.drawLine(QPointF(rect.left(), value), QPointF(rect.right(), value))

    def drawForeground(self, painter, rect):
        if self.panel.data is None:
            return
        painter.save()
        painter.resetTransform()
        palette = self.palette()
        height, width = self.viewport().height(), self.viewport().width()
        painter.fillRect(QRectF(4, height - 53, width - 8, 49), palette.brush(QPalette.ColorRole.Base))
        painter.setPen(palette.color(QPalette.ColorRole.Text))
        x, y = 24, height - 19
        painter.drawLine(x, y, x + 34, y)
        painter.drawLine(x, y, x, y - 26)
        painter.drawText(x + 38, y + 4, 'x')
        painter.drawText(x - 3, y - 30, 'y ↑')
        scale = self.transform().m11()
        length = _step(65 / max(scale, 1e-15))
        pixels = length * scale
        right = width - 18
        painter.drawLine(QPointF(right - pixels, y), QPointF(right, y))
        for edge in (right - pixels, right):
            painter.drawLine(QPointF(edge, y - 4), QPointF(edge, y + 4))
        painter.drawText(QRectF(right - pixels - 20, y - 24, pixels + 40, 18), Qt.AlignmentFlag.AlignCenter, f'{length:g} µm')
        painter.restore()


class PreviewPanel(QWidget):
    selectionChanged = pyqtSignal(list)
    generationRequested = pyqtSignal()
    settingsRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.data = self.state = None
        self.device_labels = {}
        self.items_by_id = {}
        self.net_lines, self._net_items = [], []
        self._last_selection = []
        layout = QVBoxLayout(self)
        self.title = self._label('Generated placement — physical dimensions')
        font = self.title.font()
        font.setBold(True)
        self.title.setFont(font)
        layout.addWidget(self.title)
        actions = QHBoxLayout()
        self.generate_button = QPushButton('Generate preview')
        self.settings_button = QPushButton('ALIGN settings…')
        self.generate_button.clicked.connect(self.generationRequested)
        self.settings_button.clicked.connect(self.settingsRequested)
        actions.addWidget(self.generate_button)
        actions.addWidget(self.settings_button)
        actions.addStretch()
        layout.addLayout(actions)
        candidates = QHBoxLayout()
        candidates.addWidget(self._label('Candidate'))
        self.candidate_combo = self._combo()
        candidates.addWidget(self.candidate_combo, 1)
        fit = QPushButton('Fit all')
        candidates.addWidget(fit)
        self.grid = QCheckBox('Grid')
        self.grid.setChecked(True)
        candidates.addWidget(self.grid)
        layout.addLayout(candidates)
        self.dimensions = self._label('')
        self.provenance = self._label('')
        policy = self.provenance.sizePolicy()
        policy.setVerticalPolicy(QSizePolicy.Policy.Minimum)
        self.provenance.setSizePolicy(policy)
        self.stale_badge = self._label('Earlier inputs — regenerate to compare current inputs.')
        policy = self.stale_badge.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.stale_badge.setSizePolicy(policy)
        for label in (self.dimensions, self.provenance, self.stale_badge):
            layout.addWidget(label)
        nets = QHBoxLayout()
        nets.addWidget(self._label('Connections'))
        self.net_combo = self._combo()
        nets.addWidget(self.net_combo, 1)
        layout.addLayout(nets)
        self.scene = QGraphicsScene(self)
        self.view = PhysicalView(self)
        layout.addWidget(self.view, 1)
        self.status = self._label('')
        layout.addWidget(self.status)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(105)
        self.details.setPlaceholderText('Select a block to inspect source membership and compiled identity.')
        layout.addWidget(self.details)
        self.candidate_combo.currentIndexChanged.connect(self._candidate_changed)
        self.net_combo.currentIndexChanged.connect(self._draw_net)
        self.scene.selectionChanged.connect(self._selection_changed)
        fit.clicked.connect(self.view.fit_all)
        self.grid.toggled.connect(self.view.viewport().update)
        self.clear()

    @staticmethod
    def _label(text):
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        return label

    @staticmethod
    def _combo():
        combo = QComboBox()
        combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        combo.setMinimumWidth(60)
        return combo

    def clear(self):
        self.data = self.state = None
        self.scene.blockSignals(True)
        self.scene.clear()
        self.scene.blockSignals(False)
        self.items_by_id = {}
        self.net_lines, self._net_items = [], []
        for combo in (self.candidate_combo, self.net_combo):
            combo.blockSignals(True)
            combo.clear()
            combo.setEnabled(False)
            combo.blockSignals(False)
        self.dimensions.setText('No generated physical placement.')
        self.provenance.setText('Generate a preview with the configured ALIGN environment.')
        self.stale_badge.hide()
        self.status.setText('Read-only preview · wheel to zoom · middle-drag to pan')
        self.details.clear()
        self._selection_changed()

    def set_device_labels(self, labels):
        """Display roles without changing captured geometry or source identity."""
        self.device_labels = dict(labels)
        for item in self.items_by_id.values():
            item.setToolTip(_tooltip(self.block_details(item.block, self.device_labels)))
            item.update()
        self._selection_changed()

    def set_preview(self, data, state=None):
        """Display validated schema-1 geometry; state-only updates retain the view."""
        if data is None:
            self.clear()
            return
        if data.get('schema_version') != 1 or data.get('units') != 'um' or data.get('coordinate_system') != 'y-up':
            raise ValueError('Expected schema-1 physical preview in micrometres with y up')
        changed = data != self.data
        self.state = state
        if changed:
            selected = self.selected_names()
            candidate = self.candidate_combo.currentText()
            self.data = deepcopy(data)
            self.candidate_combo.blockSignals(True)
            self.candidate_combo.clear()
            self.candidate_combo.addItems([c['name'] for c in self.data['candidates']])
            index = self.candidate_combo.findText(candidate)
            self.candidate_combo.setCurrentIndex(max(0, index))
            self.candidate_combo.setEnabled(True)
            self.candidate_combo.blockSignals(False)
            self._candidate_changed()
            self.set_selection(selected)
        self._update_provenance()

    def _update_provenance(self):
        provenance = self.data['provenance']
        created = provenance.get('created_at')
        if isinstance(created, (int, float)) and not isinstance(created, bool):
            try:
                created = datetime.fromtimestamp(created).astimezone().strftime('%Y-%m-%d %H:%M %Z')
            except (OSError, OverflowError, ValueError):
                created = 'Time unavailable'
        self.provenance.setText(
            f"{provenance.get('technology') or 'Technology unknown'} · ALIGN {provenance.get('align_version') or 'unknown'}"
            f"\nRun {self.data['run_id']} · {created or 'Time not recorded'}")
        self.provenance.setToolTip(_tooltip(str(getattr(self.state, 'run_dir', '') or self.data['run_id'])))
        self.stale_badge.setVisible(bool(getattr(self.state, 'stale', False)))
        # The first result replaces a single-line placeholder. Allocate its
        # wrapped provenance before this frame can be painted or captured.
        self.layout().activate()

    def current_candidate(self):
        if self.data is None:
            return None
        return self.data['candidates'][self.candidate_combo.currentIndex()]

    def content_bounds(self):
        candidate = self.current_candidate()
        return _rect(candidate['bbox']) if candidate else QRectF(-1, -1, 2, 2)

    def _candidate_changed(self, *_args):
        candidate = self.current_candidate()
        if candidate is None:
            return
        selected = self.selected_names()
        previous_net = self.net_combo.currentData()
        self.scene.blockSignals(True)
        self.scene.clear()
        self.items_by_id = {}
        self.net_lines, self._net_items = [], []
        boundary = self.scene.addRect(_rect(candidate['bbox']), QPen(self.palette().color(QPalette.ColorRole.Mid), 0))
        boundary.setZValue(-2)
        boundary.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        for block in candidate['blocks']:
            item = _PhysicalBlock(block, self)
            self.scene.addItem(item)
            self.items_by_id[block['id']] = item
        self.scene.blockSignals(False)
        bounds = self.content_bounds()
        margin = max(bounds.width(), bounds.height()) * .3
        self.scene.setSceneRect(bounds.adjusted(-margin, -margin, margin, margin))
        self.dimensions.setText(f'Width {bounds.width():g} µm × height {bounds.height():g} µm · physical y ↑')
        self.net_combo.blockSignals(True)
        self.net_combo.clear()
        self.net_combo.addItem('None', None)
        for net in candidate['nets']:
            self.net_combo.addItem(net['name'], net['name'])
        self.net_combo.setCurrentIndex(max(0, self.net_combo.findData(previous_net)))
        self.net_combo.setEnabled(True)
        self.net_combo.blockSignals(False)
        self._draw_net()
        self.set_selection(selected)
        self.view.fit_all()

    def _draw_net(self, *_args):
        for item in self._net_items:
            self.scene.removeItem(item)
        self.net_lines, self._net_items = [], []
        candidate = self.current_candidate()
        if candidate is None:
            return
        mapping = candidate['mapping']
        warning = ''
        if mapping['status'] != 'complete':
            warning = ' · Incomplete source mapping; unknown membership is not inferred'
            missing = mapping['unmapped_source_instances']
            if missing:
                warning += ': ' + ', '.join(missing)
        name = self.net_combo.currentData()
        net = next((n for n in candidate['nets'] if n['name'] == name), None)
        self.status.setText('Read-only · connections are not routing' + warning)
        if net is None:
            return
        pins = [c for c in net['connections'] if c['kind'] == 'pin']
        omitted = len(net['connections']) - len(pins)
        color = self.palette().color(QPalette.ColorRole.Link)
        pen = QPen(color, 1.5, Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        points = sorted({tuple(c['position']) for c in pins})
        for a, b in zip(points, points[1:]):
            line = self.scene.addLine(a[0], -a[1], b[0], -b[1], pen)
            self.net_lines.append(line)
            self._net_items.append(line)
        for pin in pins:
            item = self.scene.addRect(_rect(pin['bbox']), QPen(color, 0))
            item.setToolTip(_tooltip(f"{name}: {pin['block_id']}.{pin['pin']}"))
            self._net_items.append(item)
        for item in self._net_items:
            item.setZValue(2)
            item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        note = f'{name}: connections, not routing · {len(pins)} physical terminals'
        if omitted:
            note += f' · {omitted} block-level terminals have no pin geometry and are not drawn'
        self.status.setText(note + warning)

    @staticmethod
    def block_details(block, device_labels=None):
        sources = ', '.join(block['source_instances']) or 'Unknown — no established source membership'
        bbox = block['bbox']
        text = (f"Source instances: {sources}\nCompiled block: {block['path']}\n"
                f"Concrete: {block['concrete_name']} · Abstract: {block['abstract_name']}\n"
                f"Physical bounds: ({bbox[0]:g}, {bbox[1]:g})–({bbox[2]:g}, {bbox[3]:g}) µm\n"
                f"Mapping: {block['mapping_status']}")
        if block['mapping_reason']:
            text += '\n' + block['mapping_reason']
        roles = [f"{name}: {device_labels[name]}" for name in block['source_instances']
                 if device_labels and name in device_labels]
        if roles:
            text += '\nRoles: ' + '; '.join(roles)
        return text

    def selected_names(self):
        return list(dict.fromkeys(source for item in self.items_by_id.values() if item.isSelected()
                                  for source in item.block['source_instances']))

    def set_selection(self, source_names):
        names = {name.casefold() for name in source_names}
        self.scene.blockSignals(True)
        for item in self.items_by_id.values():
            item.setSelected(any(name.casefold() in names for name in item.block['source_instances']))
        self.scene.blockSignals(False)
        self._selection_changed()

    def _selection_changed(self):
        names = self.selected_names()
        self.details.setPlainText('\n\n'.join(self.block_details(item.block, self.device_labels)
                                             for item in self.items_by_id.values() if item.isSelected()))
        if names != self._last_selection:
            self._last_selection = names
            self.selectionChanged.emit(list(names))
