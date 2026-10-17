"""Qt authoring UI for detached ROAR floorplan projects.

No layout engine is imported or run here. All edited dimensions are micrometres,
and MOS width is per finger, independently of the source's width convention.
"""

from __future__ import annotations

import json
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
    QMessageBox, QPushButton, QTabWidget, QTableWidget, QTableWidgetItem,
    QTextEdit, QVBoxLayout, QWidget,
)

from .model import FloorplanProject


class FloorplanDialog(QDialog):
    """A persistent editor; ``snapshot_provider`` returns a detached Design."""

    def __init__(self, parent=None, snapshot_provider=None):
        super().__init__(parent)
        self.setWindowTitle("ROAR Floorplan")
        self.resize(980, 760)
        self.project = None
        self.project_path = None
        self.snapshot_provider = snapshot_provider
        self.roar_design = None
        self._dirty = False
        self._rendering = False
        layout = QVBoxLayout(self)
        note = QLabel(
            "Author device sizes and placement intent, then export ALIGN inputs. "
            "This editor does not place devices or generate a physical layout.")
        note.setWordWrap(True)
        layout.addWidget(note)
        bar = QHBoxLayout()
        for title, action in (("Load netlist…", self._load_netlist),
                              ("Open project…", self._open_project),
                              ("Save project…", self._save_project),
                              ("Export ALIGN inputs…", self._export_align)):
            bar.addWidget(self._button(title, action))
        layout.addLayout(bar)
        self.project_label = QLabel("Load a SPICE netlist or open a floorplan project.")
        self.project_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.project_label)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self._build_devices_tab()
        self._build_constraints_tab()
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        close = self._button("Close", self.close)
        bottom = QHBoxLayout()
        bottom.addStretch()
        bottom.addWidget(close)
        layout.addLayout(bottom)
        self.tabs.setEnabled(False)

    def _button(self, text, action):
        button = QPushButton(text)
        button.clicked.connect(lambda _checked=False: self._guard(action))
        return button

    def _guard(self, action):
        try:
            action()
        except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
            self.status.setText(str(exc))
            QMessageBox.warning(self, "Floorplan", str(exc))

    def _mark_dirty(self, *_args):
        if not self._rendering:
            self._dirty = True

    def _build_devices_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        label = QLabel(
            "Select device rows to use their names below. Edit MOS W per finger, "
            "L and NF directly; NF must be even and at least 2. Other devices "
            "remain in the netlist; their physical generators are not checked here.")
        label.setWordWrap(True)
        layout.addWidget(label)
        self.devices_table = QTableWidget(0, 6)
        self.devices_table.setHorizontalHeaderLabels(
            ["Instance", "Kind", "Model", "W per finger (µm)", "L (µm)", "NF"])
        self.devices_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.devices_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.devices_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.devices_table.itemChanged.connect(self._mark_dirty)
        self.devices_table.itemSelectionChanged.connect(self._use_selection)
        layout.addWidget(self.devices_table)
        box = QGroupBox("Import sizes from a detached ROAR design snapshot")
        form = QFormLayout(box)
        refresh_row = QHBoxLayout()
        refresh_row.addWidget(self._button("Read current ROAR sizes", self._read_roar))
        self.snapshot_label = QLabel("No snapshot read.")
        refresh_row.addWidget(self.snapshot_label)
        form.addRow(refresh_row)
        self.source_instance = QComboBox()
        form.addRow("ROAR source instance:", self.source_instance)
        self.import_targets = QLineEdit()
        self.import_targets.setPlaceholderText("Explicit target names, e.g. M1, M2")
        form.addRow("Selected-circuit targets:", self.import_targets)
        self.roar_unit = QComboBox()
        self.roar_unit.addItem("Metres (m)", "m")
        self.roar_unit.addItem("Micrometres (µm)", "um")
        form.addRow("ROAR W/L units:", self.roar_unit)
        self.roar_width = QComboBox()
        self.roar_width.addItem("Total width (divide by NF)", "total")
        self.roar_width.addItem("Width per finger", "per_finger")
        form.addRow("ROAR W means:", self.roar_width)
        form.addRow(self._button("Apply sizes to mapped targets", self._apply_roar))
        layout.addWidget(box)
        self.tabs.addTab(tab, "Devices")

    def _build_constraints_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        help_label = QLabel(
            "Choose devices on the Devices tab, or type names in the desired order. "
            "Add an intent below, or edit/import the native ALIGN JSON list. "
            "Separate pairs or rows with semicolons (M1, M2; M3, M4). "
            "Symmetry pairs share one axis; rows express intent, not fixed coordinates.")
        help_label.setWordWrap(True)
        layout.addWidget(help_label)
        self.constraint_instances = QLineEdit()
        self.constraint_instances.setPlaceholderText("Ordered instances, e.g. M1, M2, M3")
        layout.addWidget(self.constraint_instances)
        controls = QHBoxLayout()
        self.constraint_kind = QComboBox()
        self.constraint_kind.addItems(["Symmetry", "Relative order", "Align", "Row", "Rows / regions", "Packing"])
        self.constraint_option = QComboBox()
        self.constraint_kind.currentTextChanged.connect(self._constraint_options)
        self._constraint_options()
        controls.addWidget(self.constraint_kind)
        controls.addWidget(self.constraint_option)
        controls.addWidget(self._button("Add intent", self._add_intent))
        layout.addLayout(controls)
        self.constraints_json = QTextEdit()
        self.constraints_json.setAcceptRichText(False)
        self.constraints_json.setPlainText("[]")
        self.constraints_json.textChanged.connect(self._mark_dirty)
        layout.addWidget(self.constraints_json)
        buttons = QHBoxLayout()
        buttons.addWidget(self._button("Validate / format JSON", self._validate_editor))
        buttons.addWidget(self._button("Import constraints…", self._import_constraints))
        buttons.addWidget(self._button("Export constraints…", self._export_constraints))
        layout.addLayout(buttons)
        self.tabs.addTab(tab, "Constraints")

    def _constraint_options(self, *_args):
        options = {
            "Symmetry": [("Vertical axis (left / right)", "V"),
                         ("Horizontal axis (above / below)", "H")],
            "Relative order": [("Left to right", "left_to_right"),
                               ("Top to bottom", "top_to_bottom")],
            "Align": [("Horizontal centers (same y)", "h_center"),
                      ("Vertical centers (same x)", "v_center"),
                      ("Bottom edges", "h_bottom"), ("Top edges", "h_top"),
                      ("Left edges", "v_left"), ("Right edges", "v_right")],
            "Row": [("Horizontal row", "horizontal"), ("Vertical column", "vertical")],
            "Rows / regions": [("Ordered rows, top to bottom", "ordered"),
                               ("Ordered symmetric rows", "symmetric"),
                               ("Unordered regions", "unordered")],
            "Packing": [("Pack left", "left"), ("Pack center", "center"),
                        ("Pack right", "right")],
        }
        self.constraint_option.clear()
        for label, value in options[self.constraint_kind.currentText()]:
            self.constraint_option.addItem(label, value)

    @staticmethod
    def _names(text):
        return [name.strip() for name in text.split(",") if name.strip()]

    def _use_selection(self):
        if self._rendering:
            return
        rows = sorted({index.row() for index in self.devices_table.selectedIndexes()})
        names = ", ".join(self.devices_table.item(row, 0).text() for row in rows)
        self.constraint_instances.setText(names)
        self.import_targets.setText(names)

    def _require_project(self):
        if self.project is None:
            raise ValueError("Load a netlist or open a floorplan project first.")

    def _collect_project(self, read_constraints=True):
        """Validate a copy so a bad cell/JSON edit never partially changes state."""
        self._require_project()
        candidate = FloorplanProject.from_dict(self.project.to_dict())
        for row, device in enumerate(candidate.devices):
            if device.kind != "mos":
                continue
            try:
                width = float(self.devices_table.item(row, 3).text())
                length = float(self.devices_table.item(row, 4).text())
                nf = int(self.devices_table.item(row, 5).text())
            except (ValueError, AttributeError) as exc:
                raise ValueError(f"{device.name}: enter numeric W/L and an integer NF.") from exc
            candidate.set_mos_parameters(device.name, width_um=width, length_um=length, nf=nf)
        if read_constraints:
            try:
                constraints = json.loads(self.constraints_json.toPlainText())
            except json.JSONDecodeError as exc:
                raise ValueError(f"Constraint JSON: {exc}") from exc
            if not isinstance(constraints, list) or any(not isinstance(item, dict) for item in constraints):
                raise ValueError("Constraints must be a JSON list of native ALIGN dictionaries.")
            # Project saving preserves imported constraints, including unsupported
            # types. Full semantic validation belongs to Validate and Export.
            candidate.constraints = constraints
        return candidate

    def _render(self):
        self._rendering = True
        try:
            project = self.project
            self.project_label.setText(
                f"Circuit: {project.top}  •  {len(project.devices)} devices  •  "
                + (str(self.project_path) if self.project_path else "Unsaved project"))
            self.devices_table.setRowCount(len(project.devices))
            for row, device in enumerate(project.devices):
                values = [device.name, device.kind, device.model,
                          device.width_um if device.kind == "mos" else "—",
                          device.length_um if device.kind == "mos" else "—",
                          device.nf if device.kind == "mos" else "—"]
                for column, value in enumerate(values):
                    item = QTableWidgetItem(str(value))
                    item.setToolTip(str(value))
                    if column < 3 or device.kind != "mos":
                        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    self.devices_table.setItem(row, column, item)
            self.constraints_json.setPlainText(json.dumps(project.constraints, indent=2))
            self.constraint_instances.clear()
            self.import_targets.clear()
            self.tabs.setEnabled(True)
        finally:
            self._rendering = False

    def set_project(self, project, path=None):
        self.project = project
        self.project_path = Path(path) if path else None
        self._render()
        self._dirty = False
        self.status.setText("Project loaded. Dimensions are W per finger and L in µm.")

    def _discard_edits(self):
        if not self._dirty:
            return True
        return QMessageBox.question(
            self, "Replace floorplan project?", "Discard unsaved floorplan edits?",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel) == QMessageBox.StandardButton.Discard

    def _load_netlist(self):
        if not self._discard_edits():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Load SPICE netlist", "", "SPICE (*.sp *.spi *.cir *.spice);;All files (*)")
        if not path:
            return
        top, ok = QInputDialog.getText(self, "Selected circuit", "Subcircuit name (blank if the file contains one circuit):")
        if not ok:
            return
        unit, ok = QInputDialog.getItem(self, "Netlist dimensions", "Units for bare W/L numbers (SPICE suffixes remain explicit):", ["Metres (m)", "Micrometres (µm)"], 0, False)
        if not ok:
            return
        width, ok = QInputDialog.getItem(self, "Netlist width convention", "Netlist W means:", ["Width per finger", "Total width (divide by NF)"], 0, False)
        if not ok:
            return
        project = FloorplanProject.from_netlist(
            path, top=top.strip() or None,
            dimension_unit="um" if unit.startswith("Micro") else "m",
            width_convention="total" if width.startswith("Total") else "per_finger")
        self.set_project(project)
        self._dirty = True

    def _open_project(self):
        if not self._discard_edits():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open floorplan project", "", "Floorplan project (*.floorplan.json *.json);;All files (*)")
        if path:
            self.set_project(FloorplanProject.load(path), path)

    def _save_project(self):
        candidate = self._collect_project()
        path, _ = QFileDialog.getSaveFileName(self, "Save floorplan project", str(self.project_path or f"{candidate.top}.floorplan.json"), "Floorplan project (*.floorplan.json);;JSON (*.json)")
        if path:
            candidate.save(path)
            self.project = candidate
            self.project_path = Path(path)
            self._dirty = False
            self.project_label.setText(f"Circuit: {candidate.top}  •  {len(candidate.devices)} devices  •  {path}")
            self.status.setText(f"Saved {path}")

    def _export_align(self):
        candidate = self._collect_project()
        candidate.validate()
        directory = QFileDialog.getExistingDirectory(self, "Choose parent folder for fresh ALIGN input directory")
        if directory:
            netlist, constraints = candidate.export_align(directory)
            self.project = candidate
            self.status.setText(
                f"Exported {netlist} and {constraints}. Inputs validated; PDK generators "
                "and physical layout have not been checked.")

    def _read_roar(self):
        self.roar_design = None
        self.source_instance.clear()
        self.snapshot_label.setText("No snapshot read.")
        if self.snapshot_provider is None:
            raise ValueError("A running ROAR design is required to read sizes.")
        snapshot = self.snapshot_provider()
        parameters = snapshot.get_instance_parameters()
        if not parameters:
            raise ValueError("The ROAR design has no instance parameters. Assign W, L and NF first.")
        self.roar_design = snapshot
        self.source_instance.clear()
        self.source_instance.addItems(list(parameters))
        self.snapshot_label.setText("Detached snapshot read; read again to refresh.")
        self.status.setText("Choose a source and explicit selected-circuit targets before applying sizes.")

    def _apply_roar(self):
        candidate = self._collect_project()
        if self.roar_design is None:
            raise ValueError("Read current ROAR sizes first.")
        targets = self._names(self.import_targets.text())
        candidate.import_roar_parameters(
            self.roar_design, self.source_instance.currentText(), targets,
            dimension_unit=self.roar_unit.currentData(),
            width_convention=self.roar_width.currentData())
        self.project = candidate
        self._render()
        self._dirty = True
        self.status.setText("Imported sizes into the floorplan project. The ROAR design is unchanged.")

    def _add_intent(self):
        candidate = self._collect_project()
        groups = [self._names(group) for group in self.constraint_instances.text().split(";")]
        names = groups[0]
        kind = self.constraint_kind.currentText()
        if kind not in ("Symmetry", "Rows / regions") and len(groups) > 1:
            raise ValueError("Use one comma-separated device list for this intent.")
        option = self.constraint_option.currentData()
        additions = []
        if kind == "Symmetry":
            if any(len(group) not in (1, 2) for group in groups):
                raise ValueError("Use two devices per symmetry pair; an additional one-device group can sit on the shared axis. Separate groups with semicolons.")
            additions = [{"constraint": "SymmetricBlocks", "pairs": groups, "direction": option}]
        elif kind == "Relative order":
            additions = [{"constraint": "Order", "instances": names, "direction": option}]
        elif kind == "Align":
            additions = [{"constraint": "Align", "instances": names, "line": option}]
        elif kind == "Row":
            additions = [
                {"constraint": "Order", "instances": names,
                 "direction": "left_to_right" if option == "horizontal" else "top_to_bottom"},
                {"constraint": "Align", "instances": names,
                 "line": "h_center" if option == "horizontal" else "v_center"},
            ]
        elif kind == "Rows / regions":
            additions = [{"constraint": "Floorplan", "regions": groups,
                          "order": option != "unordered", "symmetrize": option == "symmetric"}]
        else:
            additions = [{"constraint": "CompactPlacement", "style": option}]
        candidate.set_constraints(candidate.constraints + additions)
        self.project = candidate
        self._rendering = True
        self.constraints_json.setPlainText(json.dumps(candidate.constraints, indent=2))
        self._rendering = False
        self._dirty = True
        self.status.setText("Placement intent added. Edit the JSON list to revise or remove constraints.")

    def _validate_editor(self):
        candidate = self._collect_project()
        candidate.validate()
        self.project = candidate
        self.constraints_json.setPlainText(json.dumps(candidate.constraints, indent=2))
        self.status.setText("Sizes and supported constraint fields are valid. Physical feasibility is not checked.")

    def _import_constraints(self):
        candidate = self._collect_project(read_constraints=False)
        path, _ = QFileDialog.getOpenFileName(self, "Import native ALIGN constraint list", "", "JSON (*.json)")
        if path:
            candidate.set_constraints(json.loads(Path(path).read_text(encoding="utf-8")))
            self.project = candidate
            self.constraints_json.setPlainText(json.dumps(candidate.constraints, indent=2))
            self.status.setText(f"Imported constraints from {path}")

    def _export_constraints(self):
        candidate = self._collect_project()
        candidate.validate()
        path, _ = QFileDialog.getSaveFileName(self, "Export native ALIGN constraint list", f"{candidate.top}.const.json", "JSON (*.json)")
        if path:
            if candidate.source_path and Path(path).resolve() == Path(candidate.source_path).resolve():
                raise ValueError("Constraint export cannot overwrite the source netlist.")
            Path(path).write_text(json.dumps(candidate.constraints, indent=2) + "\n", encoding="utf-8")
            self.status.setText(f"Exported {path}")
