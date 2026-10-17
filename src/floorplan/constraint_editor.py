"""Typed editors and readable summaries for the supported ALIGN constraints."""

from copy import deepcopy

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QLabel, QListWidget, QPushButton, QTableWidget,
    QVBoxLayout, QWidget,
)

from .model import FloorplanProject


KINDS = {
    "SymmetricBlocks": ("Symmetry", {"pairs", "direction"}),
    "Order": ("Relative order", {"instances", "direction", "abut"}),
    "Align": ("Align centers / edges", {"instances", "line"}),
    "Floorplan": ("Rows / regions", {"regions", "order", "symmetrize"}),
    "CompactPlacement": ("Packing", {"style"}),
    "PlaceCloser": ("Place closer", {"instances"}),
    "PowerPorts": ("Power ports", {"ports", "propagate"}),
    "GroundPorts": ("Ground ports", {"ports", "propagate"}),
}
LINES = [("Horizontal centers (same y)", "h_center"),
         ("Vertical centers (same x)", "v_center"),
         ("Top edges", "h_top"), ("Bottom edges", "h_bottom"),
         ("Left edges", "v_left"), ("Right edges", "v_right")]


def editable_constraint(constraint):
    kind = constraint.get("constraint")
    if not isinstance(kind, str) or kind not in KINDS or set(constraint) - KINDS[kind][1] - {"constraint"}:
        return False
    if any(key in constraint and type(constraint[key]) is not bool
           for key in ("abut", "order", "symmetrize", "propagate")):
        return False
    for key in ("instances", "ports", "pairs", "regions"):
        if key not in constraint:
            continue
        values = constraint[key]
        if not isinstance(values, list):
            return False
        if key in ("pairs", "regions"):
            if any(not isinstance(group, list) for group in values):
                return False
            if key == "pairs" and any(not 1 <= len(group) <= 2 for group in values):
                return False
            values = [name for group in values for name in group]
        if any(not isinstance(name, str) for name in values):
            return False
    if any(key in constraint and not isinstance(constraint[key], str) for key in ("direction", "line", "style")):
        return False
    enums = {
        "SymmetricBlocks": ("direction", {"H", "V"}, None),
        "Order": ("direction", {"left_to_right", "top_to_bottom"}, None),
        "Align": ("line", {value for _label, value in LINES}, None),
        "CompactPlacement": ("style", {"left", "center", "right"}, "left"),
    }
    if kind in enums:
        key, allowed, default = enums[kind]
        if constraint.get(key, default) not in allowed:
            return False
    return True


def constraint_summary(constraint):
    """Return three human-readable table cells without changing source data."""
    kind = constraint.get("constraint")
    title = KINDS[kind][0] if isinstance(kind, str) and kind in KINDS else f"Unsupported: {kind or 'unnamed'}"
    if not editable_constraint(constraint):
        return title, "Preserved as imported", "Unsupported settings preserved; remove or replace to change them"
    names = ", ".join(constraint.get("instances", constraint.get("ports", [])))
    if kind == "SymmetricBlocks":
        names = "; ".join(" ↔ ".join(pair) if len(pair) > 1 else f"{pair[0]} (on axis)" if pair else "Empty group"
                          for pair in constraint.get("pairs", []))
        settings = "Shared vertical axis" if constraint.get("direction") == "V" else "Shared horizontal axis"
    elif kind == "Order":
        settings = "Left to right" if constraint.get("direction") == "left_to_right" else "Top to bottom"
        settings += "; touching" if constraint.get("abut", False) else "; spacing allowed"
    elif kind == "Align":
        settings = dict((value, label) for label, value in LINES).get(constraint.get("line"), "Choose alignment")
    elif kind == "Floorplan":
        names = "; ".join(f"Row {index + 1}: " + " → ".join(row) for index, row in enumerate(constraint.get("regions", [])))
        settings = "Rows top to bottom"
        settings += "; within rows left to right" if constraint.get("order", False) else "; within-row order free"
        settings += "; shared vertical symmetry" if constraint.get("symmetrize", False) else ""
    elif kind == "CompactPlacement":
        names = "Whole circuit"
        settings = "Final compaction toward " + constraint.get("style", "left")
    elif kind == "PlaceCloser":
        settings = "Prefer nearby placement"
    else:
        settings = "Propagate to child circuits" if constraint.get("propagate", True) else "This circuit only"
        if constraint.get("ports"):
            settings += "; first grid port: " + constraint["ports"][0]
    return title, names, settings


def _button(text, action):
    button = QPushButton(text)
    button.clicked.connect(lambda _checked=False: action())
    return button


class NamesEditor(QWidget):
    """An ordered choice list, used for both device names and circuit ports."""

    def __init__(self, choices, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.choice = QComboBox()
        self.choice.addItems(choices)
        row.addWidget(self.choice, 1)
        row.addWidget(_button("Add", self.add_name))
        layout.addLayout(row)
        self.items = QListWidget()
        self.items.setMaximumHeight(140)
        layout.addWidget(self.items)
        controls = QHBoxLayout()
        controls.addWidget(_button("Move up", lambda: self.move(-1)))
        controls.addWidget(_button("Move down", lambda: self.move(1)))
        controls.addWidget(_button("Remove", self.remove))
        layout.addLayout(controls)

    def names(self):
        return [self.items.item(index).text() for index in range(self.items.count())]

    def set_names(self, names):
        self.items.clear()
        self.items.addItems(names)

    def add_name(self):
        name = self.choice.currentText()
        if name and name.upper() not in {value.upper() for value in self.names()}:
            self.items.addItem(name)
            self.items.setCurrentRow(self.items.count() - 1)

    def remove(self):
        if self.items.currentRow() >= 0:
            self.items.takeItem(self.items.currentRow())

    def move(self, delta):
        index = self.items.currentRow()
        if index >= 0 and 0 <= index + delta < self.items.count():
            item = self.items.takeItem(index)
            self.items.insertItem(index + delta, item)
            self.items.setCurrentRow(index + delta)


class PairsEditor(QWidget):
    """One table row per symmetry pair or device on the shared axis."""

    def __init__(self, choices, pairs, parent=None):
        super().__init__(parent)
        self.choices = choices
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["First device", "Paired device / on axis"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 230)
        self.table.setMaximumHeight(220)
        layout.addWidget(self.table)
        row = QHBoxLayout()
        row.addWidget(_button("Add pair / axis device", self.add_pair))
        row.addWidget(_button("Remove selected group", self.remove))
        layout.addLayout(row)
        for pair in pairs or [[]]:
            self.add_pair(pair)

    def add_pair(self, pair=None):
        pair = pair or []
        row = self.table.rowCount()
        self.table.insertRow(row)
        for column in range(2):
            choice = QComboBox()
            choice.addItem("Choose device…" if column == 0 else "On the shared axis", None)
            for name in self.choices:
                choice.addItem(name, name)
            if column < len(pair):
                index = choice.findData(pair[column])
                if index < 0:
                    choice.addItem(pair[column], pair[column])
                    index = choice.count() - 1
                choice.setCurrentIndex(index)
            self.table.setCellWidget(row, column, choice)
        self.table.setCurrentCell(row, 0)

    def remove(self):
        if self.table.currentRow() >= 0:
            self.table.removeRow(self.table.currentRow())

    def pairs(self):
        pairs = []
        for row in range(self.table.rowCount()):
            first = self.table.cellWidget(row, 0).currentData()
            second = self.table.cellWidget(row, 1).currentData()
            if first is None:
                raise ValueError(f"Choose the first device in symmetry group {row + 1}.")
            pairs.append([first, second] if second is not None else [first])
        return pairs


class RegionsEditor(QWidget):
    """Rows of ordered devices, with explicit add/update and row ordering."""

    def __init__(self, choices, regions, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.rows = QListWidget()
        self.rows.setMaximumHeight(100)
        self.rows.currentRowChanged.connect(self._selected)
        layout.addWidget(self.rows)
        self.devices = NamesEditor(choices)
        layout.addWidget(self.devices)
        self._loading = False
        self.devices.items.model().rowsInserted.connect(self._names_changed)
        self.devices.items.model().rowsRemoved.connect(self._names_changed)
        row = QHBoxLayout()
        row.addWidget(_button("Add row", lambda: self.add_row([])))
        row.addWidget(_button("Remove row", self.remove))
        row.addWidget(_button("Row up", lambda: self.move(-1)))
        row.addWidget(_button("Row down", lambda: self.move(1)))
        layout.addLayout(row)
        for region in regions or [[]]:
            self.add_row(region)

    def _selected(self, row):
        if row >= 0:
            self._loading = True
            try:
                self.devices.set_names(self.rows.item(row).data(Qt.ItemDataRole.UserRole))
            finally:
                self._loading = False

    def _names_changed(self, *_args):
        if not self._loading:
            self.update_row()

    def add_row(self, names=None):
        names = self.devices.names() if names is None else names
        self.rows.addItem(" → ".join(names) or "Empty row — add devices below")
        self.rows.item(self.rows.count() - 1).setData(Qt.ItemDataRole.UserRole, list(names))
        self.rows.setCurrentRow(self.rows.count() - 1)

    def update_row(self):
        row = self.rows.currentRow()
        if row >= 0:
            item = self.rows.item(row)
            item.setText(" → ".join(self.devices.names()) or "Empty row — add devices below")
            item.setData(Qt.ItemDataRole.UserRole, self.devices.names())

    def remove(self):
        if self.rows.currentRow() >= 0:
            self.rows.takeItem(self.rows.currentRow())

    def move(self, delta):
        index = self.rows.currentRow()
        if index >= 0 and 0 <= index + delta < self.rows.count():
            item = self.rows.takeItem(index)
            self.rows.insertItem(index + delta, item)
            self.rows.setCurrentRow(index + delta)

    def regions(self):
        return [self.rows.item(index).data(Qt.ItemDataRole.UserRole) for index in range(self.rows.count())]


class ConstraintEditorDialog(QDialog):
    """Edit one supported constraint transactionally using the model validator."""

    def __init__(self, project, constraint=None, selected_devices=(), parent=None):
        super().__init__(parent)
        if constraint is not None and not editable_constraint(constraint):
            raise ValueError("Unsupported settings are preserved unchanged; remove or replace this constraint to change them.")
        self.setWindowTitle("Edit constraint" if constraint is not None else "Add constraint")
        self.setMinimumWidth(660)
        self.project = FloorplanProject.from_dict(project.to_dict())
        self.original = deepcopy(constraint or {})
        self.selected_devices = list(selected_devices)
        self.constraint = None
        layout = QVBoxLayout(self)
        self.kind = QComboBox()
        for key, (title, _fields) in KINDS.items():
            self.kind.addItem(title, key)
        if constraint is not None:
            self.kind.setCurrentIndex(self.kind.findData(constraint["constraint"]))
            self.kind.setEnabled(False)
        layout.addWidget(self.kind)
        self.content = QVBoxLayout()
        layout.addLayout(self.content)
        self.error = QLabel()
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.kind.currentIndexChanged.connect(self._build_form)
        self._build_form()

    def _choice(self, label, options, value):
        choice = QComboBox()
        for title, data in options:
            choice.addItem(title, data)
        index = choice.findData(value)
        if index < 0:
            choice.addItem(f"Unsupported value: {value}", value)
            index = choice.count() - 1
        choice.setCurrentIndex(index)
        self.form.addRow(label, choice)
        return choice

    def _flag(self, label, key, default=False):
        flag = QCheckBox(label)
        flag.setChecked(self.original.get(key, default))
        self.form.addRow(flag)
        self.flags[key] = (flag, default)
        return flag

    def _build_form(self, *_args):
        while self.content.count():
            widget = self.content.takeAt(0).widget()
            if widget:
                widget.deleteLater()
        page = QWidget()
        self.form = QFormLayout(page)
        self.content.addWidget(page)
        self.flags = {}
        kind = self.kind.currentData()
        devices = [device.name for device in self.project.devices]
        if kind == "SymmetricBlocks":
            self.option = self._choice("Shared symmetry axis:", [("Vertical (left / right)", "V"), ("Horizontal (above / below)", "H")], self.original.get("direction", "V"))
            groups = self.original.get("pairs", [self.selected_devices[index:index + 2] for index in range(0, len(self.selected_devices), 2)])
            self.pairs_editor = PairsEditor(devices, groups)
            self.form.addRow(self.pairs_editor)
            self.pairs_editor.setToolTip("All groups share one axis. Paired devices must have matching models and sizes.")
        elif kind == "Floorplan":
            self.regions_editor = RegionsEditor(devices, self.original.get("regions", [self.selected_devices] if self.selected_devices else []))
            self.form.addRow("Rows (top to bottom):", self.regions_editor)
            self._flag("Order devices left to right within each row", "order")
            self._flag("Mirror row ends about one shared vertical axis", "symmetrize")
            self.regions_editor.rows.setToolTip("Select a row to edit its devices. Rows run top to bottom and add no center or edge alignment.")
            self.flags["symmetrize"][0].setToolTip("Pairs the listed ends of each row about one shared axis; an odd middle device lies on the axis.")
        elif kind == "CompactPlacement":
            self.option = self._choice("Final compaction:", [("Toward left", "left"), ("Toward center", "center"), ("Toward right", "right")], self.original.get("style", "left"))
            self.option.setToolTip("Final placement compaction; sets no fixed boundary or spacing.")
        else:
            ports = kind in ("PowerPorts", "GroundPorts")
            key = "ports" if ports else "instances"
            self.names_editor = NamesEditor(self.project.ports if ports else devices)
            self.names_editor.set_names(self.original.get(key, [] if ports else self.selected_devices))
            self.form.addRow("Ports in order:" if ports else "Devices in order:" if kind == "Order" else "Devices:", self.names_editor)
            if kind == "Order":
                self.option = self._choice("Direction:", [("Left to right", "left_to_right"), ("Top to bottom", "top_to_bottom")], self.original.get("direction", "left_to_right"))
                self._flag("Devices touch (abut)", "abut")
                self.names_editor.setToolTip("List order determines placement order. Adds no center or edge alignment.")
                self.flags["abut"][0].setToolTip("Devices touch along the ordering direction; adds no center or edge alignment.")
            elif kind == "Align":
                self.option = self._choice("Align:", LINES, self.original.get("line", "h_center"))
                self.option.setToolTip("Selected centers or edges share a line.")
            elif ports:
                self._flag("Propagate port roles to child circuits", "propagate", True)
                self.names_editor.setToolTip("ALIGN uses the first port for the top-level power or ground grid.")
            else:
                self.names_editor.setToolTip("Prefers nearby placement without a fixed distance.")

    def value(self):
        kind = self.kind.currentData()
        result = deepcopy(self.original)
        result["constraint"] = kind
        if kind == "SymmetricBlocks":
            result.update(pairs=self.pairs_editor.pairs(), direction=self.option.currentData())
        elif kind == "Floorplan":
            result["regions"] = self.regions_editor.regions()
        elif kind == "CompactPlacement":
            if "style" in self.original or self.option.currentData() != "left":
                result["style"] = self.option.currentData()
        else:
            result["ports" if kind in ("PowerPorts", "GroundPorts") else "instances"] = self.names_editor.names()
            if kind == "Order":
                result["direction"] = self.option.currentData()
            elif kind == "Align":
                result["line"] = self.option.currentData()
        for key, (flag, default) in self.flags.items():
            if key in self.original or flag.isChecked() != default:
                result[key] = flag.isChecked()
        return result

    def accept(self):
        try:
            value = self.value()
            self.project.set_constraints([value])
        except (ValueError, TypeError, KeyError) as exc:
            self.error.setText(str(exc))
            return
        self.constraint = value
        super().accept()
