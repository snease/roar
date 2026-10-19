"""Design-document operations used by the floorplan editor, without Qt."""
from copy import deepcopy
from contextlib import nullcontext
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile

from roar_api import Design
from .documents import document_savepoint, load_document, validate_document


def load_roar_design(path):
    """Validate a saved ROAR design, rejecting unrelated JSON documents."""
    if not str(path).strip():
        raise ValueError("Choose a saved ROAR design.")
    state = load_document(path)
    editor = state.get("design_editor", state) if isinstance(state, dict) else None
    tables = ("instance_table", "expression_editor", "constraint_editor")
    if not isinstance(editor, dict) or not any(key in editor for key in tables):
        raise ValueError("Choose a ROAR design, not a floorplan or another JSON document.")
    for key in tables:
        if key in editor and (not isinstance(editor[key], list) or
                              any(not isinstance(row, dict) for row in editor[key])):
            raise ValueError(f"Invalid ROAR design: {key} must be an editor table.")
        for row in editor.get(key, []):
            if "_attributes" in row and (not isinstance(row["_attributes"], dict) or
                    any(not isinstance(spec, dict) for spec in row["_attributes"].values())):
                raise ValueError("Invalid ROAR design: assigned attributes must be dictionaries.")
    tabs = editor.get("expression_tabs")
    if tabs is not None and (not isinstance(tabs, list) or
                            any(not isinstance(name, str) or not name.strip() for name in tabs)):
        raise ValueError("Invalid ROAR design: expression tabs must be names.")
    try:
        return Design.from_state(state)
    except (AttributeError, IndexError, KeyError, TypeError, RecursionError, OverflowError) as exc:
        raise ValueError(f"Invalid ROAR design: {exc}") from None


def _editor_state(value):
    """Detach editor data without requiring completed, uniquely named rows."""
    state = value.to_state() if isinstance(value, Design) else value
    validate_document(state)
    if not isinstance(state, dict):
        raise ValueError("The current ROAR editor could not be read.")
    editor = state.get("design_editor", state)
    tables = ("expression_editor", "constraint_editor", "instance_table")
    if not isinstance(editor, dict) or not any(table in editor for table in tables):
        raise ValueError("The current ROAR editor could not be read.")
    result = Design().to_state()["design_editor"]
    result.update(deepcopy(editor))
    for table in tables:
        if (not isinstance(result[table], list) or
                any(not isinstance(row, dict) for row in result[table])):
            raise ValueError(f"Invalid ROAR editor table: {table}")
    return result


def editor_signature(design):
    """Compare design content, allowing only known editor display defaults.

    Assigned attributes retain their exact resolved data; their rounded display
    cells are redundant. Empty columns and implicit row flags are GUI defaults.
    Graphs, timestamps, theme and window geometry are outside this document.
    """
    state = _editor_state(design)
    if not state.get("expression_tabs"):
        state["expression_tabs"] = list(dict.fromkeys(
            row.get("_tab", "Expressions") for row in state["expression_editor"])) or ["Expressions"]
    for table in ("expression_editor", "constraint_editor", "instance_table"):
        for row in state[table]:
            for attr, spec in row.get("_attributes", {}).items():
                if isinstance(spec, dict) and spec.get("resolved") is not None:
                    row.pop(attr, None)
            for key in list(row):
                if row[key] == "" or (key in ("disabled", "plot") and row[key] is False):
                    del row[key]
            if row.get("_corners_data") is None:
                row.pop("_corners_data", None)
                if row.get("Corners") == "[*Global*]":
                    row.pop("Corners")
            if table == "expression_editor" and "_tab" not in row:
                row["_tab"] = state["expression_tabs"][0]
    return state


class DesignWorkspace:
    """Own a saved path and baseline while callbacks own the live editor.

    capture_design is cheap and returns a detached Design (or editor dict).
    capture_state is used only when saving a complete application document.
    apply_design replaces editor data only and must propagate failures.
    Optional capture_editor/apply_editor preserve unfinished rows for dirty
    tracking and rollback; they do not supply validated sizing data.
    save_context(path, complete) surrounds a save commit and may yield False
    to cancel it; record and switch never invoke this optional GUI hook.
    """
    def __init__(self, *, capture_design, capture_state, apply_design,
                 capture_editor=None, apply_editor=None, save_context=None):
        self._capture_design = capture_design
        self._capture_state = capture_state
        self._apply_design = apply_design
        self._capture_editor = capture_editor
        self._apply_editor = apply_editor
        self._save_context = save_context
        self.path = None
        self._baseline = None

    def snapshot(self):
        value = self._capture_design()
        return Design.from_state(value.to_state() if isinstance(value, Design) else value)

    def editor_snapshot(self):
        """Raw editor state for dirty tracking and lossless failed-switch rollback."""
        capture = self._capture_editor or self._capture_design
        return _editor_state(capture())

    def has_content(self):
        return editor_signature(self.editor_snapshot()) != editor_signature(Design())

    def is_modified(self):
        current = editor_signature(self.editor_snapshot())
        if self._baseline is None:
            return current != editor_signature(Design())
        return current != self._baseline

    def record(self, path):
        """Record a successful existing File/Load/Save operation."""
        try:
            baseline = editor_signature(self.editor_snapshot())
        except (ValueError, TypeError):
            # Do not retain an old identity if the editor could not be captured.
            self.path, self._baseline = None, None
            raise
        self.path = str(Path(path).expanduser().resolve()) if path else None
        self._baseline = baseline

    def save(self, path):
        target = Path(path).expanduser().resolve()
        design = self.snapshot()
        if target.is_file():
            load_roar_design(target)  # Never overwrite an unrelated JSON file.
            previous = load_document(target)
            complete = "design_editor" in previous
        else:
            complete = target.suffix.lower() != ".json"
        editor = design.to_state()["design_editor"]
        if complete:
            captured = self._capture_state()
            state = captured.to_state() if isinstance(captured, Design) else deepcopy(captured)
            state["design_editor"] = editor
            Design.from_state(state)
            state["saved_at"] = datetime.now().isoformat()
        else:
            state = editor
        return design if self.save_document(target, state) else None

    def save_document(self, path, state, *, indent=2, save_context=None):
        """Atomically save a File action's raw document, including unfinished rows.

        A save context may save a companion before the document commits; either
        file is restored if a later commit step raises. An explicit context can
        select dialog-free behavior for API callers.
        Loading/recording another document deliberately does not use this hook.
        """
        target = Path(path).expanduser().resolve()
        baseline = editor_signature(self.editor_snapshot())
        content = json.dumps(state, indent=indent, allow_nan=False) + "\n"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        previous = self.path, self._baseline
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                             prefix="." + target.name + ".", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
            context = save_context
            if context is None:
                context = (self._save_context(str(target), "design_editor" in state)
                           if self._save_context is not None else nullcontext())
            with document_savepoint(target), context as ready:
                if ready is False:
                    return False
                os.replace(temporary, target)
                self.path, self._baseline = str(target), baseline
        except Exception:
            self.path, self._baseline = previous
            raise
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return True

    def switch(self, path):
        target = Path(path).expanduser().resolve()
        desired = load_roar_design(target)  # Validate before changing the editor.
        previous = self.editor_snapshot()
        try:
            self._apply_design(desired)
            actual = self.snapshot()
            if editor_signature(actual) != editor_signature(desired):
                raise ValueError("ROAR could not load all of the selected design's editor data.")
        except Exception as error:
            try:
                if self._apply_editor is not None:
                    self._apply_editor(previous)
                else:
                    self._apply_design(Design.from_state(previous))
                if editor_signature(self.editor_snapshot()) != editor_signature(previous):
                    raise ValueError("restored editor data did not match")
            except Exception as rollback_error:
                raise RuntimeError(f"Design switch failed ({error}); restoring the previous design also failed: {rollback_error}") from error
            raise
        self.path, self._baseline = str(target), editor_signature(actual)
        return actual
