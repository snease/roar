"""Bound user-authored JSON and preserve documents across coordinated saves."""
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import tempfile


MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_DOCUMENT_DEPTH = 64
MAX_DOCUMENT_ITEMS = 250_000


class DocumentRollbackError(RuntimeError):
    """A coordinated save failed and its original file could not be restored."""

    def __init__(self, path, error, rollback_error):
        self.path = path
        super().__init__(f'Save failed ({error}); restoring {path} also failed: {rollback_error}')


@contextmanager
def document_savepoint(path):
    """Restore a document if a later step in a coordinated save raises.

    Writers still replace files atomically. This handles ordinary exceptions,
    not a crash or power loss between commits to separate documents.
    """
    path = Path(path).expanduser().resolve()
    original = (path.read_bytes(), path.stat().st_mode & 0o777) if path.exists() else None
    try:
        yield
    except Exception as error:
        temporary = None
        try:
            if original is None:
                path.unlink(missing_ok=True)
            elif (not path.is_file() or path.read_bytes() != original[0]
                  or path.stat().st_mode & 0o777 != original[1]):
                with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.' + path.name + '.',
                                                 suffix='.rollback', delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(original[0])
                temporary.chmod(original[1])
                os.replace(temporary, path)
        except OSError as rollback_error:
            raise DocumentRollbackError(path, error, rollback_error) from error
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass  # Keep the original restore error available to recovery handling.
        raise


def validate_document(value):
    """Keep opaque JSON fields, but reject excessive or non-finite input."""
    pending = [(value, 0)]
    items = text_size = 0
    while pending:
        current, depth = pending.pop()
        items += 1
        if depth > MAX_DOCUMENT_DEPTH or items > MAX_DOCUMENT_ITEMS:
            raise ValueError("Document is too complex; reduce its nesting or number of entries.")
        if isinstance(current, dict):
            if items + len(pending) + len(current) > MAX_DOCUMENT_ITEMS:
                raise ValueError("Document is too complex; reduce its number of entries.")
            if any(not isinstance(key, str) for key in current):
                raise ValueError("Document field names must be text.")
            text_size += sum(len(key) for key in current)
            pending.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, (list, tuple)):
            if items + len(pending) + len(current) > MAX_DOCUMENT_ITEMS:
                raise ValueError("Document is too complex; reduce its number of entries.")
            pending.extend((item, depth + 1) for item in current)
        elif isinstance(current, str):
            text_size += len(current)
        elif isinstance(current, (int, float)) and not isinstance(current, bool):
            try:
                finite = math.isfinite(current)
            except OverflowError:
                finite = False
            if not finite:
                raise ValueError("Document numbers must be finite and within the supported numeric range.")
        elif current is not None and not isinstance(current, bool):
            raise ValueError("Document values must be JSON data.")
        if text_size > MAX_DOCUMENT_BYTES:
            raise ValueError("Document is too large; the supported limit is 16 MiB.")
    return value


def load_document(path):
    """Read a bounded UTF-8 JSON file and report parser limits as input errors."""
    with Path(path).expanduser().open("rb") as stream:
        content = stream.read(MAX_DOCUMENT_BYTES + 1)
    if len(content) > MAX_DOCUMENT_BYTES:
        raise ValueError("Document is too large; the supported limit is 16 MiB.")
    try:
        state = json.loads(content.decode("utf-8"))
    except (RecursionError, OverflowError):
        raise ValueError("Document is too complex; reduce its nesting or numeric values.") from None
    return validate_document(state)
