"""Qt authoring UI for detached ROAR floorplan projects.

No layout engine is imported or run here. All edited dimensions are micrometres,
and MOS width is per finger, independently of the source's width convention.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QPushButton, QTabWidget, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from .model import FloorplanProject, _scalar
from .constraint_editor import ConstraintEditorDialog, constraint_summary, editable_constraint

from .workspace import editor_signature, load_roar_design


def _refresh_source_bindings(project, design, origin):
    """Migrate only explicit mappings, leaving manual values untouched."""
    candidate = FloorplanProject.from_dict(project.to_dict())
    for row in project.parameter_rows:
        binding = row["binding"]
        if binding:
            candidate.bind_parameter(
                design, row["symbol"], binding["instance"], binding["attribute"],
                dimension_unit=row["dimension_unit"],
                width_convention=row["width_convention"], source=origin)
    return candidate


def _source_switch_choice(parent, workspace, path):
    question = QMessageBox(parent)
    question.setWindowTitle("Switch ROAR design?")
    dirty = workspace.is_modified()
    question.setText("The current ROAR design has unsaved changes." if dirty else
                     "The selected floorplan uses a different ROAR design.")
    question.setInformativeText(
        f"Current design: {workspace.path or 'Unsaved design'}\nLinked design: {path}\n"
        "Change saved association uses the current design for this floorplan.")
    save = question.addButton("Save and Switch…", QMessageBox.ButtonRole.AcceptRole) if dirty else None
    switch = question.addButton("Discard and Switch" if dirty else "Switch to linked design", QMessageBox.ButtonRole.DestructiveRole)
    association = question.addButton("Change saved association", QMessageBox.ButtonRole.ActionRole)
    frozen = question.addButton("Use saved floorplan sizes", QMessageBox.ButtonRole.ActionRole)
    question.addButton(QMessageBox.StandardButton.Cancel)
    question.setDefaultButton(save or switch)
    question.exec()
    clicked = question.clickedButton()
    return "save" if save is not None and clicked is save else "switch" if clicked is switch else "current" if clicked is association else "saved" if clicked is frozen else "cancel"


def _save_workspace(parent, workspace, protected_path=None):
    path, _ = QFileDialog.getSaveFileName(parent, "Save current ROAR design", str(workspace.path or "design.roar"), "ROAR design (*.roar)")
    if not path:
        return False
    if not Path(path).suffix:
        path += ".roar"
    if protected_path and Path(path).expanduser().resolve() == Path(protected_path).expanduser().resolve():
        raise ValueError("Save the current design to a different file; this path is the linked design you are switching to.")
    workspace.save(path)
    return True


def _choose_source(parent, project, mode, path, workspace=None, snapshot_provider=None):
    """Preflight mappings before changing the active design or floorplan."""
    if mode == "saved":
        return FloorplanProject.from_dict(project.to_dict()), None, None
    if mode == "live":
        provider = workspace.snapshot if workspace is not None else snapshot_provider
        if provider is None:
            raise ValueError("The current ROAR design is unavailable.")
        design = provider()
        candidate = _refresh_source_bindings(project, design, "live")
        candidate.roar_project_path = workspace.path if workspace is not None else None
        return candidate, design, "live"
    if workspace is not None and workspace.path and Path(workspace.path).resolve() == Path(path).expanduser().resolve():
        return _choose_source(parent, project, "live", "", workspace, snapshot_provider)
    design = load_roar_design(path)
    if workspace is None:
        candidate = _refresh_source_bindings(project, design, "linked")
        candidate.roar_project_path = str(Path(path).expanduser().resolve())
        return candidate, design, "linked"
    if workspace is not None:
        current = workspace.snapshot()
        differs = editor_signature(current) != editor_signature(design)
        same_path = workspace.path and Path(workspace.path).resolve() == Path(path).expanduser().resolve()
        from roar_api import Design
        has_current = editor_signature(current) != editor_signature(Design())
        choice = "switch"
        if differs and has_current:
            choice = _source_switch_choice(parent, workspace, path)
            if choice == "cancel":
                return None
            if choice == "saved":
                return _choose_source(parent, project, "saved", path, workspace, snapshot_provider)
            if choice == "current":
                # Validate mappings before asking to save an unnamed design.
                _refresh_source_bindings(project, current, "live")
                if not workspace.path and not _save_workspace(parent, workspace, path):
                    return None
                return _choose_source(parent, project, "live", "", workspace, snapshot_provider)
        # Validate selected target mappings before any save or design switch.
        _refresh_source_bindings(project, design, "live")
        if choice == "save" and not _save_workspace(parent, workspace, path):
            return None
        if differs or not same_path:
            workspace.switch(path)
        design = workspace.snapshot()
        candidate = _refresh_source_bindings(project, design, "live")
        candidate.roar_project_path = workspace.path
    return candidate, design, "live" if workspace is not None else "linked"


class OpenFloorplanDialog(QDialog):
    """Choose a floorplan and the source that supplies its bound sizes."""

    def __init__(self, parent=None, floorplan_path=None, *, workspace=None, snapshot_provider=None):
        super().__init__(parent)
        self.workspace, self.snapshot_provider = workspace, snapshot_provider
        self.setWindowTitle("Open floorplan")
        self.setMinimumWidth(650)
        self.project = self.design = self.source_origin = None
        self._candidate = None
        self._suggested_design = ""
        self._manual_design = self._manual_source = False
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.floorplan_path, self.design_path = QLineEdit(), QLineEdit()
        row = QHBoxLayout()
        row.addWidget(self.floorplan_path)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_floorplan)
        row.addWidget(browse)
        form.addRow("Floorplan (.roar.floorplan):", row)
        self.source_mode = QComboBox()
        for label, key in (("Current ROAR design", "live"), ("Saved ROAR file", "linked"), ("Saved floorplan sizes", "saved")):
            self.source_mode.addItem(label, key)
        form.addRow("Sizing source:", self.source_mode)
        row = QHBoxLayout()
        row.addWidget(self.design_path)
        self.browse_design = QPushButton("Browse…")
        self.browse_design.clicked.connect(self._browse_design)
        row.addWidget(self.browse_design)
        form.addRow("ROAR design (.roar):", row)
        layout.addLayout(form)
        self.link_status = QLabel()
        self.link_status.setTextFormat(Qt.TextFormat.PlainText)
        self.link_status.setWordWrap(True)
        layout.addWidget(self.link_status)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Open | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.floorplan_path.textChanged.connect(self._invalidate_floorplan)
        self.floorplan_path.editingFinished.connect(self._floorplan_changed)
        self.design_path.textEdited.connect(self._design_edited)
        self.design_path.textChanged.connect(self._update_status)
        self.source_mode.activated.connect(self._source_edited)
        self.source_mode.currentIndexChanged.connect(self._update_status)
        if floorplan_path:
            self.floorplan_path.setText(str(floorplan_path))
            self._floorplan_changed()
        else:
            self._update_status()

    def _browse_floorplan(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose Floorplan", self.floorplan_path.text(), "Floorplan (*.roar.floorplan *.floorplan.json *.json);;All files (*)")
        if path:
            self.floorplan_path.setText(path)
            self._floorplan_changed()

    def _browse_design(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose saved ROAR design", self.design_path.text(), "ROAR design (*.roar *.json);;All files (*)")
        if path:
            self.design_path.setText(path)
            self._design_edited()

    def _design_edited(self, *_args):
        self._manual_design = self._manual_source = True
        self.source_mode.setCurrentIndex(self.source_mode.findData("linked"))
        self._update_status()

    def _source_edited(self, *_args):
        self._manual_source = True
        self._update_status()

    def _invalidate_floorplan(self, *_args):
        self._candidate = None
        self._load_error = "Finish entering the floorplan path."
        self._update_status()

    def _floorplan_changed(self, *_args):
        self._candidate, self._load_error = None, ""
        try:
            self._candidate = FloorplanProject.load(self.floorplan_path.text().strip())
        except (ValueError, TypeError, KeyError, OSError) as exc:
            self._load_error = str(exc)
        self._suggested_design = str(self._candidate.roar_project_path or "") if self._candidate else ""
        if not self._manual_design:
            self.design_path.setText(self._suggested_design)
        if not self._manual_source:
            mode = "saved"
            if self._suggested_design and Path(self._suggested_design).is_file():
                mode = "linked"
            elif not self._suggested_design:
                try:
                    provider = self.workspace.snapshot if self.workspace is not None else self.snapshot_provider
                    if provider and provider().get_instance_parameters():
                        mode = "live"
                except (ValueError, RuntimeError, OSError):
                    pass
            self.source_mode.setCurrentIndex(self.source_mode.findData(mode))
        self._update_status()

    def _update_status(self, *_args):
        mode = self.source_mode.currentData()
        self.design_path.setEnabled(mode == "linked")
        self.browse_design.setEnabled(mode == "linked")
        message = ""
        valid = self._candidate is not None
        if not valid:
            message = getattr(self, "_load_error", "") or "Choose a floorplan."
        elif mode == "linked" and not Path(self.design_path.text().strip()).is_file():
            message, valid = "ROAR file is missing. Browse to relink, or choose Saved floorplan sizes.", False
        elif mode == "live" and self.workspace is None and self.snapshot_provider is None:
            message, valid = "The current ROAR design is unavailable.", False
        elif self._suggested_design and not Path(self._suggested_design).is_file():
            message = "Associated ROAR file is missing; saved floorplan sizes are available."
        elif self._manual_design and self.design_path.text().strip() != self._suggested_design:
            message = "Association will change to the selected ROAR file."
        self.link_status.setText(message)
        self.buttons.button(QDialogButtonBox.StandardButton.Open).setEnabled(valid)

    def accept(self):
        try:
            candidate = FloorplanProject.load(self.floorplan_path.text().strip())
            result = _choose_source(self, candidate, self.source_mode.currentData(), self.design_path.text().strip(), self.workspace, self.snapshot_provider)
            if result is None:
                return
            self.project, self.design, self.source_origin = result
        except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
            self.link_status.setText(str(exc))
            return
        super().accept()


class NetlistImportDialog(QDialog):
    """Choose the circuit and interpret source dimensions before importing."""

    def __init__(self, path, parent=None, snapshot_available=False):
        super().__init__(parent)
        self.design = None
        self.setWindowTitle("Import SPICE netlist")
        self.setMinimumWidth(540)
        layout = QVBoxLayout(self)
        filename = QLabel(str(Path(path)))
        filename.setTextFormat(Qt.TextFormat.PlainText)
        filename.setWordWrap(True)
        layout.addWidget(filename)
        form = QFormLayout()
        self.circuit_combo = QComboBox()
        names = FloorplanProject.subcircuit_names(path)
        if len(names) > 1:
            self.circuit_combo.addItem("Choose a subcircuit…", None)
        for name in names:
            self.circuit_combo.addItem(name, name)
        form.addRow("Subcircuit in this file:", self.circuit_combo)
        self.dimension_unit = QComboBox()
        self.dimension_unit.addItem("Micrometres — W=1 means 1 µm", "um")
        self.dimension_unit.addItem("Metres — W=1 means 1 m", "m")
        form.addRow("W/L without a suffix:", self.dimension_unit)
        self.width_convention = QComboBox()
        self.width_convention.addItem("Width per finger", "per_finger")
        self.width_convention.addItem("Total width — divide W by NF", "total")
        form.addRow("Netlist W is:", self.width_convention)
        self.source_mode = QComboBox()
        self.source_mode.addItem("No ROAR design", "none")
        self.source_mode.addItem("Saved ROAR design (.roar)", "linked")
        if snapshot_available:
            self.source_mode.addItem("Current ROAR design", "live")
        form.addRow("Optional sizing source:", self.source_mode)
        self.design_path = QLineEdit()
        self.design_path.setPlaceholderText("Saved ROAR design path")
        source_row = QHBoxLayout()
        source_row.addWidget(self.design_path)
        self.browse_design = QPushButton("Browse…")
        self.browse_design.clicked.connect(self._browse_design)
        source_row.addWidget(self.browse_design)
        form.addRow("ROAR design (.roar):", source_row)
        self.source_mode.currentIndexChanged.connect(self._source_changed)
        self._source_changed()
        layout.addLayout(form)
        self.dimension_unit.setToolTip("SPICE suffixes override this scale: W=1u always means 1 µm.")
        self.width_convention.setToolTip("Total W=8u with NF=4 becomes 2 µm per finger in the device table.")
        self.source_status = QLabel()
        self.source_status.setTextFormat(Qt.TextFormat.PlainText)
        self.source_status.setWordWrap(True)
        layout.addWidget(self.source_status)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Import")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.circuit_combo.currentIndexChanged.connect(self._update_import_button)
        self._update_import_button()
        layout.addWidget(self.buttons)

    def _source_changed(self, *_args):
        linked = self.source_mode.currentData() == "linked"
        self.design_path.setEnabled(linked)
        self.browse_design.setEnabled(linked)

    def _browse_design(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose saved ROAR design", "", "ROAR design (*.roar *.json);;All files (*)")
        if path:
            self.design_path.setText(path)

    def _update_import_button(self, *_args):
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            self.circuit_combo.currentData() is not None)

    def accept(self):
        try:
            self.design = load_roar_design(self.design_path.text().strip()) if self.source_mode.currentData() == "linked" else None
        except (ValueError, TypeError, KeyError, OSError) as exc:
            self.source_status.setText(str(exc))
            return
        super().accept()


class ParameterImportDialog(QDialog):
    """Interpret a numeric .param file independently of the selected symbol."""

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Import numeric .param values")
        layout = QVBoxLayout(self)
        filename = QLabel(str(path))
        filename.setTextFormat(Qt.TextFormat.PlainText)
        filename.setWordWrap(True)
        layout.addWidget(filename)
        form = QFormLayout()
        self.dimension_unit = QComboBox()
        self.dimension_unit.addItem("Micrometres — W=1 means 1 µm", "um")
        self.dimension_unit.addItem("Metres — W=1 means 1 m", "m")
        form.addRow("W/L without a suffix:", self.dimension_unit)
        self.width_convention = QComboBox()
        self.width_convention.addItem("Width per finger", "per_finger")
        self.width_convention.addItem("Total width — divide W by NF", "total")
        form.addRow("W values mean:", self.width_convention)
        layout.addLayout(form)
        self.dimension_unit.setToolTip("SPICE suffixes override this scale. Expressions are not evaluated.")
        note = QLabel("Replaces matching values and clears their ROAR bindings.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Import")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)


class FloorplanDialog(QDialog):
    """A persistent editor; ``snapshot_provider`` returns a detached Design."""

    def __init__(self, parent=None, snapshot_provider=None, *, placement_options=None, workspace=None, source_changed_signals=()):
        super().__init__(parent)
        self.setWindowTitle("ROAR Floorplan")
        self.resize(980, 760)
        self.project = None
        self.project_path = None
        self.workspace = workspace
        self.snapshot_provider = workspace.snapshot if workspace is not None else snapshot_provider
        self._source_error = ""
        self._source_mode = "saved"
        self._source_timer = QTimer(self)
        self._source_timer.setSingleShot(True)
        self._source_timer.setInterval(300)
        self._source_timer.timeout.connect(self._live_source_update)
        for signal in source_changed_signals:
            signal.connect(self._schedule_source_update)
        self.roar_design = None
        self._source_origin = None
        self._dirty = False
        self._rendering = False
        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        for title, action in (("New floorplan…", self._load_netlist),
                              ("Open floorplan…", self._open_project),
                              ("Save", self._save_project),
                              ("Save as…", lambda: self._save_project(save_as=True)),
                              ("Export ALIGN inputs…", self._export_align)):
            button = self._button(title, action)
            if title == "Export ALIGN inputs…":
                button.setToolTip("Export a sized netlist and placement constraints together for ALIGN.")
            bar.addWidget(button)
        layout.addLayout(bar)
        source_bar = QHBoxLayout()
        source_bar.addWidget(QLabel("Sizing source:"))
        self.source_mode = QComboBox()
        for label, key in (("Current ROAR design", "live"), ("Saved ROAR file", "linked"), ("Saved floorplan sizes", "saved")):
            self.source_mode.addItem(label, key)
        self.source_mode.setCurrentIndex(self.source_mode.findData("saved"))
        self.source_mode.activated.connect(self._source_selected)
        source_bar.addWidget(self.source_mode)
        self.source_path = QLineEdit()
        self.source_path.setReadOnly(True)
        source_bar.addWidget(self.source_path, 1)
        source_bar.addWidget(self._button("Browse…", self._load_linked_roar))
        self.source_reload_button = self._button("Reload source", self._refresh_bindings)
        self.source_reload_button.setVisible(False)
        source_bar.addWidget(self.source_reload_button)
        layout.addLayout(source_bar)
        self.snapshot_label = QLabel()
        self.snapshot_label.setTextFormat(Qt.TextFormat.PlainText)
        self.snapshot_label.setWordWrap(True)
        layout.addWidget(self.snapshot_label)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self._build_devices_tab()
        self._build_parameters_tab()
        self._build_constraints_tab()
        from .align_dialog import PlacementPanel
        self.placement_panel = PlacementPanel(
            self._collect_project, lambda: self.project_path, self._set_align_profile,
            self, **dict({"before_generate": self._ensure_current_source}, **(placement_options or {})))
        self.tabs.addTab(self.placement_panel, "Placement")
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
            if hasattr(self, "placement_panel"):
                self.placement_panel.schedule_refresh()

    def _set_align_profile(self, profile):
        candidate = self._collect_project()
        candidate.align_profile = profile
        self.project = candidate
        self._dirty = True
        self.placement_panel.schedule_refresh()

    def closeEvent(self, event):
        self._source_timer.stop()
        self.placement_panel.close_panel()
        super().closeEvent(event)

    def reject(self):
        self._source_timer.stop()
        self.placement_panel.close_panel()
        super().reject()

    def showEvent(self, event):
        self.placement_panel.resume()
        if self._source_mode == "live":
            self._source_timer.start()
        super().showEvent(event)

    def _build_devices_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        self.devices_table = QTableWidget(0, 6)
        self.devices_table.setHorizontalHeaderLabels(
            ["Instance", "Kind", "Model", "W per finger (µm)", "L (µm)", "NF"])
        self.devices_table.setToolTip("Select rows to use their names as targets. Shared sizes are edited on Parameters.")
        self.devices_table.horizontalHeaderItem(5).setToolTip("Even integer, at least 2.")
        self.devices_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.devices_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.devices_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.devices_table.itemChanged.connect(self._mark_dirty)
        self.devices_table.itemSelectionChanged.connect(self._use_selection)
        layout.addWidget(self.devices_table)
        box = QGroupBox("Copy ROAR sizes")
        form = QFormLayout(box)
        self.source_instance = QComboBox()
        form.addRow("ROAR instance:", self.source_instance)
        self.import_targets = QLineEdit()
        self.import_targets.setPlaceholderText("Explicit target names, e.g. M1, M2")
        form.addRow("Target devices:", self.import_targets)
        self.roar_unit = QComboBox()
        self.roar_unit.addItem("Metres (m)", "m")
        self.roar_unit.addItem("Micrometres (µm)", "um")
        form.addRow("Source W/L units:", self.roar_unit)
        self.roar_unit.setToolTip("Units of the ROAR source values before conversion; the device table shows µm.")
        self.roar_width = QComboBox()
        self.roar_width.addItem("Total width (divide by NF)", "total")
        self.roar_width.addItem("Width per finger", "per_finger")
        form.addRow("ROAR W means:", self.roar_width)
        form.addRow(self._button("Apply sizes", self._apply_roar))
        layout.addWidget(box)
        self.tabs.addTab(tab, "Devices")

    def _build_parameters_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        self.pending_label = QLabel()
        self.pending_label.setTextFormat(Qt.TextFormat.PlainText)
        self.pending_label.setWordWrap(True)
        layout.addWidget(self.pending_label)
        self.parameters_table = QTableWidget(0, 5)
        self.parameters_table.setHorizontalHeaderLabels(
            ["Netlist symbol", "Used by", "Captured value", "Units / W convention", "Source"])
        self.parameters_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.parameters_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.parameters_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.parameters_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.parameters_table.itemSelectionChanged.connect(self._parameter_selected)
        layout.addWidget(self.parameters_table)
        form = QFormLayout()
        self.parameter_symbol = QComboBox()
        self.parameter_symbol.currentIndexChanged.connect(self._parameter_selected)
        form.addRow("Shared netlist symbol:", self.parameter_symbol)
        self.parameter_value = QLineEdit()
        self.parameter_value.setPlaceholderText("Numeric value; blank clears the value and binding")
        self.parameter_unit = QComboBox()
        self.parameter_unit.addItem("Micrometres (µm)", "um")
        self.parameter_unit.addItem("Metres (m)", "m")
        self.parameter_width = QComboBox()
        self.parameter_width.addItem("Width per finger", "per_finger")
        self.parameter_width.addItem("Total width", "total")
        manual = QHBoxLayout()
        manual.addWidget(self.parameter_value, 1)
        manual.addWidget(self.parameter_unit)
        manual.addWidget(self.parameter_width)
        manual.addWidget(self._button("Set manual value", self._set_parameter))
        form.addRow("Manual value:", manual)
        parameter_import = QHBoxLayout()
        import_button = self._button("Import numeric .param…", self._import_parameters)
        import_button.setToolTip("Choose units when importing. Replaces matching values and clears their bindings.")
        parameter_import.addWidget(import_button)
        form.addRow(parameter_import)
        self.binding_instance = QComboBox()
        self.binding_attribute = QComboBox()
        self.binding_instance.currentIndexChanged.connect(self._binding_attributes)
        binding = QHBoxLayout()
        binding.addWidget(self.binding_instance)
        binding.addWidget(self.binding_attribute)
        binding.addWidget(self._button("Bind and capture value", self._bind_parameter))
        form.addRow("ROAR instance.attribute:", binding)
        self.binding_unit = QComboBox()
        self.binding_unit.addItem("Metres (m)", "m")
        self.binding_unit.addItem("Micrometres (µm)", "um")
        self.binding_unit.setToolTip("Units of the ROAR source values before conversion; the device table shows µm.")
        self.binding_width = QComboBox()
        self.binding_width.addItem("Total width (divide by NF)", "total")
        self.binding_width.addItem("Width per finger", "per_finger")
        interpretation = QHBoxLayout()
        interpretation.addWidget(self.binding_unit)
        interpretation.addWidget(self.binding_width)
        form.addRow("Source W/L units and W meaning:", interpretation)
        layout.addLayout(form)
        # Retain the alias for callers; the header owns the single reload action.
        self.refresh_bindings_button = self.source_reload_button
        self.refresh_bindings_button.setEnabled(False)
        self.binding_instance.setToolTip("Bindings follow the selected sizing source; manual values are unchanged.")
        self.tabs.addTab(tab, "Parameters")

    def _parameter_selected(self, *_args):
        if self._rendering or self.project is None:
            return
        sender = self.sender()
        if sender is self.parameters_table:
            rows = self.parameters_table.selectionModel().selectedRows()
            if rows:
                self.parameter_symbol.setCurrentIndex(rows[0].row())
        symbol = self.parameter_symbol.currentText()
        row = next((row for row in self.project.parameter_rows if row["symbol"] == symbol), None)
        if row is None:
            self.parameter_value.clear()
            return
        self.parameter_value.setText("" if row["value"] is None else str(row["value"]))
        self.parameter_unit.setCurrentIndex(self.parameter_unit.findData(row["dimension_unit"]))
        self.parameter_width.setCurrentIndex(self.parameter_width.findData(row["width_convention"]))
        dimensional = bool(set(row["roles"]) & {"W", "L"})
        self.parameter_unit.setEnabled(dimensional)
        self.binding_unit.setEnabled(dimensional)
        self.parameter_width.setEnabled("W" in row["roles"])
        self.binding_width.setEnabled("W" in row["roles"])
        binding = row["binding"]
        if binding:
            self.binding_unit.setCurrentIndex(self.binding_unit.findData(row["dimension_unit"]))
            self.binding_width.setCurrentIndex(self.binding_width.findData(row["width_convention"]))
            self.binding_instance.setCurrentIndex(self.binding_instance.findText(binding["instance"]))
            self._binding_attributes()
            index = next((index for index in range(self.binding_attribute.count())
                          if self.binding_attribute.itemData(index).upper() == binding["attribute"].upper()), -1)
            self.binding_attribute.setCurrentIndex(index)

    def _set_parameter(self):
        candidate = self._collect_project()
        symbol = self.parameter_symbol.currentText()
        if not symbol:
            raise ValueError("This netlist has no shared sizing symbols.")
        raw = self.parameter_value.text().strip()
        candidate.set_parameter(symbol, raw if raw else None,
                                dimension_unit=self.parameter_unit.currentData(),
                                width_convention=self.parameter_width.currentData())
        self.project = candidate
        self._render()
        self._dirty = True
        self.status.setText(f"Updated shared symbol {symbol}.")

    def _import_parameters(self):
        candidate = self._collect_project()
        path, _ = QFileDialog.getOpenFileName(
            self, "Import numeric SPICE .param values", "", "SPICE parameters (*.param *.sp *.spi *.spice *.cir);;All files (*)")
        if path:
            options = ParameterImportDialog(path, self)
            if options.exec() != QDialog.DialogCode.Accepted:
                options.deleteLater()
                return
            unit = options.dimension_unit.currentData()
            width = options.width_convention.currentData()
            options.deleteLater()
            unused = candidate.import_parameters(
                path, dimension_unit=unit, width_convention=width)
            self.project = candidate
            self._render()
            self._dirty = True
            self.status.setText("Imported parameter values." + (
                " Unused names: " + ", ".join(unused) if unused else ""))

    def _binding_attributes(self, *_args):
        self.binding_attribute.clear()
        if self.roar_design is None:
            return
        attrs = self.roar_design.get_instance_parameters().get(self.binding_instance.currentText(), {})
        for name, info in attrs.items():
            value = info.get("value")
            try:
                _scalar(value, name)
            except (TypeError, ValueError):
                continue
            self.binding_attribute.addItem(f"{name} = {value}", name)

    def _bind_parameter(self):
        symbol = self.parameter_symbol.currentText()
        instance = self.binding_instance.currentText()
        attribute = self.binding_attribute.currentData()
        unit = self.binding_unit.currentData()
        width = self.binding_width.currentData()
        self._ensure_current_source()
        candidate = self._collect_project()
        if self.roar_design is None:
            raise ValueError("Choose a ROAR sizing source first.")
        if not symbol or not attribute:
            raise ValueError("Choose a shared symbol and a resolved scalar ROAR attribute.")
        candidate.bind_parameter(
            self.roar_design, symbol, instance, attribute,
            dimension_unit=unit, width_convention=width, source=self._source_origin)
        self.project = candidate
        self._render()
        self._dirty = True
        self.status.setText(f"Captured {symbol} from the selected ROAR attribute.")

    def _refresh_bindings(self):
        self._ensure_current_source()

    def _schedule_source_update(self, *args):
        if self._source_mode != "live" or self.project is None or not self.isVisible():
            return
        if len(args) >= 3 and args[2]:
            ignored = {int(role) for role in (Qt.ItemDataRole.BackgroundRole, Qt.ItemDataRole.ForegroundRole, Qt.ItemDataRole.DecorationRole, Qt.ItemDataRole.ToolTipRole, Qt.ItemDataRole.FontRole, Qt.ItemDataRole.TextAlignmentRole, Qt.ItemDataRole.SizeHintRole)}
            if set(args[2]) <= ignored:
                return
        self._source_timer.start()

    def _live_source_update(self):
        try:
            self._ensure_current_source()
        except (ValueError, TypeError, KeyError, OSError, RuntimeError):
            # Source error is inline; repeated edits must not open popup storms.
            pass

    def _ensure_current_source(self):
        if self.project is None or self._source_mode == "saved":
            return
        try:
            candidate = self._collect_project()
            if self._source_mode == "live":
                if self.snapshot_provider is None:
                    raise ValueError("The current ROAR design is unavailable.")
                if self.workspace is not None and candidate.roar_project_path and self.workspace.path != candidate.roar_project_path:
                    raise ValueError("The active ROAR design changed. Select a sizing source to continue.")
                design = self.snapshot_provider()
                origin = "live"
            else:
                design = load_roar_design(candidate.roar_project_path)
                origin = "linked"
            candidate = _refresh_source_bindings(candidate, design, origin)
            changed = candidate.to_dict() != self.project.to_dict()
            self.project = candidate
            self._source_error = ""
            self._set_source(design, origin)
            if changed:
                self._render()
                self._dirty = True
        except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
            self._source_error = str(exc)
            self.snapshot_label.setText("Sizing source error: " + str(exc))
            raise

    def _build_constraints_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        self.constraints_table = QTableWidget(0, 3)
        self.constraints_table.setHorizontalHeaderLabels(["Constraint", "Devices / ports", "Settings"])
        self.constraints_table.setToolTip("Double-click to edit. Unsupported settings are preserved; export validates all rules.")
        self.constraints_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.constraints_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.constraints_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.constraints_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.constraints_table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.constraints_table.itemSelectionChanged.connect(self._constraint_selection)
        self.constraints_table.cellDoubleClicked.connect(lambda _row, _column: self._guard(self._edit_constraint))
        layout.addWidget(self.constraints_table)
        controls = QHBoxLayout()
        controls.addWidget(self._button("Add constraint…", self._add_constraint))
        self.edit_constraint_button = self._button("Edit…", self._edit_constraint)
        self.remove_constraint_button = self._button("Remove", self._remove_constraint)
        controls.addWidget(self.edit_constraint_button)
        controls.addWidget(self.remove_constraint_button)
        controls.addStretch()
        controls.addWidget(self._button("Check constraints", self._check_constraints))
        layout.addLayout(controls)
        advanced = QGroupBox("Advanced: ALIGN interchange")
        advanced.setCheckable(True)
        advanced.setChecked(False)
        advanced_layout = QVBoxLayout(advanced)
        advanced_content = QWidget()
        advanced_content_layout = QVBoxLayout(advanced_content)
        advanced_content_layout.setContentsMargins(0, 0, 0, 0)
        load_button = self._button("Load ALIGN constraints…", self._import_constraints)
        load_button.setToolTip("Loads only constraints; keeps the circuit, sizes and ROAR association. Use Open floorplan for a complete document.")
        advanced_content_layout.addWidget(load_button)
        advanced_layout.addWidget(advanced_content)
        advanced.toggled.connect(advanced_content.setVisible)
        advanced_content.setVisible(False)
        layout.addWidget(advanced)
        self.tabs.addTab(tab, "Constraints")
        self._constraint_selection()

    def _constraint_selection(self):
        index = self.constraints_table.currentRow()
        selected = self.project is not None and 0 <= index < len(self.project.constraints)
        editable = selected and editable_constraint(self.project.constraints[index])
        self.edit_constraint_button.setEnabled(editable)
        self.edit_constraint_button.setToolTip("" if editable else "Unsupported settings are preserved; remove or replace this row to change them.")
        self.remove_constraint_button.setEnabled(selected)

    def _render_constraints(self):
        self.constraints_table.setRowCount(len(self.project.constraints))
        for row, constraint in enumerate(self.project.constraints):
            for column, value in enumerate(constraint_summary(constraint)):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.constraints_table.setItem(row, column, item)
        self._constraint_selection()
        if hasattr(self, "placement_panel"):
            self.placement_panel.schedule_refresh()

    def _selected_device_names(self):
        rows = sorted({index.row() for index in self.devices_table.selectedIndexes()})
        return [self.devices_table.item(row, 0).text() for row in rows]

    def _add_constraint(self):
        candidate = self._collect_project()
        options = ConstraintEditorDialog(candidate, selected_devices=self._selected_device_names(), parent=self)
        if options.exec() == QDialog.DialogCode.Accepted:
            candidate.constraints.append(options.constraint)
            self.project = candidate
            self._render_constraints()
            self.constraints_table.selectRow(len(candidate.constraints) - 1)
            self._dirty = True
            self.status.setText("Constraint added.")
        options.deleteLater()

    def _edit_constraint(self):
        candidate = self._collect_project()
        index = self.constraints_table.currentRow()
        if not 0 <= index < len(candidate.constraints):
            return
        options = ConstraintEditorDialog(candidate, candidate.constraints[index], parent=self)
        if options.exec() == QDialog.DialogCode.Accepted:
            candidate.constraints[index] = options.constraint
            self.project = candidate
            self._render_constraints()
            self.constraints_table.selectRow(index)
            self._dirty = True
            self.status.setText("Constraint updated.")
        options.deleteLater()

    def _remove_constraint(self):
        candidate = self._collect_project()
        index = self.constraints_table.currentRow()
        if 0 <= index < len(candidate.constraints):
            candidate.remove_constraint(index)
            self.project = candidate
            self._render_constraints()
            self._dirty = True
            self.status.setText("Constraint removed.")

    def _check_constraints(self):
        candidate = self._collect_project()
        candidate.set_constraints(candidate.constraints)
        self.status.setText("Constraint fields valid; placement feasibility unchecked.")

    @staticmethod
    def _names(text):
        return [name.strip() for name in text.split(",") if name.strip()]

    def _use_selection(self):
        if self._rendering:
            return
        rows = sorted({index.row() for index in self.devices_table.selectedIndexes()})
        names = ", ".join(self.devices_table.item(row, 0).text() for row in rows)
        self.import_targets.setText(names)

    def _require_project(self):
        if self.project is None:
            raise ValueError("Load a netlist or open a floorplan project first.")

    def _collect_project(self):
        """Validate a copy so a bad cell/JSON edit never partially changes state."""
        self._require_project()
        candidate = FloorplanProject.from_dict(self.project.to_dict())
        for row, device in enumerate(candidate.devices):
            if device.kind != "mos":
                continue
            try:
                width = device.width_um if "W" in device.symbols or device.width_um is None else float(self.devices_table.item(row, 3).text())
                length = device.length_um if "L" in device.symbols else float(self.devices_table.item(row, 4).text())
                nf = device.nf if "NF" in device.symbols else int(self.devices_table.item(row, 5).text())
            except (ValueError, AttributeError) as exc:
                raise ValueError(f"{device.name}: enter numeric W/L and an integer NF.") from exc
            candidate.set_mos_parameters(device.name, width_um=width, length_um=length, nf=nf)
        return candidate

    def _render(self):
        self._rendering = True
        selected_symbol = self.parameter_symbol.currentText()
        try:
            project = self.project
            self._update_document_title()
            self.devices_table.setRowCount(len(project.devices))
            for row, device in enumerate(project.devices):
                values = [device.name, device.kind, device.model,
                          device.width_um if device.kind == "mos" else "—",
                          device.length_um if device.kind == "mos" else "—",
                          device.nf if device.kind == "mos" else "—"]
                for column, value in enumerate(values):
                    text = "Unresolved" if value is None else str(value)
                    item = QTableWidgetItem(text)
                    role = {3: "W", 4: "L", 5: "NF"}.get(column)
                    controlled = role in device.symbols or (role is not None and value is None)
                    item.setToolTip(
                        f"Shared symbol {device.symbols[role]}; edit or bind it on Parameters."
                        if role in device.symbols else "Resolve the missing shared parameters first."
                        if controlled else text)
                    if column < 3 or device.kind != "mos" or controlled:
                        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    self.devices_table.setItem(row, column, item)
            self._render_constraints()
            self.import_targets.clear()
            self.parameter_symbol.clear()
            rows = project.parameter_rows
            self.parameters_table.setRowCount(len(rows))
            for index, row in enumerate(rows):
                self.parameter_symbol.addItem(row["symbol"])
                dimensional = bool(set(row["roles"]) & {"W", "L"})
                unit = row["dimension_unit"] if dimensional else "unitless"
                if "W" in row["roles"]:
                    unit += "; " + row["width_convention"].replace("_", " ")
                values = [row["symbol"], ", ".join(row["usages"]),
                          "Unresolved" if row["value"] is None else str(row["value"]),
                          unit, row["source"]]
                for column, value in enumerate(values):
                    item = QTableWidgetItem(str(value))
                    item.setToolTip(str(value))
                    self.parameters_table.setItem(index, column, item)
            if selected_symbol:
                self.parameter_symbol.setCurrentIndex(max(0, self.parameter_symbol.findText(selected_symbol)))
            missing = project.unresolved_parameters
            message = "Missing values: " + ", ".join(missing) + ". Resolve before export or placement." if missing else ""
            if project.omitted_parameters:
                message += (" " if message else "") + f"Layout-derived parameters omitted for {len(project.omitted_parameters)} devices."
            self.pending_label.setText(message)
            self.pending_label.setVisible(bool(message))
            self.pending_label.setToolTip("\n".join(
                f"{name}: {', '.join(parameters)}" for name, parameters in project.omitted_parameters.items()))
            self.tabs.setEnabled(True)
        finally:
            self._rendering = False
        self._parameter_selected()
        self.placement_panel.schedule_refresh()

    def _update_document_title(self):
        identity = f"{self.project.top} — {self.project_path.name}" if self.project_path else f"{self.project.top} (unsaved)"
        self.setWindowTitle(f"{identity} — ROAR Floorplan")
        self.setWindowFilePath(str(self.project_path or ""))

    def set_project(self, project, path=None, design=None, source_origin=None):
        self.placement_panel.project_changed()
        self.project = project
        self.project_path = Path(path) if path else None
        self._set_source(design, source_origin)
        self._render()
        self._dirty = False
        self.status.clear()

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
        initial_directory = ""
        design_directory = os.environ.get("ROAR_DESIGN")
        if design_directory:
            examples = Path(design_directory) / "floorplan"
            if examples.is_dir():
                initial_directory = str(examples)
            elif Path(design_directory).is_dir():
                initial_directory = design_directory
        path, _ = QFileDialog.getOpenFileName(self, "Choose circuit netlist", initial_directory, "SPICE (*.sp *.spi *.cir *.spice);;All files (*)")
        if not path:
            return
        options = NetlistImportDialog(path, self, snapshot_available=self.snapshot_provider is not None)
        if options.exec() != QDialog.DialogCode.Accepted:
            options.deleteLater()
            return
        top = options.circuit_combo.currentData()
        unit = options.dimension_unit.currentData()
        width = options.width_convention.currentData()
        origin = options.source_mode.currentData()
        design_path = options.design_path.text().strip()
        design = options.design
        options.deleteLater()
        project = FloorplanProject.from_netlist(
            path, top=top, dimension_unit=unit, width_convention=width)
        if origin == "linked":
            if not design_path:
                raise ValueError("Choose a saved ROAR design path, or import without a ROAR source.")
            project.roar_project_path = design_path
        elif origin == "live":
            design = self.snapshot_provider()
            project.roar_project_path = self.workspace.path if self.workspace is not None else None
        if origin == "linked" and self.workspace is not None:
            result = _choose_source(self, project, "linked", design_path, self.workspace, self.snapshot_provider)
            if result is None:
                return
            project, design, origin = result
        self.set_project(project, design=design, source_origin=origin if design is not None else None)
        if project.unresolved_parameters:
            self.tabs.setCurrentWidget(self.parameters_table.parentWidget())
        self._dirty = True

    def _open_project(self):
        if not self._discard_edits():
            return
        options = OpenFloorplanDialog(self, workspace=self.workspace, snapshot_provider=self.snapshot_provider)
        if options.exec() == QDialog.DialogCode.Accepted:
            self.set_project(options.project, options.floorplan_path.text(), options.design,
                             options.source_origin)
            self._dirty = self.project.to_dict() != options._candidate.to_dict()
        options.deleteLater()

    def _save_project(self, save_as=False):
        candidate = self._collect_project()
        path = str(self.project_path) if self.project_path and not save_as else ""
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, "Save Floorplan", str(self.project_path or f"{candidate.top}.roar.floorplan"), "Floorplan (*.roar.floorplan);;Legacy floorplan (*.floorplan.json)")
            if path and not Path(path).suffix:
                path += ".roar.floorplan"
        if path:
            candidate.save(path)
            self.project = candidate
            self.project_path = Path(path)
            self._dirty = False
            self._update_document_title()
            self.status.setText(f"Saved {path}")
            self.placement_panel.saved_as(self.project_path)

    def _export_align(self):
        self._ensure_current_source()
        candidate = self._collect_project()
        candidate.validate()
        directory = QFileDialog.getExistingDirectory(self, "Choose parent folder for fresh ALIGN input directory")
        if directory:
            netlist, constraints = candidate.export_align(directory)
            self.project = candidate
            self.status.setText(f"Exported ALIGN inputs to {netlist.parent}.")

    def _source_selected(self, *_args):
        self._guard(lambda: self._activate_source(self.source_mode.currentData()))

    def _activate_source(self, mode, path=None):
        candidate = self._collect_project()
        try:
            result = _choose_source(self, candidate, mode, path or candidate.roar_project_path or "", self.workspace, self.snapshot_provider)
            if result is None:
                self.source_mode.setCurrentIndex(self.source_mode.findData(self._source_mode))
                return
            self.project, design, origin = result
            self._set_source(design, origin)
            self._render()
            self._dirty = True
        except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
            self._source_error = str(exc)
            self.source_mode.setCurrentIndex(self.source_mode.findData(self._source_mode))
            self.snapshot_label.setText("Sizing source error: " + str(exc))
            raise

    def _read_roar(self):
        self._activate_source("live")

    def _set_source(self, design, origin=None):
        selected_instance = self.binding_instance.currentText()
        selected_attr = self.binding_attribute.currentData()
        selected_source = self.source_instance.currentText()
        self.roar_design = design
        self._source_origin = origin if design is not None else None
        self._source_mode = self._source_origin or "saved"
        self._source_error = ""
        self.source_mode.setCurrentIndex(self.source_mode.findData(self._source_mode))
        self.source_path.setText(str(self.project.roar_project_path or "") if self.project else "")
        self.source_instance.clear()
        self.binding_instance.clear()
        self.binding_attribute.clear()
        self.source_reload_button.setVisible(self._source_mode == "linked")
        self.source_reload_button.setEnabled(self._source_mode == "linked")
        self.snapshot_label.setText("")
        if design is None:
            self._source_timer.stop()
            return
        parameters = design.get_instance_parameters()
        enabled = {row["Instance"] for row in design.instances if not row.get("disabled")}
        names = [name for name in parameters if name in enabled]
        self.source_instance.addItems(names)
        self.binding_instance.addItems(names)
        if selected_instance in names:
            self.binding_instance.setCurrentText(selected_instance)
        if selected_source in names:
            self.source_instance.setCurrentText(selected_source)
        self._binding_attributes()
        if selected_attr is not None:
            index = self.binding_attribute.findData(selected_attr)
            if index >= 0:
                self.binding_attribute.setCurrentIndex(index)

    def _load_linked_roar(self):
        self._require_project()
        path, _ = QFileDialog.getOpenFileName(self, "Choose saved ROAR design", str(self.project.roar_project_path or ""), "ROAR design (*.roar *.json);;All files (*)")
        if path:
            self._activate_source("linked", path)

    def _apply_roar(self):
        targets_text = self.import_targets.text()
        source_name = self.source_instance.currentText()
        self._ensure_current_source()
        self.import_targets.setText(targets_text)
        self.source_instance.setCurrentText(source_name)
        candidate = self._collect_project()
        if self.roar_design is None:
            raise ValueError("Choose a ROAR design in Sizing source first.")
        targets = self._names(self.import_targets.text())
        candidate.import_roar_parameters(
            self.roar_design, self.source_instance.currentText(), targets,
            dimension_unit=self.roar_unit.currentData(),
            width_convention=self.roar_width.currentData())
        self.project = candidate
        self._render()
        self._dirty = True
        self.status.setText("Sizes applied.")

    def _import_constraints(self):
        candidate = self._collect_project()
        path, _ = QFileDialog.getOpenFileName(
            self, "Load ALIGN constraints", "", "ALIGN placement rules (*.const.json *.json);;All files (*)")
        if not path:
            return
        try:
            constraints = json.loads(Path(path).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise ValueError("Choose ALIGN placement rules (.const.json). No netlist, device sizes or ROAR design are loaded by this action.") from exc
        if isinstance(constraints, dict) and "source_netlist" in constraints:
            raise ValueError("This is a complete floorplan. Use Open floorplan to open it; Load ALIGN constraints expects placement rules (.const.json) only.")
        if not isinstance(constraints, list) or any(not isinstance(item, dict) for item in constraints):
            raise ValueError("Choose ALIGN placement rules (.const.json), not a ROAR design or netlist. This action loads only constraints.")
        # Unknown records remain visible and saved. Validate supported records
        # individually so opaque rows cannot block ordinary supported edits.
        for constraint in constraints:
            if editable_constraint(constraint):
                candidate.set_constraints([constraint])
        question = QMessageBox(self)
        question.setWindowTitle("Load ALIGN constraints")
        question.setText(f"Load {len(constraints)} placement rules? Only constraints change; the circuit, sizes and ROAR design link stay the same.")
        add = question.addButton("Add to existing", QMessageBox.ButtonRole.AcceptRole)
        replace = question.addButton("Replace existing", QMessageBox.ButtonRole.DestructiveRole)
        question.addButton(QMessageBox.StandardButton.Cancel)
        question.setDefaultButton(QMessageBox.StandardButton.Cancel)
        question.exec()
        if question.clickedButton() not in (add, replace):
            return
        candidate.constraints = (self.project.constraints + constraints if question.clickedButton() is add else constraints)
        self.project = candidate
        self._render_constraints()
        self._dirty = True
        self.status.setText(f"Loaded {len(constraints)} placement rules.")
